import asyncio
from dataclasses import dataclass, field

import pytest

import aggregate
from aggregate import UnknownPlatform, gather, select
from cache import ResultCache
from social import Post, QuerySpec
from sources import SourceUnavailable

SPEC = QuerySpec(terms="claude", limit=5)


@dataclass
class Fake:
    name: str
    posts: list[Post] = field(default_factory=list)
    fails_with: BaseException | None = None
    hangs: bool = False
    unavailable: str = ""

    async def search(self, _spec):
        if self.hangs:
            await asyncio.sleep(3600)
        if self.fails_with:
            raise self.fails_with
        return self.posts


def post(platform):
    return Post(platform=platform, id="1", url=f"https://{platform}/1")


async def test_one_failing_source_does_not_hide_the_others():
    sources = [
        Fake("hackernews", posts=[post("hackernews")]),
        Fake("reddit", fails_with=RuntimeError("archive is down")),
    ]

    posts, failures, took = await gather(sources, SPEC)

    assert list(posts) == ["hackernews"]
    assert failures == [("reddit", "RuntimeError: archive is down")]
    assert set(took) == {"hackernews", "reddit"}


async def test_a_hanging_source_is_cut_off_and_named(monkeypatch):
    monkeypatch.setattr(aggregate, "FANOUT_TIMEOUT", 0.05)

    posts, failures, _ = await gather([Fake("slow", hangs=True)], SPEC)

    assert posts == {}
    assert failures[0][0] == "slow"
    assert "не ответил" in failures[0][1]


async def test_unconfigured_source_is_reported_as_configuration_not_as_a_crash():
    _, failures, _ = await gather(
        [Fake("bluesky", fails_with=SourceUnavailable("set BLUESKY_IDENTIFIER"))], SPEC
    )

    assert failures == [("bluesky", "не настроен: set BLUESKY_IDENTIFIER")]


async def test_sources_run_concurrently_rather_than_one_after_another():
    @dataclass
    class Slow:
        name: str
        unavailable: str = ""

        async def search(self, _spec):
            await asyncio.sleep(0.2)
            return [post(self.name)]

    started = asyncio.get_running_loop().time()
    posts, _, _ = await gather([Slow("a"), Slow("b"), Slow("c")], SPEC)
    elapsed = asyncio.get_running_loop().time() - started

    assert len(posts) == 3
    assert elapsed < 0.5


async def test_a_repeated_question_is_served_from_the_cache():
    @dataclass
    class Counting:
        name: str = "hackernews"
        calls: int = 0
        unavailable: str = ""

        async def search(self, _spec):
            self.calls += 1
            return [post(self.name)]

    source = Counting()
    cache = ResultCache()

    await gather([source], SPEC, cache)
    await gather([source], SPEC, cache)

    assert source.calls == 1


async def test_a_different_question_is_not_served_from_the_cache():
    @dataclass
    class Counting:
        name: str = "hackernews"
        calls: int = 0
        unavailable: str = ""

        async def search(self, _spec):
            self.calls += 1
            return [post(self.name)]

    source = Counting()
    cache = ResultCache()

    await gather([source], SPEC, cache)
    await gather([source], QuerySpec(terms="rust", limit=5), cache)

    assert source.calls == 2


def test_empty_platform_list_skips_unconfigured_sources():
    ready = Fake("hackernews")
    off = Fake("youtube", unavailable="set YOUTUBE_API_KEY")

    assert select([ready, off], []) == [ready]


def test_naming_an_unconfigured_platform_still_selects_it():
    off = Fake("youtube", unavailable="set YOUTUBE_API_KEY")

    assert select([Fake("hackernews"), off], ["youtube"]) == [off]


def test_unknown_platform_lists_the_available_ones():
    with pytest.raises(UnknownPlatform, match="hackernews"):
        select([Fake("hackernews")], ["twitter"])
