"""HTTP clients for the two backing services, and a plain-HTTP page reader."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser

from sanitize import rejection_reason

SEARCH_TIMEOUT = 15.0
CRAWL_TIMEOUT = 120.0
# Per network read, then for the whole page: a throttled connection keeps
# trickling bytes, so a read timeout alone never fires.
READ_TIMEOUT = 15.0
READ_DEADLINE = 25.0
READ_MAX_BYTES = 5 * 1024 * 1024
READ_MAX_REDIRECTS = 5
READ_CONCURRENCY = 8
HTML_TYPES = ("text/html", "application/xhtml+xml")
# A bare HTTP client is turned away on its User-Agent alone by many sites. The
# header set of a desktop Chrome gets past that and nothing stronger.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


@dataclass(slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    engine: str


@dataclass(slots=True)
class PageResult:
    url: str
    title: str
    markdown: str
    error: str | None = None


class Searxng:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def search(
        self,
        query: str,
        *,
        count: int,
        language: str = "ru",
        categories: str = "general",
        time_range: str = "",
    ) -> list[SearchHit]:
        params: dict[str, str | int] = {
            "q": query,
            "format": "json",
            "language": language,
            "categories": categories,
            "pageno": 1,
        }
        if time_range:
            params["time_range"] = time_range

        response = await self._client.get(
            f"{self._base_url}/search", params=params, timeout=SEARCH_TIMEOUT
        )
        if response.status_code == 403:
            raise RuntimeError(
                "SearXNG rejected the JSON request (403). Check that settings.yml "
                "has `search.formats` containing `json` and `server.limiter: false`."
            )
        response.raise_for_status()

        hits = []
        for item in response.json().get("results", [])[:count]:
            url = item.get("url")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=item.get("title") or url,
                    snippet=item.get("content") or "",
                    engine=item.get("engine") or "",
                )
            )
        return hits


class Crawl4ai:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client

    async def crawl(self, urls: list[str], *, cache_mode: str) -> list[PageResult]:
        """Crawl every URL in one call so the server reuses its browser pool."""
        payload = {
            "urls": urls,
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "stream": False,
                    "cache_mode": cache_mode,
                    "markdown_generator": {
                        "type": "DefaultMarkdownGenerator",
                        "params": {
                            "content_filter": {
                                "type": "PruningContentFilter",
                                "params": {
                                    "threshold": 0.45,
                                    "threshold_type": "dynamic",
                                    "min_word_threshold": 5,
                                },
                            },
                            "options": {"ignore_links": True},
                        },
                    },
                },
            },
        }

        response = await self._client.post(
            f"{self._base_url}/crawl",
            json=payload,
            headers=self._headers,
            timeout=CRAWL_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()

        raw_results = body.get("results", [])
        by_url = {r.get("url"): r for r in raw_results}
        # Redirects make the returned URL differ from the requested one, so fall
        # back to request order when the counts line up.
        positional = raw_results if len(raw_results) == len(urls) else []

        results = []
        for index, url in enumerate(urls):
            raw = by_url.get(url) or (positional[index] if positional else None)
            if raw is None:
                results.append(
                    PageResult(
                        url=url, title="", markdown="", error="no result returned"
                    )
                )
                continue
            results.append(_to_page_result(url, raw))
        return results


def _to_page_result(url: str, raw: dict) -> PageResult:
    metadata = raw.get("metadata") or {}
    title = metadata.get("title") or url

    if not raw.get("success", False):
        return PageResult(
            url=url,
            title=title,
            markdown="",
            error=_crawl_error(raw.get("error_message") or "")
            or f"HTTP {raw.get('status_code')}",
        )

    markdown = raw.get("markdown") or {}
    if isinstance(markdown, str):
        text = markdown
    else:
        text = markdown.get("fit_markdown") or markdown.get("raw_markdown") or ""

    text = text.strip()
    if not text:
        return PageResult(
            url=url, title=title, markdown="", error="page had no text content"
        )
    return PageResult(url=url, title=title, markdown=text)


def _crawl_error(message: str) -> str:
    """One line out of Crawl4AI's error, which carries a call log and source context.

    The Playwright line (`Page.goto: net::ERR_...`) says what happened; failing
    that, the first non-empty line.
    """
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("Page.") or "net::ERR_" in line:
            return line[:300]
    return lines[0][:300] if lines else ""


class ReadFailed(RuntimeError):
    """One page could not be read over plain HTTP."""


# Domestic zones turn foreign and datacenter exits away, while foreign hosts are
# throttled on the direct route here, so the zone decides which exit goes first.
DIRECT_FIRST = (".ru", ".su", ".xn--p1ai", ".by", ".kz")


def prefers_direct(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower().endswith(DIRECT_FIRST)


class Reader:
    """Fetch a page with a plain GET and extract its text with trafilatura.

    No browser, so it is fast, but a page rendered by JavaScript comes back
    without text. Such a page is reported as failed and the caller hands it to
    Crawl4AI. With a direct client given as well, each page is tried from both
    exits, the one its zone prefers first.
    """

    def __init__(
        self, client: httpx.AsyncClient, direct: httpx.AsyncClient | None = None
    ) -> None:
        self._client = client
        self._direct = direct

    async def read(self, urls: list[str]) -> list[PageResult]:
        """Read every URL, in input order, never more than a few at once."""
        gate = asyncio.Semaphore(READ_CONCURRENCY)

        async def one(url: str) -> PageResult:
            async with gate:
                errors = []
                for client in self._clients(url):
                    page = await self._attempt(client, url)
                    if not page.error:
                        return page
                    errors.append(page.error)
                return PageResult(url=url, title="", markdown="", error="; ".join(errors))

        return list(await asyncio.gather(*(one(url) for url in urls)))

    def _clients(self, url: str) -> list[httpx.AsyncClient]:
        if self._direct is None:
            return [self._client]
        if prefers_direct(url):
            return [self._direct, self._client]
        return [self._client, self._direct]

    async def _attempt(self, client: httpx.AsyncClient, url: str) -> PageResult:
        try:
            async with asyncio.timeout(READ_DEADLINE):
                return await self._read(client, url)
        except TimeoutError:
            error = f"не прочитана за {READ_DEADLINE:.0f} с"
        except ReadFailed as exc:
            error = str(exc)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        return PageResult(url=url, title="", markdown="", error=error)

    async def _read(self, client: httpx.AsyncClient, url: str) -> PageResult:
        body, final_url = await self._fetch(client, url)
        text = await asyncio.to_thread(
            trafilatura.extract, body, url=final_url, output_format="markdown"
        )
        if not text or not text.strip():
            raise ReadFailed("page had no text content")
        node = LexborHTMLParser(body).css_first("title")
        title = node.text(strip=True) if node else ""
        return PageResult(url=url, title=title, markdown=text.strip())

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
        """GET with redirects followed by hand, each hop vetted like the first.

        httpx would follow a redirect into 127.0.0.1 or the metadata address
        without asking; NO_PROXY sends localhost past the proxy, so that hop
        would land on this machine.
        """
        for _ in range(READ_MAX_REDIRECTS + 1):
            reason = await asyncio.to_thread(rejection_reason, url)
            if reason:
                raise ReadFailed(f"переход на {url} отклонён: {reason}")
            async with client.stream(
                "GET",
                url,
                headers=BROWSER_HEADERS,
                follow_redirects=False,
                timeout=READ_TIMEOUT,
            ) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code >= 400:
                    raise ReadFailed(f"HTTP {response.status_code}")
                kind = response.headers.get("content-type", "").split(";")[0].strip()
                if kind and kind not in HTML_TYPES:
                    raise ReadFailed(f"не HTML: {kind}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= READ_MAX_BYTES:
                        break
                return bytes(body[:READ_MAX_BYTES]), url
        raise ReadFailed(f"больше {READ_MAX_REDIRECTS} редиректов")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value
