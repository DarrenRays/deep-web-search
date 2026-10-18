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


ARTICLE = (
    "<html><head><title>Readable</title></head><body><article><h1>Heading</h1><p>"
    + "A sentence of real article text that trafilatura keeps. " * 20
    + "</p></article></body></html>"
)


@pytest.fixture
def public_only(monkeypatch):
    import backends

    def vet(url):
        return "resolves to a private or unroutable address" if "127.0.0.1" in url else None

    monkeypatch.setattr(backends, "rejection_reason", vet)


async def test_reader_extracts_text_and_title_with_browser_headers(public_only):
    from backends import Reader

    async def handler(request):
        assert "Chrome/" in request.headers["user-agent"]
        return httpx.Response(200, headers={"content-type": "text/html"}, text=ARTICLE)

    async with client_returning(handler) as client:
        [page] = await Reader(client).read(["https://example.com/a"])

    assert page.error is None
    assert page.title == "Readable"
    assert "real article text" in page.markdown


async def test_reader_follows_a_public_redirect(public_only):
    from backends import Reader

    async def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=ARTICLE)

    async with client_returning(handler) as client:
        [page] = await Reader(client).read(["https://example.com/old"])

    assert page.error is None
    assert page.url == "https://example.com/old"


async def test_reader_refuses_a_redirect_into_localhost(public_only):
    from backends import Reader

    seen = []

    async def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:11235/admin"})

    async with client_returning(handler) as client:
        [page] = await Reader(client).read(["https://example.com/"])

    assert "отклонён" in page.error
    assert seen == ["https://example.com/"]


async def test_reader_reports_status_and_non_html(public_only):
    from backends import Reader

    async def handler(request):
        if request.url.path == "/blocked":
            return httpx.Response(403, text="no")
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    async with client_returning(handler) as client:
        blocked, pdf = await Reader(client).read(
            ["https://example.com/blocked", "https://example.com/doc.pdf"]
        )

    assert blocked.error == "HTTP 403"
    assert pdf.error == "не HTML: application/pdf"


async def test_reader_names_a_page_without_text(public_only):
    from backends import Reader

    async def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body><div id=app></div></body></html>"
        )

    async with client_returning(handler) as client:
        [page] = await Reader(client).read(["https://example.com/spa"])

    assert page.error == "page had no text content"


async def test_reader_tries_ru_zone_direct_first_and_falls_back(public_only):
    from backends import Reader

    seen = []

    def answering(name, status):
        async def handler(request):
            seen.append((name, request.url.host))
            if status != 200:
                return httpx.Response(status, text="no")
            return httpx.Response(200, headers={"content-type": "text/html"}, text=ARTICLE)

        return client_returning(handler)

    async with answering("proxy", 200) as proxied, answering("direct", 403) as direct:
        ru, com = await Reader(proxied, direct).read(
            ["https://www.avito.ru/x", "https://example.com/x"]
        )

    assert ru.error is None and com.error is None
    # .ru: direct first, refused, then the proxy. .com: the proxy only.
    assert seen.count(("direct", "www.avito.ru")) == 1
    assert seen.count(("proxy", "www.avito.ru")) == 1
    assert ("direct", "example.com") not in seen


async def test_reader_names_both_exits_when_both_fail(public_only):
    from backends import Reader

    async def refuse(request):
        return httpx.Response(403, text="no")

    async with client_returning(refuse) as proxied, client_returning(refuse) as direct:
        [page] = await Reader(proxied, direct).read(["https://example.com/"])

    assert page.error == "HTTP 403; HTTP 403"


def test_crawl_error_keeps_the_playwright_line():
    from backends import _crawl_error

    message = (
        "Unexpected error in _crawl_web at line 779\nError: Failed on navigating ACS-GOTO:\n"
        "Page.goto: net::ERR_CONNECTION_CLOSED at https://x.example/\nCall log:\n  - navigating\n"
        "Code context:\n 774   tag='GOTO'"
    )
    assert _crawl_error(message) == "Page.goto: net::ERR_CONNECTION_CLOSED at https://x.example/"
    assert _crawl_error("Blocked by anti-bot protection: Cloudflare JS challenge") == (
        "Blocked by anti-bot protection: Cloudflare JS challenge"
    )
    assert _crawl_error("") == ""
