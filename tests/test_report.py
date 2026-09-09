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
    assert "Заблокировано" not in report([Failure("tavily+jina", "no keys")])
