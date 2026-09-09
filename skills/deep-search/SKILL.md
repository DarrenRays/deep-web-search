---
name: deep-search
description: Full web research through two sources at once — Tavily+Jina and self-hosted SearXNG+Crawl4AI — run in parallel, deduplicated by URL, reported as one document. Use it when coverage and cross-checking matter: comparing sources, a disputed or fresh fact, changelogs and release notes, market overviews, "collect everything there is on X", plus an optional social media block. This is the only web search route: the Tavily+Jina script lives inside the skill as branch A. By default it collects 50 sources per branch and reads 10 of them in full. For a quick question use the same skill with `--only tavily --max-results 5 --full-pages 3`.
allowed-tools: Bash(~/.claude/skills/deep-search/scripts/deep_search.py *)
---

# Deep Search Skill

One query goes to two independent stacks at once; the results are merged by URL:

| Branch | How it fetches | Cost |
|---|---|---|
| `tavily+jina` | `scripts/tavily_jina.py` next to this skill (Tavily search + Jina Reader) | paid keys |
| `searxng+crawl4ai` | self-hosted SearXNG `127.0.0.1:8880` + Crawl4AI `127.0.0.1:11235` over plain HTTP | free, no rate limit |

The skill imports the stack's modules (`backends.py`, `sanitize.py`,
`sources.py`, `aggregate.py`, `social.py`) from `INTERNET_CLAUDE_DIR/lib` and
calls the services itself. Nothing is loaded into context until the skill is
called.

## When to use it

- one question needs several search engines (cross-checking);
- the fact is disputed and sources contradict each other;
- fresh news, changelogs, release notes, GitHub issues;
- "collect everything there is on X" — an overview, a comparison, a market;
- a social media slice is wanted too (`--social`).

## When not to

- one quick fact → this same skill with `--only tavily --max-results 5 --full-pages 3`;
- the exact URL is known → `fetch_url` or `curl`;
- a question about a library's API or config → the `find-docs` skill;
- the internals of a GitHub repository → the `deepwiki` skill.

## Command

```bash
~/.claude/skills/deep-search/scripts/deep_search.py "query" [flags]
```

| Flag | Default | What it does |
|---|---|---|
| `--max-results N` | 50 | total sources in the report **per branch**: full pages plus links |
| `--full-pages N` | 10 | how many sources to read in full; the rest stay links with a snippet |
| `--engines` | `auto` | `auto` — every general SearXNG engine (52 right now); `builtin` — only the ones enabled by default (4 that actually search); or a comma-separated list |
| `--search-timeout N` | 40 | SearXNG request timeout; a wide pool needs more than 15 s |
| `--max-chars-per-page N` | 4000 | trim one page's text |
| `--snippet-chars N` | 200 | trim the snippet of a second-tier link |
| `--total-budget N` | 60000 | total output character budget |
| `--language LANG` | `all` | SearXNG search language; `ru` narrows the results to the Russian-language web |
| `--time-range` | `` | `day` \| `week` \| `month` \| `year` |
| `--only tavily\|searxng` | both | switch one branch off |
| `--fresh` | off | `cache_mode=bypass` on Crawl4AI |
| `--social` | off | add a social block across 8 platforms |
| `--social-limit N` | 20 | posts in the social block |
| `--social-per-platform N` | 8 | how many to request from each platform |
| `--social-chars N` | 600 | trim one post's text |
| `--json` | off | raw JSON instead of markdown |
| `--no-proxy` | off | do not use the proxy for this run |

## Examples

```bash
# ordinary research: 50 sources per branch, 10 read in full
~/.claude/skills/deep-search/scripts/deep_search.py "Crawl4AI vs Firecrawl"

# fresh news from the past week, no paid keys
~/.claude/skills/deep-search/scripts/deep_search.py "python 3.14 release" \
  --only searxng --time-range week

# deep dive: more full pages and more budget
~/.claude/skills/deep-search/scripts/deep_search.py "Oppo Find X9 Pro vs Samsung S25 Ultra" \
  --max-results 80 --full-pages 20 --total-budget 120000

# research plus what people are posting
~/.claude/skills/deep-search/scripts/deep_search.py "Claude Code hooks" --social --full-pages 5
```

## Output

The header reads `Источники — tavily+jina: N, searxng+crawl4ai: M, совпадений:
K, прочитано полностью: F`, then `движки: запрошено 52, с результатами 12, без
ответа 20`, then a `прокси:` line. The report labels are Russian; the page text
is in whatever language the page is written in.

Then two tiers:

1. **Full pages** (`--full-pages`, 10 by default) — title, URL, source and
   engine labels, text. An entry found by both branches comes first and carries
   both source labels — the signal that two engines confirmed it. The longer of
   the two texts is used.
2. **`## Ещё источники (N)`** — the remaining links, one line each: title, URL,
   engine, the engine's snippet. This is the map of what else exists, for
   reading a specific one next.

If a first-tier page does not open (Cloudflare, empty HTML), the next one on the
list takes its place — one refill, so the tier does not thin out.

## Choosing flags from how the request is phrased

| What is asked | How to run it |
|---|---|
| "find", "what are people writing", ordinary research | no flags — 50 sources, 10 full |
| "quick look", "in two words" | `--only tavily --max-results 5 --full-pages 3` |
| "dig up everything", "as complete as possible" | `--max-results 80 --full-pages 20 --total-budget 120000` |
| "only fresh", "from the past week" | `--time-range week` |
| "what's on social media", "what are people saying" | `--social` |
| "search Google/Brave" | `--engines google cse,brave` |
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

The failed branch is named in the `### Не удалось получить` block, the other one
keeps working, and the exit code stays `0`. An empty list is never printed as
"nothing found". Decoding the messages:

| Message | What to do |
|---|---|
| `tavily+jina — ERROR: No TAVILY_API_KEY_1..3 variables found` | the keys are not in the process environment; configure them outside the chat |
| `searxng+crawl4ai — нет каталога …/lib` | set `INTERNET_CLAUDE_DIR` |
| `searxng+crawl4ai — httpx недоступен` | run `uv sync` in the stack directory |
| `searxng+crawl4ai — CRAWL4AI_API_TOKEN не задан` | add the token to the environment or to `<repo>/.env` |
| `searxng+crawl4ai — SearXNG вернул 0 результатов; движки: …` | rare with `--engines auto`: a pool of 52 engines survives a CAPTCHA on the main four. If it is still zero — wait, or use `--engines builtin` |
| `N ссыл(ок) от мусорных движков — домен не резолвится` | normal for a wide pool: some engines return link farms, and those are dropped before crawling |
| `Blocked by anti-bot protection: Cloudflare JS challenge` | the site is closed to the crawler; the text usually arrives through `tavily+jina` — Jina fetches the page from its own side |
| `прокси: … недоступен, работаю напрямую` | the proxy container is down; bring it up or live with it — mastodon and reddit will end up in the failures |
| `ConnectError` on 8880/11235 | run `docker compose up -d` in the stack directory |

Both branches down → exit code `3`, reasons on stderr.

## Proxy

External requests go through `http://127.0.0.1:8118` by default; localhost never
does. The skill rewrites the proxy variables of its own process (`HTTP_PROXY`,
`HTTPS_PROXY`, `NO_PROXY=127.0.0.1,localhost`), so branch B on httpx and the
branch A subprocess on urllib get the same proxy. The machine's own `NO_PROXY`
is overwritten in the process: httpx chokes on IPv6 entries such as `::1`
(`Invalid port: ':'`).

The proxy is TCP-probed before the run. A dead proxy does not take out every
external source — the work continues directly and the report says
`прокси: … недоступен, работаю напрямую`. The `прокси:` line is always printed.

What it buys, measured: mastodon.social `000`→`200`, arctic-shift (reddit)
`422`→`200`, `api.tavily.com` `403`→`200`. The last one means branch A does not
work at all on this network without the proxy.

Turn it off with `--no-proxy` for one run, or `DEEP_SEARCH_PROXY=` in the
environment. Change the address with `DEEP_SEARCH_PROXY=http://host:port`. The
SOCKS5 port `1081` of the same gost container is not supported — httpx would
need the `socksio` package, and both ports have the same exit IP anyway.

**SearXNG engines are not proxied.** Measured: `html.duckduckgo.com` serves the
same challenge page directly and through the proxy (`202`, 0 results). A proxy
does not cure a CAPTCHA, and google and brave hit one faster from a datacenter
IP.

**Crawl4AI pages are not proxied.** Passing one through is impossible without
patching the sources: `proxy_config` in the request is rejected (`api.py` loads
any body as `Provenance.UNTRUSTED`), the container's env is ignored by Chromium
(Playwright installs its own `--proxy-server` shim), and the server-side
`crawler.base_config` lives only in `/app/config.yml` inside the image — and
replacing it would bring back `--allow-insecure-localhost` with
`--ignore-certificate-errors` from the defaults, disabling TLS verification on
every crawl. In practice a geo-blocked page comes back as an error in branch B,
but its text usually arrives through branch A — Jina fetches the page from its
own side. Which is exactly why the two sources are joined.

## Checking after changes

```bash
bash ~/.claude/skills/deep-search/tests/e2e.sh
```

18 scenarios, 53 checks: each branch alone and together, dedup, the social
block, both degradations, exit code 3, the output budget, JSON, `--time-range`,
the re-exec into the venv from the system python3, the SSRF veto on private
addresses, the proxy default, the fallback on a dead proxy, `--no-proxy`, the
wide engine pool, the tiered output, the utility-engine filter. Do not run them
back to back: a burst of queries makes the SearXNG engines serve CAPTCHAs.

## Environment variables

| Variable | For |
|---|---|
| `DEEP_SEARCH_PROXY` | proxy for external requests, `http://127.0.0.1:8118` by default; an empty value means direct |
| `TAVILY_API_KEY_1..3`, `JINA_API_KEY` | branch `tavily+jina` (read by `tavily_jina.py` itself) |
| `CRAWL4AI_API_TOKEN` | branch `searxng+crawl4ai`; falls back to `<repo>/.env` |
| `INTERNET_CLAUDE_DIR` | path to the stack, `~/internet-claude` by default |
| `SEARXNG_PORT`, `CRAWL4AI_PORT` | localhost ports, `8880` / `11235` by default |
| `DEEP_SEARCH_SEARXNG_URL`, `DEEP_SEARCH_CRAWL4AI_URL` | full address override |

## Security

- never print key and token values into the chat or pass them on the command
  line — the script reads them from the environment itself;
- URLs from search results pass through `sanitize.partition_safe` (an SSRF veto
  on private, loopback and link-local addresses) before they reach Crawl4AI; a
  rejected URL lands in the failures block with its reason.
