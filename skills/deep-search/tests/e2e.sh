#!/bin/bash
# e2e for the deep-search skill. Each case prints PASS/FAIL with a reason.
#
# Live SearXNG appears in three cases only. Everything else points branch B at a
# stubbed SearXNG on 127.0.0.1:8899: the real engines CAPTCHA under a burst of
# queries, which made the suite fail on the environment rather than on the code.
# The stub answers with one crawlable public URL and two unroutable ones, so both
# the happy path and the SSRF veto stay deterministic.
# Override to test a checkout that is not installed into ~/.claude/skills.
S="${DEEP_SEARCH_SCRIPT:-$HOME/.claude/skills/deep-search/scripts/deep_search.py}"
OUT="${DEEP_SEARCH_E2E_OUT:-${TMPDIR:-/tmp}/deep-search-e2e}"
STUB="http://127.0.0.1:8899"
mkdir -p "$OUT"
PASS=0
FAIL=0
stub_pid=""

check() { # check <name> <condition-description> <0|1>
  if [ "$3" = "0" ]; then
    printf 'PASS  %-34s %s\n' "$1" "$2"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-34s %s\n' "$1" "$2"
    FAIL=$((FAIL + 1))
  fi
}

run() { # run <file> <args...>  — live SearXNG
  local file="$1"; shift
  "$S" "$@" >"$OUT/$file" 2>&1
  echo $?
}

runs() { # runs <file> <args...>  — stubbed SearXNG
  local file="$1"; shift
  DEEP_SEARCH_SEARXNG_URL="$STUB" "$S" "$@" >"$OUT/$file" 2>&1
  echo $?
}

start_stub() {
  python3 - <<'PY' >"$OUT/stub.log" 2>&1 &
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

SEARCH = json.dumps({"results": [
    {"url": "https://example.com/", "title": "public", "content": "c", "engine": "stub"},
    {"url": "http://10.0.0.1/secret", "title": "internal", "content": "c", "engine": "stub"},
    {"url": "http://169.254.169.254/latest/meta-data", "title": "imds", "content": "c", "engine": "stub"},
], "unresponsive_engines": [["stub2", "CAPTCHA"]]}).encode()

# /config is what the skill reads to build the wide engine pool.
CONFIG = json.dumps({"engines": [
    {"name": "stub", "categories": ["general"], "enabled": True},
    {"name": "stub2", "categories": ["general"], "enabled": False},
    {"name": "currency", "categories": ["general"], "enabled": True},
    {"name": "stub-images", "categories": ["images"], "enabled": True},
]}).encode()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = CONFIG if self.path.startswith("/config") else SEARCH
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", 8899), Handler).serve_forever()
PY
  stub_pid=$!
  sleep 1
}

stop_stub() {
  [ -n "$stub_pid" ] && kill "$stub_pid" 2>/dev/null
  stub_pid=""
}
trap stop_stub EXIT

echo "== 1. only searxng, широкий пул движков (live) =="
code=$(run t1.md "crawl4ai release notes" --only searxng --full-pages 3)
check only-searxng "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## 1\. ' "$OUT/t1.md"; check only-searxng "есть запись #1" $?
grep -q 'searxng+crawl4ai:' "$OUT/t1.md"; check only-searxng "метка источника" $?
grep -q 'tavily' "$OUT/t1.md"; check only-searxng "ветка A не запускалась" $([ $? = 1 ] && echo 0 || echo 1)
asked=$(sed -n 's/^движки: запрошено \([0-9]*\).*/\1/p' "$OUT/t1.md")
check only-searxng "движков в запросе: ${asked:-нет строки}" $([ -n "$asked" ] && [ "$asked" -ge 40 ] && echo 0 || echo 1)
full=$(grep -c '^## [0-9]\+\. ' "$OUT/t1.md")
check only-searxng "полных страниц: $full (просили 3)" $([ "$full" -le 3 ] && [ "$full" -ge 1 ] && echo 0 || echo 1)
grep -q '^## Ещё источники (' "$OUT/t1.md"; check only-searxng "ярус ссылок присутствует" $?
brief=$(grep -c '^- \[' "$OUT/t1.md")
check only-searxng "ссылок во втором ярусе: $brief" $([ "$brief" -ge 5 ] && echo 0 || echo 1)

echo "== 1b. --engines builtin сужает пул (live) =="
# Успех тут не гарантирован и не проверяется: builtin — это те четыре движка,
# что регулярно лежат под капчей, ради чего и сделан широкий пул. Проверяем,
# что выбор пула действительно применён и назван в отчёте.
code=$(run t1b.md "crawl4ai release notes" --only searxng --engines builtin --max-results 10 --full-pages 2)
check engines-builtin "exit=$code (0 или 3 допустимы)" $([ "$code" = 0 ] || [ "$code" = 3 ] && echo 0 || echo 1)
grep -q 'движки: запрошено по умолчанию' "$OUT/t1b.md"; check engines-builtin "пул по умолчанию назван" $?

echo "== 2. only tavily (live) =="
code=$(run t2.md "crawl4ai release notes" --only tavily --max-results 5 --full-pages 3)
check only-tavily "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'tavily+jina:' "$OUT/t2.md"; check only-tavily "метка источника" $?

echo "== 3. both + dedupe (live) =="
code=$(run t3.md "Crawl4AI vs Firecrawl" --max-results 12 --full-pages 3)
check both "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
dups=$(grep -oE '^https?://[^ ]+$' "$OUT/t3.md" | sort | uniq -d | wc -l | tr -d ' ')
check both "дублей URL: $dups" $([ "$dups" = 0 ] && echo 0 || echo 1)
overlap=$(sed -n 's/.*совпадений: \([0-9]*\).*/\1/p' "$OUT/t3.md")
if [ -n "$overlap" ] && [ "$overlap" != 0 ]; then
  # -m1: crawled page markdown carries "## 1. ..." headings of its own.
  first=$(grep -m1 -A2 '^## 1\. ' "$OUT/t3.md" | tail -1)
  case "$first" in *"searxng+crawl4ai, tavily+jina"*) r=0;; *) r=1;; esac
  check both "совпадений $overlap, запись #1 из двух источников" $r
else
  check both "движки не дали пересечений — проверка пропущена" 0
fi

start_stub

echo "== 3b. пул движков из /config, фильтр утилит (stub) =="
code=$(runs t3b.md "anything" --only searxng --max-results 3 --full-pages 1)
check engine-pool "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
# stub, stub2 берутся; currency отфильтрован как утилита, stub-images — не general.
grep -q '^движки: запрошено 2,' "$OUT/t3b.md"; check engine-pool "запрошено 2 движка из 4" $?
grep -q 'без ответа 1' "$OUT/t3b.md"; check engine-pool "unresponsive_engines учтён" $?

echo "== 3c. нечитаемый /config -> пул по умолчанию, не отказ (stub) =="
code=$(DEEP_SEARCH_SEARXNG_URL="http://127.0.0.1:8898" "$S" "anything" --only searxng >"$OUT/t3c.md" 2>&1; echo $?)
check config-fallback "мёртвый SearXNG -> exit=$code" $([ "$code" = 3 ] && echo 0 || echo 1)
grep -q 'searxng+crawl4ai: ' "$OUT/t3c.md"; check config-fallback "причина названа" $?

echo "== 4. social (stub) =="
code=$(runs t4.md "Claude Code hooks" --social --max-results 2)
check social "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## Социальные сети' "$OUT/t4.md"; check social "блок соцсетей" $?
grep -qE 'hackernews|telegram|reddit|mastodon' "$OUT/t4.md"; check social "платформы названы" $?

echo "== 5. no repo -> branch B named, A works =="
code=$(DEEP_SEARCH_DIR=/nonexistent "$S" "crawl4ai release notes" --max-results 2 >"$OUT/t5.md" 2>&1; echo $?)
check no-repo "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'нет каталога /nonexistent/lib' "$OUT/t5.md"; check no-repo "причина отказа точна" $?
grep -q 'tavily+jina:' "$OUT/t5.md"; check no-repo "ветка A отдала результат" $?

echo "== 6. no tavily keys -> branch A named, B works (stub) =="
code=$(env -u TAVILY_API_KEY_1 -u TAVILY_API_KEY_2 -u TAVILY_API_KEY_3 \
  DEEP_SEARCH_SEARXNG_URL="$STUB" "$S" "anything" --max-results 2 >"$OUT/t6.md" 2>&1; echo $?)
check no-keys "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'tavily+jina — ' "$OUT/t6.md"; check no-keys "ветка A названа упавшей" $?
grep -q 'searxng+crawl4ai:' "$OUT/t6.md"; check no-keys "ветка B отдала результат" $?

echo "== 7. both fail -> exit 3 =="
code=$(env -u TAVILY_API_KEY_1 -u TAVILY_API_KEY_2 -u TAVILY_API_KEY_3 \
  DEEP_SEARCH_DIR=/nonexistent "$S" "anything" >"$OUT/t7.md" 2>&1; echo $?)
check both-fail "exit=$code" $([ "$code" = 3 ] && echo 0 || echo 1)
grep -cq 'tavily+jina: ' "$OUT/t7.md" && grep -q 'searxng+crawl4ai: ' "$OUT/t7.md"
check both-fail "обе причины в stderr" $?

echo "== 8. output budget (live tavily) =="
code=$(run t8.md "kubernetes operator patterns" --only tavily --max-results 6 --full-pages 4 --total-budget 8000)
size=$(wc -c < "$OUT/t8.md" | tr -d ' ')
check budget "exit=$code, размер=$size" $([ "$code" = 0 ] && [ "$size" -lt 12000 ] && echo 0 || echo 1)

echo "== 9. json (stub) =="
code=$(runs t9.json "anything" --only searxng --max-results 2 --json)
check json "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
python3 -c "
import json
d=json.load(open('$OUT/t9.json'))
assert set(d) >= {'query','proxy','entries','failures','social'}, sorted(d)
assert all(set(e) >= {'url','title','text','sources'} for e in d['entries'])
assert d['proxy'].startswith('http'), d['proxy']
" 2>"$OUT/t9.err"
check json "структура JSON и поле proxy" $?

echo "== 10. time-range (stub) =="
code=$(runs t10.md "anything" --only searxng --max-results 2 --time-range month)
check time-range "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
code=$("$S" "x" --time-range decade >"$OUT/t10b.md" 2>&1; echo $?)
check time-range "плохое значение -> exit $code" $([ "$code" = 2 ] && echo 0 || echo 1)

echo "== 11. bootstrap from system python3 (stub) =="
code=$(DEEP_SEARCH_SEARXNG_URL="$STUB" /usr/bin/python3 "$S" "anything" --only searxng --max-results 2 >"$OUT/t11.md" 2>&1; echo $?)
check bootstrap "exit=$code (re-exec в venv)" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## 1\. ' "$OUT/t11.md"; check bootstrap "есть запись" $?

echo "== 12. SSRF veto (stub) =="
code=$(runs t12.md "anything" --only searxng --max-results 3)
check ssrf "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'private or unroutable' "$OUT/t12.md"; check ssrf "приватный URL отклонён" $?
grep -q '10.0.0.1' "$OUT/t12.md" && grep -q '169.254.169.254' "$OUT/t12.md"
check ssrf "оба URL названы в отказах" $?
grep -q 'example.com' "$OUT/t12.md"; check ssrf "публичный URL прошёл" $?
grep -q '^прокси: http' "$OUT/t12.md"; check ssrf "localhost не проксируется" $?

echo "== 13. proxy on by default (stub) =="
code=$(runs t13.md "Claude Code hooks" --only searxng --social --max-results 2)
check proxy-on "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -qE '^прокси: http://127\.0\.0\.1:8118$' "$OUT/t13.md"; check proxy-on "строка прокси в отчёте" $?
# Only the social block's own failure list; the branch-B one above it is unrelated.
sed -n '/^## Социальные сети/,$p' "$OUT/t13.md" | sed -n '/Не удалось получить/,$p' | grep -q 'mastodon'
check proxy-on "mastodon не в отказах" $([ $? = 1 ] && echo 0 || echo 1)

echo "== 14. dead proxy -> fallback (stub) =="
code=$(DEEP_SEARCH_PROXY=http://127.0.0.1:9999 DEEP_SEARCH_SEARXNG_URL="$STUB" \
  "$S" "anything" --only searxng --max-results 2 >"$OUT/t14.md" 2>&1; echo $?)
check dead-proxy "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'недоступен, работаю напрямую' "$OUT/t14.md"; check dead-proxy "фолбэк назван в отчёте" $?
grep -q '^Источники — ' "$OUT/t14.md"; check dead-proxy "ветка продолжила работу" $?

echo "== 15. --no-proxy (stub) =="
code=$(runs t15.md "Claude Code hooks" --only searxng --social --no-proxy --max-results 2)
check no-proxy "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^прокси: выключен$' "$OUT/t15.md"; check no-proxy "прокси выключен в отчёте" $?
sed -n '/^## Социальные сети/,$p' "$OUT/t15.md" | sed -n '/Не удалось получить/,$p' | grep -q 'mastodon'
check no-proxy "без прокси mastodon в отказах" $?

echo "== 16. proxy named even when everything fails =="
code=$(env -u TAVILY_API_KEY_1 -u TAVILY_API_KEY_2 -u TAVILY_API_KEY_3 \
  DEEP_SEARCH_DIR=/nonexistent "$S" "anything" >"$OUT/t16.md" 2>&1; echo $?)
grep -q '^прокси: ' "$OUT/t16.md"; check proxy-note "прокси назван и при exit $code" $?

stop_stub
echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
