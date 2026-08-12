# CLAUDE.md

Working notes for agents editing this repo. Product docs live in `docs/`;
this file is about *how to work here*.

## What this is

DashLLM ("relay") is an OpenAI-compatible LLM proxy plus a telemetry
dashboard, running as **one process on one port** (default `:4000`). FastAPI
backend under `web/backend`, Astro + React + ECharts dashboard under
`web/frontend`, served statically from the same process in production.

Read [docs/architecture.md](docs/architecture.md) before changing anything in
`app/services/`.

## Commands

Backend (must run from `web/backend` — `pyproject.toml` sets
`testpaths = ["../../tests/backend"]`):

```bash
cd web/backend && uv run pytest
```

```bash
cd web/backend && uv run uvicorn app.main:app --port 4000
```

Frontend:

```bash
cd web/frontend && npm run check && npm run build
```

Both dev servers together (backend `:4000` with reload, Astro `:4321`
proxying `/admin`, `/v1`, `/health`):

```bash
./scripts/dev.sh
```

End-to-end checks against a *running* relay (`RELAY_BASE`, default
`http://127.0.0.1:4000`) and whatever endpoints are registered on it:

```bash
./scripts/validate_live.sh
```

Seed synthetic telemetry for dashboard work (from the repo root; rows are
tagged `client_key = '…seed'` so they can be purged):

```bash
uv run --project web/backend python utils/seed_telemetry.py --days 30
```

If relay is installed as a systemd user service, `relay restart` picks up
backend changes and `npm run build` + `relay restart` picks up frontend ones.

## Conventions

- **Python**: 4-space indent, type hints on public functions, module
  docstrings that explain *why* rather than restating the code. Every service
  module has one — match that style. Log through `app/core/logging.py`
  (`get_logger("<subsystem>")`) with structured `extra={"data": {...}}`.
- **TypeScript/React**: inline style objects, shared values from
  `src/lib/styles.ts`. No Tailwind, no component library. Poll with
  `usePoll`, never a hand-rolled `setInterval`. UI actions should call
  `log.*` so they land in the backend log stream.
- Screens are one module each. ~850 lines is the ceiling — extract before
  exceeding it (`Endpoints.tsx` shed its add/edit forms into
  `EndpointForm.tsx` for exactly this reason; add endpoint fields there, once,
  not twice).
- Comments explain non-obvious decisions. Several load-bearing ones are
  already documented in place (why bodies are zlib BLOBs, why histogram edges
  are frozen, why in-flight tracking starts before the concurrency gate) —
  don't strip them.

## Things that will bite you

- **The API contract is written three times.** `app/schemas/__init__.py`,
  `web/frontend/src/lib/types.ts`, and `docs/api-contract.md`. Change one,
  change all three.
- **Bucket granularity is written twice.** `_TS_BUCKETS` in
  `app/services/stats.py` and `GRAN` in `screens/Dashboard.tsx`.
- **`histogram.METRICS` is an on-disk format version.** Its bucket edges are
  frozen constants; changing them invalidates every stored `bucket_idx` in
  `request_rollup_hist`.
- **DB migrations are additive only** — `db.py::_migrate` adds columns inside
  try/except. There is no migration framework and no down-migration.
- **Copying the SQLite file alone loses data.** WAL mode means recent writes
  live in `relay.db-wal`; copy the sidecars too or use `sqlite3 .backup`.
- **`request_bodies.prompt/completion` are zlib BLOBs.** Read them through
  `telemetry.body_unpack`, not directly.
- **The static dashboard mount is a catch-all at `/`.** It must stay after
  every `include_router` call in `main.py`.
- **Rollups are never pruned**, only raw `requests` rows are. Aggregate
  history outlives raw retention by design.
- **A new upstream protocol is an adapter, not a branch.** Add it under
  `app/services/adapters/` and register it in that package's `__init__`;
  `protocol` on `endpoints` is the only dispatch key (`kind` gets overwritten,
  `server_type` is cosmetic). Implementing `probe` is what keeps `health.py`
  from needing a change. Non-`openai` protocols are alias-only — `_eligible`,
  the manual pin, and the first-endpoint auto-pin in `router.py` all exclude
  them, and there are tests asserting each.
- **`Router.create` inserts positionally.** Its `row` dict key order must
  match the explicit column list in the INSERT right below it.
- **Nothing runtime-configurable belongs in a systemd unit file.** Both units
  `ExecStart` a wrapper (`scripts/{relay,opencode}-serve.sh`) that sources
  `.env`, because systemd can't expand variables in `WorkingDirectory=` and
  because a value baked into the unit is silently reverted the next time
  `install-systemd.sh` runs — which is exactly how a local `--host 0.0.0.0`
  edit got lost once.
- **`uv` exits 143 on SIGTERM**, so `relay.service` needs
  `SuccessExitStatus=143` or a clean stop parks the unit in `failed`.
- **Never write to the repo's `.env`.** It is the user's real configuration and
  is gitignored, so an overwrite is unrecoverable. `launch.sh --env-file` and
  a scratch path exist for testing.

## Testing

136 tests in `tests/backend/`, all against fake upstream ASGI apps
(`fake_upstream.py`, routed by hostname: `good` / `strict` / `opencode` /
`flaky` / dead) — no real model server, agent server, or SSH host required.
Tunnel lifecycle is tested with an injectable fake command, since real SSH
can't be exercised in CI.

When adding a proxy or router behavior, add the case to `test_proxy.py` or
`test_router.py` rather than only validating by hand against a live server.

Note that `httpx.ASGITransport` ignores request timeouts, so a deadline that
must hold in tests has to be enforced by relay (`asyncio.wait_for`), not
handed to the HTTP client — see `adapters/opencode.py`.

## Skills worth reaching for

- **`portfolio-readme`** — when reworking `README.md` for presentation to
  reviewers or recruiters, rather than as internal documentation.
- **`dataviz`** — before adding or restyling any chart. The dashboard already
  has a committed palette in `src/lib/styles.ts`; use that skill's method to
  keep new charts consistent with it, not to replace the palette.
- **`security-review`** — before changing `app/security.py`,
  `tunnel_sessions.py`, or `tunnels.py`. Those handle auth, key injection, and
  subprocess argv construction.
- **`simplify`** — after a large change, for reuse/altitude cleanups.
- **`/code-review`** — for correctness review of a working diff.

There are no project-local skills under `.claude/skills/`. `.claude/launch.json`
defines a `relay` preview target (`uv run uvicorn app.main:app --port 4000`)
for the browser preview tools.

## Repo workflow

Runtime state (`web/backend/data/`, `web/backend/logs/`, `*.db*`) is
gitignored and must never be committed. Commit only after the relevant checks
pass — `uv run pytest` for backend changes, `npm run check && npm run build`
for frontend ones.
