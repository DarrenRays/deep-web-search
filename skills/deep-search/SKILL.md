---
name: deep-search
description: Full web research on a fully self-hosted, open-source stack — two SearXNG instances on different exits queried in parallel, deduplicated by URL, pages read with trafilatura, Crawl4AI and, for sites that block bots (Avito, Ozon, Cloudflare), a real Chrome — reported as one document. Use it when coverage and cross-checking matter: comparing sources, a disputed or fresh fact, changelogs and release notes, market overviews, "collect everything there is on X", plus an optional social media block. This is the only web search route. By default it takes 50 links from each instance and reads 10 pages in full. For a quick question use `--max-results 10 --full-pages 3`.
allowed-tools:
  - Bash(~/.claude/skills/deep-search/scripts/deep_search.py *)
  - mcp__playwright__browser_navigate
  - mcp__playwright__browser_evaluate
  - mcp__playwright__browser_close
---

# Deep Search Skill

No paid APIs, no keys beyond the local Crawl4AI token. One query goes to two
SearXNG instances at once; the links are merged by URL, and the top ones are read
in full:

| Step | What does it | Where it leaves the machine |
|---|---|---|
| search `searxng` | SearXNG `127.0.0.1:8880` | its own `outgoing.proxies` (gost here) |
| search `searxng-b` | SearXNG `127.0.0.1:8881` | a different exit (direct here) |
| read, first try | trafilatura over a plain GET, browser-like headers | `DEEP_SEARCH_PROXY` |
| read, second try | Crawl4AI `127.0.0.1:11235` (Chromium), for JS pages and what trafilatura missed | `crawl4ai-egress` tun2socks sidecar |
| read, third try | the system Chrome driven by patchright, for pages both readers were refused | direct for `.ru`-zone hosts, the proxy for the rest, then the other one |
| read, last resort | Playwright MCP, by the model — see below | wherever that MCP is configured |

Two instances on two exits matter because CAPTCHA budgets are per IP: a wave on
one IP leaves the other instance answering. A link both instances found is the
"confirmed twice" signal — weaker than two independent indexes, since both scrape
the same upstream engines, but it still ranks those links first.

The skill imports the stack's modules (`backends.py`, `sanitize.py`,
`sources.py`, `aggregate.py`, `social.py`) from the checkout's `lib/` and
calls the services itself. Nothing is loaded into context until the skill is
called.

## When to use it

- one question needs several search engines (cross-checking);
- the fact is disputed and sources contradict each other;
- fresh news, changelogs, release notes, GitHub issues;
- "collect everything there is on X" — an overview, a comparison, a market;
- a social media slice is wanted too (`--social`).

## When not to

- the exact URL is known → `fetch_url` or `curl`;
- a question about a library's API or config → the `find-docs` skill;
- the internals of a GitHub repository → the `deepwiki` skill.

## Command

```bash
~/.claude/skills/deep-search/scripts/deep_search.py "query" [flags]
```

| Flag | Default | What it does |
|---|---|---|
| `--max-results N` | 50 | links taken **from each** SearXNG instance |
| `--full-pages N` | 10 | how many sources to read in full; the rest stay links with a snippet |
| `--engines` | `auto` | `auto` — every general SearXNG engine (57 right now, mwmbl and yacy included); `builtin` — only the ones enabled by default; or a comma-separated list |
| `--search-timeout N` | 40 | SearXNG request timeout; a wide pool needs more than 15 s |
| `--max-chars-per-page N` | 4000 | trim one page's text |
| `--snippet-chars N` | 200 | trim the snippet of a second-tier link |
| `--total-budget N` | 60000 | total output character budget |
| `--language LANG` | `all` | SearXNG search language; `ru` narrows the results to the Russian-language web |
| `--time-range` | `` | `day` \| `week` \| `month` \| `year` |
| `--only searxng\|searxng-b` | both | query one instance only |
| `--fresh` | off | `cache_mode=bypass` on Crawl4AI |
| `--no-browser` | off | do not reopen blocked pages in Chrome; they go straight to the browser block |
| `--social` | off | add a social block across 8 platforms |
| `--social-limit N` | 20 | posts in the social block |
| `--social-per-platform N` | 8 | how many to request from each platform |
| `--social-chars N` | 600 | trim one post's text |
| `--json` | off | raw JSON instead of markdown |
| `--no-proxy` | off | do not use the proxy for this run |

## Examples

```bash
# ordinary research: 50 links per instance, 10 read in full
~/.claude/skills/deep-search/scripts/deep_search.py "Crawl4AI vs Firecrawl"

# quick look
~/.claude/skills/deep-search/scripts/deep_search.py "python 3.14 release date" \
  --max-results 10 --full-pages 3

# fresh news from the past week
~/.claude/skills/deep-search/scripts/deep_search.py "python 3.14 release" --time-range week

# deep dive: more full pages and more budget
~/.claude/skills/deep-search/scripts/deep_search.py "Oppo Find X9 Pro vs Samsung S25 Ultra" \
  --max-results 80 --full-pages 20 --total-budget 120000

# research plus what people are posting
~/.claude/skills/deep-search/scripts/deep_search.py "Claude Code hooks" --social --full-pages 5
```

## Output

The header reads `Источники — searxng: N, searxng-b: M, совпадений: K,
прочитано полностью: F`, then one `движки <instance>: запрошено 57, с
результатами 12, без ответа 20` line per instance, then a `прокси:` line. The
report labels are Russian; the page text is in whatever language the page is
written in.

Then two tiers:

1. **Full pages** (`--full-pages`, 10 by default) — title, URL, source and
   engine labels, text. Links found by both instances are read first and carry
   both labels.
2. **`## Ещё источники (N)`** — the remaining links, one line each: title, URL,
   engine, the engine's snippet. This is the map of what else exists, for
   reading a specific one next.

A page is read with trafilatura first; if that gets no text (a JavaScript app,
a 403, a PDF), Crawl4AI renders it. If neither opens it, the next link takes its
place — one refill, so the tier does not thin out. Up to three pages that were
refused by both (a Cloudflare challenge, HTTP 403/429, an empty body) are then
reopened in a real Chrome; a page read that way carries `браузер` in its engine
label.

Two more sections can follow: `### Заблокировано, добрать браузером` and
`### Не удалось получить`. With `--social`, the social block is last.

## Sites that block bots (Avito, Ozon, Cloudflare)

The script opens refused pages itself in the system Chrome, driven by
patchright — a Playwright fork with the automation markers removed. The window
is placed off-screen rather than run headless, since headless mode is a signal
of its own; a Chrome icon may flash in the Dock. Each page is tried from two
exits: domestic zones (`.ru`, `.su`, `.рф`, `.by`, `.kz`) direct first, because
those sites turn foreign and datacenter IPs away; everything else through the
proxy first, because the direct route throttles foreign hosts. Then the other
exit. One profile per exit lives in `~/.cache/deep-search-browser/`, so a
challenge cleared once stays cleared for that IP.

Measured: avito.ru, ozon.ru and Cloudflare-challenged pages (slashdot) read in
full. What it cannot get past:

- **IP rate limits.** Avito answers `Доступ ограничен: проблема с IP` after a
  few visits in a row from the same IP; wait a while instead of retrying.
- **CAPTCHAs that want a human.** There is no solver. The page ends up in
  `### Не удалось получить` with `страница-заглушка антибота`.

A page the browser could not open either goes to `### Не удалось получить` with
all three reasons. `### Заблокировано, добрать браузером` appears only when the
browser step did not run — `--no-browser`, or no Chrome and no Chromium on the
machine (`uv run python -m patchright install chromium` adds the bundled one).

## Blocked pages: finish them with the Playwright MCP

When `### Заблокировано, добрать браузером (N)` is in the report, the built-in
browser did not run. If a Playwright MCP is configured, read those pages before
answering:

1. `mcp__playwright__browser_navigate` with the URL;
2. `mcp__playwright__browser_evaluate` with
   `() => document.body.innerText`, then keep at most `--max-chars-per-page`
   characters (4000 by default);
3. after the last one, `mcp__playwright__browser_close`.

No Playwright MCP and the page matters → suggest adding one, for the user to
run:

```bash
claude mcp add playwright -- npx @playwright/mcp@latest
```

Without `--proxy-server` it goes out directly, which is what `.ru`-zone sites
need; with `--proxy-server=http://127.0.0.1:8118` it suits foreign sites under
DPI and fails on Avito (`ERR_TUNNEL_CONNECTION_FAILED`). The Claude in Chrome
extension, when installed, is the user's own browser with their cookies — the
strongest option for a site that knows the user, and the one to suggest when
everything else was refused.

Rules:

- take URLs from this block only. Addresses under `### Не удалось получить` were
  vetoed by the SSRF check, do not resolve, or were already refused by a real
  browser — opening those again is either the request the veto exists to prevent
  or a wasted attempt;
- if the text is still the challenge itself (`Just a moment`, `Один момент`,
  `Verify you are human`), stop at one attempt and say in the answer that the
  site is closed to the browser as well;
- skip the whole step when the user asked for something quick ("in two words")
  or when the first tier already answers the question;
- say which sources were read with the browser, the same way you cite the rest;
- the browser runs as the user, with their IP and their cookies: navigate and
  read, never log in, submit or buy anything.

## Choosing flags from how the request is phrased

| What is asked | How to run it |
|---|---|
| "find", "what are people writing", ordinary research | no flags — 50 links per instance, 10 full |
| "quick look", "in two words" | `--max-results 10 --full-pages 3` |
| "dig up everything", "as complete as possible" | `--max-results 80 --full-pages 20 --total-budget 120000` |
| "only fresh", "from the past week" | `--time-range week` |
| "what's on social media", "what are people saying" | `--social` |
| "search Google/Brave" | `--engines google,brave` |
| "the Russian-language web only" | `--language ru` |
| a specific site is needed | `--engines auto`, then read the link tier and `fetch_url`/`curl` the one you want |

Do not construct URLs by hand. Take links from the report — guessed GSMArena and
DXOMARK ids and slugs lead to a different device.

## Answer language

The flags decide what is searched, not what you write back.

- `--language ru` narrows SearXNG to the Russian-language web, `--language en`
  to the English one, `all` (the default) mixes them. Reach for it when the
  answer depends on regional sources: prices, laws, local services.
- Reply in the language the user asked in, unless they asked for another one
  ("answer in English", "ответь по-русски"). Titles and quotes stay in the
  original, with a translation beside them where it matters.
- The two settings are independent: a Russian question about an English-only
  topic is `--language all` (or `en`) plus a Russian answer.

## When a source fails

A failed instance is named in the `### Не удалось получить` block, the other one
keeps working, and the exit code stays `0`. An empty list is never printed as
"nothing found". Decoding the messages:

| Message | What to do |
|---|---|
| `searxng-b — ConnectError` | the second instance is down: `docker compose up -d searxng-b`; the first keeps answering |
| `<instance> — SearXNG вернул 0 результатов; движки: …` | that instance's IP is under a CAPTCHA wave; the other usually still answers. Both zero — wait, or `--engines builtin` |
| `deep-search: нет каталога …/lib` | the skill was copied out of its checkout; set `DEEP_SEARCH_DIR` |
| `deep-search: httpx недоступен` | run `uv sync` in the stack directory |
| `CRAWL4AI_API_TOKEN не задан …` on a page | trafilatura still reads; only the browser fallback is off. Add the token to the environment or to `<repo>/.env` |
| `Crawl4AI недоступен: ConnectError` on a page | `docker compose up -d crawl4ai-egress crawl4ai` |
| `N ссыл(ок) от мусорных движков — домен не резолвится` | normal for a wide pool: some engines return link farms, and those are dropped before reading |
| `Blocked by anti-bot protection: Cloudflare JS challenge (trafilatura: HTTP 403); браузер — …` | closed to all three readers; see "Sites that block bots" for what is out of reach |
| `браузер — ни Chrome, ни Chromium не запустились` | install Google Chrome, or `uv run python -m patchright install chromium` in the stack directory |
| `net::ERR_TIMED_OUT` from Crawl4AI | the egress sidecar is not routing: check `docker logs crawl4ai-egress` and the proxy it points at |
| `прокси: … недоступен, работаю напрямую` | the proxy container is down; bring it up or live with it — page reads go direct, mastodon and reddit will end up in the failures |

Both instances down → exit code `3`, reasons on stderr.

## Proxy

Page reads and the social sources go through `http://127.0.0.1:8118` by default;
localhost never does. The skill rewrites the proxy variables of its own process
(`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY=127.0.0.1,localhost`). The machine's own
`NO_PROXY` is overwritten in the process: httpx chokes on IPv6 entries such as
`::1` (`Invalid port: ':'`).

The proxy is TCP-probed before the run. A dead proxy does not take out every
external source — the work continues directly and the report says
`прокси: … недоступен, работаю напрямую`. The `прокси:` line is always printed.

Turn it off with `--no-proxy` for one run, or `DEEP_SEARCH_PROXY=` in the
environment. Change the address with `DEEP_SEARCH_PROXY=http://host:port`. The
SOCKS5 port `1081` of the same gost container is not supported here — httpx
would need the `socksio` package, and both ports have the same exit IP anyway.

**SearXNG engines** leave through each instance's own `outgoing.proxies`, not
through this variable. Give the two instances different exits.

**Crawl4AI** cannot take an upstream proxy: Chromium is pinned to the server's
own SSRF-guarding forward proxy (`egress_proxy.py`), which dials the target
itself. So the container shares the network namespace of `crawl4ai-egress`, a
tun2socks sidecar whose `PROXY` is `CRAWL4AI_EGRESS_PROXY` — every connection
Crawl4AI opens goes through it. DNS stays on Docker's embedded resolver, which
does not cross the tunnel.

## Checking after changes

```bash
uv run pytest -q                              # unit tests, no network
bash ~/.claude/skills/deep-search/tests/e2e.sh   # end-to-end, stubbed and live
```

The e2e suite covers each instance alone and both together, dedup, the social
block, one instance down, exit code 3, the output budget, JSON, `--time-range`,
the re-exec into the venv from the system python3, the SSRF veto on private
addresses, the trafilatura reader and the Crawl4AI fallback, the proxy default,
the fallback on a dead proxy, `--no-proxy`, the wide engine pool and the tiered
output. Do not run it back to back: a burst of queries makes the SearXNG engines
serve CAPTCHAs.

## Environment variables

| Variable | For |
|---|---|
| `DEEP_SEARCH_PROXY` | proxy for page reads and social sources, `http://127.0.0.1:8118` by default; an empty value means direct |
| `CRAWL4AI_API_TOKEN` | the Crawl4AI fallback; falls back to `<repo>/.env` |
| `DEEP_SEARCH_DIR` | path to the checkout; only needed when the skill was copied rather than symlinked out of it |
| `SEARXNG_PORT`, `SEARXNG_B_PORT`, `CRAWL4AI_PORT` | localhost ports, `8880` / `8881` / `11235` by default |
| `DEEP_SEARCH_SEARXNG_URL`, `DEEP_SEARCH_SEARXNG_B_URL`, `DEEP_SEARCH_CRAWL4AI_URL` | full address override |

## Security

- never print token values into the chat or pass them on the command line — the
  script reads them from the environment itself;
- URLs from search results pass through `sanitize.partition_safe` (an SSRF veto
  on private, loopback and link-local addresses) before either reader sees them;
  the trafilatura reader follows redirects by hand and vets every hop the same
  way. A rejected URL lands in the failures block with its reason;
- the readers send a desktop Chrome header set and the browser step uses a real
  Chrome. No CAPTCHA solving and no way around an IP rate limit: a site that
  still refuses lands in the failures, named as such;
- the browser step vets every navigation, redirects included, with the same SSRF
  check, and blocks the ones into private addresses.
