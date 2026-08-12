# Data Flow

## Proxied request (hot path)

1. Client calls e.g. `POST /v1/chat/completions` on relay (`:4000`).
   `app/routes/v1.py` hands it straight to `ProxyService.handle` — there is
   no key to check. If the caller sent `Authorization: Bearer …` anyway (most
   OpenAI SDKs require the field), the last four characters are kept as a
   `client_key` label for telemetry and nothing else.
2. `GET /v1/models` short-circuits here: relay answers from its own registry
   and never forwards.
3. The body is parsed once. If `model` matches an endpoint `alias`, the
   request is pinned to that endpoint and `model` is rewritten to what that
   server runs. Stream flags are normalized: `stream_passthrough=false`
   downgrades a streaming request to non-streaming, and
   `inject_stream_usage` adds `stream_options.include_usage` when the caller
   didn't set `stream_options`, so upstreams emit real token counts.
4. The request is marked **in-flight immediately** — before it waits on a
   concurrency slot — so the live concurrency figure reflects everything
   moving through the proxy, not just what the model server has started
   answering.
5. Concurrency gate (`max_concurrency`, default 18): queue when
   `queue_requests` is on, otherwise reject with `429`.
6. `router.resolve()` returns the target from **live state**. `proxy._forward`
   opens a pooled httpx stream, strips hop-by-hop headers (including the
   caller's `Authorization`), and injects the endpoint's upstream key.
7. Response bytes are teed to the client unchanged. The first chunk stamps
   `ttft_ms`; SSE events are counted for live progress; the last 64 KiB of the
   stream is kept so the trailing `usage` object can be parsed. When upstream
   sends no usage, completion tokens are approximated as one per SSE event.
8. Failures **before the first byte** (connect errors, 5xx) signal the router
   (`consecutive_fails++`) and, if `auto_failover` is on, retry on the next
   eligible endpoint — up to 3 attempts. Alias-pinned requests skip this and
   surface the error. After the first byte, errors surface to the client.
9. A 4xx/5xx that rejects a pinned `model_override` as an unknown model
   triggers the self-heal path: clear the override, retry the same endpoint
   with the discovered model.
10. On completion a telemetry `Record` is pushed to an asyncio queue; a
    background writer inserts into SQLite and updates hourly rollups off the
    hot path. The in-flight registry drops the request — for streams the tee
    generator owns that, so the row stays live until the stream really ends.

## Dashboard reads (poll loop)

`usePoll` fetches immediately, then on an interval, and **pauses while the tab
is hidden**.

| Source | Interval | Owner |
| --- | --- | --- |
| `/admin/stats/live` | 2s | `App.tsx` |
| `/admin/endpoints` | 5s | `App.tsx` |
| `/admin/proxy` | 30s | `App.tsx` |
| `/admin/stats/recent` | 1.5s | `Requests.tsx` |
| `/admin/endpoints/tunnel-sessions` | 1.5s | `Endpoints.tsx` |
| windowed stats (`summary`, `volume`, `tokens/*`, `by-endpoint`) | on range/detail change | `Dashboard.tsx` |

Endpoint pool and live concurrency are polled once in the shell and passed
down, so screens don't duplicate those requests.

Stats responses are ECharts `dimensions`/`source` payloads; the option
builders in `src/lib/chartOptions.ts` bind them without reshaping.

## Dashboard writes (control plane)

- Hot-swap dropdown / "Set active" → `POST /admin/endpoints/{id}/activate`
  (router pin mutates live state; the next request routes there).
- "Test connection" → `POST /admin/endpoints/{id}/test`; the result is also
  fed to the health machine as a probe signal.
- Endpoint tunnel **Connect** → `POST /admin/endpoints/{id}/tunnel/connect`,
  then poll `/tunnel` and answer prompts via `/tunnel/respond` until the
  session reaches `up`.
- Saved routes → CRUD under `/admin/endpoints/{eid}/routes`, `activate` to
  swap which ssh command the endpoint uses.
- SSH form edits → `/admin/tunnels` CRUD; "Test connection" →
  `POST /admin/tunnels/{id}/test`; command box ← `GET /admin/tunnels/{id}/command`.
- Settings toggles / port / retention → `PUT /admin/settings`.
- Key regenerate → `POST /admin/proxy/key`.

## Logging and telemetry

- Every subsystem logs through `app/core/logging.py`: console + rotating file
  under `RELAY_LOG_DIR` (JSON lines).
- Every proxied request → one row in `requests` (metadata only by default).
  With `log_bodies` on, prompt and completion text also land in
  `request_bodies` as zlib-compressed BLOBs.
- The same writer accumulates `request_rollup_hourly` and
  `request_rollup_hist` so wide dashboard windows never scan raw rows.
- Router health transitions, tunnel lifecycle events, alias routing
  decisions, settings changes, and frontend UI events
  (`POST /admin/logs/frontend`) all land in the same log stream, tagged by
  subsystem.
- Retention: a housekeeping task samples live concurrency every 1.5s and, at
  most hourly, prunes `requests` older than `retention_days`. Orphaned
  `request_bodies` and old `frontend_logs` are pruned with it.
  `retention_days <= 0` means keep forever. The rollup tables are **never**
  pruned, so aggregate history stays visible in the charts after the raw rows
  behind it expire.
