from backends import PageResult
from server import MAX_TOTAL_CHARS, _render


def page(url, body, title="T", error=None):
    return PageResult(url=url, title=title, markdown=body, error=error)


def test_render_numbers_pages_and_keeps_urls():
    output = _render(
        [page("https://a.com", "body a"), page("https://b.com", "body b")],
        [],
        max_chars_per_page=1000,
    )

    assert "## 1. T\nhttps://a.com" in output
    assert "## 2. T\nhttps://b.com" in output


def test_render_lists_failures_so_gaps_are_visible():
    output = _render(
        [page("https://a.com", "body a"), page("https://b.com", "", error="timeout")],
        [("http://127.0.0.1/", "resolves to a private or unroutable address")],
        max_chars_per_page=1000,
    )

    assert "## 1. T" in output
    assert "## 2." not in output
    assert "https://b.com — timeout" in output
    assert "http://127.0.0.1/ — resolves to a private" in output


def test_render_applies_the_per_page_limit():
    output = _render([page("https://a.com", "x" * 5000)], [], max_chars_per_page=100)

    assert "truncated" in output
    assert len(output) < 400


def test_render_stops_at_the_total_budget():
    pages = [page(f"https://a{i}.com", "x" * 20_000) for i in range(10)]

    output = _render(pages, [], max_chars_per_page=20_000)

    assert len(output) < MAX_TOTAL_CHARS + 2_000
    assert "total output budget exhausted" in output


def test_render_handles_nothing_at_all():
    assert _render([], [], max_chars_per_page=100) == "No results."
