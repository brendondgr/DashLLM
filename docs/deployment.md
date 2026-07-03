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

## Security posture

- Bind to localhost or LAN; for remote access put Cloudflare Access (or
  equivalent) in front and set `RELAY_ADMIN_TOKEN` plus client keys.
- Upstream keys are stored server-side and injected on forward; the caller's
  key never reaches an upstream, upstream keys never reach a client.
- SSH tunnels are spawned as argv lists (no shell), keys referenced by path.
