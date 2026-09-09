from datetime import datetime, timedelta, timezone

import pytest

from social import (
    Post,
    QueryError,
    QuerySpec,
    canonical_url,
    epoch_to_utc,
    iso_to_utc,
    merge,
    parse_time_bound,
    render,
    within_window,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def post(platform, url, *, score=None, days_old=0, text=None):
    return Post(
        platform=platform,
        id=url,
        url=url,
        created_at=NOW - timedelta(days=days_old),
        title=f"{platform} post",
        text=text,
        score=score,
    )


def test_relative_time_bounds_count_back_from_now():
    assert parse_time_bound("7d", now=NOW) == NOW - timedelta(days=7)
    assert parse_time_bound("2w", now=NOW) == NOW - timedelta(weeks=2)
    assert parse_time_bound("3mo", now=NOW) == NOW - timedelta(days=90)


def test_iso_time_bound_without_zone_is_read_as_utc():
    assert parse_time_bound("2026-01-02") == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert parse_time_bound("2026-01-02T03:04:05Z") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )


def test_empty_time_bound_means_no_bound():
    assert parse_time_bound("") is None


def test_unparsable_time_bound_explains_the_accepted_formats():
    with pytest.raises(QueryError, match="7d"):
        parse_time_bound("last tuesday")


def test_timestamps_normalise_to_utc():
    assert epoch_to_utc(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert epoch_to_utc(None) is None
    assert iso_to_utc("2026-08-16T17:05:08.750Z").hour == 17
    assert iso_to_utc("not a date") is None


def test_canonical_url_ignores_tracking_and_trailing_slash():
    assert canonical_url("https://WWW.Example.com/a/?utm_source=x&b=2") == canonical_url(
        "https://example.com/a?b=2"
    )


def test_primary_tag_strips_punctuation_for_tag_only_sources():
    assert QuerySpec(terms="#Rust lang!").primary_tag == "rust"
    assert QuerySpec(terms="   ").primary_tag is None


def test_posts_without_a_date_survive_the_window_filter():
    spec = QuerySpec(terms="x", since=NOW - timedelta(days=1))
    assert within_window(post("hn", "https://a", days_old=0), spec)
    assert not within_window(post("hn", "https://a", days_old=5), spec)
    assert within_window(Post(platform="web", id="1", url="https://a"), spec)


def test_same_link_from_two_platforms_collapses_into_one():
    merged = merge(
        {
            "hackernews": [post("hackernews", "https://blog.example/x", score=500)],
            "reddit": [post("reddit", "https://blog.example/x/?utm_source=r", score=10)],
        },
        sort="engagement",
        limit=10,
        now=NOW,
    )

    assert len(merged) == 1
    assert merged[0].platform == "hackernews"
    assert merged[0].also_seen_on == ["reddit"]


def test_engagement_ranks_inside_each_source_so_scales_cannot_dominate():
    """HN points run into the thousands, Mastodon favourites into the tens."""
    merged = merge(
        {
            "hackernews": [
                post("hackernews", "https://hn/low", score=800),
                post("hackernews", "https://hn/high", score=4000),
            ],
            "mastodon": [
                post("mastodon", "https://m/low", score=1),
                post("mastodon", "https://m/high", score=90),
            ],
        },
        sort="engagement",
        limit=2,
        now=NOW,
    )

    assert {item.url for item in merged} == {"https://hn/high", "https://m/high"}


def test_recent_sort_ignores_score():
    merged = merge(
        {
            "hackernews": [post("hackernews", "https://old", score=9000, days_old=30)],
            "mastodon": [post("mastodon", "https://new", score=1, days_old=1)],
        },
        sort="recent",
        limit=2,
        now=NOW,
    )

    assert [item.url for item in merged] == ["https://new", "https://old"]


def test_limit_cuts_the_merged_list():
    per_source = {"hackernews": [post("hackernews", f"https://a/{i}") for i in range(10)]}
    assert len(merge(per_source, sort="relevance", limit=3, now=NOW)) == 3


def test_render_reports_failures_and_source_counts():
    output = render(
        [post("hackernews", "https://a", score=5, text="body text")],
        [("bluesky", "не настроен")],
        {"hackernews": "1 (120 ms)"},
        max_chars_per_post=100,
    )

    assert "https://a" in output
    assert "body text" in output
    assert "Источники — hackernews: 1 (120 ms)" in output
    assert "bluesky — не настроен" in output


def test_render_says_nothing_found_when_there_is_nothing_at_all():
    assert render([], [], {}, max_chars_per_post=100) == "No results."
