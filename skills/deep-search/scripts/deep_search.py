#!/usr/bin/env python3
"""Deep search: two self-hosted SearXNG instances in parallel, pages read in full.

Nothing reaches the model context until this skill is called. The instances
leave the machine through different exits, so a CAPTCHA wave on one IP does not
empty the search. Pages are read with a plain GET and trafilatura first, and
only the ones that need a browser go to Crawl4AI. The stack's own modules are
imported from the checkout's lib/ instead of restating its HTTP contracts, which
also keeps the SSRF vetting in sanitize.py on the path.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

BOOTSTRAP_FLAG = "DEEP_SEARCH_BOOTSTRAPPED"
SEARXNG = "searxng"
SEARXNG_B = "searxng-b"
# Instance name -> (full URL override, port variable, default port).
INSTANCES = {
    SEARXNG: ("DEEP_SEARCH_SEARXNG_URL", "SEARXNG_PORT", "8880"),
    SEARXNG_B: ("DEEP_SEARCH_SEARXNG_B_URL", "SEARXNG_B_PORT", "8881"),
}
PROXY_DEFAULT = "http://127.0.0.1:8118"
# Present in the general category but not web search: dictionaries, translators,
# currency and reverse-image lookups. They only crowd out real results.
SKIP_ENGINES = frozenset(
    {
        "currency",
        "ddg definitions",
        "dictzone",
        "lingva",
        "mozhi",
        "mymemory translated",
        "tineye",
        "wolframalpha",
    }
)
PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
# Reasons that mean a live page closed to the crawler, not a broken address.
# Crawl4AI passes the first ones through from its own error_message; the last is
# set by the text filter when a challenge page came back with HTTP 200.
BROWSER_RETRY_MARKERS = (
    "anti-bot",
    "cloudflare",
    "captcha",
    "http 401",
    "http 403",
    "http 429",
    "http 503",
    "page had no text content",
)
BROWSER_RETRY_MAX = 3


class BranchFailed(RuntimeError):
    """One source is unusable. Its reason is reported, never swallowed."""


@dataclass
class Entry:
    url: str
    title: str
    text: str
    snippet: str = ""
    engine: str = ""
    sources: list[str] = field(default_factory=list)


@dataclass
class Failure:
    source: str
    reason: str
    # A page a browser could still open. Only set for a crawl failure, never for
    # an SSRF veto or a dead domain: those addresses must not be handed onwards.
    retryable: bool = False


def browser_can_retry(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in BROWSER_RETRY_MARKERS)


def browser_candidates(failures: list[Failure]) -> list[Failure]:
    """Pages worth reopening in a browser, capped so the retry stays cheap."""
    return [failure for failure in failures if failure.retryable][:BROWSER_RETRY_MAX]


def repo_dir() -> Path:
    """Where lib/, .venv and .env live.

    The skill sits at <repo>/skills/deep-search/scripts/, and resolve() follows
    the symlink that install-skills.sh puts in ~/.claude/skills, so the checkout
    is found without any configuration. DEEP_SEARCH_DIR overrides it for a copied
    install, or when the services and the venv live somewhere else.
    """
    override = os.environ.get("DEEP_SEARCH_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3]


def normalize_proxy_env(proxy: str) -> None:
    """Set one proxy for this process.

    A machine's own NO_PROXY can carry IPv6 entries that httpx refuses to parse
    ("Invalid port: ':'"), so the list is replaced rather than extended.
    """
    for name in PROXY_ENV:
        os.environ.pop(name, None)
    if not proxy:
        return
    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def proxy_alive(proxy: str, timeout: float = 1.0) -> bool:
    """A dead proxy would take out every external source; direct still serves some."""
    host, _, port = urlsplit(proxy).netloc.partition(":")
    if not host:
        return False
    try:
        with socket.socket() as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, int(port or 80))) == 0
    except (OSError, ValueError):
        return False


def setup_proxy(disabled: bool) -> str:
    """Apply the proxy for this run and return the note for the report."""
    proxy = "" if disabled else os.environ.get("DEEP_SEARCH_PROXY", PROXY_DEFAULT)
    if proxy and not proxy_alive(proxy):
        normalize_proxy_env("")
        return f"{proxy} недоступен, работаю напрямую"
    normalize_proxy_env(proxy)
    return proxy or "выключен"


def bootstrap() -> None:
    """Re-exec under the stack's venv when this interpreter lacks the dependencies."""
    if os.environ.get(BOOTSTRAP_FLAG):
        return
    if all(importlib.util.find_spec(name) for name in ("httpx", "trafilatura")):
        return
    venv_python = repo_dir() / ".venv/bin/python"
    if not venv_python.exists():
        return
    os.environ[BOOTSTRAP_FLAG] = "1"
    os.execv(
        str(venv_python),
        [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def _stack_dir() -> Path:
    package = repo_dir() / "lib"
    if not package.is_dir():
        raise BranchFailed(f"нет каталога {package}; задай DEEP_SEARCH_DIR")
    return package


def _stack_modules(*names: str) -> list:
    path = str(_stack_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        return [importlib.import_module(name) for name in names]
    except ImportError as exc:
        raise BranchFailed(f"импорт модулей стека не удался: {exc}") from exc


def _require_httpx():
    _stack_dir()
    try:
        import httpx
    except ImportError as exc:
        raise BranchFailed(
            f"httpx недоступен: нет {repo_dir() / '.venv/bin/python'} "
            f"или в нём не установлены зависимости (uv sync)"
        ) from exc
    return httpx


def _searxng_url(name: str = SEARXNG) -> str:
    override, port_name, port = INSTANCES[name]
    return os.environ.get(override) or f"http://127.0.0.1:{os.environ.get(port_name, port)}"


def _crawl4ai_url() -> str:
    override = os.environ.get("DEEP_SEARCH_CRAWL4AI_URL")
    port = os.environ.get("CRAWL4AI_PORT", "11235")
    return override or f"http://127.0.0.1:{port}"


def _token_from_env_file(path: Path) -> str:
    """Read only the crawler token out of the stack's .env. Value is never printed."""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "CRAWL4AI_API_TOKEN":
            return value.strip().strip("'\"")
    return ""


def crawl_token() -> str:
    token = os.environ.get("CRAWL4AI_API_TOKEN") or _token_from_env_file(
        repo_dir() / ".env"
    )
    if not token:
        raise BranchFailed("CRAWL4AI_API_TOKEN не задан ни в окружении, ни в <repo>/.env")
    return token


async def discover_engines(client, name: str) -> tuple[list[str], str]:
    """Every general-category engine this SearXNG has, minus the non-search utilities.

    Read from /config rather than hardcoded, so an image upgrade that adds or
    renames engines needs no edit here. Engines disabled by default are included
    on purpose: an explicit `engines=` parameter activates them per request, and
    four enabled engines is what made one CAPTCHA wave empty the whole branch.
    """
    httpx = _require_httpx()
    try:
        response = await client.get(f"{_searxng_url(name)}/config", timeout=10.0)
        response.raise_for_status()
        catalogue = response.json().get("engines", [])
    except (httpx.HTTPError, ValueError) as exc:
        # An unreadable /config must not take the branch down: without an explicit
        # engines list SearXNG just uses the ones enabled in its own settings.
        return [], f"список движков не прочитан ({type(exc).__name__})"

    names = [
        engine["name"]
        for engine in catalogue
        if engine.get("name")
        and "general" in (engine.get("categories") or [])
        and engine["name"] not in SKIP_ENGINES
    ]
    if not names:
        return [], "SearXNG не отдал ни одного general-движка"
    return names, ""


async def resolve_engines(client, name: str, spec: str) -> tuple[list[str], str]:
    if spec == "builtin":
        return [], ""
    if spec == "auto":
        return await discover_engines(client, name)
    return [engine.strip() for engine in spec.split(",") if engine.strip()], ""


async def searxng_search(
    client,
    name: str,
    query: str,
    *,
    count: int,
    language: str,
    time_range: str,
    engines: list[str],
    timeout: float,
) -> tuple[list, list[tuple[str, str]]]:
    """Query one SearXNG directly, returning hits and the engines that did not answer.

    backends.Searxng cannot cover this call: it has no `engines` parameter and
    reads only `results`, so a rate-limited pool came back as "nothing found".
    SearchHit still comes from backends so the downstream shape stays shared.
    """
    (backends,) = _stack_modules("backends")
    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "language": language,
        "categories": "general",
        "pageno": 1,
    }
    if time_range:
        params["time_range"] = time_range
    if engines:
        params["engines"] = ",".join(engines)

    response = await client.get(
        f"{_searxng_url(name)}/search", params=params, timeout=timeout
    )
    if response.status_code == 403:
        raise BranchFailed(
            "SearXNG отклонил JSON-запрос (403): проверь search.formats и server.limiter"
        )
    response.raise_for_status()
    body = response.json()

    hits = []
    for item in body.get("results", [])[:count]:
        url = item.get("url")
        if not url:
            continue
        hits.append(
            backends.SearchHit(
                url=url,
                title=item.get("title") or url,
                snippet=item.get("content") or "",
                engine=item.get("engine") or ", ".join(item.get("engines") or []),
            )
        )
    down = [
        (str(entry[0]), str(entry[1]) if len(entry) > 1 else "")
        for entry in body.get("unresponsive_engines", [])
        if entry
    ]
    return hits, down


async def search_instance(client, name: str, args) -> tuple[list[Entry], str]:
    """One SearXNG instance, as links. Raises BranchFailed when it found nothing."""
    chosen, engines_warning = await resolve_engines(client, name, args.engines)
    hits, down = await searxng_search(
        client,
        name,
        args.query,
        # Exactly max_results, not a multiple: SearXNG returns its pool in score
        # order and a wide engine set puts link-farm domains in the tail.
        count=args.max_results,
        language=args.language,
        time_range=args.time_range,
        engines=chosen,
        timeout=args.search_timeout,
    )
    asked = str(len(chosen)) if chosen else "по умолчанию"
    answered = len({hit.engine for hit in hits if hit.engine})
    note = (
        f"движки {name}: запрошено {asked}, с результатами {answered}, "
        f"без ответа {len(down)}"
    )
    if engines_warning:
        note += f" ({engines_warning})"

    if not hits:
        listed = ", ".join(f"{engine} ({reason})" for engine, reason in down)
        detail = f"; недоступны: {listed}" if listed else ""
        raise BranchFailed(f"SearXNG вернул 0 результатов; {note}{detail}")

    entries = [
        Entry(
            url=hit.url,
            title=hit.title,
            text="",
            snippet=hit.snippet,
            engine=hit.engine,
            sources=[name],
        )
        for hit in hits
    ]
    return entries, note


async def read_pages(
    client, entries: list[Entry], *, full_pages: int, fresh: bool, browser: bool
) -> tuple[list[Entry], list[Failure]]:
    """Read the first full_pages entries in full; the rest stay as links.

    A page that no reader could open is dropped from the entries and named in
    the failures instead.
    """
    (backends,) = _stack_modules("backends")
    httpx = _require_httpx()
    # The process client carries the proxy. A second one without it lets the
    # reader try .ru-zone sites direct first, as the browser step does.
    direct = httpx.AsyncClient(trust_env=False) if os.environ.get("HTTPS_PROXY") else None
    try:
        return await _read_pages(
            backends.Reader(client, direct), backends, client, entries,
            full_pages=full_pages, fresh=fresh, browser=browser,
        )
    finally:
        if direct is not None:
            await direct.aclose()


async def _read_pages(
    reader, backends, client, entries: list[Entry], *, full_pages: int, fresh: bool, browser: bool
) -> tuple[list[Entry], list[Failure]]:
    try:
        crawler = backends.Crawl4ai(_crawl4ai_url(), crawl_token(), client)
        crawler_problem = ""
    except BranchFailed as exc:
        crawler, crawler_problem = None, str(exc)
    cache_mode = "bypass" if fresh else "enabled"

    wanted, spare = entries[:full_pages], entries[full_pages:]
    failures = await _read_batch(reader, crawler, crawler_problem, wanted, cache_mode)

    # Cloudflare challenges and empty pages are common enough that asking for ten
    # full reads and settling for seven wastes the tier. One backfill round only.
    shortfall = full_pages - sum(1 for entry in wanted if entry.text)
    if shortfall > 0 and spare:
        extra, spare = spare[:shortfall], spare[shortfall:]
        failures.extend(
            await _read_batch(reader, crawler, crawler_problem, extra, cache_mode)
        )
        wanted += extra

    if browser:
        failures = await _browser_pass(wanted, failures)

    failed = {failure.source for failure in failures}
    kept = [entry for entry in wanted if entry.url not in failed] + spare
    return kept, failures


async def _browser_pass(wanted: list[Entry], failures: list[Failure]) -> list[Failure]:
    """Reopen the pages the readers were refused in a real browser.

    A page the browser read leaves the failures. One the browser was refused too
    stays, no longer offered for a browser retry. When no browser can start, the
    candidates stay retryable, so the model can still try its own.
    """
    candidates = browser_candidates(failures)
    if not candidates:
        return failures
    (browser,) = _stack_modules("browser")
    try:
        pages = await browser.read(
            [failure.source for failure in candidates], os.environ.get("HTTPS_PROXY", "")
        )
    except browser.BrowserUnavailable as exc:
        return [*failures, Failure("браузер", str(exc))]

    by_url = {entry.url: entry for entry in wanted}
    done: set[str] = set()
    for failure, page in zip(candidates, pages, strict=True):
        if page.error:
            failure.reason = f"{failure.reason}; браузер — {page.error}"
            failure.retryable = False
            continue
        entry = by_url[failure.source]
        entry.text = page.markdown
        entry.title = page.title or entry.title
        entry.engine = " · ".join(part for part in (entry.engine, "браузер") if part)
        done.add(failure.source)
    return [failure for failure in failures if failure.source not in done]


async def _read_batch(
    reader, crawler, crawler_problem: str, batch: list[Entry], cache_mode: str
) -> list[Failure]:
    """trafilatura first; what it could not read goes to Crawl4AI in one call."""
    if not batch:
        return []
    missed: list[tuple[Entry, str]] = []
    for entry, page in zip(batch, await reader.read([e.url for e in batch]), strict=True):
        if page.error:
            missed.append((entry, page.error))
            continue
        entry.text = page.markdown
        entry.title = page.title or entry.title
    if not missed:
        return []

    (backends,) = _stack_modules("backends")
    urls = [entry.url for entry, _ in missed]
    problem = crawler_problem
    crawled = []
    if crawler is not None:
        httpx = _require_httpx()
        try:
            crawled = await crawler.crawl(urls, cache_mode=cache_mode)
        except httpx.HTTPError as exc:
            problem = f"Crawl4AI недоступен: {type(exc).__name__}"
    if problem:
        crawled = [
            backends.PageResult(url=url, title="", markdown="", error=problem)
            for url in urls
        ]

    failures: list[Failure] = []
    for (entry, first_error), page in zip(missed, crawled, strict=True):
        if page.error:
            reason = f"{page.error} (trafilatura: {first_error})"
            # The address passed the SSRF check and the host answered or timed
            # out, so a real browser is worth a try whatever the readers said —
            # proxy errors and DPI stalls included. A file that is not HTML is
            # the one case a browser does not change.
            retryable = not first_error.startswith("не HTML")
            failures.append(Failure(entry.url, reason, retryable=retryable))
            continue
        entry.text = page.markdown.strip()
        if page.title and page.title != entry.url:
            entry.title = page.title
    return failures


def _describe_rejects(rejected: list[tuple[str, str]]) -> list[Failure]:
    """Separate dead domains from a real SSRF veto.

    sanitize blocks both with one message, and a wide engine pool drags in
    link-farm hosts that do not resolve. Collapsing those into a count keeps the
    failure block readable while every private-address hit stays itemised.
    """
    dead = 0
    described: list[Failure] = []
    for url, reason in rejected:
        host = urlsplit(url).hostname or ""
        try:
            socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            dead += 1
            continue
        described.append(Failure(url, reason))
    if dead:
        described.append(
            Failure(
                f"{dead} ссыл(ок) от мусорных движков",
                "домен не резолвится, отброшено до краула",
            )
        )
    return described


async def search_social(
    client, name: str, query: str, *, limit: int, per_platform: int, max_chars: int
) -> str:
    backends, sources, aggregate, social = _stack_modules(
        "backends", "sources", "aggregate", "social"
    )
    searxng = backends.Searxng(_searxng_url(name), client)
    chosen = aggregate.select(sources.build_sources(client, searxng), None)
    spec = social.QuerySpec(terms=query, limit=per_platform)
    per_source, failures, took = await aggregate.gather(chosen, spec, None)
    posts = social.merge(per_source, sort="relevance", limit=limit)
    stats = {
        source: f"{len(found)} ({took[source]} ms)" for source, found in per_source.items()
    }
    return social.render(posts, failures, stats, max_chars_per_post=max_chars)


@dataclass
class Research:
    entries: list[Entry]
    failures: list[Failure]
    stats: list[str]
    engines_note: str
    social_block: str


class NothingFound(RuntimeError):
    """Every SearXNG instance failed; the failures say why."""

    def __init__(self, failures: list[Failure]) -> None:
        super().__init__("all sources failed")
        self.failures = failures


async def research(args) -> Research:
    httpx = _require_httpx()
    sanitize, social = _stack_modules("sanitize", "social")
    names = [name for name in INSTANCES if args.only in ("", name)]
    # Proxy comes from the env normalize_proxy_env() has already rewritten, which
    # keeps 127.0.0.1 (SearXNG, Crawl4AI) direct and sends page reads through.
    async with httpx.AsyncClient(follow_redirects=True) as client:
        outcomes = await asyncio.gather(
            *(search_instance(client, name, args) for name in names),
            return_exceptions=True,
        )
        groups: list[list[Entry]] = []
        failures: list[Failure] = []
        stats: list[str] = []
        notes: list[str] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures.append(Failure(name, _reason(outcome)))
                continue
            entries, note = outcome
            groups.append(entries)
            notes.append(note)
            stats.append(f"{name}: {len(entries)}")
        if not groups:
            raise NothingFound(failures)

        # Found by both instances first: those are the ones worth reading.
        merged = merge(groups, social.canonical_url)
        safe, rejected = await asyncio.to_thread(
            sanitize.partition_safe, [entry.url for entry in merged]
        )
        failures.extend(await asyncio.to_thread(_describe_rejects, rejected))
        allowed = set(sanitize.dedupe_by_domain(safe))
        merged = [entry for entry in merged if entry.url in allowed]

        entries, page_failures = await read_pages(
            client,
            merged,
            full_pages=args.full_pages,
            fresh=args.fresh,
            browser=not args.no_browser,
        )
        failures.extend(page_failures)

        social_block = ""
        if args.social:
            social_block = await search_social(
                client,
                names[0],
                args.query,
                limit=args.social_limit,
                per_platform=args.social_per_platform,
                max_chars=args.social_chars,
            )

    # Full reads first again: the backfill may have read entries further down.
    entries.sort(key=lambda entry: 0 if entry.text else 1)
    both = sum(1 for entry in entries if len(entry.sources) > 1)
    if both:
        stats.append(f"совпадений: {both}")
    stats.append(f"прочитано полностью: {sum(1 for entry in entries if entry.text)}")
    return Research(entries, failures, stats, "\n".join(notes), social_block)


def merge(groups: list[list[Entry]], canonicalize) -> list[Entry]:
    merged: dict[str, Entry] = {}
    for entries in groups:
        for entry in entries:
            key = canonicalize(entry.url)
            kept = merged.get(key)
            if kept is None:
                merged[key] = entry
                continue
            kept.sources = sorted({*kept.sources, *entry.sources})
            if len(entry.text) > len(kept.text):
                kept.text = entry.text
            if kept.title in {"", kept.url} and entry.title:
                kept.title = entry.title
            kept.snippet = kept.snippet or entry.snippet
            kept.engine = kept.engine or entry.engine

    ordered = list(merged.values())
    # Pages read in full first, confirmed-by-both ahead of the rest inside each tier.
    ordered.sort(
        key=lambda entry: (
            0 if entry.text else 1,
            0 if len(entry.sources) > 1 else 1,
        )
    )
    return ordered


def render(
    query: str,
    entries: list[Entry],
    failures: list[Failure],
    stats: list[str],
    *,
    max_chars_per_page: int,
    total_budget: int,
    truncate,
    proxy_note: str,
    engines_note: str,
    snippet_chars: int,
) -> str:
    parts = [f"# Deep search: {query}"]
    if stats:
        parts.append("Источники — " + ", ".join(stats))
    if engines_note:
        parts.append(engines_note)
    parts.append(f"прокси: {proxy_note}")

    full = [entry for entry in entries if entry.text]
    brief = [entry for entry in entries if not entry.text]

    budget = total_budget
    dropped = 0
    blocks: list[str] = []
    for position, entry in enumerate(full, start=1):
        meta = " · ".join(part for part in [", ".join(entry.sources), entry.engine] if part)
        # Page markdown carries its own headings, so a rule marks where an entry starts.
        head = f"---\n\n## {position}. {entry.title}\n{entry.url}\n{meta}"
        allowance = min(max_chars_per_page, budget)
        if allowance <= 0:
            dropped += 1
            blocks.append(head)
            continue
        body = truncate(entry.text, allowance)
        budget -= len(body)
        blocks.append(f"{head}\n\n{body}")

    parts.append("\n\n".join(blocks) if blocks else "Ни одна страница не прочитана.")
    if dropped:
        parts.append(f"_{dropped} страниц(ы) без текста: общий бюджет вывода исчерпан._")

    if brief:
        listed = "\n".join(
            "- [{title}]({url}){engine}{snippet}".format(
                title=entry.title.replace("]", ")"),
                url=entry.url,
                engine=f" · {entry.engine}" if entry.engine else "",
                snippet=f" — {truncate(entry.snippet, snippet_chars)}"
                if entry.snippet
                else "",
            )
            for entry in brief
        )
        parts.append(f"---\n\n## Ещё источники ({len(brief)})\n{listed}")

    blocked = browser_candidates(failures)
    if blocked:
        listed = "\n".join(f"- {failure.source} — {failure.reason}" for failure in blocked)
        parts.append(f"### Заблокировано, добрать браузером ({len(blocked)})\n{listed}")

    picked = {id(failure) for failure in blocked}
    rest = [failure for failure in failures if id(failure) not in picked]
    if rest:
        listed = "\n".join(f"- {failure.source} — {failure.reason}" for failure in rest)
        parts.append(f"### Не удалось получить\n{listed}")
    return "\n\n".join(parts)


async def run(args) -> tuple[str, int]:
    try:
        found = await research(args)
    except NothingFound as exc:
        listed = "\n".join(f"{f.source}: {f.reason}" for f in exc.failures)
        return f"{listed}\nпрокси: {args.proxy_note}", 3
    except BranchFailed as exc:
        return f"deep-search: {exc}\nпрокси: {args.proxy_note}", 3

    if args.json:
        payload = {
            "query": args.query,
            "proxy": args.proxy_note,
            "engines": found.engines_note,
            "entries": [vars(entry) for entry in found.entries],
            "failures": [
                {"source": f.source, "reason": f.reason} for f in found.failures
            ],
            "blocked": [
                {"url": f.source, "reason": f.reason}
                for f in browser_candidates(found.failures)
            ],
            "social": found.social_block,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), 0

    (sanitize,) = _stack_modules("sanitize")
    output = render(
        args.query,
        found.entries,
        found.failures,
        found.stats,
        max_chars_per_page=args.max_chars_per_page,
        total_budget=args.total_budget,
        truncate=sanitize.truncate,
        proxy_note=args.proxy_note,
        engines_note=found.engines_note,
        snippet_chars=args.snippet_chars,
    )
    if found.social_block:
        output += f"\n\n## Социальные сети\n\n{found.social_block}"
    return output, 0


def _reason(error: BaseException) -> str:
    if isinstance(error, BranchFailed):
        return str(error)
    return f"{type(error).__name__}: {error}"


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Два self-hosted SearXNG параллельно, страницы через trafilatura и Crawl4AI."
    )
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Сколько ссылок брать с каждого SearXNG",
    )
    parser.add_argument(
        "--full-pages",
        type=int,
        default=10,
        help="Сколько источников прочитать целиком; остальные идут ссылкой со сниппетом",
    )
    parser.add_argument(
        "--engines",
        default="auto",
        help="auto — все general-движки SearXNG; builtin — только включённые по умолчанию; "
        "либо список через запятую (brave,duckduckgo,bing)",
    )
    parser.add_argument(
        "--search-timeout",
        type=float,
        default=40.0,
        help="Таймаут запроса к SearXNG; широкому пулу движков нужно больше 15 с",
    )
    parser.add_argument("--max-chars-per-page", type=int, default=4000)
    parser.add_argument("--snippet-chars", type=int, default=200, help="Обрезка сниппета ссылки")
    parser.add_argument("--total-budget", type=int, default=60000, help="Бюджет символов вывода")
    parser.add_argument(
        "--language",
        default="all",
        help="Язык поиска SearXNG; ru сужает выдачу до русскоязычной",
    )
    parser.add_argument(
        "--time-range", default="", choices=["", "day", "week", "month", "year"]
    )
    parser.add_argument("--only", choices=list(INSTANCES), default="")
    parser.add_argument("--fresh", action="store_true", help="Обойти кэш Crawl4AI")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Не открывать заблокированные страницы в Chrome",
    )
    parser.add_argument("--social", action="store_true", help="Добавить блок social_search")
    parser.add_argument("--social-limit", type=int, default=20)
    parser.add_argument("--social-per-platform", type=int, default=8)
    parser.add_argument("--social-chars", type=int, default=600)
    parser.add_argument("--json", action="store_true", help="Сырой JSON вместо markdown")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help=f"Не использовать прокси (по умолчанию DEEP_SEARCH_PROXY или {PROXY_DEFAULT})",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    args.proxy_note = setup_proxy(args.no_proxy)
    output, code = asyncio.run(run(args))
    print(output, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    bootstrap()
    sys.exit(main())
