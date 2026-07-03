#!/usr/bin/env bash
# Install + enable the DashLLM systemd *user* services so relay and its remote
# vLLM tunnel come up on boot. Idempotent: safe to re-run after editing units.
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
install -m 644 "$SRC/vllm-tunnel-skynet.service" "$UNIT_DIR/"
install -m 644 "$SRC/relay.service" "$UNIT_DIR/"

systemctl --user daemon-reload

# Boot-before-login: user services run even when nobody is logged in.
loginctl enable-linger "$USER" 2>/dev/null || true

if [ "$START" -eq 1 ]; then
  # Free ports held by manually-started instances so the services can bind.
  echo "freeing :9090 (manual ssh tunnel) and :4000 (manual relay) if present"
  fuser -k 9090/tcp 2>/dev/null || true
  fuser -k 4000/tcp 2>/dev/null || true
  sleep 1
  systemctl --user enable --now vllm-tunnel-skynet.service
  systemctl --user enable --now relay.service
else
  systemctl --user enable vllm-tunnel-skynet.service relay.service
fi

echo
systemctl --user --no-pager status \
  vllm-tunnel-skynet.service relay.service 2>&1 | sed -n '1,12p' || true
echo
echo "done. logs: journalctl --user -u relay.service -f"
