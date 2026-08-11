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
| `RELAY_HOST` | `127.0.0.1` | bind host; read from `.env` by the systemd wrapper too |
| `RELAY_PORT` | `4000` | proxy/dashboard port (also persisted via Settings) |
| `RELAY_DB_PATH` | `web/backend/data/relay.db` | SQLite database |
| `RELAY_LOG_DIR` | `web/backend/logs` | rotating JSON-lines logs |
| `RELAY_LOG_LEVEL` | `INFO` | log verbosity |
| `RELAY_ADMIN_TOKEN` | *(empty)* | legacy shared secret for scripts (`X-Admin-Token`) |
| `RELAY_ADMIN_USER` | `admin` | admin login name |
| `RELAY_ADMIN_PASSWORD_HASH` | *(empty)* | **preferred** admin credential; generate with the command below |
| `RELAY_ADMIN_PASSWORD` | *(empty)* | plaintext admin password; works, but warns at boot |
| `RELAY_SIGNUP_CODE` | *(empty = signup off)* | invite code required to register an account |
| `RELAY_SESSION_TTL_HOURS` | `12` | dashboard session lifetime |
| `RELAY_COOKIE_SECURE` | `true` | send session cookies only over HTTPS |
| `RELAY_TRUSTED_PROXY` | `false` | honour `X-Forwarded-For` when rate-limiting logins |
| `RELAY_REQUIRE_CLIENT_KEY` | `false` | enforce `Authorization: Bearer` on `/v1/*` |
| `RELAY_API_KEY` | *(empty = generated)* | pin the client key instead of generating one on first boot |
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

## Public deployment

### TLS is a prerequisite, not a hardening step

Passwords, session cookies, and proxy keys all cross the wire on every
request. Over plain HTTP on a public host they are readable by anyone on the
path, and no amount of application-side design changes that. Terminate TLS in
a reverse proxy in front of relay and leave `RELAY_COOKIE_SECURE` at its
default.

### Set an admin credential

```bash
cd web/backend && uv run python -m app.services.users hash 'your admin password'
```

Put the output in `RELAY_ADMIN_PASSWORD_HASH`. Relay **refuses to start** on a
non-loopback bind with no admin credential configured — a misconfigured public
deploy fails loudly at boot instead of quietly serving your upstream keys.

`RELAY_ADMIN_TOKEN` also satisfies that check, but it is a credential for
*scripts* only: it authenticates an `X-Admin-Token` header, and a browser has
no way to send one. A token-only config boots and warns that dashboard login
is unavailable. Set the password hash as well if you want to sign in.

**Upgrading an existing deployment:** if you already run with
`RELAY_HOST=0.0.0.0` and no admin credential, relay will not start after this
change until you set one — systemd retries, and every request during the loop
is refused. Set the hash before restarting.

### Decide who may register

`RELAY_SIGNUP_CODE` is empty by default, which disables signup entirely. Set
it to hand out accounts; each one carries a proxy key to your hardware, so
open registration is a compute giveaway. Disable an account from the Users
list — that also kills its live sessions.

### Behind a reverse proxy

Set `RELAY_TRUSTED_PROXY=1` **only** when relay really is behind a proxy you
control. Otherwise any client can forge `X-Forwarded-For` and get a fresh
login-attempt budget per request.

## Security posture

- With no admin credential configured, the admin plane is open on a
  **loopback** bind only — deliberate for a localhost tool — and startup fails
  on any other bind. See above.
- Two planes: `admin_guard` (endpoints, tunnels, settings, proxy info, users)
  and `user_guard` (stats, self-scoped for non-admins). Hiding tabs in the UI
  is cosmetic; the guards are the enforcement point.
- Sessions are opaque random tokens; only SHA-256 hashes are stored, so a DB
  dump does not yield session takeover. Passwords are scrypt; per-user proxy
  keys are 256-bit and stored hashed, so a leaked DB yields nothing usable
  against `/v1`.
- Logins are rate-limited to 10 failures per IP per 15 minutes, counted in the
  DB so a restart is not a free reset.
- Cookie-authenticated mutations require a matching `X-Relay-CSRF` header on
  top of `SameSite=Lax`.
- `GET /admin/proxy` returns the client API key in full, so it is only as
  protected as the admin plane.
- CORS `*` applies to `/v1` only. The cookie-authenticated admin and auth
  planes never carry CORS headers.
- Credential-looking keys in a proxied request body (`user_pass`, `password`,
  `api_key`, …) are stripped before the body is stored or forwarded, so a
  client that improvises them cannot write a password to `request_bodies` or
  ship it to a model server.
- Upstream keys are stored server-side and injected on forward. The caller's
  `Authorization` header is stripped as hop-by-hop, so a client key can never
  reach an upstream and an upstream key never reaches a client.
- SSH commands are parsed into argv and exec'd directly — never through a
  shell — so a pasted command string is injection-safe. Keys are referenced by
  path; key material is never read or returned.
