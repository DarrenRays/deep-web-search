from datetime import datetime, timezone

from cache import ResultCache, key_for
from social import Post, QuerySpec


def post(url):
    return Post(platform="hackernews", id=url, url=url)


def test_the_same_question_hits_the_same_key():
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)

    assert key_for("hackernews", QuerySpec(terms="Claude ", since=since)) == key_for(
        "hackernews", QuerySpec(terms="claude", since=since)
    )


def test_different_windows_and_sources_are_different_keys():
    spec = QuerySpec(terms="claude")
    other = QuerySpec(terms="claude", since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert key_for("hackernews", spec) != key_for("mastodon", spec)
    assert key_for("hackernews", spec) != key_for("hackernews", other)


def test_entries_are_served_until_they_expire():
    cache = ResultCache(ttl=10)
    cache.put("k", [post("https://a")], now=100.0)

    assert cache.get("k", now=105.0) is not None
    assert cache.get("k", now=111.0) is None


def test_an_empty_result_is_cached_rather_than_re_fetched():
    """A source that legitimately found nothing must not be retried every call."""
    cache = ResultCache(ttl=10)
    cache.put("k", [], now=100.0)

    assert cache.get("k", now=101.0) == []


def test_the_cache_drops_the_oldest_entry_when_full():
    cache = ResultCache(ttl=1000, max_entries=2)
    cache.put("old", [post("https://a")], now=100.0)
    cache.put("mid", [post("https://b")], now=200.0)
    cache.put("new", [post("https://c")], now=300.0)

    assert cache.get("old", now=301.0) is None
    assert cache.get("mid", now=301.0) is not None
    assert cache.get("new", now=301.0) is not None
