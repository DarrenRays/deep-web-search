# deep-search-stack

Self-hosted research stack for Claude Code: three compose services, one MCP
server, and the `deep-search` skill that drives the same services without
loading tool schemas. See README.md for setup and the reasoning behind each
source.

## Which tool for which question

| Question | Tool |
|---|---|
| What are people saying / posting about X | `social_search` |
| Which platforms are reachable right now | `social_sources` |
| General web question, needs page text | `search_and_scrape` |
| Just need candidate links | `search` |
| One page whose URL is already known | `fetch_url` |

`social_search` is the single entry point for social media — it already fans out
to every configured platform, so there is no reason to call one platform at a
time. Reach for `search_and_scrape` on a social topic only when `social_search`
has reported the relevant source as failing.

## Code layout

Flat modules under `mcp-server/`, imported by bare name (`pythonpath` in
`pyproject.toml` points there).

| Module | Holds |
|---|---|
| `server.py` | MCP tool definitions and the lifespan-built services |
| `backends.py` | HTTP clients for SearXNG and Crawl4AI |
| `sanitize.py` | URL vetting against SSRF, output trimming |
| `sources.py` | One adapter per social platform |
| `social.py` | `Post`, `QuerySpec`, dedupe, ranking, rendering |
| `aggregate.py` | Concurrent fan-out and per-source failure reporting |
| `cache.py` | Short-lived result cache |

The skills live under `skills/`, installed into `~/.claude/skills` as symlinks by
`scripts/install-skills.sh`.

| Path | Holds |
|---|---|
| `skills/deep-search/scripts/deep_search.py` | both branches, merge, tiered report; imports `mcp-server/` modules from `INTERNET_CLAUDE_DIR` |
| `skills/deep-search/tests/e2e.sh` | 18 scenarios against a stub SearXNG; `DEEP_SEARCH_SCRIPT` points it at a checkout |
| `skills/custom-search/scripts/search.py` | branch `tavily+jina`, run as a subprocess |

## Conventions

- Tools return markdown strings, not structured payloads, and every tool caps
  its own output. Context budget is the scarce resource here.
- A source that fails is named in the answer. Never let a failure return an
  empty list — "the source is down" must not read as "nothing was found".
- Adding a platform means one class in `sources.py` with `name`, `unavailable`
  and `async search(spec)`, plus one entry in `build_sources`. Nothing in the
  aggregator or the tools should need to change.
- Tests use `httpx.MockTransport` and never touch the network.
