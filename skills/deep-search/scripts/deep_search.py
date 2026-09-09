#!/usr/bin/env python3
"""Deep search: Tavily+Jina and self-hosted SearXNG+Crawl4AI in parallel, merged.

Nothing reaches the model context until this skill is called. Branch A shells
out to tavily_jina.py next to this file; branch B imports the stack's own
modules from INTERNET_CLAUDE_DIR/lib instead of restating its HTTP contracts,
which also keeps the SSRF vetting in sanitize.py on the path.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_REPO = "~/internet-claude"
BOOTSTRAP_FLAG = "DEEP_SEARCH_BOOTSTRAPPED"
TAVILY_SCRIPT = "tavily_jina.py"
TAVILY_TIMEOUT = 180
TAVILY = "tavily+jina"
SELFHOSTED = "searxng+crawl4ai"
TAVILY_MAX_RESULTS = 20
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


class BranchFailed(RuntimeError):
    """One source branch is unusable. Its reason is reported, never swallowed."""


@dataclass
class Entry:
    url: str
    title: str
    text: str
    snippet: str = ""
    engine: str = ""
    sources: list[str] = field(default_factory=list)


def repo_dir() -> Path:
    return Path(os.environ.get("INTERNET_CLAUDE_DIR", DEFAULT_REPO)).expanduser()


def normalize_proxy_env(proxy: str) -> None:
    """Set one proxy for this process and everything it spawns.

    A machine's own NO_PROXY can carry IPv6 entries that httpx refuses to parse
    ("Invalid port: ':'"), so the list is replaced rather than extended. Branch A
    runs as a subprocess on urllib and picks the same values up from here.
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
    """Re-exec under the stack's venv when this interpreter has no httpx."""
    if os.environ.get(BOOTSTRAP_FLAG):
        return
    if importlib.util.find_spec("httpx") is not None:
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
        raise BranchFailed(f"нет каталога {package}; задай INTERNET_CLAUDE_DIR")
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


def _searxng_url() -> str:
    override = os.environ.get("DEEP_SEARCH_SEARXNG_URL")
    port = os.environ.get("SEARXNG_PORT", "8880")
    return override or f"http://127.0.0.1:{port}"


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


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def tavily_script() -> Path:
    """Locate the Tavily+Jina script that runs branch A."""
    path = Path(__file__).resolve().parent / TAVILY_SCRIPT
    if not path.exists():
        raise BranchFailed(f"скрипт ветки не найден: {path}")
    return path


def search_tavily(
    query: str, *, max_results: int, full_pages: int, max_chars: int
) -> list[Entry]:
    script = tavily_script()
    # Tavily refuses more than 20 results per request; pages read in full are
    # capped by the same tier budget the self-hosted branch uses.
    asked = max(1, min(max_results, TAVILY_MAX_RESULTS))
    command = [
        shutil.which("python3") or sys.executable,
        str(script),
        query,
        "--max-results",
        str(asked),
        "--jina-pages",
        str(max(1, min(full_pages, asked))),
        "--max-chars",
        str(max_chars),
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=TAVILY_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise BranchFailed(f"не ответил за {TAVILY_TIMEOUT} с") from exc
    if done.returncode != 0:
        raise BranchFailed(_first_line(done.stderr) or f"exit code {done.returncode}")
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise BranchFailed(f"вернул не JSON: {_first_line(done.stdout)}") from exc
    return _entries_from_tavily(payload)


def _entries_from_tavily(payload: dict) -> list[Entry]:
    entries: list[Entry] = []
    read: set[str] = set()

    for page in payload.get("normalized_pages", []):
        url = (page.get("url") or "").strip()
        if not url:
            continue
        jina = page.get("jina") or {}
        snippet = (page.get("tavily_content") or "").strip()
        read.add(url)
        entries.append(
            Entry(
                url=url,
                title=page.get("title") or url,
                text=(jina.get("content") or snippet).strip(),
                snippet=snippet,
                sources=[TAVILY],
            )
        )

    for item in payload.get("results", []):
        url = (item.get("url") or "").strip()
        if not url or url in read:
            continue
        entries.append(
            Entry(
                url=url,
                title=item.get("title") or url,
                text="",
                snippet=(item.get("content") or "").strip(),
                sources=[TAVILY],
            )
        )
    return entries


async def discover_engines(client) -> tuple[list[str], str]:
    """Every general-category engine this SearXNG has, minus the non-search utilities.

    Read from /config rather than hardcoded, so an image upgrade that adds or
    renames engines needs no edit here. Engines disabled by default are included
    on purpose: an explicit `engines=` parameter activates them per request, and
    four enabled engines is what made one CAPTCHA wave empty the whole branch.
    """
    httpx = _require_httpx()
    try:
        response = await client.get(f"{_searxng_url()}/config", timeout=10.0)
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


async def resolve_engines(client, spec: str) -> tuple[list[str], str]:
    if spec == "builtin":
        return [], ""
    if spec == "auto":
        return await discover_engines(client)
    return [name.strip() for name in spec.split(",") if name.strip()], ""


async def searxng_search(
    client,
    query: str,
    *,
    count: int,
    language: str,
    time_range: str,
    engines: list[str],
    timeout: float,
) -> tuple[list, list[tuple[str, str]]]:
    """Query SearXNG directly, returning hits and the engines that did not answer.

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
        f"{_searxng_url()}/search", params=params, timeout=timeout
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


async def search_selfhosted(
    client,
    query: str,
    *,
    max_results: int,
    full_pages: int,
    language: str,
    time_range: str,
    engines: str,
    search_timeout: float,
    fresh: bool,
) -> tuple[list[Entry], list[tuple[str, str]], str]:
    backends, sanitize = _stack_modules("backends", "sanitize")
    token = crawl_token()
    chosen, engines_warning = await resolve_engines(client, engines)

    hits, down = await searxng_search(
        client,
        query,
        # Exactly max_results, not a multiple: SearXNG returns its pool in score
        # order and a wide engine set puts link-farm domains in the tail.
        count=max_results,
        language=language,
        time_range=time_range,
        engines=chosen,
        timeout=search_timeout,
    )
    asked = str(len(chosen)) if chosen else "по умолчанию"
    answered = len({hit.engine for hit in hits if hit.engine})
    note = f"движки: запрошено {asked}, с результатами {answered}, без ответа {len(down)}"
    if engines_warning:
        note += f" ({engines_warning})"

    if not hits:
        listed = ", ".join(f"{name} ({reason})" for name, reason in down)
        detail = f"; недоступны: {listed}" if listed else ""
        raise BranchFailed(f"SearXNG вернул 0 результатов; {note}{detail}")

    safe, rejected = await asyncio.to_thread(
        sanitize.partition_safe, [hit.url for hit in hits]
    )
    kept = sanitize.dedupe_by_domain(safe)[:max_results]
    failures = await asyncio.to_thread(_describe_rejects, rejected)
    if not kept:
        return [], failures, note

    by_url = {hit.url: hit for hit in hits}
    crawler = backends.Crawl4ai(_crawl4ai_url(), token, client)
    cache_mode = "bypass" if fresh else "enabled"

    entries, crawl_failures = await _crawl_batch(
        crawler, kept[:full_pages], by_url, cache_mode
    )
    failures.extend(crawl_failures)
    spare = kept[full_pages:]

    # Cloudflare challenges and empty pages are common enough that asking for ten
    # full reads and settling for seven wastes the tier. One backfill round only.
    shortfall = full_pages - len(entries)
    if shortfall > 0 and spare:
        extra, spare = spare[:shortfall], spare[shortfall:]
        more, more_failures = await _crawl_batch(crawler, extra, by_url, cache_mode)
        entries.extend(more)
        failures.extend(more_failures)

    # The rest stay as links with the engine's own snippet: a map of what else
    # exists, cheap enough to keep dozens of them in the report.
    for url in spare:
        hit = by_url[url]
        entries.append(
            Entry(
                url=url,
                title=hit.title,
                text="",
                snippet=hit.snippet,
                engine=hit.engine,
                sources=[SELFHOSTED],
            )
        )
    return entries, failures, note


async def _crawl_batch(
    crawler, urls: list[str], by_url: dict, cache_mode: str
) -> tuple[list[Entry], list[tuple[str, str]]]:
    if not urls:
        return [], []
    entries: list[Entry] = []
    failures: list[tuple[str, str]] = []
    for page in await crawler.crawl(urls, cache_mode=cache_mode):
        if page.error:
            failures.append((page.url, page.error))
            continue
        hit = by_url.get(page.url)
        entries.append(
            Entry(
                url=page.url,
                title=page.title or (hit.title if hit else page.url),
                text=page.markdown.strip(),
                snippet=hit.snippet if hit else "",
                engine=hit.engine if hit else "",
                sources=[SELFHOSTED],
            )
        )
    return entries, failures


def _describe_rejects(rejected: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Separate dead domains from a real SSRF veto.

    sanitize blocks both with one message, and a wide engine pool drags in
    link-farm hosts that do not resolve. Collapsing those into a count keeps the
    failure block readable while every private-address hit stays itemised.
    """
    dead = 0
    described: list[tuple[str, str]] = []
    for url, reason in rejected:
        host = urlsplit(url).hostname or ""
        try:
            socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            dead += 1
            continue
        described.append((url, reason))
    if dead:
        described.append(
            (f"{dead} ссыл(ок) от мусорных движков", "домен не резолвится, отброшено до краула")
        )
    return described


async def search_social(
    client, query: str, *, limit: int, per_platform: int, max_chars: int
) -> str:
    backends, sources, aggregate, social = _stack_modules(
        "backends", "sources", "aggregate", "social"
    )
    searxng = backends.Searxng(_searxng_url(), client)
    chosen = aggregate.select(sources.build_sources(client, searxng), None)
    spec = social.QuerySpec(terms=query, limit=per_platform)
    per_source, failures, took = await aggregate.gather(chosen, spec, None)
    posts = social.merge(per_source, sort="relevance", limit=limit)
    stats = {
        name: f"{len(found)} ({took[name]} ms)" for name, found in per_source.items()
    }
    return social.render(posts, failures, stats, max_chars_per_post=max_chars)


async def selfhosted_branch(
    args,
) -> tuple[list[Entry], list[tuple[str, str]], str, str]:
    httpx = _require_httpx()
    # Proxy comes from the env normalize_proxy_env() has already rewritten, which
    # keeps 127.0.0.1 (SearXNG, Crawl4AI) direct and sends external hosts through.
    async with httpx.AsyncClient(follow_redirects=True) as client:
        entries, failures, engines_note = await search_selfhosted(
            client,
            args.query,
            max_results=args.max_results,
            full_pages=args.full_pages,
            language=args.language,
            time_range=args.time_range,
            engines=args.engines,
            search_timeout=args.search_timeout,
            fresh=args.fresh,
        )
        social_block = ""
        if args.social:
            social_block = await search_social(
                client,
                args.query,
                limit=args.social_limit,
                per_platform=args.social_per_platform,
                max_chars=args.social_chars,
            )
    return entries, failures, social_block, engines_note


def make_canonicalizer():
    """Stack's canonical_url when reachable; branch A must work without the repo."""
    try:
        (social,) = _stack_modules("social")
    except BranchFailed:
        return lambda url: url.strip().rstrip("/").lower()
    return social.canonical_url


def make_truncate():
    try:
        (sanitize,) = _stack_modules("sanitize")
    except BranchFailed:

        def cut(text: str, limit: int) -> str:
            if limit <= 0 or len(text) <= limit:
                return text
            return (
                text[:limit].rstrip()
                + f"\n\n…[truncated, {len(text) - limit} chars omitted]"
            )

        return cut
    return sanitize.truncate


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
    failures: list[tuple[str, str]],
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

    if failures:
        listed = "\n".join(f"- {name} — {reason}" for name, reason in failures)
        parts.append(f"### Не удалось получить\n{listed}")
    return "\n\n".join(parts)


async def run(args) -> tuple[str, int]:
    names: list[str] = []
    branches = []
    if args.only != "searxng":
        names.append(TAVILY)
        branches.append(
            asyncio.to_thread(
                search_tavily,
                args.query,
                max_results=args.max_results,
                full_pages=args.full_pages,
                max_chars=args.max_chars_per_page,
            )
        )
    if args.only != "tavily":
        names.append(SELFHOSTED)
        branches.append(selfhosted_branch(args))

    outcomes = await asyncio.gather(*branches, return_exceptions=True)

    groups: list[list[Entry]] = []
    failures: list[tuple[str, str]] = []
    stats: list[str] = []
    social_block = ""
    engines_note = ""
    alive = 0

    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failures.append((name, _reason(outcome)))
            continue
        alive += 1
        if name == TAVILY:
            entries = outcome
        else:
            entries, page_failures, social_block, engines_note = outcome
            failures.extend(page_failures)
        groups.append(entries)
        stats.append(f"{name}: {len(entries)}")

    if not alive:
        listed = "\n".join(f"{name}: {reason}" for name, reason in failures)
        return f"{listed}\nпрокси: {args.proxy_note}", 3

    merged = merge(groups, make_canonicalizer())
    both = sum(1 for entry in merged if len(entry.sources) > 1)
    if both:
        stats.append(f"совпадений: {both}")
    read_fully = sum(1 for entry in merged if entry.text)
    stats.append(f"прочитано полностью: {read_fully}")

    if args.json:
        payload = {
            "query": args.query,
            "proxy": args.proxy_note,
            "engines": engines_note,
            "entries": [vars(entry) for entry in merged],
            "failures": [{"source": name, "reason": reason} for name, reason in failures],
            "social": social_block,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2), 0

    output = render(
        args.query,
        merged,
        failures,
        stats,
        max_chars_per_page=args.max_chars_per_page,
        total_budget=args.total_budget,
        truncate=make_truncate(),
        proxy_note=args.proxy_note,
        engines_note=engines_note,
        snippet_chars=args.snippet_chars,
    )
    if social_block:
        output += f"\n\n## Социальные сети\n\n{social_block}"
    return output, 0


def _reason(error: BaseException) -> str:
    if isinstance(error, BranchFailed):
        return str(error)
    return f"{type(error).__name__}: {error}"


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Tavily+Jina и self-hosted SearXNG+Crawl4AI параллельно, один отчёт."
    )
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Всего источников в отчёте с каждой ветки (полные + ссылки)",
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
    parser.add_argument("--only", choices=["tavily", "searxng"], default="")
    parser.add_argument("--fresh", action="store_true", help="Обойти кэш Crawl4AI")
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
