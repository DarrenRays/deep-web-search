from datetime import datetime, timezone

import httpx
import pytest

from social import QuerySpec
from sources import (
    Bluesky,
    Feeds,
    HackerNews,
    Mastodon,
    Missing,
    RedditArchive,
    SourceUnavailable,
    TelegramPublic,
    YouTube,
    build_sources,
)

SPEC = QuerySpec(terms="claude", limit=5)

HN_BODY = {
    "hits": [
        {
            "objectID": "48734373",
            "title": "Claude Code marks requests",
            "url": "https://example.dev/post",
            "author": "kirushik",
            "points": 2445,
            "num_comments": 750,
            "created_at_i": 1782834264,
        },
        {
            "objectID": "1",
            "title": "Ask HN: anything?",
            "url": None,
            "author": "someone",
            "points": 3,
            "num_comments": 0,
            "created_at_i": 1782834264,
        },
    ]
}

MASTODON_BODY = [
    {
        "id": "117106272417478266",
        "url": "https://mastodon.social/@taoofmac/117106272417478266",
        "created_at": "2026-08-16T17:05:08.750Z",
        "account": {"acct": "taoofmac"},
        "favourites_count": 4,
        "replies_count": 1,
        "content": "<p>Notes about <a href='#'>claude</a> tooling</p>",
    }
]

TELEGRAM_HTML = """
<div class="tgme_widget_message" data-post="durov/522">
  <div class="tgme_widget_message_text">Claude is now available here</div>
  <time datetime="2026-06-01T15:46:12+00:00"></time>
</div>
<div class="tgme_widget_message" data-post="durov/523">
  <div class="tgme_widget_message_text">Unrelated announcement</div>
  <time datetime="2026-06-02T15:46:12+00:00"></time>
</div>
"""


def client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_hackernews_maps_fields_and_falls_back_to_the_discussion_url():
    async def handler(request):
        assert request.url.params["tags"] == "story"
        return httpx.Response(200, json=HN_BODY)

    async with client_returning(handler) as client:
        posts = await HackerNews(client).search(SPEC)

    assert posts[0].url == "https://example.dev/post"
    assert posts[0].score == 2445
    assert posts[0].created_at.tzinfo is timezone.utc
    assert posts[1].url == "https://news.ycombinator.com/item?id=1"


async def test_hackernews_passes_the_since_bound_as_a_numeric_filter():
    seen = {}

    async def handler(request):
        seen["filter"] = request.url.params.get("numericFilters")
        return httpx.Response(200, json={"hits": []})

    spec = QuerySpec(terms="x", since=datetime(2026, 1, 1, tzinfo=timezone.utc), limit=5)
    async with client_returning(handler) as client:
        await HackerNews(client).search(spec)

    assert seen["filter"] == "created_at_i>1767225600"


async def test_mastodon_queries_the_tag_timeline_and_strips_html():
    async def handler(request):
        assert request.url.path == "/api/v1/timelines/tag/claude"
        return httpx.Response(200, json=MASTODON_BODY)

    async with client_returning(handler) as client:
        posts = await Mastodon(client, ["mastodon.social"]).search(SPEC)

    assert posts[0].text == "Notes about claude tooling"
    assert posts[0].author == "taoofmac"
    assert posts[0].score == 4


async def test_mastodon_returns_nothing_when_the_query_has_no_usable_tag():
    async def handler(request):
        raise AssertionError("must not call the network without a tag")

    async with client_returning(handler) as client:
        assert await Mastodon(client, ["mastodon.social"]).search(QuerySpec(terms="!!")) == []


async def test_reddit_filters_the_listing_by_keyword_locally():
    body = {
        "data": [
            {
                "id": "abc",
                "title": "Claude Code review",
                "subreddit": "programming",
                "author": "dev",
                "score": 12,
                "num_comments": 3,
                "created_utc": 1786899539,
                "permalink": "/r/programming/comments/abc/",
                "selftext": "thoughts",
            },
            {
                "id": "def",
                "title": "Something else entirely",
                "subreddit": "programming",
                "author": "dev",
                "score": 99,
                "num_comments": 1,
                "created_utc": 1786899539,
                "permalink": "/r/programming/comments/def/",
                "selftext": "",
            },
        ]
    }

    async def handler(request):
        assert request.url.params["subreddit"] == "programming"
        return httpx.Response(200, json=body)

    async with client_returning(handler) as client:
        posts = await RedditArchive(client, ["programming"]).search(SPEC)

    assert [item.id for item in posts] == ["abc"]
    assert posts[0].url == "https://reddit.com/r/programming/comments/abc/"


async def test_reddit_surfaces_the_archive_rate_limit_instead_of_returning_nothing():
    """Arctic Shift answers its own rate limit as 422 with the reason in the body."""

    async def handler(request):
        return httpx.Response(422, json={"data": None, "error": "Timeout. Maybe slow down a bit"})

    async with client_returning(handler) as client:
        with pytest.raises(RuntimeError, match="Maybe slow down"):
            await RedditArchive(client, ["programming"]).search(SPEC)


async def test_reddit_still_raises_on_a_plain_transport_failure():
    async def handler(request):
        return httpx.Response(503, text="upstream down")

    async with client_returning(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await RedditArchive(client, ["programming"]).search(SPEC)


async def test_reddit_without_subreddits_reports_what_to_configure():
    async with client_returning(lambda request: httpx.Response(200)) as client:
        source = RedditArchive(client, [])
        assert "SOCIAL_REDDIT_SUBREDDITS" in source.unavailable
        with pytest.raises(SourceUnavailable):
            await source.search(SPEC)


async def test_telegram_reads_the_public_preview_and_filters_by_keyword():
    async def handler(request):
        assert request.url.path == "/s/durov"
        return httpx.Response(200, text=TELEGRAM_HTML)

    async with client_returning(handler) as client:
        posts = await TelegramPublic(client, ["durov"]).search(SPEC)

    assert [item.id for item in posts] == ["durov/522"]
    assert posts[0].url == "https://t.me/durov/522"
    assert posts[0].created_at == datetime(2026, 6, 1, 15, 46, 12, tzinfo=timezone.utc)


async def test_bluesky_exchanges_the_app_password_for_a_token_then_searches():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "token-123"})
        assert request.headers["Authorization"] == "Bearer token-123"
        return httpx.Response(
            200,
            json={
                "posts": [
                    {
                        "uri": "at://did:plc:x/app.bsky.feed.post/3kabc",
                        "cid": "cid1",
                        "author": {"handle": "alice.bsky.social"},
                        "record": {"text": "claude thoughts", "createdAt": "2026-08-16T10:00:00Z"},
                        "likeCount": 7,
                        "replyCount": 2,
                    }
                ]
            },
        )

    async with client_returning(handler) as client:
        posts = await Bluesky(client, "alice.bsky.social", "app-pass").search(SPEC)

    assert calls == [
        "/xrpc/com.atproto.server.createSession",
        "/xrpc/app.bsky.feed.searchPosts",
    ]
    assert posts[0].url == "https://bsky.app/profile/alice.bsky.social/post/3kabc"
    assert posts[0].score == 7


async def test_bluesky_401_names_the_app_password_requirement():
    async def handler(request):
        return httpx.Response(401, json={"error": "AuthenticationRequired"})

    async with client_returning(handler) as client:
        with pytest.raises(RuntimeError, match="app password"):
            await Bluesky(client, "alice.bsky.social", "wrong").search(SPEC)


async def test_youtube_403_explains_quota_rather_than_raising_a_bare_status():
    async def handler(request):
        return httpx.Response(403, json={"error": {"message": "quotaExceeded"}})

    async with client_returning(handler) as client:
        with pytest.raises(RuntimeError, match="quota"):
            await YouTube(client, "key").search(SPEC)


async def test_youtube_maps_a_video_hit():
    body = {
        "items": [
            {
                "id": {"videoId": "vid1"},
                "snippet": {
                    "title": "Claude Code walkthrough",
                    "description": "a demo",
                    "channelTitle": "Some Channel",
                    "publishedAt": "2026-08-01T09:00:00Z",
                },
            }
        ]
    }

    async def handler(request):
        assert request.url.params["type"] == "video"
        return httpx.Response(200, json=body)

    async with client_returning(handler) as client:
        posts = await YouTube(client, "key").search(SPEC)

    assert posts[0].url == "https://www.youtube.com/watch?v=vid1"
    assert posts[0].author == "Some Channel"


YOUTUBE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <id>yt:video:vid1</id>
    <title>Claude Code walkthrough</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid1"/>
    <author><name>Some Channel</name></author>
    <published>2026-08-01T09:00:00+00:00</published>
    <media:group><media:description>a claude demo</media:description></media:group>
  </entry>
  <entry>
    <id>yt:video:vid2</id>
    <title>Gardening tips</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid2"/>
    <author><name>Some Channel</name></author>
    <published>2026-08-02T09:00:00+00:00</published>
    <media:group><media:description>mulch</media:description></media:group>
  </entry>
</feed>
"""

RSS_20 = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Claude release notes</title>
    <link>https://example.com/notes</link>
    <guid>note-1</guid>
    <pubDate>Sat, 01 Aug 2026 09:00:00 +0000</pubDate>
    <description>what changed</description>
  </item>
</channel></rss>
"""


async def test_rss_reads_a_youtube_atom_feed_without_an_api_key():
    async def handler(request):
        return httpx.Response(200, text=YOUTUBE_ATOM)

    async with client_returning(handler) as client:
        posts = await Feeds(client, ["https://www.youtube.com/feeds/videos.xml"]).search(SPEC)

    assert [item.url for item in posts] == ["https://www.youtube.com/watch?v=vid1"]
    assert posts[0].author == "Some Channel"
    assert posts[0].created_at == datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


async def test_rss_reads_rfc822_dates_from_a_plain_rss_feed():
    async def handler(request):
        return httpx.Response(200, text=RSS_20)

    async with client_returning(handler) as client:
        posts = await Feeds(client, ["https://example.com/feed.xml"]).search(SPEC)

    assert posts[0].id == "note-1"
    assert posts[0].created_at == datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


async def test_sources_needing_credentials_say_so_instead_of_disappearing(monkeypatch):
    for name in ("BLUESKY_IDENTIFIER", "BLUESKY_APP_PASSWORD", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("SOCIAL_REDDIT_SUBREDDITS", "SOCIAL_TELEGRAM_CHANNELS", "SOCIAL_RSS_FEEDS"):
        monkeypatch.setenv(name, "")

    async with client_returning(lambda request: httpx.Response(200)) as client:
        sources = build_sources(client, searxng=None)

    off = {source.name: source.unavailable for source in sources if source.unavailable}
    assert set(off) == {"reddit", "telegram", "rss", "bluesky", "youtube"}
    assert "YOUTUBE_API_KEY" in off["youtube"]
    assert isinstance(next(s for s in sources if s.name == "youtube"), Missing)


async def test_configured_environment_turns_the_sources_on(monkeypatch):
    monkeypatch.setenv("SOCIAL_REDDIT_SUBREDDITS", "programming, rust")
    monkeypatch.setenv("SOCIAL_TELEGRAM_CHANNELS", "durov")
    monkeypatch.setenv("SOCIAL_RSS_FEEDS", "https://example.com/feed.xml")
    monkeypatch.setenv("BLUESKY_IDENTIFIER", "alice.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setenv("YOUTUBE_API_KEY", "key")

    async with client_returning(lambda request: httpx.Response(200)) as client:
        sources = build_sources(client, searxng=None)

    assert [source.unavailable for source in sources] == [""] * len(sources)
    reddit = next(source for source in sources if source.name == "reddit")
    assert reddit.subreddits == ["programming", "rust"]
