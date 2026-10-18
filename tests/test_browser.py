from browser import exits, looks_blocked

PROXY = "http://127.0.0.1:8118"


def test_domestic_hosts_go_direct_first():
    assert exits("https://www.avito.ru/moskva", PROXY) == ["", PROXY]
    assert exits("https://xn--80aswg.xn--p1ai/", PROXY) == ["", PROXY]


def test_foreign_hosts_go_through_the_proxy_first():
    assert exits("https://steamdb.info/app/1/", PROXY) == [PROXY, ""]
    # A suffix match on the zone, not a substring: .run is not .ru.
    assert exits("https://example.run/", PROXY) == [PROXY, ""]


def test_without_a_proxy_there_is_one_exit():
    assert exits("https://www.avito.ru/", "") == [""]


def test_challenge_pages_are_recognised():
    assert looks_blocked("Just a moment...\nChecking your browser before accessing")
    assert looks_blocked("Доступ ограничен: проблема с IP")


def test_a_long_page_that_mentions_a_captcha_is_not_a_challenge():
    assert not looks_blocked("How to solve a captcha in tests. " * 200)
