import json

import httpx
import pytest

from backends import Crawl4ai, Searxng

SEARX_BODY = {
    "query": "test",
    "results": [
        {
            "url": "https://example.com/a",
            "title": "Page A",
            "content": "snippet a",
            "engine": "duckduckgo",
        },
        {"url": "https://example.com/b", "title": "Page B"},
        {"title": "no url here"},
    ],
}


def client_returning(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_search_maps_fields_and_skips_entries_without_url():
    async def handler(request):
        assert request.url.params["format"] == "json"
        assert request.url.params["language"] == "en"
        return httpx.Response(200, json=SEARX_BODY)

    async with client_returning(handler) as client:
        hits = await Searxng("http://searxng:8080", client).search(
            "test", count=10, language="en"
        )

    assert [hit.url for hit in hits] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert hits[0].snippet == "snippet a"
    assert hits[1].snippet == ""


async def test_search_count_limits_results():
    async def handler(request):
        return httpx.Response(200, json=SEARX_BODY)

    async with client_returning(handler) as client:
        hits = await Searxng("http://searxng:8080", client).search("test", count=1)

    assert len(hits) == 1


async def test_search_explains_the_403_that_means_json_is_disabled():
    async def handler(request):
        return httpx.Response(403, text="Forbidden")

    async with client_returning(handler) as client:
        with pytest.raises(RuntimeError, match="search.formats"):
            await Searxng("http://searxng:8080", client).search("test", count=5)


async def test_crawl_sends_token_and_prefers_fit_markdown():
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": [
                    {
                        "url": "https://example.com/a",
                        "success": True,
                        "metadata": {"title": "Page A"},
                        "markdown": {
                            "raw_markdown": "raw noise",
                            "fit_markdown": "clean body",
                        },
                    }
                ],
            },
        )

    async with client_returning(handler) as client:
        pages = await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a"], cache_mode="enabled"
        )

    assert pages[0].markdown == "clean body"
    assert pages[0].title == "Page A"
    assert pages[0].error is None


async def test_crawl_falls_back_to_raw_markdown():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "success": True,
                        "markdown": {"raw_markdown": "raw body", "fit_markdown": ""},
                    }
                ]
            },
        )

    async with client_returning(handler) as client:
        pages = await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a"], cache_mode="enabled"
        )

    assert pages[0].markdown == "raw body"


async def test_crawl_reports_per_page_failure_without_losing_the_batch():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "success": True,
                        "markdown": {"fit_markdown": "body a"},
                    },
                    {
                        "url": "https://example.com/b",
                        "success": False,
                        "status_code": 404,
                        "error_message": "not found",
                    },
                ]
            },
        )

    async with client_returning(handler) as client:
        pages = await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a", "https://example.com/b"], cache_mode="enabled"
        )

    assert pages[0].markdown == "body a"
    assert pages[1].error == "not found"


async def test_crawl_matches_by_position_when_a_redirect_changed_the_url():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a/",
                        "success": True,
                        "markdown": {"fit_markdown": "body a"},
                    }
                ]
            },
        )

    async with client_returning(handler) as client:
        pages = await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a"], cache_mode="enabled"
        )

    assert pages[0].markdown == "body a"


async def test_crawl_marks_missing_result():
    async def handler(request):
        return httpx.Response(200, json={"results": []})

    async with client_returning(handler) as client:
        pages = await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a"], cache_mode="enabled"
        )

    assert pages[0].error == "no result returned"


async def test_crawl_sends_the_pruning_markdown_generator():
    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    async with client_returning(handler) as client:
        await Crawl4ai("http://crawl4ai:11235", "secret", client).crawl(
            ["https://example.com/a"], cache_mode="bypass"
        )

    params = captured["crawler_config"]["params"]
    assert params["cache_mode"] == "bypass"
    assert (
        params["markdown_generator"]["params"]["content_filter"]["type"]
        == "PruningContentFilter"
    )
