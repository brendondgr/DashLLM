#!/usr/bin/env bash
# Start backend (:4000) and frontend dev server (:4321) together.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT

(cd "$ROOT/web/backend" && uv run uvicorn app.main:app --port "${RELAY_PORT:-4000}" --reload) &
(cd "$ROOT/web/frontend" && npm run dev) &

wait
