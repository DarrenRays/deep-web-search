---
name: custom-search
description: DO NOT CALL DIRECTLY — superseded by the deep-search skill, which runs this script as one of its two branches. Searches the web through Tavily and normalises pages through Jina Reader. It stays here only as a dependency of deep-search and as the manual /custom-search command for a single-source lookup.
disable-model-invocation: true
---

> Disabled for auto-invocation. Web search goes through the `deep-search` skill,
> which runs `scripts/search.py` as the `tavily+jina` branch in parallel with
> self-hosted SearXNG+Crawl4AI. Do not delete this directory — branch A breaks.

# Custom Search Skill

This skill searches the web through Tavily and normalises the pages it finds
through Jina Reader.

Configuration:

- `TAVILY_API_KEY_1` — Tavily account 1
- `TAVILY_API_KEY_2` — Tavily account 2
- `TAVILY_API_KEY_3` — Tavily account 3
- `JINA_API_KEY` — one Jina account for the Reader API

Use this skill when the user asks to:

- find current information;
- check a fact;
- compare data from several sources;
- find documentation;
- go through an article, changelog, issue or release notes;
- collect sources on a technical topic;
- get LLM-friendly page text;
- do research with citable URLs.

Do not use it for:

- analysing local project files;
- tasks where the repository already holds enough context;
- reading secrets, `.env`, keys or tokens;
- working around an API provider's limits.

## Answer language

Reply in the language the user asked in, unless they asked for another one
("answer in English", "ответь по-русски"). The script takes no language flag:
the results are whatever the query's own language brings back, so ask in the
language of the sources you want.

## Security

Never print the values of these environment variables:

- `TAVILY_API_KEY_1`
- `TAVILY_API_KEY_2`
- `TAVILY_API_KEY_3`
- `JINA_API_KEY`

If a key is missing or does not work, tell the user the name of the missing
variable only — do not try to read secret files.

## Command

The executable script lives here:

```bash
~/.claude/skills/custom-search/scripts/search.py
```
