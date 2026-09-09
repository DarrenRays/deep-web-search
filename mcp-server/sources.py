"""One adapter per social source. Each turns a QuerySpec into a list of Post.

Adapters never swallow failures: whatever they raise is reported to the caller
as a named source error, so "the source is down" never looks like "nothing was
found".
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx
from defusedxml.ElementTree import fromstring as defused_fromstring
from selectolax.parser import HTMLParser

from backends import Searxng
from social import Post, QuerySpec, epoch_to_utc, iso_to_utc, within_window

SOURCE_TIMEOUT = 12.0
BLUESKY_HOST = "https://bsky.social"
ARCTIC_SHIFT_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
# The archive answers "Timeout. Maybe slow down a bit" to bursts, so subreddits
# are walked one at a time with a gap rather than fanned out.
ARCTIC_SHIFT_PAGE = 50
ARCTIC_SHIFT_GAP = 1.0
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
TELEGRAM_PREVIEW_URL = "https://t.me/s"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

SOCIAL_HOSTS = (
    "reddit.com",
    "x.com",
    "news.ycombinator.com",
    "bsky.app",
    "mastodon.social",
    "t.me",
)


class SourceUnavailable(RuntimeError):
    """The source is not configured, so it must not be queried at all."""


class Source(Protocol):
    name: str

    @property
    def unavailable(self) -> str:
        """Why the source cannot answer, or an empty string when it is ready."""

    async def search(self, spec: QuerySpec) -> list[Post]: ...


def _split_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _text_of(html: str) -> str:
    return HTMLParser(html).text(separator=" ", strip=True)


def _child_text(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _rss_date(value: str) -> datetime | None:
    """RSS dates are RFC 822, which fromisoformat cannot read."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _archive_error(response: httpx.Response) -> str:
    """Arctic Shift's own error string, or an empty string when it answered."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json().get("error") or ""
    return ""


def _matches(spec: QuerySpec, *fields: str | None) -> bool:
    """Keyword filter for sources that cannot search server-side."""
    haystack = " ".join(field.lower() for field in fields if field)
    return all(word.lower() in haystack for word in spec.keywords)


@dataclass(slots=True)
class HackerNews:
    """Algolia's HN index: real full-text search, no credentials."""

    client: httpx.AsyncClient
    name: str = "hackernews"

    @property
    def unavailable(self) -> str:
        return ""

    async def search(self, spec: QuerySpec) -> list[Post]:
        params = [
            ("query", spec.terms),
            ("tags", "story"),
            ("hitsPerPage", str(spec.limit)),
        ]
        if spec.since:
            params.append(("numericFilters", f"created_at_i>{int(spec.since.timestamp())}"))

        response = await self.client.get(HN_SEARCH_URL, params=params, timeout=SOURCE_TIMEOUT)
        response.raise_for_status()
        return [self._to_post(hit) for hit in response.json().get("hits", [])]

    def _to_post(self, hit: dict) -> Post:
        item_id = str(hit.get("objectID"))
        discussion = f"https://news.ycombinator.com/item?id={item_id}"
        return Post(
            platform=self.name,
            id=item_id,
            url=hit.get("url") or discussion,
            author=hit.get("author"),
            created_at=epoch_to_utc(hit.get("created_at_i")),
            title=hit.get("title"),
            text=hit.get("story_text") or f"Обсуждение: {discussion}",
            score=hit.get("points"),
            num_comments=hit.get("num_comments"),
        )


@dataclass(slots=True)
class Mastodon:
    """Public hashtag timelines. Mastodon has no cross-instance full-text search,
    so the query's first keyword is used as the tag."""

    client: httpx.AsyncClient
    instances: list[str]
    name: str = "mastodon"

    @property
    def unavailable(self) -> str:
        return ""

    async def search(self, spec: QuerySpec) -> list[Post]:
        tag = spec.primary_tag
        if not tag:
            return []

        posts: list[Post] = []
        for instance in self.instances:
            response = await self.client.get(
                f"https://{instance}/api/v1/timelines/tag/{tag}",
                params={"limit": spec.limit},
                timeout=SOURCE_TIMEOUT,
            )
            response.raise_for_status()
            posts.extend(self._to_post(status, instance) for status in response.json())
        return [post for post in posts if within_window(post, spec)]

    def _to_post(self, status: dict, instance: str) -> Post:
        account = status.get("account") or {}
        return Post(
            platform=self.name,
            id=f"{instance}:{status.get('id')}",
            url=status.get("url") or "",
            author=account.get("acct"),
            created_at=iso_to_utc(status.get("created_at")),
            title=None,
            text=_text_of(status.get("content") or ""),
            score=status.get("favourites_count"),
            num_comments=status.get("replies_count"),
        )


@dataclass(slots=True)
class RedditArchive:
    """Arctic Shift, the public Reddit archive.

    Its full-text `query` parameter times out on anything but tiny slices, so
    this adapter lists configured subreddits by date and filters keywords
    locally. Keyword discovery across all of Reddit goes through the web source
    instead.
    """

    client: httpx.AsyncClient
    subreddits: list[str]
    name: str = "reddit"

    @property
    def unavailable(self) -> str:
        if self.subreddits:
            return ""
        return (
            "SOCIAL_REDDIT_SUBREDDITS is empty; set it to the subreddits worth "
            "watching, or rely on the web source for Reddit"
        )

    async def search(self, spec: QuerySpec) -> list[Post]:
        if self.unavailable:
            raise SourceUnavailable(self.unavailable)

        posts: list[Post] = []
        for position, subreddit in enumerate(self.subreddits):
            if position:
                await asyncio.sleep(ARCTIC_SHIFT_GAP)
            posts.extend(await self._list_subreddit(subreddit, spec))
        return [post for post in posts if within_window(post, spec)]

    async def _list_subreddit(self, subreddit: str, spec: QuerySpec) -> list[Post]:
        params = {"subreddit": subreddit, "limit": ARCTIC_SHIFT_PAGE, "sort": "desc"}
        if spec.since:
            params["after"] = spec.since.date().isoformat()
        if spec.until:
            params["before"] = spec.until.date().isoformat()

        response = await self.client.get(
            ARCTIC_SHIFT_URL, params=params, timeout=SOURCE_TIMEOUT
        )
        # The archive signals its own rate limit as HTTP 422 with the reason in the
        # body, so the body is read before the status is judged.
        detail = _archive_error(response)
        if detail:
            raise RuntimeError(f"r/{subreddit}: {detail}")
        response.raise_for_status()
        # Matched on the raw fields: the rendered title carries an "r/<sub>:" prefix
        # and link posts fall back to their URL, both of which would match the very
        # keyword being searched for.
        return [
            self._to_post(raw)
            for raw in response.json().get("data") or []
            if _matches(spec, raw.get("title"), raw.get("selftext"))
        ]

    def _to_post(self, raw: dict) -> Post:
        permalink = raw.get("permalink") or ""
        return Post(
            platform=self.name,
            id=str(raw.get("id")),
            url=f"https://reddit.com{permalink}" if permalink else raw.get("url") or "",
            author=raw.get("author"),
            created_at=epoch_to_utc(raw.get("created_utc")),
            title=f"r/{raw.get('subreddit')}: {raw.get('title')}",
            text=raw.get("selftext") or raw.get("url"),
            score=raw.get("score"),
            num_comments=raw.get("num_comments"),
        )


@dataclass(slots=True)
class TelegramPublic:
    """Public channel previews at t.me/s/<channel>: no login, no phone number.

    Telegram exposes no public search, so this lists configured channels and
    filters locally, same as the Reddit archive.
    """

    client: httpx.AsyncClient
    channels: list[str]
    name: str = "telegram"

    @property
    def unavailable(self) -> str:
        if self.channels:
            return ""
        return "SOCIAL_TELEGRAM_CHANNELS is empty; set it to the public channels worth watching"

    async def search(self, spec: QuerySpec) -> list[Post]:
        if self.unavailable:
            raise SourceUnavailable(self.unavailable)

        posts: list[Post] = []
        for channel in self.channels:
            response = await self.client.get(
                f"{TELEGRAM_PREVIEW_URL}/{channel}", timeout=SOURCE_TIMEOUT
            )
            response.raise_for_status()
            posts.extend(self._parse(response.text))
        return [post for post in posts if _matches(spec, post.text) and within_window(post, spec)]

    def _parse(self, html: str) -> list[Post]:
        posts = []
        for node in HTMLParser(html).css(".tgme_widget_message"):
            slug = node.attributes.get("data-post")
            if not slug:
                continue
            body = node.css_first(".tgme_widget_message_text")
            stamp = node.css_first("time")
            posts.append(
                Post(
                    platform=self.name,
                    id=slug,
                    url=f"https://t.me/{slug}",
                    author=slug.split("/")[0],
                    created_at=iso_to_utc(stamp.attributes.get("datetime") if stamp else None),
                    title=None,
                    text=body.text(separator=" ", strip=True) if body else None,
                )
            )
        return posts


@dataclass(slots=True)
class Bluesky:
    """AT Protocol post search. Needs a handle and an app password, both free."""

    client: httpx.AsyncClient
    identifier: str
    app_password: str
    name: str = "bluesky"

    @property
    def unavailable(self) -> str:
        return ""

    async def search(self, spec: QuerySpec) -> list[Post]:
        token = await self._access_token()
        params: dict[str, str | int] = {"q": spec.terms, "limit": spec.limit}
        if spec.since:
            params["since"] = spec.since.astimezone(timezone.utc).isoformat()
        if spec.until:
            params["until"] = spec.until.astimezone(timezone.utc).isoformat()

        response = await self.client.get(
            f"{BLUESKY_HOST}/xrpc/app.bsky.feed.searchPosts",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=SOURCE_TIMEOUT,
        )
        response.raise_for_status()
        return [self._to_post(item) for item in response.json().get("posts", [])]

    async def _access_token(self) -> str:
        response = await self.client.post(
            f"{BLUESKY_HOST}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.identifier, "password": self.app_password},
            timeout=SOURCE_TIMEOUT,
        )
        if response.status_code == 401:
            raise RuntimeError(
                "Bluesky rejected the credentials. BLUESKY_APP_PASSWORD must be an "
                "app password from Settings → Privacy and Security, not the account password."
            )
        response.raise_for_status()
        return response.json()["accessJwt"]

    def _to_post(self, item: dict) -> Post:
        author = item.get("author") or {}
        handle = author.get("handle", "")
        record = item.get("record") or {}
        rkey = str(item.get("uri", "")).rsplit("/", 1)[-1]
        return Post(
            platform=self.name,
            id=item.get("cid") or rkey,
            url=f"https://bsky.app/profile/{handle}/post/{rkey}",
            author=handle,
            created_at=iso_to_utc(record.get("createdAt")),
            title=None,
            text=record.get("text"),
            score=item.get("likeCount"),
            num_comments=item.get("replyCount"),
        )


@dataclass(slots=True)
class YouTube:
    """Data API v3 search. Free quota is 10 000 units/day and one search costs 100."""

    client: httpx.AsyncClient
    api_key: str
    name: str = "youtube"

    @property
    def unavailable(self) -> str:
        return ""

    async def search(self, spec: QuerySpec) -> list[Post]:
        params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",
            "q": spec.terms,
            "maxResults": min(spec.limit, 50),
            "order": "relevance",
            "key": self.api_key,
        }
        if spec.since:
            params["publishedAfter"] = _rfc3339(spec.since)
        if spec.until:
            params["publishedBefore"] = _rfc3339(spec.until)

        response = await self.client.get(
            YOUTUBE_SEARCH_URL, params=params, timeout=SOURCE_TIMEOUT
        )
        if response.status_code == 403:
            raise RuntimeError(
                "YouTube refused the request (403): the daily quota is spent or the "
                "API key is not authorised for the Data API."
            )
        response.raise_for_status()
        return [self._to_post(item) for item in response.json().get("items", [])]

    def _to_post(self, item: dict) -> Post:
        video_id = (item.get("id") or {}).get("videoId", "")
        snippet = item.get("snippet") or {}
        return Post(
            platform=self.name,
            id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            author=snippet.get("channelTitle"),
            created_at=iso_to_utc(snippet.get("publishedAt")),
            title=snippet.get("title"),
            text=snippet.get("description"),
        )


@dataclass(slots=True)
class Feeds:
    """Plain RSS/Atom feeds, filtered locally.

    This is also how YouTube channels can be followed without an API key at all:
    https://www.youtube.com/feeds/videos.xml?channel_id=<id> is a public Atom feed
    and costs none of the Data API's daily quota.
    """

    client: httpx.AsyncClient
    urls: list[str]
    name: str = "rss"

    @property
    def unavailable(self) -> str:
        if self.urls:
            return ""
        return "SOCIAL_RSS_FEEDS is empty; set it to the feed URLs worth watching"

    async def search(self, spec: QuerySpec) -> list[Post]:
        if self.unavailable:
            raise SourceUnavailable(self.unavailable)

        posts: list[Post] = []
        for url in self.urls:
            response = await self.client.get(url, timeout=SOURCE_TIMEOUT)
            response.raise_for_status()
            posts.extend(self._parse(response.text))
        return [
            post
            for post in posts
            if _matches(spec, post.title, post.text) and within_window(post, spec)
        ]

    def _parse(self, document: str) -> list[Post]:
        root = defused_fromstring(document.encode())
        entries = root.iter(f"{ATOM_NS}entry")
        atom = [self._from_atom(entry) for entry in entries]
        return atom or [self._from_rss(item) for item in root.iter("item")]

    def _from_atom(self, entry) -> Post:
        link = entry.find(f"{ATOM_NS}link")
        return Post(
            platform=self.name,
            id=_child_text(entry, f"{ATOM_NS}id"),
            url=link.attrib.get("href", "") if link is not None else "",
            author=_child_text(entry, f"{ATOM_NS}author/{ATOM_NS}name") or None,
            created_at=iso_to_utc(_child_text(entry, f"{ATOM_NS}published")),
            title=_child_text(entry, f"{ATOM_NS}title") or None,
            text=_child_text(entry, f"{MEDIA_NS}group/{MEDIA_NS}description") or None,
        )

    def _from_rss(self, item) -> Post:
        link = _child_text(item, "link")
        return Post(
            platform=self.name,
            id=_child_text(item, "guid") or link,
            url=link,
            author=_child_text(item, "author") or None,
            created_at=_rss_date(_child_text(item, "pubDate")),
            title=_child_text(item, "title") or None,
            text=_child_text(item, "description") or None,
        )


@dataclass(slots=True)
class SocialWeb:
    """SearXNG restricted to social hosts.

    This is how platforms without a usable free API — X above all, and Reddit
    keyword search — still get covered: through what public search engines have
    already indexed.
    """

    searxng: Searxng
    name: str = "web"

    @property
    def unavailable(self) -> str:
        return ""

    async def search(self, spec: QuerySpec) -> list[Post]:
        sites = " OR ".join(f"site:{host}" for host in SOCIAL_HOSTS)
        hits = await self.searxng.search(
            f"{spec.terms} ({sites})",
            count=spec.limit,
            language="all",
            time_range=_time_range(spec),
        )
        return [
            Post(
                platform=self.name,
                id=hit.url,
                url=hit.url,
                title=hit.title,
                text=hit.snippet,
            )
            for hit in hits
        ]


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_range(spec: QuerySpec) -> str:
    """Map the query window onto the coarse buckets SearXNG understands."""
    if spec.since is None:
        return ""
    days = (datetime.now(timezone.utc) - spec.since).days
    for limit, bucket in ((1, "day"), (7, "week"), (31, "month"), (366, "year")):
        if days <= limit:
            return bucket
    return ""


def build_sources(client: httpx.AsyncClient, searxng: Searxng) -> list[Source]:
    """Every adapter the environment can support, in a stable order.

    Adapters that need credentials are built anyway and raise SourceUnavailable
    when queried, so `social_sources` can explain what is missing instead of the
    source silently disappearing.
    """
    instances = _split_env("SOCIAL_MASTODON_INSTANCES") or ["mastodon.social"]
    return [
        SocialWeb(searxng),
        HackerNews(client),
        RedditArchive(client, _split_env("SOCIAL_REDDIT_SUBREDDITS")),
        Mastodon(client, instances),
        Feeds(client, _split_env("SOCIAL_RSS_FEEDS")),
        TelegramPublic(client, _split_env("SOCIAL_TELEGRAM_CHANNELS")),
        _bluesky_or_stub(client),
        _youtube_or_stub(client),
    ]


@dataclass(slots=True)
class Missing:
    """Placeholder for a source whose credentials are not configured."""

    name: str
    detail: str

    @property
    def unavailable(self) -> str:
        return self.detail

    async def search(self, _spec: QuerySpec) -> list[Post]:
        raise SourceUnavailable(self.detail)



def _bluesky_or_stub(client: httpx.AsyncClient) -> Source:
    identifier = os.environ.get("BLUESKY_IDENTIFIER", "")
    password = os.environ.get("BLUESKY_APP_PASSWORD", "")
    if identifier and password:
        return Bluesky(client, identifier, password)
    return Missing("bluesky", "set BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD")


def _youtube_or_stub(client: httpx.AsyncClient) -> Source:
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if api_key:
        return YouTube(client, api_key)
    return Missing("youtube", "set YOUTUBE_API_KEY")
