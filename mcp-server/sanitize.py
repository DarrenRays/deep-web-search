"""URL vetting and output trimming.

The tools here accept URLs that a language model proposes, so every URL is
treated as untrusted input before it reaches Crawl4AI.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

TRUNCATION_MARKER = "\n\n…[truncated, {dropped} chars omitted]"


def _is_blocked_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return True

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


def rejection_reason(url: str) -> str | None:
    """Return why the URL must not be fetched, or None if it is safe.

    Blocks anything resolving into private, loopback or link-local space:
    without this the server would happily proxy requests into the compose
    network, the Docker host and cloud metadata endpoints.
    """
    parts = urlsplit(url)

    if parts.scheme not in ALLOWED_SCHEMES:
        return f"scheme {parts.scheme or '(none)'!r} is not allowed"
    if not parts.hostname:
        return "no host in URL"
    if _is_blocked_address(parts.hostname):
        return "resolves to a private or unroutable address"
    return None


def partition_safe(urls: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Split URLs into (safe, [(url, reason), ...]). Blocking: run in a thread."""
    safe: list[str] = []
    rejected: list[tuple[str, str]] = []
    for url in urls:
        reason = rejection_reason(url)
        if reason is None:
            safe.append(url)
        else:
            rejected.append((url, reason))
    return safe, rejected


def registrable_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def dedupe_by_domain(urls: list[str], max_per_domain: int = 2) -> list[str]:
    """Keep search order, drop repeats of the same host beyond the cap."""
    seen: dict[str, int] = {}
    kept: list[str] = []
    for url in urls:
        host = registrable_host(url)
        if seen.get(host, 0) >= max_per_domain:
            continue
        seen[host] = seen.get(host, 0) + 1
        kept.append(url)
    return kept


def truncate(text: str, limit: int) -> str:
    """Cut at the last paragraph break before the limit, marking what was lost."""
    if limit <= 0 or len(text) <= limit:
        return text

    head = text[:limit]
    boundary = head.rfind("\n\n")
    if boundary > limit // 2:
        head = head[:boundary]
    return head.rstrip() + TRUNCATION_MARKER.format(dropped=len(text) - len(head))
