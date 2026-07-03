# Deployment

## Local / single-node (primary target)

Production is one process: FastAPI serves the API **and** the built dashboard.

```bash
cd web/frontend && npm run build          # → web/frontend/dist
cd web/backend && uv sync && \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 4000
```

- Dashboard: `http://127.0.0.1:4000/`
- OpenAI-compatible endpoint: `http://127.0.0.1:4000/v1`

## Development

```bash
./scripts/dev.sh   # backend :4000 (reload) + astro dev :4321 (API proxied)
```

## Configuration (env, prefix `RELAY_`)

| Var | Default | Purpose |
| --- | --- | --- |
| `RELAY_PORT` | `4000` | proxy/dashboard port (also persisted via Settings) |
| `RELAY_DB_PATH` | `web/backend/data/relay.db` | SQLite database |
| `RELAY_LOG_DIR` | `web/backend/logs` | rotating JSON-lines logs |
| `RELAY_ADMIN_TOKEN` | *(empty = open)* | admin plane auth (`X-Admin-Token`) |
| `RELAY_PROBE_INTERVAL` | `15` | seconds between active health probes |
| `RELAY_LOG_LEVEL` | `INFO` | log verbosity |

Runtime state lives in `web/backend/data/` and `web/backend/logs/` — both
gitignored; back up the SQLite file to preserve history.

## Run on startup (systemd user services)

relay and its dependencies run as **systemd user services** so the stack comes
up on boot. Install with:

```bash
./scripts/install-systemd.sh          # install + enable + start now
./scripts/install-systemd.sh --no-start
```

This installs two units from `deploy/systemd/` into
`~/.config/systemd/user/` and enables user lingering (so they start at boot
before login):

| Unit | Purpose |
| --- | --- |
| `vllm-tunnel-skynet.service` | `ssh -N -L 127.0.0.1:9090:localhost:9090 skynet-alt` — the SSH tunnel to the remote vLLM, replacing the manual command so `:9090` returns on boot. Uses the running ssh-agent (`SSH_AUTH_SOCK=%t/ssh-agent.socket`), `ExitOnForwardFailure`, keepalives, `BatchMode`. |
| `relay.service` | `uvicorn app.main:app` on `:4000`; ordered `After` the llama.cpp router and the vLLM tunnel; `Restart=always`. |

The local model server (`:7070`) is already its own service
(`llamacpp-router.service`), so relay only `After`s it.

**How the connection survives a reboot:** relay's endpoint registry lives in
SQLite (`web/backend/data/relay.db`), not in code. Registered endpoints (their
base URLs, aliases, `model_override`) are reloaded at startup, so relay comes
back already knowing `local → :7070` and `skynet → :9090`. The health prober
re-establishes their live status within a probe cycle. relay does **not**
launch the model servers themselves — llama.cpp and the vLLM tunnel are the
units above; relay just forwards HTTP to their ports.

Manage / inspect:

```bash
systemctl --user status relay.service vllm-tunnel-skynet.service
journalctl --user -u relay.service -f
systemctl --user restart relay.service        # e.g. after a code change
```

To rebuild the dashboard after a frontend change, run `npm run build` in
`web/frontend` then restart `relay.service`.

## Security posture

- Bind to localhost or LAN; for remote access put Cloudflare Access (or
  equivalent) in front and set `RELAY_ADMIN_TOKEN` plus client keys.
- Upstream keys are stored server-side and injected on forward; the caller's
  key never reaches an upstream, upstream keys never reach a client.
- SSH tunnels are spawned as argv lists (no shell), keys referenced by path.
