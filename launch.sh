#!/usr/bin/env bash
# launch.sh — bring up relay and an OpenCode agent server together, configured
# entirely from .env, with the agent endpoint registered in relay automatically.
#
#   cp .env.example .env    # set OPENCODE_SERVER_PASSWORD at minimum
#   ./launch.sh
#
#   ./launch.sh --list-models   # print the models your OpenCode server offers
#                               # (for OPENCODE_MODELS) and exit
#   ./launch.sh --takeover      # stop the relay systemd service first
#
# Both processes run in the foreground as children of this script: Ctrl-C
# stops both. For a boot-time setup use the systemd unit instead
# (scripts/install-systemd.sh) — it reads the same .env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

LIST_MODELS=0
TAKEOVER=0
for arg in "$@"; do
  case "$arg" in
    --list-models) LIST_MODELS=1 ;;
    --takeover) TAKEOVER=1 ;;
    # the header comment block above is the help text
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ---- config -------------------------------------------------------------
if [ ! -f .env ]; then
  echo "no .env found. Start from the template:" >&2
  echo "  cp .env.example .env" >&2
  exit 1
fi
# set -a exports everything sourced, so the values reach both child processes.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

RELAY_PORT="${RELAY_PORT:-4000}"
OPENCODE_ENABLED="${OPENCODE_ENABLED:-1}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-opencode}"
OPENCODE_PROJECT_DIR="${OPENCODE_PROJECT_DIR:-$ROOT}"
export RELAY_PORT OPENCODE_PORT OPENCODE_SERVER_USERNAME

say() { printf '\033[1;33m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✕\033[0m %s\n' "$*" >&2; exit 1; }

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for() { # wait_for <url> <label> [curl args...]
  local url="$1" label="$2"; shift 2
  for _ in $(seq 1 60); do
    curl -fsS --max-time 2 "$@" "$url" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  die "$label never became reachable at $url"
}

# ---- preflight ----------------------------------------------------------
if [ "$OPENCODE_ENABLED" = "1" ]; then
  command -v opencode >/dev/null || die "opencode is not on PATH (https://opencode.ai)"
  [ -n "${OPENCODE_SERVER_PASSWORD:-}" ] || die \
    "OPENCODE_SERVER_PASSWORD is empty in .env — without one, anyone who can
   reach :$OPENCODE_PORT can run shell commands in $OPENCODE_PROJECT_DIR"
  [ -d "$OPENCODE_PROJECT_DIR" ] || die \
    "OPENCODE_PROJECT_DIR does not exist: $OPENCODE_PROJECT_DIR"
  port_busy "$OPENCODE_PORT" && die \
    "port $OPENCODE_PORT is already in use (OPENCODE_PORT)"
fi

if [ "$LIST_MODELS" = "0" ] && port_busy "$RELAY_PORT"; then
  if [ "$TAKEOVER" = "1" ]; then
    say "stopping the relay service to free :$RELAY_PORT"
    systemctl --user stop relay.service 2>/dev/null || true
    sleep 1
  else
    die "port $RELAY_PORT is already in use.
   If that is the relay systemd service, either use it as-is, or run:
     ./launch.sh --takeover"
  fi
fi

# ---- children -----------------------------------------------------------
pids=()
cleanup() {
  local status=$?
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

if [ "$OPENCODE_ENABLED" = "1" ]; then
  say "starting opencode serve on :$OPENCODE_PORT (project: $OPENCODE_PROJECT_DIR)"
  # `cd` in a subshell: `opencode serve` binds to its launch directory, which
  # is the project its bash/edit/write tools act on.
  (cd "$OPENCODE_PROJECT_DIR" && exec opencode serve --port "$OPENCODE_PORT") &
  pids+=("$!")
  wait_for "http://127.0.0.1:$OPENCODE_PORT/global/health" "opencode" \
    -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD"
  say "opencode healthy"
fi

if [ "$LIST_MODELS" = "1" ]; then
  echo
  echo "Models offered by this OpenCode server (for OPENCODE_MODELS in .env):"
  curl -fsS -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD" \
    "http://127.0.0.1:$OPENCODE_PORT/config/providers" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
for p in d.get("providers", []):
    for mid in (p.get("models") or {}):
        print(f"  {p[\"id\"]}/{mid}")
print("\ndefault:", d.get("default"))'
  exit 0
fi

say "starting relay on :$RELAY_PORT"
(cd "$ROOT/web/backend" && exec uv run uvicorn app.main:app \
  --host 127.0.0.1 --port "$RELAY_PORT") &
pids+=("$!")
wait_for "http://127.0.0.1:$RELAY_PORT/health" "relay"
say "relay healthy"

# ---- register the endpoint ----------------------------------------------
if [ "$OPENCODE_ENABLED" = "1" ]; then
  say "registering the agent endpoint"
  python3 "$ROOT/scripts/register_opencode.py" \
    || die "endpoint registration failed (relay is still running above)"
fi

# ---- summary ------------------------------------------------------------
api_key=$(curl -fsS ${RELAY_ADMIN_TOKEN:+-H "X-Admin-Token: $RELAY_ADMIN_TOKEN"} \
  "http://127.0.0.1:$RELAY_PORT/admin/proxy" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])' 2>/dev/null || echo "?")

cat <<EOF

  dashboard   http://127.0.0.1:$RELAY_PORT
  base_url    http://127.0.0.1:$RELAY_PORT/v1
  api key     $api_key$([ "${RELAY_REQUIRE_CLIENT_KEY:-0}" = "1" ] || echo "   (not enforced — RELAY_REQUIRE_CLIENT_KEY=0)")

  try it:
    curl http://127.0.0.1:$RELAY_PORT/v1/chat/completions \\
      -H "Authorization: Bearer $api_key" \\
      -H 'Content-Type: application/json' \\
      -d '{"model": "${OPENCODE_ALIAS:-agent}", "messages": [{"role": "user", "content": "hi"}]}'

  Ctrl-C stops both servers.

EOF

wait
