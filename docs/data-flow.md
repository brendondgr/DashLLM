# Data Flow

## Proxied request (hot path)

1. Client calls `POST /v1/chat/completions` on relay (`:4000`).
2. `security.py` validates the client key locally (if configured); the
   inbound `Authorization` header is stripped.
3. `router.resolve()` returns the target endpoint from **live state**
   (manual pin, else highest-priority healthy endpoint).
4. `proxy.forward()` opens a pooled httpx stream to the target, injecting the
   endpoint's upstream key. For streams, `stream_options.include_usage=true`
   is injected when the caller didn't set it (settings toggle).
5. Response bytes are teed to the client unchanged. First chunk stamps
   `ttft_ms`; the SSE tail is parsed for the trailing `usage` object.
6. Failures before the first byte: passive failure signal to the router
   (`consecutive_fails++`), and — if auto-failover is on — one retry on the
   next healthy endpoint. After first byte: error surfaces to the client.
7. On completion a telemetry `Record` is pushed to an asyncio queue; a
   background writer inserts into SQLite off the hot path. The in-flight
   registry drops the request and the concurrency series ticks down.

## Dashboard reads (poll loop)

- The React island polls (`usePoll`): `/admin/stats/live` + `/admin/stats/recent`
  every ~2s; windowed stats (`summary`, `volume`, `tokens/*`, `by-endpoint`)
  every ~5s or on range change; `/admin/endpoints` every ~5s.
- Stats responses are ECharts `dimensions`/`source` payloads; chart option
  builders in `src/lib/chartOptions.ts` bind them without reshaping.

## Dashboard writes (control plane)

- Hot-swap dropdown / "Set active" → `POST /admin/endpoints/{id}/activate`
  (router pin mutates live state; next request routes there).
- "Test connection" → `POST /admin/endpoints/{id}/test` (live probe result).
- SSH form edits → `PUT`-style upsert via `/admin/tunnels`; "Test connection"
  → `POST /admin/tunnels/{id}/test`; command box ← `GET /admin/tunnels/{id}/command`.
- Settings toggles / port / retention → `PUT /admin/settings`.
- Key regenerate → `POST /admin/proxy/key`.

## Logging & telemetry

- Every subsystem logs through `app/core/logging.py`: console + rotating file
  `web/backend/logs/relay.log` (JSON lines). Uvicorn access logs included.
- Every proxied request → one row in `requests` (metadata only by default;
  bodies only when `log_bodies` is enabled).
- Router health transitions, tunnel lifecycle events, settings changes, and
  frontend UI events (`POST /admin/logs/frontend`) all land in the same log
  stream, tagged by subsystem.
- Retention: rows older than `retention_days` are pruned by a periodic task.
