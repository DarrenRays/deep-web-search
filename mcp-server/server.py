"""MCP server joining SearXNG (find URLs) to Crawl4AI (read them)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.session import ServerSession
from mcp.server.transport_security import TransportSecuritySettings

from aggregate import UnknownPlatform, gather, select
from backends import Crawl4ai, PageResult, Searxng, env
from cache import ResultCache
from sanitize import dedupe_by_domain, partition_safe, truncate
from social import QueryError, QuerySpec, merge, parse_time_bound
from social import render as render_posts
from sources import Source, build_sources

MAX_TOTAL_CHARS = 60_000
MAX_RESULTS_CAP = 20
SOCIAL_LIMIT_CAP = 100
SOCIAL_PER_PLATFORM_CAP = 25


@dataclass(slots=True)
class Services:
    searxng: Searxng
    crawl4ai: Crawl4ai
    social: list[Source]
    social_cache: ResultCache


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[Services]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        searxng = Searxng(env("SEARXNG_URL", "http://searxng:8080"), client)
        yield Services(
            searxng=searxng,
            crawl4ai=Crawl4ai(
                env("CRAWL4AI_URL", "http://crawl4ai:11235"),
                env("CRAWL4AI_API_TOKEN"),
                client,
            ),
            social=build_sources(client, searxng),
            social_cache=ResultCache(),
        )


mcp = MCPServer(
    "searxng-crawl",
    instructions=(
        "Self-hosted web research. Use search_and_scrape for a normal question, "
        "search when you only need to pick promising links, and fetch_url when you "
        "already know the exact page. For what people are saying on social media, "
        "use social_search: it queries every configured platform at once."
    ),
    lifespan=lifespan,
)


ToolContext = Context[ServerSession, Services]


async def _gather_pages(
    services: Services, urls: list[str], *, cache_mode: str
) -> tuple[list[PageResult], list[tuple[str, str]]]:
    safe, rejected = await asyncio.to_thread(partition_safe, urls)
    if not safe:
        return [], rejected
    return await services.crawl4ai.crawl(safe, cache_mode=cache_mode), rejected


def _render(
    pages: list[PageResult],
    rejected: list[tuple[str, str]],
    *,
    max_chars_per_page: int,
) -> str:
    blocks: list[str] = []
    failures: list[str] = [f"{url} — {reason}" for url, reason in rejected]
    budget = MAX_TOTAL_CHARS
    position = 0

    for page in pages:
        if page.error:
            failures.append(f"{page.url} — {page.error}")
            continue
        if budget <= 0:
            failures.append(f"{page.url} — skipped, total output budget exhausted")
            continue

        position += 1
        body = truncate(page.markdown, min(max_chars_per_page, budget))
        budget -= len(body)
        blocks.append(f"## {position}. {page.title}\n{page.url}\n\n{body}")

    if not blocks and not failures:
        return "No results."

    output = "\n\n---\n\n".join(blocks)
    if failures:
        listed = "\n".join(f"- {item}" for item in failures)
        output += f"\n\n---\n\n### Не удалось получить\n{listed}"
    return output


@mcp.tool()
async def search_and_scrape(
    ctx: ToolContext,
    query: str,
    max_results: int = 5,
    max_chars_per_page: int = 4000,
    language: str = "ru",
    time_range: str = "",
    fresh: bool = False,
) -> str:
    """Search the web and return the readable text of the top results.

    Args:
        query: What to search for.
        max_results: How many pages to read, at most 20.
        max_chars_per_page: Per-page cut-off; raise it only when a page is truncated
            and the missing part matters.
        language: SearXNG language code, e.g. "ru", "en", "all".
        time_range: Empty, or one of "day", "week", "month", "year".
        fresh: Skip the crawler cache and refetch every page.
    """
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    services = ctx.request_context.lifespan_context

    hits = await services.searxng.search(
        query, count=max_results * 3, language=language, time_range=time_range
    )
    if not hits:
        return f"SearXNG returned no results for {query!r}."

    chosen = dedupe_by_domain([hit.url for hit in hits])[:max_results]
    pages, rejected = await _gather_pages(
        services, chosen, cache_mode="bypass" if fresh else "enabled"
    )
    return _render(pages, rejected, max_chars_per_page=max_chars_per_page)


@mcp.tool()
async def search(
    ctx: ToolContext,
    query: str,
    max_results: int = 10,
    language: str = "ru",
    time_range: str = "",
) -> str:
    """List search results as title, URL and snippet without fetching the pages.

    Cheap reconnaissance: use it to choose which links are worth reading, then
    pass them to fetch_url.
    """
    hits = await ctx.request_context.lifespan_context.searxng.search(
        query, count=max_results, language=language, time_range=time_range
    )
    if not hits:
        return f"SearXNG returned no results for {query!r}."

    lines = []
    for position, hit in enumerate(hits, start=1):
        suffix = f" [{hit.engine}]" if hit.engine else ""
        lines.append(f"{position}. {hit.title}{suffix}\n   {hit.url}\n   {hit.snippet}")
    return "\n".join(lines)


@mcp.tool()
async def fetch_url(
    ctx: ToolContext, url: str, max_chars: int = 8000, fresh: bool = False
) -> str:
    """Fetch one known URL and return it as readable markdown."""
    pages, rejected = await _gather_pages(
        ctx.request_context.lifespan_context,
        [url],
        cache_mode="bypass" if fresh else "enabled",
    )
    return _render(pages, rejected, max_chars_per_page=max_chars)


@mcp.tool()
async def social_search(
    ctx: ToolContext,
    query: str,
    platforms: str = "",
    since: str = "",
    until: str = "",
    sort: str = "relevance",
    limit: int = 30,
    per_platform: int = 10,
    max_chars_per_post: int = 600,
) -> str:
    """Search every configured social platform at once and return one merged list.

    This is the single entry point for social media: one query fans out to
    Hacker News, Reddit, Mastodon, Telegram, Bluesky, YouTube and web search,
    and the results come back deduplicated and ranked together. Platforms that
    are unconfigured or down are named at the end instead of silently omitted.

    Args:
        query: What to look for.
        platforms: Comma-separated subset, e.g. "reddit,hackernews". Empty means all.
            Run social_sources to see the names.
        since: Lower time bound: an ISO date or an offset like "7d", "2w", "3mo".
        until: Upper time bound, same format.
        sort: "relevance" (engagement plus freshness), "recent", or "engagement".
        limit: Total posts to return, at most 100.
        per_platform: How many to ask each platform for, at most 25.
        max_chars_per_post: Per-post text cut-off.
    """
    if sort not in {"relevance", "recent", "engagement"}:
        return f"Unknown sort {sort!r}. Use relevance, recent or engagement."

    try:
        spec = QuerySpec(
            terms=query,
            since=parse_time_bound(since),
            until=parse_time_bound(until),
            limit=max(1, min(per_platform, SOCIAL_PER_PLATFORM_CAP)),
        )
        services = ctx.request_context.lifespan_context
        chosen = select(
            services.social,
            [name.strip() for name in platforms.split(",") if name.strip()],
        )
    except (QueryError, UnknownPlatform) as exc:
        return str(exc)

    per_source, failures, took = await gather(chosen, spec, services.social_cache)
    posts = merge(per_source, sort=sort, limit=max(1, min(limit, SOCIAL_LIMIT_CAP)))
    counts = {name: len(found) for name, found in per_source.items()}
    return render_posts(
        posts,
        failures,
        {name: f"{count} ({took[name]} ms)" for name, count in counts.items()},
        max_chars_per_post=max_chars_per_post,
    )


@mcp.tool()
async def social_sources(ctx: ToolContext) -> str:
    """List the social platforms social_search can reach, and what the rest need."""
    lines = []
    for source in ctx.request_context.lifespan_context.social:
        reason = source.unavailable
        status = f"выключен: {reason}" if reason else "готов"
        lines.append(f"- {source.name} — {status}")
    return "\n".join(lines)


if __name__ == "__main__":
    port = int(env("MCP_PORT", "8765"))
    mcp.run(
        transport="streamable-http",
        host=env("MCP_HOST", "0.0.0.0"),
        port=port,
        # The container listens on 0.0.0.0 but is published on 127.0.0.1 only, so
        # the Host header arriving from Claude Code is a loopback address.
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
        ),
    )
