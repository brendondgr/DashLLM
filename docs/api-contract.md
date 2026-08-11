# API Contract

Shared contract between `web/backend` and `web/frontend`. The authoritative
source is `web/backend/app/schemas/__init__.py`; the frontend's typed client
(`web/frontend/src/lib/types.ts`) mirrors it.

## Conventions

- All admin responses are JSON. Errors: `{"detail": "..."}` with proper HTTP
  status codes. `/v1` proxy errors instead use the OpenAI error envelope:
  `{"error": {"message", "type": "relay_proxy_error", "code"}}`.
- Stats endpoints return the ECharts `dataset` contract:
  `{"dimensions": [...], "source": [[...], ...]}` — the frontend binds series
  to dimensions with zero reshaping.
- Secrets never round-trip: upstream keys are exposed only as `has_key`; SSH
  key *paths* only, never key material; PTY output is redacted before it is
  surfaced.
- Time buckets are **local time**, and zero-filled across the window so
  category axes stay dense.

### Window and detail params

`window` ∈ `1h | 24h | 7d | 30d | 1y | all`, **or** `from`+`to` (unix
seconds). `all` spans from the earliest recorded request. Unknown values fall
back to `24h`.

`/admin/stats/volume` and `/admin/stats/tokens/timeseries` also take
`detail` ∈ `summary | detailed`, which selects the tick granularity:

| window | `summary` | `detailed` |
| --- | --- | --- |
| `1h` | per minute | per minute |
| `24h` | per hour | per 15 min |
| `7d` | per day | per hour |
| `30d` | per day | per 3 hours |
| `1y` | per month | per day |
| `all` / custom | per day / per hour | same |

All stats endpoints also accept optional `endpoint_id` and `model` filters.

## Objects

### Endpoint
```json
{
  "id": "uuid", "name": "llama.cpp · local", "alias": "local",
  "kind": "local|remote_direct|remote_tunnel",
  "server_type": "llama.cpp|vLLM|ollama|openai|opencode",
  "protocol": "openai|opencode",
  "available_models": [],
  "base_url": "http://127.0.0.1:7070/v1",
  "has_key": false,
  "tunnel_id": null,
  "tunnel_command": "ssh -N -L 9090:localhost:9090 skynet",
  "tunnel_local_port": 9090,
  "priority": 100, "weight": 1, "enabled": true,
  "model": "gemma-4-26B-it", "model_override": null,
  "health": "healthy|degraded|failed|unknown",
  "ewma_latency_ms": 42.1, "last_ok_ts": 1780000000.0,
  "consecutive_fails": 0, "active": true, "share": 0.46
}
```

`model` is discovered by the health prober (from `/v1/models`, or
`/config/providers` for `protocol: "opencode"`). `share` is that endpoint's
fraction of requests in the window. `active` marks the router's manual pin.
`kind` is inferred from the URL and tunnel fields when not supplied. Writes
accept `upstream_key` (never returned).

`protocol` is the wire protocol relay speaks to the upstream and the only
field any backend code branches on — `server_type` remains a cosmetic badge
and `kind` is topology, re-derived on every PATCH. Endpoints whose `protocol`
is not `openai` are **alias-only**: never resolved for `model: "auto"`, never
a failover target, never the default pin. See [opencode.md](opencode.md).

### Model allowlist (`available_models`)

An explicit list of model ids an endpoint serves, protocol-agnostic. Ids match
`^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$`, are deduped case-insensitively,
and must not collide with `auto`, another endpoint's alias, or another
endpoint's allowlist (`422` if they do). Every entry:

- is advertised in `GET /v1/models` as its own model, `owned_by`
  `relay:<endpoint name>`;
- **routes to that endpoint** when a client sends it as `model`, with the id
  forwarded upstream **verbatim** — it outranks `model_override`, since
  naming a model is the point of asking for it;
- narrows the endpoint's discovered catalog, so the default `model` is always
  one the operator permitted. An allowlisted id the prober did not report is
  still offered (a provider catalog can lag what the provider serves).

Send `[]` to clear it. This is what makes one OpenCode server fronting many
provider models individually addressable.

### Model-alias routing

`alias` is a unique routing name (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`,
`auto` reserved, uniqueness checked case-insensitively). A `/v1` request whose
body `model` matches an alias is **pinned to that endpoint**: relay rewrites
`model` to `model_override` (if set) or the endpoint's discovered default
before forwarding, so the client never needs to know what is actually running
there. Alias-pinned requests do **not** fail over. `model: "auto"` (or any
non-alias value) uses the normal pin / priority-failover resolution.

`GET /v1/models` is synthesized by relay: it lists `auto`, every enabled
endpoint's alias, and every id in an enabled endpoint's `available_models` —
each carrying `relay.endpoint`, `relay.health`, `relay.protocol`, and
`relay.upstream_model` metadata. Everything listed is routable: sending any of
these ids back as `model` reaches the endpoint that advertised it.

### Tunnel (structured, supervised)
```json
{
  "id": "uuid", "name": "gpu-box", "ssh_host": "gpu-box.lan", "ssh_port": 22,
  "ssh_user": "sander", "key_path": "~/.ssh/id_ed25519",
  "remote_host": "127.0.0.1", "remote_port": 8000, "local_port": 8443,
  "compress": true, "keepalive": true, "extra_opts": null, "enabled": false,
  "status": "stopped|starting|up|error", "pid": null,
  "last_error": null, "started_at": null, "uptime_s": 0
}
```

### Tunnel session (interactive, PTY-backed)
```json
{
  "endpoint_id": "uuid", "status": "idle|connecting|awaiting_input|up|error|stopped",
  "prompt": "sander@skynet's password:", "prompt_secret": true,
  "output": ["…redacted recent terminal lines…"],
  "last_error": null, "local_port": 9090, "pid": 41233, "uptime_s": 12.4
}
```

One session per endpoint. Nothing autostarts. When `status` is
`awaiting_input`, POST the answer to `.../respond`; `prompt_secret` means the
UI should mask the field. Sessions for structured tunnels reuse the same
manager under the key `t:<tunnel_id>`, and `endpoint_id` carries that key.

### SSH config host
```json
{
  "alias": "skynet", "hostname": "10.0.0.4", "user": "sander", "port": 22,
  "identity_files": ["~/.ssh/id_ed25519"], "identity_file": "~/.ssh/id_ed25519",
  "identity_explicit": true, "proxyjump": null
}
```
Resolved by running `ssh -G <alias>` — the same resolution ssh itself
performs. `identity_explicit` distinguishes "this host has a configured key"
from "ssh will try every default key".

### Tunnel route
```json
{
  "id": "uuid", "endpoint_id": "uuid", "label": "via bastion",
  "command": "ssh -N -L 9090:localhost:9090 skynet-alt",
  "local_port": 9090, "active": false, "created_ts": 1780000000.0
}
```
A saved candidate ssh command for an endpoint. Activating one copies its
`command`/`local_port` onto the endpoint, so a flaky route can be swapped for
a working one without touching the endpoint's alias or `base_url`.

### Settings
```json
{
  "stream_passthrough": true, "queue_requests": true, "auto_failover": true,
  "log_bodies": false, "allow_cors": true, "inject_stream_usage": true,
  "proxy_port": 4000, "retention_days": 30, "restart_required": false
}
```
`PUT` accepts any subset. `proxy_port` is persisted but only takes effect on
restart, which is what `restart_required` reports.

### Request row (`/admin/stats/recent`)
```json
{
  "id": "req_ab12cd34", "ts": 1780000000.0, "endpoint_id": "uuid",
  "endpoint_name": "llama.cpp · local", "route": "chat.completions",
  "model": "gemma-4-26B-it", "stream": true,
  "status": 200, "ok": true, "state": "streaming|done|error", "error": null,
  "prompt_tokens": 214, "completion_tokens": 512, "total_tokens": 726,
  "ttft_ms": 84.2, "latency_ms": 5230.1, "tokens_per_sec": 99.4,
  "cost_usd": 0.0, "temperature": 0.7, "max_tokens": 1024
}
```
In-flight requests are prepended with `state: "streaming"`, live
`completion_tokens`, and nulls for everything not yet known.

## Stats shapes

| Endpoint | Shape |
| --- | --- |
| `GET /admin/stats/summary` | `{requests, errors, error_rate, prompt_tokens, completion_tokens, total_tokens, cost_usd, ttft_ms:{p50,p95}, latency_ms:{p50,p95}, tokens_per_sec:{p50}}` |
| `GET /admin/stats/volume` | `{dimensions:["time","requests","errors"], source:[["…",312,4],…]}` |
| `GET /admin/stats/tokens/timeseries` | `{dimensions:["time","input","output"], source:[…]}` |
| `GET /admin/stats/tokens/by-hour` | `{dimensions:["hour","input","output"], source:[[0,…,…],…,[23,…,…]]}` (local time, summed across days in window) |
| `GET /admin/stats/tokens/by-day` | `{dimensions:["date","input","output"], source:[["2026-07-01",…,…],…]}` |
| `GET /admin/stats/by-model` | `{dimensions:["model","requests","tokens","cost","avg_tps","errors"], source:[…]}` |
| `GET /admin/stats/by-endpoint` | `{dimensions:["endpoint","requests","input","output","errors","share","endpoint_id"], source:[…]}` |
| `GET /admin/stats/latency` | `{dimensions:["time","ttft_p50","ttft_p95","tps_p50"], source:[…]}` |
| `GET /admin/stats/recent?limit=90` | `{rows:[RequestRow,…], counts:{all,streaming,done,error}}` |
| `GET /admin/stats/live` | `{in_flight, max_concurrency, series:[[ts_ms,n],…]}` |

Percentiles in `summary` are exact (computed from raw rows) for spans up to
~24h and approximate (from stored histograms) for wider spans — see
[architecture.md](architecture.md#storage-and-scale).

## Control-plane results

- `POST /admin/endpoints/{id}/test` → `{ok, latency_ms, models[], error}`.
  The result is also fed into the health state machine as a probe signal.
- `GET /admin/endpoints/health` → `{endpoints:[{id, name, health,
  consecutive_fails, ewma_latency_ms, model}, …], resolved: "<endpoint id>"}`.
- `GET/PUT /admin/router` → `{policy, pinned_id, resolved_id, resolved_name}`.
- `POST /admin/endpoints/{eid}/routes/{rid}/test` → `{ok, latency_ms, error}`
  (quick probe; does not open a real tunnel).
- `POST /admin/tunnels/{id}/test` → `{ssh_ok, ssh_latency_ms, endpoint_ok,
  endpoint_latency_ms, models[], error}`.
- `GET /admin/tunnels/{id}/command` → `{command: "ssh -N -C -L 8443:127.0.0.1:8000 -p 22 -i ~/.ssh/id_ed25519 -o … user@host"}`.
  Cosmetic rendering of the argv list that is actually executed.
- `GET /admin/proxy` → `{base_url, port, api_key, api_key_masked, uptime_s,
  requests_total, active_clients, db_size_bytes}`. `db_size_bytes` sums the
  main DB plus its `-wal` and `-shm` files.
- `POST /admin/proxy/key` → `{api_key, api_key_masked}` (regenerates).
- `POST /admin/logs/frontend` accepts `{events:[{ts, level, event, detail?}]}`
  (max 200 per batch) → `{accepted: n}`.
- `GET /admin/logs/frontend?limit=100` → recent ingested UI events.

## Auth

- **Client plane `/v1/*`** — `Authorization: Bearer <client key>`, enforced
  only when `RELAY_REQUIRE_CLIENT_KEY` is true. The caller's key is validated
  locally against the stored proxy key and **never forwarded**; the target
  endpoint's own key is injected instead. `Authorization` is in the
  hop-by-hop strip list, so it cannot leak upstream even when auth is off.

  The proxy key is generated on first boot and persisted. Set `RELAY_API_KEY`
  to pin a known value instead — it wins over the stored one at every boot, so
  a key baked into client configs keeps matching. Regenerating from the
  dashboard still works but only holds until the next restart, and logs a
  warning saying so.
- **Admin plane `/admin/*`** — an admin session cookie, or the legacy
  `X-Admin-Token` header (kept for scripts). Split in two by guard:
  `admin_guard` on endpoints/tunnels/settings/proxy/users, `user_guard` on
  `/admin/stats/*` and `POST /admin/logs/frontend`.

  With no admin credential configured, the admin plane stays open on a
  **loopback** bind (the local-dev default) and `create_app` **refuses to
  start** on any other bind.
- **Dashboard sessions** — `relay_session` (HttpOnly, `SameSite=Lax`, `Secure`
  unless `RELAY_COOKIE_SECURE=0`) plus a readable `relay_csrf`. Mutating
  requests made with the cookie must echo the CSRF value in `X-Relay-CSRF`;
  header-token callers are exempt. Only SHA-256 hashes of session tokens are
  stored.

## Auth routes

- `GET /auth/status` → `{authenticated, signup_enabled, me}`. The only route
  the login screen may call anonymously.
- `POST /auth/login` `{username, password}` → `MeOut`, sets session cookies.
  401 on failure, 429 after 10 failed attempts from one IP in 15 minutes.
- `POST /auth/signup` `{username, password, code}` → `{api_key,
  api_key_prefix}` (201). 403 unless `RELAY_SIGNUP_CODE` is set and matches;
  409 on a duplicate or admin username; password minimum 12 characters. **The
  cleartext key is returned exactly once** — only its hash is stored.
- `POST /auth/logout` → 204, revokes the session server-side.
- `GET /auth/me` → `{kind, username, user_id, private, api_key_prefix, csrf}`.
- `PATCH /auth/me` `{private}` → `MeOut`. 400 for the admin.
- `POST /auth/me/key` → a fresh `{api_key, api_key_prefix}`; the old key stops
  working immediately.
- `GET /admin/users` → `UserOut[]` (admin only).
- `PATCH /admin/users/{id}` `{private?, disabled?}` → `UserOut`. Disabling an
  account also kills its live sessions.

## Per-user attribution and stats scope

`/v1` requests carrying a user's `rk_` key are stamped with `user_id`. An
unknown or missing key is **not** an error — the request proceeds and stays
unattributed, which also denies an attacker an oracle for probing live keys.
Credential-looking body keys (`user_pass`, `password`, `api_key`, …) are
stripped before telemetry capture and before the body is forwarded upstream.

`/admin/stats/*` accepts `scope` ∈ {`all` (default), `me`}:

- Aggregate endpoints honour `scope=me` by filtering on `user_id`. A user's
  traffic still counts toward the shared `scope=all` totals — privacy here
  means unattributed, not excluded.
- `/recent` and `/live` are **always** self-scoped for a non-admin, whatever
  `scope` says.
- A scoped query never reads the rollup tables (they carry no user dimension),
  so per-user history is bounded by `retention_days`.
