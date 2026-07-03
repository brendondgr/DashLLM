# DashLLM (relay)

An OpenAI-compatible **LLM proxy** with a polished telemetry **dashboard**.
relay sits between your clients and one or more model servers (llama.cpp,
vLLM, ollama, any OpenAI-compatible endpoint), exposes its own drop-in
`/v1` endpoint, and records detailed per-request telemetry rendered as
interactive Apache ECharts visualizations.

- **Proxy**: streaming SSE passthrough with TTFT + token usage capture,
  multi-endpoint registry, hot-swap, health detection, priority failover.
- **Remote hosting**: SSH tunnel manager — generates the exact `ssh -N -L …`
  command it runs, supervised child processes, two-level connection tests.
- **Dashboard**: Astro + React + Apache ECharts, fed by an ECharts-shaped
  stats API (`dimensions`/`source` payloads).

## Layout

| Path | Purpose |
| --- | --- |
| `web/backend/` | FastAPI service: `/v1/*` proxy, `/admin/*` control plane + stats |
| `web/frontend/` | Astro + React + ECharts dashboard |
| `docs/` | Architecture, routes, API contract, data flow, design system |
| `tests/backend/` | pytest suite (fake-upstream proxy tests, router, stats, tunnels) |
| `utils/` | Dev utilities (telemetry seeding) |
| `scripts/` | Dev orchestration and live validation |

## Quickstart

```bash
# backend (serves API on :4000, and the built dashboard if present)
cd web/backend && uv sync && uv run uvicorn app.main:app --port 4000

# frontend dev server on :4321 (proxies /admin + /v1 to :4000)
cd web/frontend && npm install && npm run dev

# production: build the dashboard, then just run the backend
cd web/frontend && npm run build
```

Point any OpenAI client at `http://127.0.0.1:4000/v1`.

## Tests

```bash
cd web/backend && uv run pytest
./scripts/validate_live.sh   # end-to-end against live local model servers
```

See `docs/` for the full architecture and API contract.
