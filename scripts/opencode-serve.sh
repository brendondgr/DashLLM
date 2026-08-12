#!/usr/bin/env bash
# ExecStart target for opencode.service.
#
# A wrapper rather than a bare ExecStart because systemd cannot expand
# variables in WorkingDirectory= or resolve `opencode` on its minimal PATH, and
# the project directory + port both live in .env. Sourcing .env here also keeps
# one config mechanism instead of two (this script and EnvironmentFile
# disagreeing about quoting would be a bad afternoon).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

# opencode installs to ~/.opencode/bin, which is not on a systemd user unit's
# default PATH.
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"

if ! command -v opencode >/dev/null; then
  echo "opencode is not on PATH (looked in ~/.opencode/bin)" >&2
  exit 127
fi

if [ "${OPENCODE_ENABLED:-1}" != "1" ]; then
  echo "OPENCODE_ENABLED is not 1 in $ROOT/.env; not starting" >&2
  exit 0
fi

# Load or mint the Basic credentials. Never served unauthenticated: this
# process runs shell commands in OPENCODE_PROJECT_DIR for anyone who reaches
# the port. relay reads the same file, so the two agree without configuration.
# shellcheck disable=SC1091
. "$ROOT/scripts/opencode-auth.sh"

PROJECT_DIR="${OPENCODE_PROJECT_DIR:-$ROOT}"
[ -d "$PROJECT_DIR" ] || { echo "OPENCODE_PROJECT_DIR does not exist: $PROJECT_DIR" >&2; exit 78; }

cd "$PROJECT_DIR"
echo "opencode serve --port ${OPENCODE_PORT:-4096} in $PROJECT_DIR"
exec opencode serve --port "${OPENCODE_PORT:-4096}"
