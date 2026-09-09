# deep-search-stack

Self-hosted web research for Claude Code, plus the skill that drives it. One
query fans out to two independent stacks at once and comes back as a single
merged report.

| Branch | How it searches | Costs |
|---|---|---|
| `tavily+jina` | Tavily search, Jina Reader for page text | paid API keys |
| `searxng+crawl4ai` | self-hosted SearXNG, Crawl4AI for page text | nothing, runs locally |

Results are merged by URL. A page found by both branches is reported first and
labelled with both sources — that is the cross-check the two branches exist for.

```
Claude Code
   │
   ├── skill deep-search ──┬──> custom-search/search.py ──> Tavily + Jina
   │                       │
   │                       └──> imports mcp-server modules ──┬──> searxng :8880
   │                                                         └──> crawl4ai :11235
   │
   └── MCP searxng-crawl :8765 ──────────────────────────────┘
       (same services, tool-call route)
```

Two routes into the same services, on purpose. The skill spends no context until
it is called, because no tool schemas are loaded; the MCP server is there when
you want `search_and_scrape` or `social_search` as a normal tool call.

## Layout

| Path | Holds |
|---|---|
| `docker-compose.yml` | the three services: SearXNG, Crawl4AI, MCP server |
| `mcp-server/` | the MCP server — see [MCP tools](#mcp-tools) |
| `tests/` | MCP server tests, `httpx.MockTransport`, no network |
| `searxng/settings.yml.example` | SearXNG config template |
| `skills/deep-search/` | the skill: `SKILL.md`, `scripts/deep_search.py`, `tests/e2e.sh` |
| `skills/custom-search/` | the `tavily+jina` branch, run as a subprocess by the skill |
| `scripts/install-skills.sh` | links both skills into `~/.claude/skills` |

`skills/*/SKILL.md` is written in Russian — that is the interface the skill's
author reads. Everything else here is English.

## Setup

```bash
cp .env.example .env          # fill in CRAWL4AI_API_TOKEN: openssl rand -hex 32
cp searxng/settings.yml.example searxng/settings.yml
                              # replace ultrasecretkey: openssl rand -hex 32
docker compose up -d --build
```

Both `.env` and the real `settings.yml` are gitignored because they hold
secrets.

Install the skills and point them at this checkout:

```bash
./scripts/install-skills.sh
export INTERNET_CLAUDE_DIR="$PWD"     # add to your shell rc
```

Optionally register the MCP server as well:

```bash
claude mcp add --transport http --scope user searxng-crawl http://127.0.0.1:8765/mcp
```

## Using the skill

```bash
# 50 sources per branch, 10 read in full
~/.claude/skills/deep-search/scripts/deep_search.py "Crawl4AI vs Firecrawl"

# quick, one branch
~/.claude/skills/deep-search/scripts/deep_search.py "python 3.14 release" \
    --only searxng --max-results 5 --full-pages 3

# research plus what people are posting
~/.claude/skills/deep-search/scripts/deep_search.py "Claude Code hooks" --social
```

The full flag table, the output format, the failure messages and the proxy
behaviour are in [`skills/deep-search/SKILL.md`](skills/deep-search/SKILL.md).

Output comes in two tiers: the pages read in full, then `## Ещё источники` — the
rest as one line each with title, URL, engine and snippet, so you can ask for a
specific one to be read next. Both branches down is exit code `3` with the
reasons on stderr; one branch down is reported in the header and the other still
answers.

## Environment

| Variable | For |
|---|---|
| `CRAWL4AI_API_TOKEN` | required; Crawl4AI demands it on every endpoint |
| `SEARXNG_PORT`, `CRAWL4AI_PORT`, `MCP_PORT` | host ports, bound to `127.0.0.1` only |
| `INTERNET_CLAUDE_DIR` | path to this checkout, default `~/internet-claude` |
| `TAVILY_API_KEY_1..3`, `JINA_API_KEY` | branch `tavily+jina` |
| `DEEP_SEARCH_PROXY` | proxy for external requests, default `http://127.0.0.1:8118`, empty = direct |
| `DEEP_SEARCH_SEARXNG_URL`, `DEEP_SEARCH_CRAWL4AI_URL` | full URLs when the services are not on the default local ports |
| `SOCIAL_*`, `BLUESKY_*`, `YOUTUBE_API_KEY` | optional social sources, see `.env.example` |

The proxy default assumes something listening on `127.0.0.1:8118` (Privoxy or
similar). If nothing is there, the skill says so in the header and goes direct;
`--no-proxy` or `DEEP_SEARCH_PROXY=` turns it off for good.

## MCP tools

| Tool | Use it for |
|---|---|
| `search_and_scrape(query, …)` | The normal case: search, then return the readable text of the top pages. |
| `search(query, …)` | Titles, URLs and snippets only. Cheap way to pick what is worth reading. |
| `fetch_url(url, …)` | One page whose URL you already know. |
| `social_search(query, …)` | What people are saying on social media, across every configured platform at once. |
| `social_sources()` | Which platforms are reachable and what the rest still need. |

`search_and_scrape` caps output twice: `max_chars_per_page` (default 4000) and a
hard `MAX_TOTAL_CHARS` ceiling in `mcp-server/server.py`. Without those, five
unclipped pages routinely land 150–200k characters in the context. Results are
deduplicated to at most two pages per host, so one verbose site cannot consume
the whole budget.

## Social search

`social_search` is one entry point over several platforms. It fans out
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

A source that is not configured is left out rather than reported as broken; ask
for it by name in `platforms` and it will tell you exactly what it needs. A
source that *fails* is always named at the end of the answer, never silently
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

`mcp-server/sanitize.py` resolves every URL and refuses private, loopback,
link-local and reserved addresses. The tools accept URLs proposed by a model, so
without that check the server would proxy requests into the compose network, the
Docker host and cloud metadata endpoints. The skill imports the same module
rather than restating the HTTP contracts, so branch B is vetted by exactly the
same code.

## Development

```bash
uv sync
uv run pytest -q                      # MCP server, no network

DEEP_SEARCH_SCRIPT="$PWD/skills/deep-search/scripts/deep_search.py" \
    bash skills/deep-search/tests/e2e.sh
```

The e2e suite covers 18 scenarios: each branch alone and together, dedup, the
social block, both degradations, exit code 3, the output budget, JSON,
`--time-range`, the venv re-exec, the SSRF veto, proxy default, dead-proxy
fallback and `--no-proxy`. Do not run it back to back — a burst of queries makes
SearXNG's engines serve CAPTCHAs.

Tests use `httpx.MockTransport` and never touch the network. The container
installs its dependencies from the same `pyproject.toml`, so the image and the
test run cannot drift apart.

## Notes

- SearXNG's JSON API is off by default; `search.formats` must list `json`, and
  `server.limiter` must be `false` because JSON clients send no browser headers.
- The SearXNG entrypoint chowns its config path, so compose mounts the
  `./searxng` directory rather than the single file.
- `crawl4ai` needs `shm_size: 1g`; Chromium crashes on heavy pages with the
  64MB default.
- Adding a social platform is one class in `mcp-server/sources.py` with `name`,
  `unavailable` and `async search(spec)`, plus one entry in `build_sources`.
  Nothing in the aggregator or the tools changes.
