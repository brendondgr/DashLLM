# Plan: DashLLM Build — LLM Proxy & Telemetry Dashboard

## 1. Introduction

This plan combines two inputs into one working product: the approved **backend
design** (FastAPI OpenAI-compatible proxy with multi-endpoint routing, hot-swap,
SSH tunnel management, telemetry capture, and an ECharts-shaped stats API) and
the existing **frontend prototype** (`LLM Proxy Dashboard.dc.html`), which must
be ported to Astro + React + Apache ECharts while keeping its visual and
interaction structure *exactly* the same — same six screens (Dashboard,
Requests, Endpoints, SSH Tunnel, Proxy Info, Settings), same layout grids, same
styling, same hot-swap header control.

The approach is backend-first: build the proxy/telemetry/stats service in
validated increments (each committed locally after its tests pass), then port
the frontend screen-for-screen against the real API, replacing every piece of
simulated data in the prototype with live backend data. Everything that passes
through the system is logged: structured rotating server logs, one telemetry
row per proxied request, health/tunnel/router state transitions, and frontend
UI events shipped to a backend ingestion endpoint. Final validation is
end-to-end against the real local model servers: llama.cpp on `:7070`
(currently live, `gemma-4-26B-it`) and vLLM on `:9090` (currently down — used
to validate health detection and failover for real).

## 2. Gaps & Unanswered Questions

- **vLLM on :9090 is not running** at build time (connection refused).
  *Assumption:* register it as a second endpoint anyway; its "failed" health
  state is the live validation of the health prober and priority failover. If
  it comes up later it rejoins the pool automatically.
- **Failover default** (open decision §13 of the design): *Assumption:*
  automatic priority failover ON, with manual pin available via
  `POST /admin/endpoints/{id}/activate` — matches the prototype's
  "Automatic failover" settings toggle default.
- **Storage**: *Assumption:* SQLite (zero-setup, personal volume), schema kept
  Postgres-compatible per the design.
- **Tunnels**: *Assumption:* subprocess `ssh` (argv list, never shell string);
  the displayed command is the executed command. Real SSH cannot be exercised
  in tests — lifecycle is tested with an injectable fake command.
- **Body capture**: *Assumption:* metadata-only by default; the prototype's
  "Log request bodies" toggle wires to the `capture_bodies` setting.
- **Port semantics**: the prototype's SSH form exposes *remote port* and
  *local port*. The backend model additionally supports `ssh_port` (default
  22) via API without altering the frontend structure.
- **Admin auth**: personal LAN tool bound to localhost. *Assumption:* admin
  token optional (empty = open), configurable via env `RELAY_ADMIN_TOKEN`.
- **Proxy port**: backend listens on **4000** (matches the prototype's
  `listening :4000` and base URL). Changing it in Settings persists the value
  and reports "restart required", as the prototype states.

## 3. Hierarchical Step-by-Step Instructions

#### Step 1: Scaffold repository structure, docs, and toolchains
- **Locations**: `docs/*.md` (plan, architecture, structure, routes,
  component-map, data-flow, deployment, design-system, api-contract),
  `web/backend/` (uv project: `pyproject.toml`, `app/` package skeleton),
  `web/frontend/` (Astro project with React integration, `astro.config.mjs`),
  `scripts/dev.sh`, `tests/`, `utils/`, `.gitignore`, `README.md`.
- **Rationale**: the repo-structure and web-interfaces skills require the
  Mode G layout (API + separate frontend under `web/`) and the planning docs
  to exist before code; both toolchains must build before features are added.
- **Action**: Undergo the verification/tests/validation process for this phase
  (backend app imports and boots; `astro build` succeeds). Once validated,
  commit to git stating: DashLLM Build (1/7) Complete: Scaffolded Mode G repo
  structure, docs, uv backend, and Astro/React frontend toolchains.

#### Step 2: Backend core — config, logging, database, schemas, telemetry writer
- **Locations**: `web/backend/app/config.py` (Settings), `app/core/logging.py`
  (rotating file + console, request-id aware), `app/db.py` (SQLite connection +
  schema: `requests`, `endpoints`, `tunnels`, `settings`, `frontend_logs`),
  `app/schemas/` (Pydantic models), `app/services/telemetry.py`
  (asyncio queue → off-hot-path writer), `app/main.py` (lifespan wiring),
  `tests/backend/test_db.py`, `tests/backend/test_telemetry.py`.
- **Rationale**: every later subsystem records through the telemetry writer and
  logs through the logging core; the DB schema is the contract for the stats
  API, so it must exist and be tested first.
- **Action**: Undergo the verification/tests/validation process for this phase
  (pytest green). Once validated, commit stating: DashLLM Build (2/7) Complete:
  Backend core with config, structured logging, SQLite schema, and async
  telemetry writer.

#### Step 3: Endpoint registry, router, health detection, control-plane API
- **Locations**: `app/services/router.py` (pool, live health state machine
  HEALTHY→DEGRADED→FAILED with hysteresis recovery, manual-pin + priority
  policies), `app/services/health.py` (active `GET /v1/models` prober),
  `app/routes/admin_endpoints.py` (CRUD, `activate`, `test`, `health`,
  router GET/PUT), `app/routes/admin_settings.py`, `app/routes/admin_logs.py`
  (frontend event ingestion), `tests/backend/test_router.py`,
  `tests/backend/test_endpoints_api.py`.
- **Rationale**: the proxy resolves its upstream from live router state at call
  time — the registry and health signals must exist before the proxy can route
  or fail over.
- **Action**: Undergo the verification/tests/validation process for this phase
  (pytest; live check: register `:7070` and `:9090`, prober marks 7070 healthy
  and 9090 failed). Once validated, commit stating: DashLLM Build (3/7)
  Complete: Endpoint registry with hot-swap, active+passive health, failover
  policy, and admin control-plane API.

#### Step 4: OpenAI-compatible proxy and stats API
- **Locations**: `app/services/proxy.py` (header scrubbing, upstream key
  injection, non-streaming usage capture, SSE tee with TTFT stamp and trailing
  usage parse, `stream_options.include_usage` injection toggle, passive failure
  signals, pre-first-token retry), `app/routes/v1.py` (chat/completions,
  completions, embeddings, models, generic passthrough), `app/services/stats.py`
  + `app/routes/admin_stats.py` (summary, volume, tokens/timeseries,
  tokens/by-hour, tokens/by-day, by-model, by-endpoint, latency, recent, live
  in-flight), `tests/backend/test_proxy.py` (fake upstream ASGI fixture:
  non-stream, stream, failover, error paths), `tests/backend/test_stats.py`.
- **Rationale**: this is the product's hot path and the data source for every
  chart; the ECharts `dimensions`/`source` contract built here is what the
  frontend binds to in Step 6.
- **Action**: Undergo the verification/tests/validation process for this phase
  (pytest; live: stream + non-stream requests through the proxy to `:7070`
  return verbatim responses, telemetry rows appear, `/admin/stats/*` shapes
  are correct). Once validated, commit stating: DashLLM Build (4/7) Complete:
  Streaming OpenAI-compatible proxy with TTFT/usage capture and full
  ECharts-shaped stats API.

#### Step 5: SSH tunnel manager
- **Locations**: `app/services/tunnels.py` (argv command builder with field
  validation, subprocess lifecycle start/stop/change-port, supervisor loop with
  backoff, two-level test: TCP/ssh reachability + `/v1/models` through the
  tunnel), `app/routes/admin_tunnels.py` (CRUD, start/stop/test/command),
  `tests/backend/test_tunnels.py` (command generation, injection rejection,
  lifecycle against a fake long-running command).
- **Rationale**: covers the remote-hosting requirements — generate/show the
  exact ssh command, test the connection, change tunnel ports — while keeping
  key material out of API responses.
- **Action**: Undergo the verification/tests/validation process for this phase
  (pytest; API-level checks of command string and structured test result).
  Once validated, commit stating: DashLLM Build (5/7) Complete: SSH tunnel
  manager with argv-safe command generation, supervised lifecycle, and
  two-level connection test.

#### Step 6: Frontend port — Astro + React + ECharts, exact structure, live data
- **Locations**: `web/frontend/src/layouts/Base.astro`, `src/pages/index.astro`,
  `src/components/App.tsx` (shell: sidebar, header, hot-swap dropdown),
  `src/components/screens/{Dashboard,Requests,Endpoints,SshTunnel,ProxyInfo,Settings}.tsx`,
  `src/components/EChart.tsx` (perf wrapper: echarts/core, canvas, resize
  observer), `src/lib/{api.ts,chartOptions.ts,format.ts,styles.ts,logger.ts}`,
  `src/hooks/usePoll.ts`. Backend: static-serve of `web/frontend/dist` from
  FastAPI + `/admin/proxy` info route.
- **Rationale**: the prototype's structure is kept exactly (same DOM structure,
  inline style values, screen layouts A/B/C, chips, chart grid) but every
  simulated value — history, live ticks, endpoint list, tunnel log, proxy
  stats — is replaced by polling the real API; UI actions log to the backend.
  Independent screens are implemented by parallel Sonnet implementation agents
  per the session's workflow instruction.
- **Action**: Undergo the verification/tests/validation process for this phase
  (`astro check` + `astro build`; dashboard served by the backend at `:4000`;
  browser-verified screens; live request visible in UI). Once validated,
  commit stating: DashLLM Build (6/7) Complete: Astro/React/ECharts frontend
  ported 1:1 from the prototype and wired to the live backend API.

#### Step 7: End-to-end validation, seed utility, docs, merge
- **Locations**: `utils/seed_telemetry.py` (synthetic 30-day history for
  dashboard development), `scripts/validate_live.sh` (e2e: request through
  proxy → telemetry row → stats → UI; failover behavior with 9090 down),
  `docs/structure.md` finalized, worktree branch merged into `main`.
- **Rationale**: proves the assembled system against real model servers and
  leaves the repo in the documented, reproducible state the skills require.
- **Action**: Undergo the verification/tests/validation process for this phase
  (full pytest suite + live script + UI check). Once validated, commit
  stating: DashLLM Build (7/7) Complete: End-to-end validation against live
  llama.cpp, seed/validation utilities, and final documentation. Then merge
  the worktree branch into `main`, fixing any issues.

## 4. Deliverables Table

| Deliverable | Description | Location (File/Path) |
| --- | --- | --- |
| FastAPI proxy service | OpenAI-compatible `/v1/*` with streaming tee, TTFT/usage capture | `web/backend/app/services/proxy.py`, `app/routes/v1.py` |
| Endpoint registry + router | Hot-swap, manual pin, priority failover, health state machine | `web/backend/app/services/router.py`, `app/services/health.py` |
| Control-plane API | Endpoints/tunnels/settings/router admin surface | `web/backend/app/routes/admin_*.py` |
| Stats API | ECharts `dimensions`/`source` payloads for all charts | `web/backend/app/services/stats.py`, `app/routes/admin_stats.py` |
| SSH tunnel manager | Command generation, supervised subprocess, two-level test | `web/backend/app/services/tunnels.py` |
| Telemetry + logging | Per-request rows, rotating structured logs, frontend event ingestion | `web/backend/app/services/telemetry.py`, `app/core/logging.py`, `app/routes/admin_logs.py` |
| Astro/React frontend | Exact port of the prototype, live data, ECharts | `web/frontend/src/**` |
| Backend unit/API tests | pytest suite incl. fake-upstream proxy tests | `tests/backend/test_*.py` |
| Seed utility | Synthetic 30d telemetry for dashboard dev | `utils/seed_telemetry.py` |
| Live validation script | E2E checks against :7070/:9090 through the proxy | `scripts/validate_live.sh` |
| Dev orchestration | One-command dev startup | `scripts/dev.sh` |
| Documentation | Architecture, routes, contracts, structure, design system | `docs/*.md` |
