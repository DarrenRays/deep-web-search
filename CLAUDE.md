# deep-search-stack

Self-hosted web research for Claude Code: two compose services and the
`deep-search` skill that drives them. See README.md for setup and the reasoning
behind each source.

## Code layout

Flat modules under `lib/`, imported by bare name (`pythonpath` in
`pyproject.toml` points there). The skill adds the same directory to `sys.path`
and imports them as a library — there is no server process in between.

| Module | Holds |
|---|---|
| `backends.py` | HTTP clients for SearXNG and Crawl4AI |
| `sanitize.py` | URL vetting against SSRF, output trimming |
| `sources.py` | One adapter per social platform |
| `social.py` | `Post`, `QuerySpec`, dedupe, ranking, rendering |
| `aggregate.py` | Concurrent fan-out and per-source failure reporting |
| `cache.py` | Short-lived result cache |

The skill lives under `skills/deep-search/`, symlinked into `~/.claude/skills`
by `scripts/install-skills.sh`.

| Path | Holds |
|---|---|
| `skills/deep-search/scripts/deep_search.py` | both branches, merge, tiered report; finds `lib/`, `.venv` and `.env` from its own path |
| `skills/deep-search/scripts/tavily_jina.py` | branch `tavily+jina`, run as a subprocess by the skill |
| `skills/deep-search/tests/e2e.sh` | 18 scenarios against a stub SearXNG; `DEEP_SEARCH_SCRIPT` points it at a checkout |

## Conventions

- Output is a markdown string with its own cap. Context budget is the scarce
  resource here.
- A source that fails is named in the answer. Never let a failure return an
  empty list — "the source is down" must not read as "nothing was found".
- Adding a platform means one class in `lib/sources.py` with `name`,
  `unavailable` and `async search(spec)`, plus one entry in `build_sources`.
  Nothing in the aggregator or the skill should need to change.
- Tests use `httpx.MockTransport` and never touch the network.
