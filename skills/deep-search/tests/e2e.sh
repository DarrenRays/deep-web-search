#!/bin/bash
# e2e for the deep-search skill. Each case prints PASS/FAIL with a reason.
#
# Live SearXNG appears in a few cases only. Everything else points both
# instances at stubbed SearXNG servers on 127.0.0.1: the real engines CAPTCHA
# under a burst of queries, which made the suite fail on the environment rather
# than on the code. The default stub answers with one readable public URL and
# two unroutable ones, so both the happy path and the SSRF veto stay
# deterministic. Other stub modes hand out pages that block bots, to exercise
# the browser step against the real sites.
# Override to test a checkout that is not installed into ~/.claude/skills.
S="${DEEP_SEARCH_SCRIPT:-$HOME/.claude/skills/deep-search/scripts/deep_search.py}"
OUT="${DEEP_SEARCH_E2E_OUT:-${TMPDIR:-/tmp}/deep-search-e2e}"
STUB_A="http://127.0.0.1:8899"
STUB_B="http://127.0.0.1:8898"
DEAD="http://127.0.0.1:8890"
mkdir -p "$OUT"
PASS=0
FAIL=0
stub_pids=()

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

runs() { # runs <file> <args...>  — both instances stubbed
  local file="$1"; shift
  DEEP_SEARCH_SEARXNG_URL="$STUB_A" DEEP_SEARCH_SEARXNG_B_URL="$STUB_B" \
    "$S" "$@" >"$OUT/$file" 2>&1
  echo $?
}

cat >"$OUT/stub.py" <<'PY'
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port, mode = int(sys.argv[1]), sys.argv[2]
RESULTS = {
    "default": [
        {"url": "https://example.com/", "title": "public", "content": "c", "engine": "stub"},
        {"url": "http://10.0.0.1/secret", "title": "internal", "content": "c", "engine": "stub"},
        {"url": "http://169.254.169.254/latest/meta-data", "title": "imds", "content": "c", "engine": "stub"},
    ],
    # Refuses plain HTTP and Crawl4AI with a Cloudflare challenge; a real Chrome gets in.
    "cloudflare": [
        {"url": "https://slashdot.org/software/comparison/Crawl4AI-vs-Firecrawl/",
         "title": "slashdot", "content": "c", "engine": "stub"},
    ],
    "avito": [
        {"url": "https://www.avito.ru/moskva/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc",
         "title": "avito", "content": "c", "engine": "stub"},
    ],
}[mode]
SEARCH = json.dumps({"results": RESULTS, "unresponsive_engines": [["stub2", "CAPTCHA"]]}).encode()

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

HTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY

start_stubs() { # start_stubs <mode>
  stop_stubs
  python3 "$OUT/stub.py" 8899 "$1" >"$OUT/stub-a.log" 2>&1 &
  stub_pids+=($!)
  python3 "$OUT/stub.py" 8898 "$1" >"$OUT/stub-b.log" 2>&1 &
  stub_pids+=($!)
  sleep 1
}

stop_stubs() {
  for pid in "${stub_pids[@]}"; do kill "$pid" 2>/dev/null; done
  stub_pids=()
}
trap stop_stubs EXIT

echo "== 1. both instances, wide engine pool (live) =="
code=$(run t1.md "crawl4ai release notes" --full-pages 3)
check live-both "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## 1\. ' "$OUT/t1.md"; check live-both "есть запись #1" $?
grep -qE 'searxng: [0-9]+' "$OUT/t1.md" && grep -qE 'searxng-b: [0-9]+' "$OUT/t1.md"
check live-both "обе метки источника в шапке" $?
for name in searxng searxng-b; do
  asked=$(sed -n "s/^движки $name: запрошено \([0-9]*\).*/\1/p" "$OUT/t1.md")
  check live-both "$name: движков ${asked:-нет строки}" $([ -n "$asked" ] && [ "$asked" -ge 40 ] && echo 0 || echo 1)
done
full=$(grep -c '^## [0-9]\+\. ' "$OUT/t1.md")
check live-both "полных страниц: $full (просили 3)" $([ "$full" -le 3 ] && [ "$full" -ge 1 ] && echo 0 || echo 1)
brief=$(grep -c '^- \[' "$OUT/t1.md")
check live-both "ссылок во втором ярусе: $brief" $([ "$brief" -ge 5 ] && echo 0 || echo 1)
grep -qiE 'tavily|jina' "$OUT/t1.md"; check live-both "ни Tavily, ни Jina" $([ $? = 1 ] && echo 0 || echo 1)
# Without the token the Crawl4AI fallback is off and only the reader runs; the
# rest of the suite would still pass, so it is checked once here.
grep -q 'CRAWL4AI_API_TOKEN не задан' "$OUT/t1.md"
check live-both "токен Crawl4AI виден скиллу" $([ $? = 1 ] && echo 0 || echo 1)

echo "== 2. --only searxng-b (live) =="
code=$(run t2.md "crawl4ai release notes" --only searxng-b --max-results 10 --full-pages 2)
check only-b "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'searxng-b: ' "$OUT/t2.md" && ! grep -qE '(^Источники — |, )searxng: ' "$OUT/t2.md"
check only-b "только вторая инстанция" $?

echo "== 2b. --engines builtin сужает пул (live) =="
# Успех тут не гарантирован: builtin — те несколько движков, что регулярно
# лежат под капчей. Проверяем, что выбор пула применён и назван в отчёте.
code=$(run t2b.md "crawl4ai release notes" --only searxng --engines builtin --max-results 10 --full-pages 2)
check engines-builtin "exit=$code (0 или 3 допустимы)" $([ "$code" = 0 ] || [ "$code" = 3 ] && echo 0 || echo 1)
grep -q 'движки searxng: запрошено по умолчанию' "$OUT/t2b.md" || grep -q 'запрошено по умолчанию' "$OUT/t2b.md"
check engines-builtin "пул по умолчанию назван" $?

echo "== 3. output budget (live) =="
code=$(run t3.md "kubernetes operator patterns" --max-results 10 --full-pages 4 --total-budget 8000)
size=$(wc -c < "$OUT/t3.md" | tr -d ' ')
check budget "exit=$code, размер=$size" $([ "$code" = 0 ] && [ "$size" -lt 14000 ] && echo 0 || echo 1)

start_stubs default

echo "== 4. пул движков из /config, фильтр утилит (stub) =="
code=$(runs t4.md "anything" --only searxng --max-results 3 --full-pages 1)
check engine-pool "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
# stub, stub2 берутся; currency отфильтрован как утилита, stub-images — не general.
grep -q '^движки searxng: запрошено 2,' "$OUT/t4.md"; check engine-pool "запрошено 2 движка из 4" $?
grep -q 'без ответа 1' "$OUT/t4.md"; check engine-pool "unresponsive_engines учтён" $?

echo "== 5. дубли между инстанциями сливаются (stub) =="
code=$(runs t5.md "anything" --max-results 3 --full-pages 1)
check dedupe "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'совпадений: 1' "$OUT/t5.md"; check dedupe "совпадение посчитано" $?
dups=$(grep -oE '^https?://[^ ]+$' "$OUT/t5.md" | sort | uniq -d | wc -l | tr -d ' ')
check dedupe "дублей URL: $dups" $([ "$dups" = 0 ] && echo 0 || echo 1)
grep -A2 '^## 1\. ' "$OUT/t5.md" | grep -q 'searxng, searxng-b'
check dedupe "запись #1 с метками обеих инстанций" $?

echo "== 6. trafilatura читает страницу без Crawl4AI (stub) =="
code=$(DEEP_SEARCH_CRAWL4AI_URL="$DEAD" runs t6.md "anything" --only searxng --max-results 3 --full-pages 1)
check reader "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -A3 '^## 1\. ' "$OUT/t6.md" | grep -q 'https://example.com/'
check reader "example.com прочитан при мёртвом Crawl4AI" $?

echo "== 7. одна инстанция упала -> названа, вторая отвечает (stub) =="
code=$(DEEP_SEARCH_SEARXNG_URL="$STUB_A" DEEP_SEARCH_SEARXNG_B_URL="$DEAD" \
  "$S" "anything" --max-results 3 >"$OUT/t7.md" 2>&1; echo $?)
check one-down "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^- searxng-b — ' "$OUT/t7.md"; check one-down "searxng-b назван упавшим" $?
grep -qE '(^Источники — |, )searxng: ' "$OUT/t7.md"; check one-down "searxng отдал результат" $?

echo "== 8. обе инстанции мертвы -> exit 3 =="
code=$(DEEP_SEARCH_SEARXNG_URL="$DEAD" DEEP_SEARCH_SEARXNG_B_URL="$DEAD" \
  "$S" "anything" >"$OUT/t8.md" 2>&1; echo $?)
check both-down "exit=$code" $([ "$code" = 3 ] && echo 0 || echo 1)
grep -q '^searxng: ' "$OUT/t8.md" && grep -q '^searxng-b: ' "$OUT/t8.md"
check both-down "обе причины в stderr" $?
grep -q '^прокси: ' "$OUT/t8.md"; check both-down "прокси назван и при exit 3" $?

echo "== 9. no repo -> exit 3, причина точна =="
code=$(DEEP_SEARCH_DIR=/nonexistent "$S" "anything" >"$OUT/t9.md" 2>&1; echo $?)
check no-repo "exit=$code" $([ "$code" = 3 ] && echo 0 || echo 1)
grep -q 'нет каталога /nonexistent/lib' "$OUT/t9.md"; check no-repo "причина отказа точна" $?

echo "== 10. social (stub) =="
code=$(runs t10.md "Claude Code hooks" --social --max-results 2)
check social "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## Социальные сети' "$OUT/t10.md"; check social "блок соцсетей" $?
grep -qE 'hackernews|telegram|reddit|mastodon' "$OUT/t10.md"; check social "платформы названы" $?

echo "== 11. json (stub) =="
code=$(runs t11.json "anything" --max-results 2 --json)
check json "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
python3 -c "
import json
d=json.load(open('$OUT/t11.json'))
assert set(d) >= {'query','proxy','engines','entries','failures','blocked','social'}, sorted(d)
assert all(set(e) >= {'url','title','text','sources'} for e in d['entries'])
assert d['proxy'].startswith('http'), d['proxy']
assert 'searxng-b' in d['engines'], d['engines']
" 2>"$OUT/t11.err"
check json "структура JSON, поля proxy и engines" $?

echo "== 12. аргументы =="
code=$(runs t12.md "anything" --max-results 2 --time-range month)
check args "time-range month -> exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
code=$("$S" "x" --time-range decade >"$OUT/t12b.md" 2>&1; echo $?)
check args "плохой time-range -> exit $code" $([ "$code" = 2 ] && echo 0 || echo 1)
code=$("$S" "x" --only tavily >"$OUT/t12c.md" 2>&1; echo $?)
check args "--only tavily больше нет -> exit $code" $([ "$code" = 2 ] && echo 0 || echo 1)

echo "== 13. bootstrap from system python3 (stub) =="
code=$(DEEP_SEARCH_SEARXNG_URL="$STUB_A" DEEP_SEARCH_SEARXNG_B_URL="$STUB_B" \
  /usr/bin/python3 "$S" "anything" --max-results 2 >"$OUT/t13.md" 2>&1; echo $?)
check bootstrap "exit=$code (re-exec в venv)" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^## 1\. ' "$OUT/t13.md"; check bootstrap "есть запись" $?

echo "== 14. SSRF veto (stub) =="
code=$(runs t14.md "anything" --only searxng --max-results 3)
check ssrf "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'private or unroutable' "$OUT/t14.md"; check ssrf "приватный URL отклонён" $?
grep -q '10.0.0.1' "$OUT/t14.md" && grep -q '169.254.169.254' "$OUT/t14.md"
check ssrf "оба URL названы в отказах" $?
grep -q 'example.com' "$OUT/t14.md"; check ssrf "публичный URL прошёл" $?
sed -n '/Заблокировано, добрать браузером/,/^### /p' "$OUT/t14.md" | grep -qE '10\.0\.0\.1|169\.254\.169\.254'
check ssrf "приватные URL не предлагаются браузеру" $([ $? = 1 ] && echo 0 || echo 1)
grep -q 'браузер' "$OUT/t14.md"; check ssrf "браузер не запускался на вето" $([ $? = 1 ] && echo 0 || echo 1)

echo "== 15. proxy on by default (stub) =="
code=$(runs t15.md "Claude Code hooks" --only searxng --social --max-results 2)
check proxy-on "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -qE '^прокси: http://127\.0\.0\.1:8118$' "$OUT/t15.md"; check proxy-on "строка прокси в отчёте" $?
# Only the social block's own failure list; the one above it is unrelated.
sed -n '/^## Социальные сети/,$p' "$OUT/t15.md" | sed -n '/Не удалось получить/,$p' | grep -q 'mastodon'
check proxy-on "mastodon не в отказах" $([ $? = 1 ] && echo 0 || echo 1)

echo "== 16. dead proxy -> fallback (stub) =="
code=$(DEEP_SEARCH_PROXY=http://127.0.0.1:9999 DEEP_SEARCH_SEARXNG_URL="$STUB_A" \
  DEEP_SEARCH_SEARXNG_B_URL="$STUB_B" "$S" "anything" --max-results 2 >"$OUT/t16.md" 2>&1; echo $?)
check dead-proxy "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q 'недоступен, работаю напрямую' "$OUT/t16.md"; check dead-proxy "фолбэк назван в отчёте" $?
grep -q '^Источники — ' "$OUT/t16.md"; check dead-proxy "работа продолжилась" $?

echo "== 17. --no-proxy (stub) =="
code=$(runs t17.md "Claude Code hooks" --only searxng --social --no-proxy --max-results 2)
check no-proxy "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -q '^прокси: выключен$' "$OUT/t17.md"; check no-proxy "прокси выключен в отчёте" $?

start_stubs cloudflare

echo "== 18. Cloudflare: оба ридера отказаны, Chrome читает (stub + live site) =="
code=$(runs t18.md "anything" --only searxng --max-results 1 --full-pages 1)
check browser-cf "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
grep -A3 '^## 1\. ' "$OUT/t18.md" | grep -q 'браузер'
check browser-cf "страница прочитана браузером" $?
grep -q 'Заблокировано, добрать браузером' "$OUT/t18.md"
check browser-cf "блок добора не нужен" $([ $? = 1 ] && echo 0 || echo 1)

echo "== 19. --no-browser -> страница уходит в блок добора (stub + live site) =="
code=$(runs t19.md "anything" --only searxng --max-results 1 --full-pages 1 --no-browser)
check no-browser "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
sed -n '/Заблокировано, добрать браузером/,$p' "$OUT/t19.md" | grep -q 'slashdot.org'
check no-browser "slashdot в блоке добора" $?

start_stubs avito

echo "== 20. Avito: прочитан или назван, никогда не молча (stub + live site) =="
# Avito limits the IP after a few visits in a row, so either outcome is
# legitimate; a silent drop is not.
code=$(runs t20.md "anything" --only searxng --max-results 1 --full-pages 1)
check avito "exit=$code" $([ "$code" = 0 ] && echo 0 || echo 1)
if grep -A3 '^## 1\. ' "$OUT/t20.md" | grep -q 'браузер'; then
  check avito "прочитан браузером" 0
else
  sed -n '/Не удалось получить/,$p' "$OUT/t20.md" | grep -q 'avito.ru'
  check avito "не прочитан, но назван с причиной" $?
fi

stop_stubs
echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
