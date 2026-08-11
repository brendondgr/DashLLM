# Execution plan: OpenCode endpoint + per-endpoint model allowlist

Derived from [plan-opencode-endpoint.md](plan-opencode-endpoint.md). That
document is the *design*; this one is the *ordered work list*, with the
validation gate for each step.

Two features ship together:

1. **OpenCode as an upstream protocol** — `protocol = "opencode"` on an
   endpoint makes relay translate inbound OpenAI chat-completion requests into
   OpenCode session calls.
2. **Per-endpoint model allowlist** (`available_models`) — an explicit list of
   model ids an endpoint serves. Clients can request any of them by name, they
   are advertised through `GET /v1/models`, and requests for them pin to that
   endpoint. This is protocol-agnostic (works for plain OpenAI endpoints too)
   but is what makes an OpenCode endpoint useful, since one OpenCode server
   fronts many provider models.

## Design deltas from the source plan

- **`available_models`** is new (not in the original plan). Stored as a JSON
  array in a single `TEXT` column. Semantics:
  - `GET /v1/models` advertises each entry as its own model id, owned by the
    endpoint.
  - `Router.resolve_alias` matches an entry the same way it matches an alias:
    the request pins to that endpoint. The requested id is forwarded
    **verbatim** (it is a real upstream model name), taking precedence over
    `model_override`.
  - Health probing intersects the discovered catalog with the allowlist when
    one is set, so `st.model` (the endpoint default) is always a permitted id.
  - Entries are validated for collisions against other endpoints' aliases and
    allowlists, and against the reserved id `auto`.
- **Model precedence for a forwarded request**: explicit allowlisted model >
  `model_override` > discovered default.
- **Concurrency**: no separate gate for adapter endpoints (source plan §4.3
  says "consider"); out of scope, noted in docs.
- **Permission hangs** (source plan §4.1): mitigated with a hard per-request
  timeout on the OpenCode message call, surfaced as `504`.
- **Streaming**: v1 synthesized only. No `/event` subscription.

---

## Step 1 — Adapter seam (pure refactor)

`web/backend/app/services/adapters/{__init__,base,passthrough}.py`.

- `base.py`: `ProbeResult`, `UpstreamAdapter` ABC with `probe` / `forward`.
- `passthrough.py`: today's `_forward` body and today's `/models` probe, moved
  verbatim.
- `__init__.py`: `get_adapter(row)` keyed on `row.get("protocol")`.
- `proxy._forward` and `health.probe_endpoint` call through the adapter.

**Validate:** `cd web/backend && uv run pytest` — all existing tests pass with
no test-file edits.

## Step 2 — `protocol` + `available_models`, end to end

- `db.py`: both columns in `SCHEMA`, both with `_migrate` branches.
- `router.py`: columns in `create`'s INSERT list; `available_models`
  normalization + collision validation; `resolve_alias` matches allowlist
  entries; `set_models` respects the allowlist; `out()` exposes both;
  `_eligible` excludes `protocol != "openai"` from auto/failover.
- `schemas/__init__.py`: `Protocol` literal, `available_models: list[str]` on
  Create/Patch/Out; `"opencode"` added to `ServerType`.
- `proxy.py`: `_models_catalog` advertises allowlist entries; alias routing
  forwards an allowlisted model verbatim.
- `types.ts`, `api.ts`, `docs/api-contract.md`: mirror.

**Validate:** `uv run pytest` + new API tests asserting create/patch round-trip
and `/v1/models` contents.

## Step 3 — OpenCode adapter

`adapters/opencode.py`:

- `probe`: `GET /global/health`, then `GET /config/providers` → catalog.
- `forward`: session create → message → delete, abort on disconnect.
- Basic-auth header from `upstream_key` (`user:password` or bare password).
- OpenAI-shaped response assembly + telemetry (tokens, `info.cost`).
- Synthesized SSE when `stream: true`.
- `501` for `completions` / `embeddings`.
- `_cost` guard so an adapter-supplied `cost_usd` is not overwritten.

**Validate:** `uv run pytest` with the new adapter tests (step 4).

## Step 4 — Tests

- `fake_upstream.py`: an `opencode` host implementing the real endpoint shapes.
- `tests/backend/test_adapter_opencode.py`: translation both ways, tokens +
  cost, session cleanup, synthesized SSE validity, `501`, probe → healthy,
  Basic auth header.
- `test_router.py`: allowlist routing, collision rejection, OpenCode excluded
  from `auto`/failover.

**Validate:** `uv run pytest`.

## Step 5 — Frontend

- Extract the shared add/edit endpoint form out of `Endpoints.tsx` into
  `components/EndpointForm.tsx` **first** (the file is at the ~850-line
  ceiling), then add the `OpenCode` type option, the model-allowlist textarea,
  and the allowlist badges on the card.

**Validate:** `cd web/frontend && npm run check && npm run build`.

## Step 6 — Docs

- `docs/api-contract.md`, `docs/architecture.md`, `docs/opencode.md`,
  `CLAUDE.md`.

**Validate:** re-read against the shipped code; no stale field names.

## Step 7 — Commit

Backend and frontend checks green, then a single commit on `main`
(Mode B: no worktree, no push).
