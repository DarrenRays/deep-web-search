# deep-search-stack

*[Русская версия](README.ru.md)*

Self-hosted web research for Claude Code, plus the skill that drives it. No paid
APIs: one query fans out to two SearXNG instances at once, and the top links are
read in full and come back as a single merged report.

| Step | What does it | Costs |
|---|---|---|
| search | two SearXNG instances, each leaving through a different exit | nothing, runs locally |
| read | trafilatura over plain HTTP, Crawl4AI (Chromium) when that gets no text, a real Chrome for sites with bot protection | nothing, runs locally |

Results are merged by URL. A link found by both instances is read first and
labelled with both sources. CAPTCHA budgets are per IP, so two exits keep the
search answering when one IP is under a challenge wave.

```
Claude Code
   │
   └── skill deep-search ── imports lib/ ──┬──> searxng   :8880
                                           ├──> searxng-b :8881
                                           ├──> trafilatura (in process)
                                           ├──> crawl4ai  :11235 ── crawl4ai-egress (tun2socks)
                                           └──> Chrome via patchright (in process)
```

One process, no server in between: the skill imports the HTTP clients as a
library and calls the containers itself. Nothing is loaded into the model's
context until the skill is called.

## Layout

| Path | Holds |
|---|---|
| `docker-compose.yml` | the services: two SearXNG, Crawl4AI and its egress sidecar |
| `lib/` | HTTP clients, the page reader, the browser step, URL vetting, social adapters — imported by the skill |
| `tests/` | tests for `lib/`, `httpx.MockTransport`, no network |
| `searxng/settings.yml.example`, `searxng-b/settings.yml.example` | SearXNG config templates |
| `skills/deep-search/` | the skill: `SKILL.md`, `scripts/deep_search.py`, `tests/e2e.sh` |
| `scripts/install-skills.sh` | links the skill into `~/.claude/skills` |

## Setup

```bash
cp .env.example .env          # fill in CRAWL4AI_API_TOKEN: openssl rand -hex 32
cp searxng/settings.yml.example searxng/settings.yml
cp searxng-b/settings.yml.example searxng-b/settings.yml
                              # replace ultrasecretkey in both: openssl rand -hex 32
docker compose up -d          # two searxng, crawl4ai and its egress sidecar
```

Give the two SearXNG instances different exits — `outgoing.proxies` in one
`settings.yml` and not the other, or two different proxies. With the same exit
the second instance only adds load.

Crawl4AI's traffic leaves through `crawl4ai-egress`, a tun2socks sidecar. By
default it goes out directly; set `CRAWL4AI_EGRESS_PROXY` (for example
`socks5://host.docker.internal:1081`) to route the browser through a proxy
without putting the whole Docker VM behind a VPN. Crawl4AI ignores proxy
settings of its own: its Chromium is pinned to a built-in SSRF-guarding forward
proxy that dials the target itself.

Both `.env` and the real `settings.yml` files are gitignored because they hold
secrets.

Install the skill and point it at this checkout:

```bash
./scripts/install-skills.sh
uv sync                               # the venv the skill re-execs into
```

The skill finds `lib/`, `.venv` and `.env` by resolving its own path back to
this checkout, symlink included — there is no path variable to set.

That is the whole requirement: four containers, one venv, one symlink.

Optional on top of that — a Playwright MCP, for the rare page the built-in
browser step could not open:

```bash
claude mcp add playwright npx @playwright/mcp@latest
```

The built-in step needs Google Chrome, or the bundled Chromium:
`uv run python -m patchright install chromium`.
See [Blocked pages](#blocked-pages) below.

## Using the skill

```bash
# 50 links per instance, 10 read in full
~/.claude/skills/deep-search/scripts/deep_search.py "Crawl4AI vs Firecrawl"

# quick
~/.claude/skills/deep-search/scripts/deep_search.py "python 3.14 release" \
    --max-results 10 --full-pages 3

# research plus what people are posting
~/.claude/skills/deep-search/scripts/deep_search.py "Claude Code hooks" --social
```

The full flag table, the output format, the failure messages and the proxy
behaviour are in [`skills/deep-search/SKILL.md`](skills/deep-search/SKILL.md).

Output comes in two tiers: the pages read in full, then `## Ещё источники` — the
rest as one line each with title, URL, engine and snippet, so you can ask for a
specific one to be read next. Both instances down is exit code `3` with the
reasons on stderr; one instance down is reported in the failures and the other
still answers.

## Blocked pages

Pages refused by both trafilatura and Crawl4AI — a Cloudflare challenge, HTTP
403/429, an empty body — are reopened in the system Chrome, driven by
[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), up to
three per run. The window opens off-screen, not headless: ozon.ru serves a stub
to headless Chrome and the catalogue to a window. Each page is tried from two
exits, chosen by the host's zone — `.ru`-zone sites refuse foreign IPs, foreign
sites are throttled on the direct route here. avito.ru, ozon.ru and
Cloudflare-challenged pages read in full this way.

Out of reach: IP rate limits (Avito closes after a few visits in a row) and
CAPTCHAs that want a human. Those end up in `### Не удалось получить`.

When no browser could start, or with `--no-browser`, the pages go into their own
section instead, for Claude to finish with a Playwright MCP:

```
### Заблокировано, добрать браузером (2)
- https://example.com/a — Blocked by anti-bot protection: Cloudflare JS challenge
- https://example.com/b — page had no text content
```

`skills/deep-search/SKILL.md` spells out that procedure and its limits.
Addresses that failed the SSRF check or did not resolve are never offered to
any browser, and the built-in one vets every navigation, redirects included.
With `--json`, the list comes back as `blocked[]`.

## Calling it from Claude Code

Three ways in, same script underneath:

| Way | What it does |
|---|---|
| `/deep-search python 3.14 release notes` | explicit call, the query is everything after the command |
| ask normally — "find out what people say about X" | the skill's description matches and Claude runs it on its own |
| the `bash` line above | no Claude at all; the report goes to stdout |

Flags can be named in plain words, and the skill maps them itself: "quick look"
becomes `--max-results 10 --full-pages 3`, "dig up everything"
becomes `--max-results 80 --full-pages 20`, "only this week" becomes
`--time-range week`, "what's on social media" adds `--social`.

## Answer language

Two independent settings — what gets searched, and what you get written back.

**What is searched** is `--language`, passed to SearXNG:

```bash
# the Russian-language web only
~/.claude/skills/deep-search/scripts/deep_search.py "тарифы на электричество" --language ru

# the English-language web only
~/.claude/skills/deep-search/scripts/deep_search.py "electricity tariffs" --language en
```

The default is `all` — both, mixed. Narrow it when the answer depends on
regional sources: prices, laws, local services.

**What is written back** is decided in the request, not by a flag. Claude
answers in the language you asked in; to override, say so in the prompt:

```
/deep-search Crawl4AI vs Firecrawl — ответь по-русски
/deep-search тарифы на электричество в Москве --language ru, answer in English
```

The report skeleton the script prints — the header, `## Ещё источники`,
`### Не удалось получить` — is Russian regardless. Page text arrives in whatever
language the page is written in; Claude translates as needed when it answers.

## Environment

| Variable | For |
|---|---|
| `CRAWL4AI_API_TOKEN` | Crawl4AI demands it on every endpoint; without it only the trafilatura reader runs |
| `SEARXNG_PORT`, `SEARXNG_B_PORT`, `CRAWL4AI_PORT` | host ports, bound to `127.0.0.1` only |
| `CRAWL4AI_EGRESS_PROXY` | where Crawl4AI's traffic leaves, a tun2socks proxy URL; default `direct://` |
| `DEEP_SEARCH_DIR` | path to the checkout; only needed if the skill was copied out of it instead of symlinked |
| `DEEP_SEARCH_PROXY` | proxy for page reads and social sources, default `http://127.0.0.1:8118`, empty = direct |
| `DEEP_SEARCH_SEARXNG_URL`, `DEEP_SEARCH_SEARXNG_B_URL`, `DEEP_SEARCH_CRAWL4AI_URL` | full URLs when the services are not on the default local ports |
| `SOCIAL_*`, `BLUESKY_*`, `YOUTUBE_API_KEY` | optional social sources, see `.env.example` |

The proxy default assumes something listening on `127.0.0.1:8118` (Privoxy or
similar). If nothing is there, the skill says so in the header and goes direct;
`--no-proxy` or `DEEP_SEARCH_PROXY=` turns it off for good.

## Social search

`--social` adds one block over several platforms at once. It fans out
concurrently, normalises every answer into the same post shape, collapses links
that appear on more than one platform, and ranks the result. Because raw scores
are not comparable — HN points run into the thousands, Mastodon favourites into
the tens — each post is ranked against the others *from its own source*, and
that percentile is combined with freshness.

| Source | How it reads | Needs |
|---|---|---|
| `hackernews` | Algolia index, real keyword search | nothing |
| `mastodon` | public hashtag timelines | nothing (instances configurable) |
| `web` | SearXNG restricted to social hosts | the SearXNG service |
| `reddit` | Arctic Shift archive, listing plus local keyword filter | `SOCIAL_REDDIT_SUBREDDITS` |
| `telegram` | `t.me/s/<channel>` public previews | `SOCIAL_TELEGRAM_CHANNELS` |
| `rss` | RSS/Atom feeds, including YouTube channel feeds | `SOCIAL_RSS_FEEDS` |
| `bluesky` | AT Protocol `searchPosts` | handle + app password |
| `youtube` | Data API v3 | API key |

Results are cached in memory for 15 minutes per source and query, so re-asking
within a session costs no requests.

A source that is not configured is left out rather than reported as broken. A
source that *fails* is always named at the end of the block, never silently
dropped, because an outage reported as "nothing found" is the worst way for a
research tool to be wrong.

### What is not covered, and why

- **X/Twitter has no free read path.** The free tier ended in February 2026 and
  the $200/mo Basic plan was retired on 1 June 2026; reads are now pay-per-use
  at roughly $0.005 per post. The `web` source picks up whatever public search
  engines have indexed from `x.com`, and that is the whole of the free coverage.
- **Reddit closed self-service API access** in November 2025 — new keys need
  manual approval, and anonymous `.json` endpoints answer 403. The Arctic Shift
  archive fills the gap, but it rate-limits hard, answering HTTP 422 with
  `Timeout. Maybe slow down a bit` in the body. Subreddits are therefore walked
  one at a time with a gap, and a rejection is reported rather than hidden. For
  keyword discovery across all of Reddit, the `web` source is the reliable path.
- **Mastodon has no cross-instance full-text search**, so the first keyword of
  the query is used as a hashtag.
- **Telegram and Reddit cannot be searched by keyword** through these free
  routes at all: both adapters list what you configured and filter locally.

## Safety

`lib/sanitize.py` resolves every URL and refuses private, loopback, link-local
and reserved addresses. The URLs come from search engines and, indirectly, from
a model's phrasing of a query, so without that check the crawler could be
pointed at the Docker host and at cloud metadata endpoints. Every hit goes
through it before either reader sees the URL; a rejected one is reported with
its reason. The trafilatura reader follows redirects by hand and vets every hop
the same way, so a public page cannot bounce the request to `127.0.0.1`.

## Development

```bash
uv sync
uv run pytest -q                      # library modules, no network

DEEP_SEARCH_SCRIPT="$PWD/skills/deep-search/scripts/deep_search.py" \
    bash skills/deep-search/tests/e2e.sh
```

The e2e suite covers each instance alone and both together, dedup, the social
block, one instance down, exit code 3, the output budget, JSON, `--time-range`,
the venv re-exec, the SSRF veto, the two readers, the browser step against live
Cloudflare and Avito pages, `--no-browser`, proxy default, dead-proxy
fallback and `--no-proxy`. Do not run it back to back — a burst of queries makes
SearXNG's engines serve CAPTCHAs.

Unit tests use `httpx.MockTransport` and never touch the network.

## Notes

- SearXNG's JSON API is off by default; `search.formats` must list `json`, and
  `server.limiter` must be `false` because JSON clients send no browser headers.
- The SearXNG entrypoint chowns its config path, so compose mounts the
  `./searxng` directory rather than the single file.
- `crawl4ai` needs `shm_size: 1g`; Chromium crashes on heavy pages with the
  64MB default.
- Adding a social platform is one class in `lib/sources.py` with `name`,
  `unavailable` and `async search(spec)`, plus one entry in `build_sources`.
  Nothing in the aggregator or the skill changes.

## License

MIT — see [LICENSE](LICENSE).
