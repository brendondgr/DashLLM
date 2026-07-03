# Architecture

## App mode

**Mode G — API plus separate frontend** (see `web-interfaces` structure
standard). The backend is a FastAPI (ASGI, async) service; the frontend is an
Astro site with a single React island rendering the dashboard, charts via
Apache ECharts. In production FastAPI serves the built `web/frontend/dist`
statically, so the whole product is one process on one port (default `:4000`).

## Surfaces

The backend exposes three surfaces:

1. **`/v1/*`** — OpenAI-compatible endpoint the *world* calls (drop-in
   replacement). Streaming and non-streaming, fully instrumented.
2. **`/admin/endpoints/*`, `/admin/tunnels/*`, `/admin/settings`,
   `/admin/router`, `/admin/proxy`, `/admin/logs`** — control plane the
   *dashboard* calls.
3. **`/admin/stats/*`** — read API the *dashboard* polls; returns ECharts
   `dataset`-friendly payloads (`dimensions` + `source`).

## Structural idea

Requests resolve their upstream target **at call time from live router
state**. Hot-swapping an endpoint or failing one over is just mutating that
state; in-flight requests are untouched and the next request picks up the
change with no restart.

```
                        ┌─────────────────────────────────────────────┐
  OpenAI SDK / curl ──▶ │  /v1/chat/completions  /v1/embeddings  ...  │
                        │                 PROXY LAYER                 │
                        │   • auth (client keys)                      │
                        │   • resolve target via ROUTER (live state)  │
                        │   • stream tee: capture TTFT + usage        │
                        │   • record telemetry off the hot path       │
                        └───────┬───────────────────────────┬─────────┘
                                │                           │
                   ┌────────────▼─────────┐       ┌─────────▼──────────┐
                   │       ROUTER         │       │  TELEMETRY WRITER  │
                   │ endpoint registry +  │       │  async queue →     │
                   │ health state + policy│       │  requests table    │
                   └───┬──────────┬───────┘       └─────────┬──────────┘
       health probes   │          │ passive failure         │
       (background)    │          │ signals                 ▼
             ┌─────────▼──┐   ┌───▼─────────┐      ┌────────────────┐
             │ endpoint A │   │ endpoint B  │      │   STATS API    │◀── dashboard
             │  (local)   │   │ (ssh tunnel)│      │ aggregates +   │    polls
             └────────────┘   └──────┬──────┘      │ ECharts shapes │
                                     │             └────────────────┘
                         ┌───────────▼──────────┐
                         │  SSH TUNNEL MANAGER  │
                         │ subprocess ssh -N -L │
                         └──────────────────────┘
```

## Subsystems (module map)

| Subsystem | Module | Notes |
| --- | --- | --- |
| App wiring / lifespan | `web/backend/app/main.py` | boots router, health prober, tunnel supervisor, telemetry writer; serves frontend dist |
| Config | `app/config.py` | pydantic-settings, `RELAY_*` env vars |
| Logging | `app/core/logging.py` | rotating file + console; every subsystem logs |
| Storage | `app/db.py` | SQLite (WAL), schema written Postgres-compatible |
| Proxy | `app/services/proxy.py` + `app/routes/v1.py` | streaming tee, TTFT, usage capture, retry pre-first-token |
| Router | `app/services/router.py` | registry, health state machine, manual/priority policies |
| Health | `app/services/health.py` | active `GET /v1/models` prober with hysteresis |
| Tunnels | `app/services/tunnels.py` + `app/routes/admin_tunnels.py` | argv `ssh` child processes, supervisor, two-level test |
| Telemetry | `app/services/telemetry.py` | asyncio queue, off-hot-path writes |
| Stats | `app/services/stats.py` + `app/routes/admin_stats.py` | ECharts-shaped aggregates |
| Control plane | `app/routes/admin_endpoints.py`, `admin_settings.py`, `admin_logs.py`, `admin_proxy.py` | CRUD + live state |
| Frontend shell | `web/frontend/src/components/App.tsx` | sidebar, header, hot-swap, screen switch |
| Screens | `web/frontend/src/components/screens/*.tsx` | 1:1 port of the prototype |
| Charts | `web/frontend/src/components/EChart.tsx`, `src/lib/chartOptions.ts` | echarts/core, canvas renderer |
| API client | `web/frontend/src/lib/api.ts`, `src/hooks/usePoll.ts` | typed fetchers + polling |

## Health-state machine

```
        probe ok / request ok
   ┌───────────────◀───────────────┐
HEALTHY ──fails ≥ N──▶ DEGRADED ──still failing──▶ FAILED (out of rotation)
   ▲                                                  │
   └──────── M consecutive probe successes ◀──────────┘
```

Active probes every `RELAY_PROBE_INTERVAL` (default 15s) + passive signals
from real request failures. Recovery requires M consecutive probe successes
(hysteresis avoids flapping).

## Streaming + failover caveat

Transparent retry on another endpoint is safe **only before the first token**
reaches the client. After first byte, errors surface to the client; no
mid-stream replay in v1.

## Frontend/backend boundary

The frontend owns rendering and interaction only; all state (endpoints,
health, tunnels, settings, telemetry) lives in the backend. The frontend
polls read endpoints and issues commands via the control plane. UI events are
shipped to `POST /admin/logs/frontend` so client-side actions are captured in
the server log stream.
