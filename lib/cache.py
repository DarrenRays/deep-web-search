"""Short-lived result cache, so re-asking inside one session costs no requests.

In-memory rather than on disk: the server is long-lived, the container's
filesystem is not, and a cache that outlives a restart would only serve stale
social posts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from social import Post, QuerySpec

CACHE_TTL = 900.0
CACHE_MAX_ENTRIES = 256


def key_for(source: str, spec: QuerySpec) -> str:
    since = spec.since.isoformat() if spec.since else ""
    until = spec.until.isoformat() if spec.until else ""
    return f"{source}|{spec.terms.strip().lower()}|{since}|{until}|{spec.limit}"


@dataclass(slots=True)
class ResultCache:
    ttl: float = CACHE_TTL
    max_entries: int = CACHE_MAX_ENTRIES
    _entries: dict[str, tuple[float, list[Post]]] = field(default_factory=dict)

    def get(self, key: str, *, now: float | None = None) -> list[Post] | None:
        moment = now if now is not None else time.monotonic()
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, posts = entry
        if moment - stored_at > self.ttl:
            del self._entries[key]
            return None
        return posts

    def put(self, key: str, posts: list[Post], *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        if len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item][0])
            del self._entries[oldest]
        self._entries[key] = (moment, posts)
