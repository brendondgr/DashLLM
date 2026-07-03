# Repository Structure

Mode G (API + separate frontend) per the web-interfaces standard.

```text
root/
├── docs/                    # plan, architecture, routes, contracts, design system
├── web/
│   ├── backend/             # FastAPI service (uv project)
│   │   ├── app/
│   │   │   ├── main.py      # app factory, lifespan, static dashboard serving
│   │   │   ├── config.py    # pydantic-settings (RELAY_* env)
│   │   │   ├── db.py        # SQLite connection + schema
│   │   │   ├── core/        # logging setup, shared helpers
│   │   │   ├── routes/      # v1 proxy + admin_* routers
│   │   │   ├── services/    # proxy, router, health, tunnels, telemetry, stats
│   │   │   └── schemas/     # Pydantic models
│   │   ├── data/            # runtime SQLite (gitignored)
│   │   ├── logs/            # rotating JSON-lines logs (gitignored)
│   │   └── pyproject.toml
│   └── frontend/            # Astro + React + ECharts dashboard
│       ├── src/
│       │   ├── pages/       # index.astro (single page)
│       │   ├── layouts/     # Base.astro (fonts, global CSS)
│       │   ├── components/  # App shell, EChart host, screens/
│       │   ├── lib/         # api, chartOptions, styles, format, logger
│       │   └── hooks/       # usePoll
│       ├── astro.config.mjs
│       └── package.json
├── tests/
│   └── backend/             # pytest: db, telemetry, router, proxy, stats, tunnels
├── utils/                   # seed_telemetry.py (dev data seeding)
├── scripts/                 # dev.sh, validate_live.sh
└── README.md
```

## Primary files

| File / Folder | Purpose |
| :--- | :--- |
| `web/backend/app/services/proxy.py` | Streaming forward, TTFT/usage capture, failover retry |
| `web/backend/app/services/router.py` | Endpoint pool, health state machine, resolution policy |
| `web/backend/app/services/health.py` | Background active prober |
| `web/backend/app/services/tunnels.py` | ssh command builder + supervised child processes |
| `web/backend/app/services/telemetry.py` | Async telemetry write queue |
| `web/backend/app/services/stats.py` | Aggregation queries → ECharts payloads |
| `web/backend/app/routes/` | `v1.py`, `admin_endpoints.py`, `admin_tunnels.py`, `admin_stats.py`, `admin_settings.py`, `admin_proxy.py`, `admin_logs.py` |
| `web/frontend/src/components/App.tsx` | Dashboard shell (sidebar, header, hot-swap) |
| `web/frontend/src/components/screens/` | One module per screen (exact prototype port) |
| `web/frontend/src/lib/api.ts` | Typed client for the API contract |
| `utils/seed_telemetry.py` | Seed synthetic 30-day telemetry for dev |
| `scripts/validate_live.sh` | Live end-to-end validation |

Other root files: `.claude/launch.json` (preview/dev launch config for the
relay server), `scripts/dev.sh` (backend + frontend dev processes),
`scripts/validate_live.sh` (live e2e checks).

*Last updated: 2026-07-03 (build complete, incl. model-alias routing).*
