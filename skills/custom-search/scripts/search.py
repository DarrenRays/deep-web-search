#!/usr/bin/env python3
"""
Custom Search Skill backend: Tavily + Jina.

Что делает:
1. Принимает поисковый запрос.
2. Перебирает до 3 Tavily API keys.
3. Делает web search через Tavily.
4. Берёт найденные URL.
5. Дочитывает и нормализует страницы через один Jina Reader API key.
6. Возвращает единый JSON для Claude Code.

Важно:
- Используйте только свои ключи и соблюдайте лимиты провайдеров.
- Ротация Tavily здесь нужна для отказоустойчивости и graceful fallback,
  а не для обхода ограничений.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def load_tavily_keys(count: int = 3) -> List[str]:
    keys: List[str] = []

    for i in range(1, count + 1):
        value = os.getenv(f"TAVILY_API_KEY_{i}", "").strip()
        if value:
            keys.append(value)

    return keys


def load_jina_key() -> str:
    return os.getenv("JINA_API_KEY", "").strip()


def build_jina_reader_url(url: str) -> str:
    """
    Jina Reader URL format:
    https://r.jina.ai/http://example.com/article

    Для исходного:
    https://example.com/article

    Получаем:
    https://r.jina.ai/http://example.com/article
    """
    clean = url.strip()

    if clean.startswith("https://"):
        clean = clean[len("https://") :]
    elif clean.startswith("http://"):
        clean = clean[len("http://") :]

    return "https://r.jina.ai/http://" + clean


def http_json_post(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int = 30,
) -> Tuple[int, Dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return status, json.loads(raw)
            except json.JSONDecodeError:
                return status, {"raw": raw}

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}
        return exc.code, data

    except Exception as exc:
        return 0, {"error": str(exc)}


def http_text_get(
    url: str,
    headers: Dict[str, str],
    timeout: int = 45,
) -> Tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain, text/markdown, */*",
            **headers,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
            return status, text

    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, text

    except Exception as exc:
        return 0, f"ERROR: {exc}"


def tavily_search_once(
    api_key: str,
    query: str,
    max_results: int,
    search_depth: str,
    include_answer: bool,
    include_raw_content: bool,
    topic: str,
    timeout: int,
) -> Tuple[bool, int, Dict[str, Any]]:
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
        "topic": topic,
    }

    status, data = http_json_post(
        TAVILY_SEARCH_URL,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )

    ok = 200 <= status < 300 and isinstance(data, dict) and "results" in data
    return ok, status, data


def tavily_search_with_rotation(
    keys: List[str],
    query: str,
    max_results: int,
    search_depth: str,
    include_answer: bool,
    include_raw_content: bool,
    topic: str,
    timeout: int,
    retries_per_key: int,
    sleep_seconds: float,
) -> Dict[str, Any]:
    errors = []

    for key_index, key in enumerate(keys, start=1):
        for attempt in range(1, retries_per_key + 1):
            ok, status, data = tavily_search_once(
                api_key=key,
                query=query,
                max_results=max_results,
                search_depth=search_depth,
                include_answer=include_answer,
                include_raw_content=include_raw_content,
                topic=topic,
                timeout=timeout,
            )

            if ok:
                data["_custom_search_meta"] = {
                    "provider": "tavily",
                    "used_tavily_account": key_index,
                    "attempt": attempt,
                    "status": status,
                }
                return data

            errors.append(
                {
                    "provider": "tavily",
                    "account": key_index,
                    "attempt": attempt,
                    "status": status,
                    "error": data,
                }
            )

            # 401/403 обычно означает неверный ключ — нет смысла ретраить этот ключ.
            if status in (401, 403):
                break

            # 429/rate limit или 5xx — можно попробовать ещё раз или следующий ключ.
            time.sleep(sleep_seconds)

    raise RuntimeError(
        json.dumps(
            {
                "message": "All Tavily keys failed",
                "errors": errors,
            },
            ensure_ascii=False,
        )
    )


def normalize_with_jina(
    url: str,
    jina_key: str,
    timeout: int,
    max_chars: int,
) -> Dict[str, Any]:
    if not jina_key:
        return {
            "url": url,
            "ok": False,
            "error": "JINA_API_KEY is not configured",
            "content": "",
        }

    reader_url = build_jina_reader_url(url)

    headers = {
        "Authorization": f"Bearer {jina_key}",
        "X-Return-Format": "markdown",
    }

    status, text = http_text_get(
        reader_url,
        headers=headers,
        timeout=timeout,
    )

    if 200 <= status < 300 and text.strip():
        return {
            "url": url,
            "reader_url": reader_url,
            "ok": True,
            "status": status,
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    return {
        "url": url,
        "reader_url": reader_url,
        "ok": False,
        "status": status,
        "error_preview": text[:500],
        "content": "",
    }


def dedupe_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []

    for item in results:
        url = item.get("url", "").strip()
        if not url or url in seen:
            continue

        seen.add(url)
        unique.append(item)

    return unique


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search via 3 Tavily keys and normalize URLs via one Jina Reader key."
    )

    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("--max-results", type=int, default=5, help="Количество результатов Tavily")
    parser.add_argument("--jina-pages", type=int, default=3, help="Сколько URL дочитать через Jina")
    parser.add_argument("--max-chars", type=int, default=6000, help="Максимум символов Jina-контента на страницу")
    parser.add_argument("--search-depth", choices=["basic", "advanced"], default="advanced")
    parser.add_argument("--topic", default="general", help="Tavily topic, например general/news")
    parser.add_argument("--include-answer", action="store_true", help="Просить Tavily вернуть answer")
    parser.add_argument("--include-raw-content", action="store_true", help="Просить Tavily вернуть raw content")
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--retries-per-key", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=0.75)
    parser.add_argument("--json-pretty", action="store_true")

    args = parser.parse_args()

    tavily_keys = load_tavily_keys(count=3)
    jina_key = load_jina_key()

    if not tavily_keys:
        eprint("ERROR: No TAVILY_API_KEY_1..3 variables found")
        return 2

    if not jina_key:
        eprint("WARNING: JINA_API_KEY is not configured; Jina normalization will fail")

    try:
        tavily_data = tavily_search_with_rotation(
            keys=tavily_keys,
            query=args.query,
            max_results=args.max_results,
            search_depth=args.search_depth,
            include_answer=args.include_answer,
            include_raw_content=args.include_raw_content,
            topic=args.topic,
            timeout=args.timeout,
            retries_per_key=args.retries_per_key,
            sleep_seconds=args.sleep,
        )

    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    raw_results = tavily_data.get("results", [])
    unique_results = dedupe_results(raw_results)

    normalized_pages = []

    for item in unique_results[: args.jina_pages]:
        url = item.get("url", "")
        if not url:
            continue

        normalized = normalize_with_jina(
            url=url,
            jina_key=jina_key,
            timeout=args.timeout,
            max_chars=args.max_chars,
        )

        normalized_pages.append(
            {
                "title": item.get("title"),
                "url": url,
                "score": item.get("score"),
                "tavily_content": item.get("content"),
                "jina": normalized,
            }
        )

    output = {
        "query": args.query,
        "meta": {
            "tavily_accounts_configured": len(tavily_keys),
            "jina_account_configured": bool(jina_key),
            "used_tavily_account": tavily_data.get("_custom_search_meta", {}).get("used_tavily_account"),
            "search_depth": args.search_depth,
            "max_results": args.max_results,
            "jina_pages": args.jina_pages,
        },
        "tavily_answer": tavily_data.get("answer"),
        "results": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
            for item in unique_results
        ],
        "normalized_pages": normalized_pages,
    }

    if args.json_pretty:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())