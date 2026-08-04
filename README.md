# DashLLM (relay)

An OpenAI-compatible **LLM proxy** with a telemetry **dashboard**. relay sits
between your clients and one or more model servers (llama.cpp, vLLM, ollama,
any OpenAI-compatible endpoint), exposes its own drop-in `/v1` endpoint, and
records per-request telemetry rendered as Apache ECharts visualizations.

- **Proxy** — streaming SSE passthrough with TTFT and token-usage capture,
  multi-endpoint registry, hot-swap, health detection, priority failover, and
  model-alias routing that pins a request to a named server.
- **Remote hosting** — interactive SSH tunnels run in a real pseudo-terminal,
  so host-key confirmations, passphrases, and password prompts are answered
  in the dashboard. Nothing connects on its own.
- **Dashboard** — Astro + React + ECharts, fed by an ECharts-shaped stats API
  (`dimensions`/`source` payloads) backed by hourly rollups.

## Quickstart

```bash
cd web/backend && uv sync && uv run uvicorn app.main:app --port 4000
```

Point any OpenAI client at `http://127.0.0.1:4000/v1`. Build the dashboard
once (`cd web/frontend && npm install && npm run build`) and the same process
serves it at `http://127.0.0.1:4000/`.

For frontend work, run both dev servers instead — Astro on `:4321` proxies
`/admin`, `/v1`, and `/health` to the backend on `:4000`:

```bash
./scripts/dev.sh
```

## Layout

```text
root/
├── docs/                    # architecture, api-contract, routes, data-flow,
│                            # frontend, deployment
├── web/
│   ├── backend/             # FastAPI service (uv project)
│   │   ├── app/
│   │   │   ├── main.py      # app factory, lifespan, static dashboard serving
│   │   │   ├── config.py    # pydantic-settings (RELAY_* env)
│   │   │   ├── db.py        # SQLite schema, migrations, rollup backfill
│   │   │   ├── security.py  # admin guard, bearer extraction, key masking
│   │   │   ├── core/        # logging.py
│   │   │   ├── routes/      # v1.py + admin_{endpoints,settings,stats,tunnels}.py
│   │   │   ├── services/    # proxy, router, health, stats, rollup, histogram,
│   │   │   │                # telemetry, settings_store, tunnels,
│   │   │   │                # tunnel_sessions, ssh_config
│   │   │   └── schemas/     # Pydantic models (the API contract)
│   │   ├── data/            # runtime SQLite (gitignored)
│   │   ├── logs/            # rotating JSON-lines logs (gitignored)
│   │   └── pyproject.toml
│   └── frontend/            # Astro + React + ECharts dashboard
│       ├── src/
│       │   ├── pages/       # index.astro (single page)
│       │   ├── layouts/     # Base.astro (fonts, global CSS, keyframes)
│       │   ├── components/  # App shell, EChart host, screens/
│       │   ├── lib/         # api, types, chartOptions, styles, format,
│       │   │                # logger, echarts
│       │   └── hooks/       # usePoll
│       ├── astro.config.mjs
│       └── package.json
├── tests/backend/           # pytest suite (91 tests)
├── utils/                   # seed_telemetry.py
├── scripts/                 # dev.sh, relay, install-systemd.sh, validate_live.sh
└── deploy/systemd/          # relay.service (systemd user unit)
```

## Tests

```bash
cd web/backend && uv run pytest
```

`pyproject.toml` points `testpaths` at `../../tests/backend`, so pytest must
be run from `web/backend`. The suite uses a fake upstream ASGI app — no real
model server or SSH host is needed.

Against live model servers:

```bash
./scripts/validate_live.sh
```

## Docs

| Doc | Covers |
| --- | --- |
| [architecture.md](docs/architecture.md) | Subsystems, routing resolution, health state machine, storage/rollup strategy |
| [api-contract.md](docs/api-contract.md) | Object shapes, stats payloads, auth |
| [routes.md](docs/routes.md) | Every HTTP route |
| [data-flow.md](docs/data-flow.md) | Hot path, poll loops, control-plane writes, logging |
| [frontend.md](docs/frontend.md) | Component map, design tokens, chart conventions |
| [deployment.md](docs/deployment.md) | Single-process production, env vars, systemd, security posture |

`CLAUDE.md` holds working conventions for agents (and humans) editing this
repo.
