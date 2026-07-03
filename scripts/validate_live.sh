#!/usr/bin/env bash
# End-to-end validation against a running relay (default http://127.0.0.1:4000)
# and whatever model servers are registered. Exits non-zero on failure.
set -uo pipefail
BASE="${RELAY_BASE:-http://127.0.0.1:4000}"
FAILURES=0

check() { # check <label> <cmd...>
  local label="$1"; shift
  if "$@" > /dev/null 2>&1; then
    echo "ok   | $label"
  else
    echo "FAIL | $label"
    FAILURES=$((FAILURES + 1))
  fi
}

json() { curl -sf -m 10 "$BASE$1"; }

echo "== relay @ $BASE =="
check "GET /health" curl -sf -m 5 "$BASE/health"
check "GET /admin/endpoints" json /admin/endpoints
check "GET /v1/models (alias catalog)" json /v1/models
check "GET /admin/stats/summary" json "/admin/stats/summary?window=24h"
check "GET /admin/stats/volume" json "/admin/stats/volume?window=1h"
check "GET /admin/stats/tokens/by-hour" json "/admin/stats/tokens/by-hour?window=30d"
check "GET /admin/stats/tokens/by-day" json "/admin/stats/tokens/by-day?window=7d"
check "GET /admin/stats/by-endpoint" json "/admin/stats/by-endpoint?window=24h"
check "GET /admin/stats/live" json /admin/stats/live
check "dashboard served at /" curl -sf -m 5 "$BASE/"

echo "== proxied completion (model=auto, non-stream) =="
RESP=$(curl -sf -m 120 "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say OK"}],"max_tokens":16}')
if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("usage",{}).get("total_tokens",0)>0' 2>/dev/null; then
  echo "ok   | completion with usage through proxy"
else
  echo "FAIL | completion through proxy: $RESP"
  FAILURES=$((FAILURES + 1))
fi

echo "== proxied completion (stream, usage injected) =="
STREAM=$(curl -sfN -m 120 "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Count to 3"}],"max_tokens":48}')
if echo "$STREAM" | grep -q '"usage"' && echo "$STREAM" | grep -q 'data: \[DONE\]'; then
  echo "ok   | SSE stream with trailing usage + [DONE]"
else
  echo "FAIL | SSE stream missing usage/[DONE]"
  FAILURES=$((FAILURES + 1))
fi

echo "== alias routing (every advertised alias) =="
ALIASES=$(json /v1/models | python3 -c '
import json, sys
for m in json.load(sys.stdin)["data"]:
    if m["id"] != "auto" and m.get("relay", {}).get("health") == "healthy":
        print(m["id"])')
for a in $ALIASES; do
  RESP=$(curl -sf -m 120 "$BASE/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$a\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}],\"max_tokens\":16}")
  if echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("model")' 2>/dev/null; then
    MODEL=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["model"])')
    echo "ok   | model=$a routed and rewritten -> $MODEL"
  else
    echo "FAIL | alias $a"
    FAILURES=$((FAILURES + 1))
  fi
done

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILURES CHECK(S) FAILED"
  exit 1
fi
