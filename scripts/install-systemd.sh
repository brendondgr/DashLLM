#!/usr/bin/env bash
# Install + enable the DashLLM relay as a systemd *user* service so the
# dashboard + proxy come up on boot. Idempotent: safe to re-run after edits.
#
# NOTE: relay does NOT connect any SSH tunnel on its own. Tunnel-backed
# endpoints (e.g. skynet) are connected manually from the Endpoints screen,
# where password/passphrase prompts are answered interactively. This script
# also removes the obsolete vllm-tunnel-skynet.service if a previous install
# left it behind.
#
# Installs relay.service and opencode.service (the agent upstream, configured
# from .env). opencode.service is PartOf=relay.service, so `relay start|stop|
# restart` controls both.
#
#   ./scripts/install-systemd.sh          # install, enable, start now
#   ./scripts/install-systemd.sh --no-start  # install + enable only
set -euo pipefail

START=1
[ "${1:-}" = "--no-start" ] && START=0

UNIT_DIR="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/systemd" && pwd)"

echo "installing units into $UNIT_DIR"
mkdir -p "$UNIT_DIR"
install -m 644 "$SRC/relay.service" "$UNIT_DIR/"
install -m 644 "$SRC/opencode.service" "$UNIT_DIR/"

# Keep the `relay` wrapper on PATH in step with the repo copy — it now drives
# both units, so a stale one would silently control only half of them.
BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -e "$HOME/.local/bin/relay" ]; then
  install -m 755 "$BIN/relay" "$HOME/.local/bin/relay"
  echo "refreshed $HOME/.local/bin/relay"
fi

# Remove the obsolete auto-connecting tunnel unit from earlier installs.
if [ -e "$UNIT_DIR/vllm-tunnel-skynet.service" ]; then
  echo "removing obsolete vllm-tunnel-skynet.service (tunnels are manual now)"
  systemctl --user disable --now vllm-tunnel-skynet.service 2>/dev/null || true
  rm -f "$UNIT_DIR/vllm-tunnel-skynet.service"
fi

systemctl --user daemon-reload

# Boot-before-login: user services run even when nobody is logged in.
loginctl enable-linger "$USER" 2>/dev/null || true

# opencode.service is PartOf=relay.service, so relay's start/stop/restart
# propagate to it. It is only enabled separately so it also comes up on boot.
if [ "$START" -eq 1 ]; then
  # Free the port held by a manually-started relay so the service can bind.
  echo "freeing :4000 (manual relay) if present"
  fuser -k 4000/tcp 2>/dev/null || true
  sleep 1
  systemctl --user enable --now relay.service
  # A missing/blank OPENCODE_SERVER_PASSWORD makes the wrapper exit 78 by
  # design; that is a configuration message, not an install failure.
  systemctl --user enable --now opencode.service || echo \
    "opencode.service did not start — check .env (journalctl --user -u opencode.service)"
else
  systemctl --user enable relay.service opencode.service
fi

echo
systemctl --user --no-pager status relay.service 2>&1 | sed -n '1,12p' || true
echo
echo "done. logs: journalctl --user -u relay.service -f"
