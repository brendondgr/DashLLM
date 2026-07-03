# API Contract

Shared contract between `web/backend` and `web/frontend`. The frontend's
typed client (`web/frontend/src/lib/api.ts`) mirrors these shapes.

## Conventions

- All admin responses are JSON. Errors: `{"detail": "..."}` with proper HTTP
  status codes.
- Stats endpoints return the ECharts `dataset` contract:
  `{ "dimensions": [...], "source": [[...], ...] }` — the frontend binds
  series to dimensions with zero reshaping.
- Secrets never round-trip: upstream keys and client keys are returned
  masked (`…last4`); SSH key *paths* only, never key material.
- Window params: `window` ∈ `1h|24h|7d|30d|all` **or** `from`+`to`
  (unix seconds). `1h` buckets per minute; `24h`/`7d` per hour; `30d`/custom
  per hour (per day for by-day).

## Objects

### Endpoint
```json
{
  "id": "uuid", "name": "llama.cpp · local", "kind": "local|remote_direct|remote_tunnel",
  "server_type": "llama.cpp|vLLM|ollama|openai",
  "base_url": "http://127.0.0.1:7070/v1",
  "has_key": false, "tunnel_id": null,
  "priority": 100, "enabled": true, "model": "gemma-4-26B-it",
  "health": "healthy|degraded|failed|unknown",
  "ewma_latency_ms": 42.1, "last_ok_ts": 1780000000.0,
  "consecutive_fails": 0, "active": true, "share": 0.46
}
```
`model` is discovered from the endpoint's `/v1/models`. `share` is that
endpoint's fraction of requests in the last 24h. `active` marks the router's
current resolved target.

### Tunnel
```json
{
  "id": "uuid", "name": "gpu-box", "ssh_host": "gpu-box.lan", "ssh_port": 22,
  "ssh_user": "sander", "key_path": "~/.ssh/id_ed25519",
  "remote_host": "127.0.0.1", "remote_port": 8000, "local_port": 8443,
  "compress": true, "keepalive": true, "extra_opts": null,
  "status": "stopped|starting|up|error", "pid": null,
  "last_error": null, "started_at": null, "uptime_s": 0
}
```

### Settings
```json
{
  "stream_passthrough": true, "queue_requests": true, "auto_failover": true,
  "log_bodies": false, "allow_cors": true,
  "inject_stream_usage": true,
  "proxy_port": 4000, "retention_days": 30,
  "restart_required": false
}
```

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
In-flight requests appear with `state: "streaming"` and live `completion_tokens`.

## Stats shapes

| Endpoint | Shape |
| --- | --- |
| `GET /admin/stats/summary` | `{requests, errors, error_rate, prompt_tokens, completion_tokens, total_tokens, cost_usd, ttft_ms:{p50,p95}, latency_ms:{p50,p95}, tokens_per_sec:{p50}}` |
| `GET /admin/stats/volume` | `{dimensions:["time","requests","errors"], source:[["…",312,4],…]}` |
| `GET /admin/stats/tokens/timeseries` | `{dimensions:["time","input","output"], source:[…]}` |
| `GET /admin/stats/tokens/by-hour` | `{dimensions:["hour","input","output"], source:[[0,…,…],…,[23,…,…]]}` (local time, summed across days in window) |
| `GET /admin/stats/tokens/by-day` | `{dimensions:["date","input","output"], source:[["2026-07-01",…,…],…]}` |
| `GET /admin/stats/by-model` | `{dimensions:["model","requests","tokens","cost","avg_tps","errors"], source:[…]}` |
| `GET /admin/stats/by-endpoint` | `{dimensions:["endpoint","requests","input","output","errors","share"], source:[…]}` |
| `GET /admin/stats/latency` | `{dimensions:["time","ttft_p50","ttft_p95","tps_p50"], source:[…]}` |
| `GET /admin/stats/recent?limit=90` | `{rows:[RequestRow,…], counts:{all,streaming,done,error}}` |
| `GET /admin/stats/live` | `{in_flight, max_concurrency, series:[[ts_ms,n],…]}` |

## Control-plane results

- `POST /admin/endpoints/{id}/test` → `{ok, latency_ms, models[], error}`
- `POST /admin/tunnels/{id}/test` → `{ssh_ok, ssh_latency_ms, endpoint_ok, endpoint_latency_ms, models[], error}`
- `GET /admin/tunnels/{id}/command` → `{command: "ssh -N -C -L 8443:127.0.0.1:8000 -p 22 -i ~/.ssh/id_ed25519 -o … user@host"}`
- `GET /admin/proxy` → `{base_url, port, api_key_masked, api_key, uptime_s, requests_total, active_clients}` (`api_key` full value only over localhost admin plane; dashboard uses it for the reveal/copy control)
- `POST /admin/logs/frontend` accepts `{events:[{ts, level, event, detail?}]}` → `{accepted: n}`

## Auth

- Client plane `/v1/*`: `Authorization: Bearer <client key>` — only enforced
  when one or more client keys are configured; the caller's key is validated
  locally and **never forwarded**; the target endpoint's own key is injected.
- Admin plane `/admin/*`: `X-Admin-Token` header, only enforced when
  `RELAY_ADMIN_TOKEN` is set.
