"""Fan-out across sources: one query in, one merged answer out."""

from __future__ import annotations

import asyncio
import time

from cache import ResultCache, key_for
from social import Post, QuerySpec
from sources import Source, SourceUnavailable

FANOUT_TIMEOUT = 20.0


class UnknownPlatform(ValueError):
    """The caller asked for a platform this server does not have."""


def select(sources: list[Source], platforms: list[str] | None) -> list[Source]:
    """Pick the sources to query.

    With no explicit platforms, unconfigured sources are left out rather than
    reported as failures on every single search; naming one explicitly still
    queries it, so the caller gets told exactly what it needs.
    """
    if not platforms:
        return [source for source in sources if not source.unavailable]
    known = {source.name: source for source in sources}
    unknown = [name for name in platforms if name not in known]
    if unknown:
        raise UnknownPlatform(
            f"unknown platform(s) {', '.join(unknown)}; available: {', '.join(known)}"
        )
    return [known[name] for name in platforms]


async def gather(
    sources: list[Source], spec: QuerySpec, cache: ResultCache | None = None
) -> tuple[dict[str, list[Post]], list[tuple[str, str]], dict[str, int]]:
    """Query every source concurrently, tolerating individual failures.

    Returns posts per source, failures as (source, reason), and how long each
    source took. A source that fails is named in the failures list rather than
    contributing an empty result, so an outage never reads as "nothing found".
    """
    started = {source.name: time.monotonic() for source in sources}
    outcomes = await asyncio.gather(
        *(_run(source, spec, cache) for source in sources), return_exceptions=True
    )

    posts: dict[str, list[Post]] = {}
    failures: list[tuple[str, str]] = []
    took: dict[str, int] = {}

    for source, outcome in zip(sources, outcomes, strict=True):
        took[source.name] = int((time.monotonic() - started[source.name]) * 1000)
        if isinstance(outcome, BaseException):
            failures.append((source.name, _reason(outcome)))
        elif outcome:
            posts[source.name] = outcome
    return posts, failures, took


async def _run(source: Source, spec: QuerySpec, cache: ResultCache | None) -> list[Post]:
    key = key_for(source.name, spec)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    async with asyncio.timeout(FANOUT_TIMEOUT):
        posts = await source.search(spec)

    if cache is not None:
        cache.put(key, posts)
    return posts


def _reason(error: BaseException) -> str:
    if isinstance(error, SourceUnavailable):
        return f"не настроен: {error}"
    if isinstance(error, TimeoutError):
        return f"не ответил за {FANOUT_TIMEOUT:.0f} с"
    return f"{type(error).__name__}: {error}"
