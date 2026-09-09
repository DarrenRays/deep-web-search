import pytest

from sanitize import dedupe_by_domain, partition_safe, rejection_reason, truncate


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/admin",
        "http://127.0.0.1:8880/search",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "not-a-url",
        "https://this-host-does-not-resolve.invalid/",
    ],
)
def test_unsafe_urls_are_rejected(url):
    assert rejection_reason(url) is not None


@pytest.mark.parametrize("url", ["https://example.com/page", "http://example.com"])
def test_public_urls_pass(url):
    assert rejection_reason(url) is None


def test_partition_safe_splits_and_keeps_reasons():
    safe, rejected = partition_safe(["https://example.com", "http://127.0.0.1/"])

    assert safe == ["https://example.com"]
    assert [url for url, _ in rejected] == ["http://127.0.0.1/"]
    assert rejected[0][1]


def test_dedupe_caps_each_host_and_keeps_order():
    urls = [
        "https://a.com/1",
        "https://a.com/2",
        "https://a.com/3",
        "https://www.a.com/4",
        "https://b.com/1",
    ]

    assert dedupe_by_domain(urls, max_per_domain=2) == [
        "https://a.com/1",
        "https://a.com/2",
        "https://b.com/1",
    ]


def test_truncate_leaves_short_text_alone():
    assert truncate("short", 100) == "short"


def test_truncate_cuts_on_paragraph_boundary_and_reports_loss():
    text = "a" * 60 + "\n\n" + "b" * 60

    result = truncate(text, 80)

    assert result.startswith("a" * 60)
    assert "b" not in result
    assert "truncated" in result


def test_truncate_falls_back_to_hard_cut_without_late_boundary():
    text = "x" * 500

    result = truncate(text, 100)

    assert result.startswith("x" * 100)
    assert "400 chars omitted" in result
