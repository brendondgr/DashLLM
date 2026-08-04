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

If `web/frontend/dist` is missing the backend still boots — it logs
`frontend dist not found; API-only mode` and serves the API only.

## Development

```bash
./scripts/dev.sh   # backend :4000 (reload) + astro dev :4321 (API proxied)
```

The Astro dev server proxies `/admin`, `/v1`, and `/health` to `:4000`, so the
same paths work in dev and production.

## Configuration (env, prefix `RELAY_`)

Boot-time only — these are read before the DB opens. Runtime-mutable settings
(toggles, retention, proxy port) live in the DB and are edited from the
Settings screen. An `.env` file in `web/backend/` is also read.

| Var | Default | Purpose |
| --- | --- | --- |
| `RELAY_HOST` | `127.0.0.1` | bind host |
| `RELAY_PORT` | `4000` | proxy/dashboard port (also persisted via Settings) |
| `RELAY_DB_PATH` | `web/backend/data/relay.db` | SQLite database |
| `RELAY_LOG_DIR` | `web/backend/logs` | rotating JSON-lines logs |
| `RELAY_LOG_LEVEL` | `INFO` | log verbosity |
| `RELAY_ADMIN_TOKEN` | *(empty = open)* | admin plane auth (`X-Admin-Token`) |
| `RELAY_REQUIRE_CLIENT_KEY` | `false` | enforce `Authorization: Bearer` on `/v1/*` |
| `RELAY_PROBE_INTERVAL` | `15` | seconds between active health probes |
| `RELAY_PROBE_TIMEOUT` | `5` | probe timeout |
| `RELAY_UNHEALTHY_AFTER` | `3` | consecutive failures → `failed` (out of rotation) |
| `RELAY_RECOVER_AFTER` | `2` | consecutive probe successes → `healthy` |
| `RELAY_CONNECT_TIMEOUT` | `10` | upstream connect timeout |
| `RELAY_READ_TIMEOUT` | `600` | upstream read timeout (long, for slow streams) |
| `RELAY_WRITE_TIMEOUT` | `60` | upstream write timeout |
| `RELAY_MAX_CONCURRENCY` | `18` | in-flight proxy slots; also the live gauge's ceiling |
| `RELAY_FRONTEND_DIST` | `web/frontend/dist` | built dashboard to serve |

Runtime state lives in `web/backend/data/` and `web/backend/logs/` — both
gitignored. Back up the SQLite file to preserve history; note that a live
copy must include the `-wal` and `-shm` sidecar files (or be taken with
`sqlite3 .backup`), since WAL mode keeps recent writes outside the main file.

## Run on startup (systemd user service)

relay runs as a **systemd user service** so the dashboard and proxy come up on
boot. Install with:

```bash
./scripts/install-systemd.sh
```

Pass `--no-start` to install and enable without starting. The script copies
`deploy/systemd/relay.service` into `~/.config/systemd/user/`, enables user
lingering (so it starts at boot before login), frees `:4000` if a manual relay
is holding it, and removes the obsolete `vllm-tunnel-skynet.service` if an
earlier install left one behind.

`relay.service` runs `uvicorn app.main:app` on `:4000` with `Restart=always`,
ordered `After` the local llama.cpp router service. It sets `RELAY_DB_PATH` and
`RELAY_LOG_DIR` explicitly so state persists under the repo. relay does **not**
launch the model servers themselves — it just forwards HTTP to their ports.

A `relay` helper wraps the common systemctl calls:

```bash
install -m755 scripts/relay ~/.local/bin/relay
```

```bash
relay status     # service status + /health
relay restart    # e.g. after a code change
relay logs       # journalctl -f
relay enable     # start on boot (enable + linger)
```

Or directly:

```bash
systemctl --user status relay.service
journalctl --user -u relay.service -f
```

To pick up a frontend change, run `npm run build` in `web/frontend`, then
restart the service.

### Tunnels are manual, not on boot

Relay never opens an SSH tunnel by itself — no boot autostart for interactive
sessions, and the structured tunnel supervisor starts with `autostart=False`.
A tunnel-backed endpoint stores a raw `ssh -N -L …` command; you press
**Connect** on the Endpoints screen to open it. The command runs in a
pseudo-terminal, so a host-key confirmation, key passphrase, or password
prompt appears in the page and you answer it there. Nothing typed at a prompt
is stored, and terminal output is redacted before it is surfaced.

### How endpoints survive a reboot

The endpoint registry lives in SQLite, not in code. Registered endpoints
(base URLs, aliases, `model_override`, `tunnel_command`, saved tunnel routes)
are reloaded at startup, so relay comes back knowing where each alias points —
but tunnel-backed endpoints stay disconnected until you connect them.

## Security posture

- Bind to localhost or LAN. For remote access, put an authenticating proxy
  (Cloudflare Access or equivalent) in front and set `RELAY_ADMIN_TOKEN` plus
  `RELAY_REQUIRE_CLIENT_KEY`.
- The admin plane is **open by default** — an unset `RELAY_ADMIN_TOKEN` means
  no auth. That is deliberate for a localhost tool; set it before exposing the
  port anywhere.
- `GET /admin/proxy` returns the client API key in full, so it is only as
  protected as the admin plane.
- CORS is `*` while `allow_cors` is on. Turn it off, or fix the admin plane,
  before exposing the port.
- Upstream keys are stored server-side and injected on forward. The caller's
  `Authorization` header is stripped as hop-by-hop, so a client key can never
  reach an upstream and an upstream key never reaches a client.
- SSH commands are parsed into argv and exec'd directly — never through a
  shell — so a pasted command string is injection-safe. Keys are referenced by
  path; key material is never read or returned.
