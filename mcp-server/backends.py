"""HTTP clients for the two backing services."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

SEARCH_TIMEOUT = 15.0
CRAWL_TIMEOUT = 120.0


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
            error=raw.get("error_message") or f"HTTP {raw.get('status_code')}",
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


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value
