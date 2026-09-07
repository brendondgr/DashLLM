#!/usr/bin/env bash
# End-to-end load check: a stub upstream, a throwaway relay, and N concurrent
# clients — all on scratch paths so nothing touches the real DB, logs or .env.
#
# It answers one question: does relay keep serving, and keep considering its
# upstream healthy, with far more requests in flight than that upstream can
# take at once?
#
#   ./scripts/stress.sh                        # 1024 concurrent, 4096 requests
#   CONCURRENCY=2048 REQUESTS=8192 ./scripts/stress.sh
#   STREAM=1 ./scripts/stress.sh               # exercise the SSE tee instead
#   BASELINE=1 ./scripts/stress.sh             # same load straight at the stub
#
# BASELINE is the honest comparison: the stub is a single Python process and
# is itself a bottleneck, so relay's numbers only mean something next to what
# the same client gets with relay taken out of the path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONCURRENCY="${CONCURRENCY:-1024}"
REQUESTS="${REQUESTS:-4096}"
WORKERS="${WORKERS:-8}"
RELAY_TEST_PORT="${RELAY_TEST_PORT:-4099}"
STUB_PORT="${STUB_PORT:-9099}"
# Held per request at the stub. Long enough that requests genuinely overlap.
export STUB_DELAY_S="${STUB_DELAY_S:-0.5}"

WORK="$(mktemp -d -t relay-stress-XXXXXX)"

# Both servers run with setsid, so each is its own process group and teardown
# can signal the group: `uv run` forks a child that survives a kill on the
# wrapper pid alone and keeps the test port bound for the next run.
cleanup() {
  local rc=$?
  local pid
  for pid in ${RELAY_PID:-} ${STUB_PID:-}; do
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in ${RELAY_PID:-} ${STUB_PID:-}; do
    kill -KILL -- "-$pid" 2>/dev/null || true
  done
  rm -rf "$WORK"
  exit $rc
}
trap cleanup EXIT

wait_for() {  # url, label
  local i
  for i in $(seq 1 150); do
    if curl -fsS -o /dev/null --max-time 1 "$1"; then return 0; fi
    sleep 0.2
  done
  echo "timed out waiting for $2 at $1" >&2
  return 1
}

echo "== stub upstream on :$STUB_PORT (holds each request ${STUB_DELAY_S}s)"
setsid uv run --project "$ROOT/web/backend" python \
  "$ROOT/utils/stub_upstream.py" --port "$STUB_PORT" \
  >"$WORK/stub.log" 2>&1 &
STUB_PID=$!
wait_for "http://127.0.0.1:$STUB_PORT/v1/models" "stub upstream"

TARGET="http://127.0.0.1:$STUB_PORT"
if [ "${BASELINE:-0}" != "1" ]; then
  echo "== relay on :$RELAY_TEST_PORT (scratch db under $WORK)"
  # Environment beats the repo .env in pydantic-settings, so the real config
  # is untouched. OpenCode registration is off: this measures the proxy, not
  # an agent server.
  export RELAY_PORT="$RELAY_TEST_PORT"
  export RELAY_HOST=127.0.0.1
  export RELAY_DB_PATH="$WORK/relay.db"
  export RELAY_LOG_DIR="$WORK/logs"
  export RELAY_PROBE_INTERVAL="${RELAY_PROBE_INTERVAL:-5}"
  export RELAY_MAX_CONCURRENCY="${RELAY_MAX_CONCURRENCY:-256}"
  export RELAY_QUEUE_LIMIT="${RELAY_QUEUE_LIMIT:-2048}"
  export OPENCODE_ENABLED=false
  setsid env -C "$ROOT/web/backend" \
    uv run uvicorn app.main:app \
      --host 127.0.0.1 --port "$RELAY_TEST_PORT" --log-level warning \
      >"$WORK/relay.log" 2>&1 &
  RELAY_PID=$!
  wait_for "http://127.0.0.1:$RELAY_TEST_PORT/health" "relay"

  curl -fsS -X POST "http://127.0.0.1:$RELAY_TEST_PORT/admin/endpoints" \
    -H 'content-type: application/json' \
    -d "{\"name\":\"stub\",\"base_url\":\"http://127.0.0.1:$STUB_PORT/v1\"}" \
    >/dev/null
  sleep 1.5  # let the first probe land, so the endpoint is healthy not unknown
  TARGET="http://127.0.0.1:$RELAY_TEST_PORT"
fi

echo "== $REQUESTS requests, $CONCURRENCY at a time, across $WORKERS clients"
STREAM_FLAG=()
[ "${STREAM:-0}" = "1" ] && STREAM_FLAG=(--stream)
set +e
uv run --project "$ROOT/web/backend" python "$ROOT/utils/stresstest.py" \
  --base "$TARGET" --workers "$WORKERS" \
  --concurrency "$CONCURRENCY" --requests "$REQUESTS" "${STREAM_FLAG[@]}"
RC=$?
set -e

echo "== stub upstream saw"
curl -fsS "http://127.0.0.1:$STUB_PORT/stub/stats"; echo
if [ "${BASELINE:-0}" != "1" ]; then
  echo -n "== ERROR lines in relay log: "
  grep -c '"level": "ERROR"' "$WORK/logs/relay.log" 2>/dev/null || echo 0
fi

exit $RC
