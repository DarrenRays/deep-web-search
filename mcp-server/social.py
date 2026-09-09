"""Shared model, merge and ranking for the federated social search.

Every source adapter returns `Post` objects; everything downstream of that
point is source-agnostic.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sanitize import truncate

RELEVANCE_RECENCY_WEIGHT = 0.4
RECENCY_HALF_LIFE_DAYS = 14.0

_RELATIVE_PATTERN = re.compile(r"^(\d+)\s*(h|d|w|mo|y)$", re.IGNORECASE)
_RELATIVE_UNITS = {
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "mo": timedelta(days=30),
    "y": timedelta(days=365),
}

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "ref_src",
        "ref_url",
        "fbclid",
        "gclid",
        "share_id",
    }
)


class QueryError(ValueError):
    """A query argument cannot be interpreted."""


@dataclass(slots=True)
class Post:
    platform: str
    id: str
    url: str
    author: str | None = None
    created_at: datetime | None = None
    title: str | None = None
    text: str | None = None
    score: int | None = None
    num_comments: int | None = None
    also_seen_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QuerySpec:
    """Source-agnostic query. Each adapter translates it into its own dialect."""

    terms: str
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 10

    @property
    def keywords(self) -> list[str]:
        return [word for word in self.terms.split() if word]

    @property
    def primary_tag(self) -> str | None:
        """First keyword reduced to a hashtag-safe token, for tag-only sources."""
        for word in self.keywords:
            tag = re.sub(r"\W", "", word, flags=re.UNICODE)
            if tag:
                return tag.lower()
        return None


def parse_time_bound(value: str, *, now: datetime | None = None) -> datetime | None:
    """Accept an ISO timestamp or a relative offset such as "7d" or "3mo"."""
    if not value:
        return None
    match = _RELATIVE_PATTERN.match(value.strip())
    if match:
        reference = now or datetime.now(timezone.utc)
        return reference - _RELATIVE_UNITS[match.group(2).lower()] * int(match.group(1))
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryError(
            f"cannot read time bound {value!r}: expected an ISO date or an offset "
            f"like 7d, 2w, 3mo"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def epoch_to_utc(seconds: float | int | str | None) -> datetime | None:
    if seconds is None or seconds == "":
        return None
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc)


def iso_to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same link from two platforms compares equal."""
    split = urlsplit(url.strip())
    host = split.netloc.lower().removeprefix("www.")
    path = split.path.rstrip("/") or "/"
    kept = sorted(
        (key, value)
        for key, value in parse_qsl(split.query)
        if key.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((split.scheme.lower() or "https", host, path, urlencode(kept), ""))


def within_window(post: Post, spec: QuerySpec) -> bool:
    """Keep posts whose date fits the window, and those with no date at all.

    Sources that cannot filter server-side (RSS, t.me) are filtered here instead.
    """
    if post.created_at is None:
        return True
    if spec.since and post.created_at < spec.since:
        return False
    if spec.until and post.created_at > spec.until:
        return False
    return True


def _percentiles(posts: list[Post]) -> dict[int, float]:
    """Rank scores inside one source, since raw scores across sources are not comparable.

    Reddit karma, HN points and Bluesky likes live on different scales; comparing
    them directly would let whichever source inflates numbers win every time.
    """
    scored = [post for post in posts if post.score is not None]
    if len(scored) < 2:
        return {id(post): 0.5 for post in posts}

    order = sorted(scored, key=lambda post: post.score or 0)
    ranks = {id(post): index / (len(order) - 1) for index, post in enumerate(order)}
    return {id(post): ranks.get(id(post), 0.5) for post in posts}


def _recency(post: Post, now: datetime) -> float:
    if post.created_at is None:
        return 0.5
    age_days = max((now - post.created_at).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)


def merge(
    per_source: dict[str, list[Post]],
    *,
    sort: str,
    limit: int,
    now: datetime | None = None,
) -> list[Post]:
    """Deduplicate across sources, rank, and cut to the output budget."""
    moment = now or datetime.now(timezone.utc)
    weights: dict[int, float] = {}
    for posts in per_source.values():
        weights.update(_percentiles(posts))

    merged = _dedupe(post for posts in per_source.values() for post in posts)

    undated = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "recent":
        merged.sort(key=lambda post: post.created_at or undated, reverse=True)
    elif sort == "engagement":
        merged.sort(key=lambda post: weights.get(id(post), 0.5), reverse=True)
    else:
        merged.sort(
            key=lambda post: (1 - RELEVANCE_RECENCY_WEIGHT) * weights.get(id(post), 0.5)
            + RELEVANCE_RECENCY_WEIGHT * _recency(post, moment),
            reverse=True,
        )
    return merged[:limit]


def _dedupe(posts) -> list[Post]:
    """Collapse the same link seen on several platforms into one entry.

    A link shared to Hacker News, Reddit and Mastodon otherwise fills the output
    with three copies of itself.
    """
    by_url: dict[str, Post] = {}
    for post in posts:
        key = canonical_url(post.url)
        kept = by_url.get(key)
        if kept is None:
            by_url[key] = post
            continue
        winner, loser = (post, kept) if (post.score or 0) > (kept.score or 0) else (kept, post)
        winner.also_seen_on = sorted(
            {*winner.also_seen_on, *loser.also_seen_on, loser.platform} - {winner.platform}
        )
        by_url[key] = winner
    return list(by_url.values())


def render(
    posts: list[Post],
    errors: list[tuple[str, str]],
    stats: dict[str, str],
    *,
    max_chars_per_post: int,
) -> str:
    """Format the merged result the way the other tools in this server do."""
    if not posts and not errors:
        return "No results."

    blocks = [
        _render_post(position, post, max_chars_per_post)
        for position, post in enumerate(posts, start=1)
    ]
    output = "\n\n".join(blocks) if blocks else "No results."

    if stats:
        summary = ", ".join(f"{name}: {value}" for name, value in sorted(stats.items()))
        output += f"\n\n---\n\nИсточники — {summary}"
    if errors:
        listed = "\n".join(f"- {name} — {reason}" for name, reason in errors)
        output += f"\n\n### Не удалось получить\n{listed}"
    return output


def _render_post(position: int, post: Post, max_chars: int) -> str:
    meta = [post.platform]
    if post.created_at:
        meta.append(post.created_at.strftime("%Y-%m-%d %H:%M UTC"))
    if post.author:
        meta.append(f"@{post.author}")
    if post.score is not None:
        meta.append(f"score {post.score}")
    if post.num_comments is not None:
        meta.append(f"comments {post.num_comments}")
    if post.also_seen_on:
        meta.append("также: " + ", ".join(post.also_seen_on))

    head = f"### {position}. {post.title or post.url}\n{' · '.join(meta)}\n{post.url}"
    body = truncate(post.text.strip(), max_chars) if post.text else ""
    return f"{head}\n\n{body}" if body else head
