"""Last-resort page reader: a real Chrome driven by patchright.

Sites that refuse both trafilatura and Crawl4AI mostly judge two things: the
browser's fingerprint and the exit IP. patchright strips the automation markers
Playwright leaves in Chromium, the system Chrome brings a real fingerprint, and
the window opens off-screen instead of headless, because headless mode is a
signal of its own: ozon.ru served a stub to headless Chrome and the catalogue to
a window. IP reputation is out of reach — a site that rate-limits the exit IP
(avito.ru after a few visits in a row) stays closed until the limit lifts.

Each URL is tried from two exits. Domestic sites turn foreign and datacenter
addresses away, foreign ones are throttled on the direct route, so the zone of
the host decides which exit goes first.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import trafilatura

from backends import PageResult, prefers_direct
from sanitize import rejection_reason

PROFILE_ROOT = Path("~/.cache/deep-search-browser").expanduser()
NAV_TIMEOUT_MS = 45_000
# Challenges that pass resolve themselves within a few seconds of the load.
SETTLE_MS = 6_000
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "enable javascript",
    # Cloudflare localises its page after the browser's language.
    "один момент",
    "проверяем, человек ли вы",
    "доступ ограничен",
    "captcha",
)
# A challenge page is short; a long page that merely mentions a captcha is not one.
CHALLENGE_MAX_CHARS = 3_000
OFFSCREEN = ["--window-position=-32000,-32000"]


class BrowserUnavailable(RuntimeError):
    """No browser could be started at all, as opposed to one page failing."""


def exits(url: str, proxy: str) -> list[str]:
    """Exits to try for this URL, in order; an empty string means direct."""
    if not proxy:
        return [""]
    return ["", proxy] if prefers_direct(url) else [proxy, ""]


def looks_blocked(text: str) -> bool:
    lowered = text.lower()
    return len(text) < CHALLENGE_MAX_CHARS and any(
        marker in lowered for marker in CHALLENGE_MARKERS
    )


async def read(urls: list[str], proxy: str) -> list[PageResult]:
    """Read each URL in a real browser, trying its exits in turn.

    Raises BrowserUnavailable when patchright or every browser is missing, so the
    caller can tell "no browser here" from "the site refused the browser".
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("patchright не установлен (uv sync)") from exc

    async with async_playwright() as playwright:
        contexts: dict = {}
        try:
            results = []
            for url in urls:
                errors = []
                for exit_ in exits(url, proxy):
                    if exit_ not in contexts:
                        contexts[exit_] = await _open(playwright, exit_)
                    page = await _read_one(contexts[exit_], url)
                    if not page.error:
                        results.append(page)
                        break
                    errors.append(f"{'прокси' if exit_ else 'напрямую'}: {page.error}")
                else:
                    results.append(
                        PageResult(url=url, title="", markdown="", error="; ".join(errors))
                    )
            return results
        finally:
            for context in contexts.values():
                await context.close()


async def _open(playwright, exit_: str):
    from patchright.async_api import Error

    # One persistent profile per exit: a cleared challenge leaves a cookie that
    # is tied to the IP that cleared it.
    profile = PROFILE_ROOT / ("proxy" if exit_ else "direct")
    options = {
        "user_data_dir": str(profile),
        "headless": False,
        "no_viewport": True,
        "args": OFFSCREEN,
        "proxy": {"server": exit_, "bypass": "127.0.0.1,localhost"} if exit_ else None,
    }
    try:
        context = await playwright.chromium.launch_persistent_context(channel="chrome", **options)
    except Error:
        # No system Chrome: the bundled Chromium (`patchright install chromium`)
        # has a weaker fingerprint but still beats no browser.
        try:
            context = await playwright.chromium.launch_persistent_context(**options)
        except Error as exc:
            raise BrowserUnavailable(
                f"ни Chrome, ни Chromium не запустились: {type(exc).__name__}"
            ) from exc
    await context.route("**/*", _guard)
    return context


async def _guard(route) -> None:
    """Keep navigations off private addresses, redirects included.

    The URL handed in has passed the SSRF check, but a redirect is followed by
    Chromium itself, and localhost is exempt from the proxy.
    """
    request = route.request
    if request.is_navigation_request():
        reason = await asyncio.to_thread(rejection_reason, request.url)
        if reason:
            await route.abort("blockedbyclient")
            return
    await route.continue_()


async def _read_one(context, url: str) -> PageResult:
    from patchright.async_api import Error

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(SETTLE_MS)
        visible = await _visible(page)
        if looks_blocked(visible):
            # A JavaScript challenge that is going to pass needs one more wait.
            await page.wait_for_timeout(SETTLE_MS)
            visible = await _visible(page)
            if looks_blocked(visible):
                return PageResult(url=url, title="", markdown="", error="страница-заглушка антибота")
        html = await page.content()
        extracted = await asyncio.to_thread(
            trafilatura.extract, html, url=page.url, output_format="markdown"
        )
        extracted = (extracted or "").strip()
        # trafilatura looks for an article; on a catalogue or a listing it keeps a
        # fraction of what is on screen, and the screen is the better text then.
        text = extracted if len(extracted) * 4 >= len(visible) else visible.strip()
        if not text:
            return PageResult(url=url, title="", markdown="", error="page had no text content")
        return PageResult(url=url, title=await page.title(), markdown=text)
    except Error as exc:
        return PageResult(url=url, title="", markdown="", error=_short(exc))
    finally:
        await page.close()


async def _visible(page) -> str:
    # The title counts too: some challenge pages say who they are only there.
    return await page.evaluate(
        "() => document.title + '\\n' + (document.body ? document.body.innerText : '')"
    )


def _short(error: BaseException) -> str:
    # Playwright errors carry a multi-line call log; the first line has the cause.
    return str(error).splitlines()[0][:200] or type(error).__name__
