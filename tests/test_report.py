from deep_search import (
    BROWSER_RETRY_MAX,
    Entry,
    Failure,
    browser_can_retry,
    browser_candidates,
    render,
)


def cut(text: str, limit: int) -> str:
    return text if limit <= 0 or len(text) <= limit else text[:limit]


def report(failures: list[Failure], entries: list[Entry] | None = None) -> str:
    return render(
        "q",
        entries or [Entry(url="https://ok.example/", title="ok", text="body")],
        failures,
        [],
        max_chars_per_page=100,
        total_budget=1000,
        truncate=cut,
        proxy_note="выключен",
        engines_note="",
        snippet_chars=50,
    )


def test_crawler_blocks_are_retryable():
    assert browser_can_retry("Blocked by anti-bot protection: Cloudflare JS challenge")
    assert browser_can_retry("HTTP 403")
    assert browser_can_retry("page had no text content")


def test_broken_addresses_are_not_retryable():
    assert not browser_can_retry("resolves to a private or unroutable address")
    assert not browser_can_retry("домен не резолвится, отброшено до краула")
    assert not browser_can_retry("CRAWL4AI_API_TOKEN не задан ни в окружении, ни в <repo>/.env")
    assert not browser_can_retry("no result returned")


def test_candidates_are_capped():
    failures = [
        Failure(f"https://site{index}.example/", "HTTP 403", retryable=True)
        for index in range(BROWSER_RETRY_MAX + 2)
    ]
    picked = browser_candidates(failures)
    assert [f.source for f in picked] == [f.source for f in failures[:BROWSER_RETRY_MAX]]


def test_report_splits_blocked_from_the_rest():
    failures = [
        Failure(f"https://site{index}.example/", "HTTP 403", retryable=True)
        for index in range(BROWSER_RETRY_MAX + 2)
    ]
    text = report(failures)

    blocked_block, _, failed_block = text.partition("### Не удалось получить")
    assert f"### Заблокировано, добрать браузером ({BROWSER_RETRY_MAX})" in blocked_block
    for failure in failures[:BROWSER_RETRY_MAX]:
        assert failure.source in blocked_block
    # The overflow is still reported, just without a browser invitation.
    for failure in failures[BROWSER_RETRY_MAX:]:
        assert failure.source in failed_block
        assert failure.source not in blocked_block


def test_vetoed_addresses_never_reach_the_browser_block():
    failures = [
        Failure("http://10.0.0.1/secret", "resolves to a private or unroutable address"),
        Failure("http://169.254.169.254/latest/meta-data", "resolves to a private or unroutable address"),
    ]
    text = report(failures)

    assert "Заблокировано, добрать браузером" not in text
    assert "10.0.0.1" in text.split("### Не удалось получить")[1]


def test_no_blocked_section_without_candidates():
    assert "Заблокировано" not in report([Failure("searxng-b", "ConnectError")])


class FakeReader:
    def __init__(self, errors):
        self.errors = errors

    async def read(self, urls):
        from backends import PageResult

        return [
            PageResult(url, "", "", error=self.errors[url])
            if url in self.errors
            else PageResult(url, "t", f"text of {url}")
            for url in urls
        ]


class FakeCrawler:
    def __init__(self, errors):
        self.errors = errors
        self.asked = []

    async def crawl(self, urls, *, cache_mode):
        from backends import PageResult

        self.asked.extend(urls)
        return [
            PageResult(url, "", "", error=self.errors[url])
            if url in self.errors
            else PageResult(url, "crawled", f"rendered {url}")
            for url in urls
        ]


def entries(*urls):
    return [Entry(url=url, title=url, text="") for url in urls]


async def test_only_what_trafilatura_missed_goes_to_crawl4ai():
    from deep_search import _read_batch

    batch = entries("https://a.example/", "https://spa.example/")
    crawler = FakeCrawler({})
    failures = await _read_batch(
        FakeReader({"https://spa.example/": "page had no text content"}),
        crawler, "", batch, "enabled",
    )

    assert failures == []
    assert crawler.asked == ["https://spa.example/"]
    assert batch[0].text == "text of https://a.example/"
    assert batch[1].text == "rendered https://spa.example/"
    assert batch[1].title == "crawled"


async def test_a_page_both_readers_miss_names_both_reasons():
    from deep_search import _read_batch

    url = "https://cf.example/"
    failures = await _read_batch(
        FakeReader({url: "HTTP 403"}),
        FakeCrawler({url: "Blocked by anti-bot protection: Cloudflare JS challenge"}),
        "", entries(url), "enabled",
    )

    [failure] = failures
    assert failure.source == url
    assert "Cloudflare" in failure.reason and "trafilatura: HTTP 403" in failure.reason
    assert failure.retryable


async def test_missing_crawler_is_named_per_page():
    from deep_search import _read_batch

    url = "https://spa.example/"
    failures = await _read_batch(
        FakeReader({url: "page had no text content"}),
        None, "CRAWL4AI_API_TOKEN не задан", entries(url), "enabled",
    )

    assert failures[0].reason.startswith("CRAWL4AI_API_TOKEN не задан")


async def test_browser_reads_what_both_readers_were_refused(monkeypatch):
    import deep_search
    from backends import PageResult

    asked = {}

    class FakeBrowser:
        class BrowserUnavailable(RuntimeError):
            pass

        @staticmethod
        async def read(urls, proxy):
            asked["urls"] = urls
            return [
                PageResult(urls[0], "From browser", "browser text"),
                PageResult(urls[1], "", "", error="страница-заглушка антибота"),
            ]

    monkeypatch.setattr(deep_search, "_stack_modules", lambda *names: [FakeBrowser])
    wanted = entries("https://a.example/", "https://b.example/", "https://ssrf.example/")
    failures = [
        Failure("https://a.example/", "HTTP 403", retryable=True),
        Failure("https://b.example/", "Cloudflare", retryable=True),
        Failure("https://ssrf.example/", "resolves to a private or unroutable address"),
    ]

    left = await deep_search._browser_pass(wanted, failures)

    assert asked["urls"] == ["https://a.example/", "https://b.example/"]
    assert wanted[0].text == "browser text" and wanted[0].engine == "браузер"
    assert [f.source for f in left] == ["https://b.example/", "https://ssrf.example/"]
    assert not left[0].retryable and "браузер — страница-заглушка" in left[0].reason


async def test_no_browser_keeps_the_candidates_for_the_model(monkeypatch):
    import deep_search

    class NoBrowser:
        class BrowserUnavailable(RuntimeError):
            pass

        @staticmethod
        async def read(urls, proxy):
            raise NoBrowser.BrowserUnavailable("patchright не установлен (uv sync)")

    monkeypatch.setattr(deep_search, "_stack_modules", lambda *names: [NoBrowser])
    failures = [Failure("https://a.example/", "HTTP 403", retryable=True)]

    left = await deep_search._browser_pass(entries("https://a.example/"), failures)

    assert left[0].retryable
    assert left[-1].source == "браузер" and "patchright" in left[-1].reason


async def test_a_proxy_error_still_earns_a_browser_try():
    from deep_search import _read_batch

    url = "https://www.avito.ru/x"
    [failure] = await _read_batch(
        FakeReader({url: "ProxyError: 503 Service Unavailable"}),
        None, "CRAWL4AI_API_TOKEN не задан", entries(url), "enabled",
    )

    assert failure.retryable


async def test_a_non_html_file_is_not_sent_to_the_browser():
    from deep_search import _read_batch

    url = "https://example.com/doc.pdf"
    [failure] = await _read_batch(
        FakeReader({url: "не HTML: application/pdf"}),
        FakeCrawler({url: "HTTP 200 but not html"}), "", entries(url), "enabled",
    )

    assert not failure.retryable
