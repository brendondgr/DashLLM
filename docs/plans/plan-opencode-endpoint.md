# Plan: OpenCode as a relay endpoint

Status: **implemented.** See [opencode.md](../opencode.md) for the shipped
feature and [plan-opencode-endpoint-execution.md](plan-opencode-endpoint-execution.md)
for the work list and the deltas from this design (notably `available_models`,
which this document predates).

Goal: in the dashboard's *Add Endpoint* form, pick type **OpenCode**, give it an
alias like `agent`, and have `POST :4000/v1/chat/completions` with
`"model": "agent"` reach a running `opencode serve` — with full relay telemetry,
using any OpenAI client unchanged.

---

## 1. What OpenCode's server actually is

`opencode serve --port 4096` is **not** OpenAI-compatible. It is a stateful
agent server with its own OpenAPI 3.1 spec (published at `/doc`).

| Concern | OpenAI (what relay speaks) | OpenCode (what we must speak) |
|---|---|---|
| Unit of work | stateless `POST /v1/chat/completions` with full `messages[]` | a **session**: `POST /session`, then `POST /session/{id}/message` |
| Model id | `"model": "qwen3-30b"` | `{"model": {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"}}` |
| Content | `messages[].content` strings | `parts: [{type: "text", text: "..."}]` |
| System prompt | a `role: "system"` message | a top-level `system` field on the message body |
| Response | `choices[].message.content` | `{info: AssistantMessage, parts: Part[]}` |
| Usage | `usage.{prompt,completion,total}_tokens` | `info.tokens` **and `info.cost` (already in USD)** |
| Streaming | SSE `chat.completion.chunk` frames on the same response | the POST blocks; deltas arrive on a **separate global** `GET /event` SSE stream |
| Catalog | `GET /models` → `{"data":[{"id":...}]}` | `GET /config/providers` → `{providers: [...], default: {...}}` |
| Health | (relay reuses `/models`) | `GET /global/health` → `{healthy, version}` |
| Auth | `Authorization: Bearer <key>` | **HTTP Basic**, user `opencode` (or `OPENCODE_SERVER_USERNAME`), password `OPENCODE_SERVER_PASSWORD` |
| Extras | — | `POST /session/{id}/abort`, `DELETE /session/{id}`, `POST /session/{id}/prompt_async` |

Every row in that table is a place where relay currently hardcodes the OpenAI
shape, which is why this needs an adapter layer rather than a config flag.

### Decisions taken (from your answers)

- **Direction:** OpenCode is an *upstream*. Relay translates inbound OpenAI
  requests into OpenCode session calls.
- **Session model:** **ephemeral per request.** Create session → send prompt →
  read reply → delete session. No session cache, no state in relay, exact
  OpenAI semantics.
- **Lifecycle:** relay connects to an **already-running** `opencode serve`.
  No subprocess supervision, no changes to `tunnels.py`.

---

## 2. Where the OpenAI assumption is currently baked in

Eight places. Each needs a seam.

| # | File:line | Assumption |
|---|---|---|
| 1 | `services/health.py:19-23,81` | prober calls `GET {base_url}/models` and parses `{"data":[{"id":…}]}`. If it fails 3× (~45s) the endpoint goes `failed`. |
| 2 | `services/proxy.py:377` | upstream URL is `base_url + "/" + path`, verbatim. |
| 3 | `services/proxy.py:382-383` | auth is `Authorization: Bearer …`. |
| 4 | `services/proxy.py:139-165` | body has top-level `model`, `stream`, `stream_options`, `temperature`, `max_tokens`. |
| 5 | `services/proxy.py:412-413,481-491` | streaming reply is `text/event-stream`, teed **byte-verbatim** via `aiter_raw`. |
| 6 | `services/proxy.py:70-90,440-449` | usage at `usage.*_tokens`, in the JSON body or a trailing SSE frame. |
| 7 | `services/proxy.py:93-111,452-454` | completion text at `choices[*].delta.content` / `choices[*].message`. |
| 8 | `services/proxy.py:41-50,332-341` | stale-`model_override` detection reads OpenAI-ish error prose. |

Two existing fields look like discriminators but are **not usable**:

- `kind` (`local`/`remote_direct`/`remote_tunnel`) is *network topology*, and
  `router._infer_kind` **re-derives and overwrites it on every PATCH**
  (`router.py:204-205`) and on tunnel-route activation (`router.py:310-311`).
  A protocol flag stored there would be silently clobbered.
- `server_type` (`llama.cpp`/`vLLM`/`ollama`/`openai`) is purely cosmetic —
  written to the DB, echoed in `EndpointOut`, rendered as a badge at
  `Endpoints.tsx:530`, and **read by no backend code at all**.

So: introduce a new field.

---

## 3. Design

### 3.1 New field: `protocol`

```python
Protocol = Literal["openai", "opencode"]   # schemas/__init__.py
```

Column `protocol TEXT NOT NULL DEFAULT 'openai'` on `endpoints`. Everything
existing keeps working untouched; `protocol` is the single dispatch key.

`server_type` stays as the cosmetic badge and gains an `"opencode"` value so the
UI reads sensibly, but **nothing branches on it** — that stays true.

### 3.2 New package: `app/services/adapters/`

```
adapters/
  __init__.py     # get_adapter(endpoint) -> UpstreamAdapter
  base.py         # UpstreamAdapter protocol + shared helpers
  passthrough.py  # today's behavior, extracted verbatim
  opencode.py     # the new one
```

`base.py` defines a narrow interface — three methods, matching the three seams:

```python
class UpstreamAdapter(Protocol):
    name: str
    supported_routes: frozenset[str]        # {"chat.completions", "models"}

    async def probe(self, http, endpoint) -> ProbeResult: ...
        # ProbeResult(ok: bool, latency_ms: float, models: list[str], error: str | None)
        # replaces health.py's hardcoded GET /models

    async def complete(self, http, endpoint, body: dict,
                       record: RequestRecord) -> tuple[int, dict]: ...
        # returns (status, OpenAI-shaped ChatCompletion dict); fills record.*

    def stream(self, http, endpoint, body: dict,
               record: RequestRecord, t0: float) -> AsyncIterator[bytes]: ...
        # yields OpenAI-shaped `data: {...}\n\n` SSE frames
```

`passthrough.py` is a pure extraction of the current code paths — a refactor
with zero behavior change, which is what makes it reviewable. `proxy._forward`
becomes: resolve the adapter, and if it is not the passthrough one, take the
adapter path instead of `build_request` + `aiter_raw`.

Keeping the interface at exactly three methods matters. The alternative (letting
adapters own the whole `handle`) would fork the failover loop, the concurrency
gate, and the telemetry call sites — all of which should stay shared.

### 3.3 Request translation (`opencode.py`)

**Model resolution.** `model_override` on the endpoint holds
`"anthropic/claude-sonnet-4-5"`; split on the first `/` into `providerID` /
`modelID`. If absent, omit `model` entirely and let OpenCode use its configured
default. The inbound OpenAI `model` field is the *alias* and is consumed by
relay's alias router, never forwarded.

**Messages → parts.** v1 flattens:

- all `role: "system"` messages → joined into the body's `system` field;
- the final `role: "user"` message → `parts: [{type: "text", text: …}]`;
- any preceding user/assistant turns → prefixed into that same text part as a
  labeled transcript (`User: …` / `Assistant: …`).

Flattening is lossy for multi-turn, but it is one HTTP call and it is
predictable. The structurally-faithful alternative — seeding each prior turn via
`POST /session/{id}/message` with `noReply: true`, then sending the real turn —
costs N+1 round trips and still can't represent an assistant turn as an
assistant turn. Worth revisiting once we see real traffic; not v1.

**Per-request lifecycle:**

```
POST   /session                      {title: "relay <req_id>"}     -> session.id
POST   /session/{id}/message         {model?, agent?, system?, parts}
DELETE /session/{id}                 (in a finally block)
```

On client disconnect (`GeneratorExit` / `CancelledError`), fire
`POST /session/{id}/abort` before the delete — otherwise the agent keeps
burning tokens on a request nobody is reading.

### 3.4 Response translation

Assemble a standard `chat.completion` object:

- `choices[0].message.content` ← concatenated `parts[*].text` where
  `type == "text"`.
- `finish_reason` ← `"stop"`, or `"length"`/`"error"` from `info.error`.
- Tool-call parts are **summarized into the text body** in v1 (`[tool: bash]`
  markers), not mapped to OpenAI `tool_calls`. Mapping them properly is a
  separate piece of work; guessing at it now would produce output that looks
  valid to a client and isn't.

Telemetry mapping — this is the part that keeps the dashboard working:

| `RequestRecord` field | Source |
|---|---|
| `prompt_tokens` / `completion_tokens` | `info.tokens.input` / `info.tokens.output` (+ `reasoning` folded into output) |
| `total_tokens` | sum |
| `cost_usd` | **`info.cost` directly** — bypass `proxy._cost`, OpenCode already priced it |
| `model` | `"{providerID}/{modelID}"` from `info` |
| `ttft_ms` | first byte of the synthesized stream (see below) |
| `latency_ms` | wall clock around the whole session lifecycle |
| `prompt_body` / `completion_body` | flattened prompt / extracted text |

`_cost` needs a guard so it doesn't overwrite an adapter-supplied `cost_usd`.

### 3.5 Streaming

**v1 — synthesized.** Await the blocking `POST /session/{id}/message`, then emit
the result as OpenAI SSE: a role chunk, one or more content chunks, a `usage`
chunk (relay's `inject_stream_usage` contract), and `data: [DONE]`. TTFT equals
total latency, which is honest for a blocking upstream. Every OpenAI client
works. `stream_options` must be stripped before it reaches OpenCode.

**v2 — real deltas, behind a setting.** `POST /session/{id}/prompt_async`
returns `204`, then subscribe to `GET /event`, filter by `sessionID`, translate
`message.part.delta` into `chat.completion.chunk` frames, terminate on
`session.idle`.

> ⚠️ Verify against your installed OpenCode version before building v2.
> [anomalyco/opencode#27966](https://github.com/anomalyco/opencode/issues/27966)
> reports that from 1.14.42 the `/event` stream stopped delivering
> `message.part.updated` / `message.updated` (events published via
> `SyncEvent.run()`), while `message.part.delta` and `session.idle` still
> arrive. That is survivable — `delta` is the one we need — but it is exactly
> the kind of thing to confirm empirically rather than trust.
>
> `/event` is also **global, not per-session**, so a single subscription must be
> shared and demultiplexed across concurrent requests. That is real state, and
> it is why it stays out of v1.

### 3.6 Health probing

`OpenCodeAdapter.probe`:

1. `GET /global/health` → liveness + `version`.
2. `GET /config/providers` → catalog as `["{providerID}/{modelID}", …]`, feeding
   `router.set_models` so `st.model` populates and the endpoint reaches
   `healthy` through the normal state machine.

`health.py` changes from *"call `/models`"* to *"ask the adapter"*. Same 15s
cadence, same N=3 / M=2 thresholds, same `LiveState`.

### 3.7 Routing and failover

Two guardrails, both important:

- **Alias-only.** An agent server is not a drop-in substitute for a raw
  llama.cpp endpoint. Exclude `protocol != "openai"` from `Router._eligible`
  (`router.py:356-363`) so OpenCode never gets picked for `model: "auto"` or as
  a failover target. Alias-pinned requests already never fail over
  (`proxy.py:268-295`) — that is the behavior we want, and it comes free.
- **Route allowlist.** `embeddings` and `completions` return `501` in the
  standard relay error envelope. `models` is already synthesized by
  `proxy._models_catalog` and just needs the alias listed.

### 3.8 Auth

`upstream_key` is reinterpreted per-adapter. For OpenCode, store `user:password`
(or bare `password`, defaulting the user to `opencode`) and emit
`Authorization: Basic base64(...)`. The header-construction line at
`proxy.py:382-383` moves into the adapter.

---

## 4. Things that will bite you

Ordered by how much they'd hurt.

1. **Permission prompts will hang requests.** OpenCode's `permission` config can
   set `edit`/`bash` to `"ask"`, which parks the turn waiting on
   `POST /session/{id}/permissions/{permissionID}`. Relay has no human to ask.
   Mitigations: document that a relay-facing OpenCode instance needs explicit
   `"permission"` settings; auto-deny pending permissions after a timeout; and
   set a hard per-request deadline. Without this the symptom is "requests hang
   forever," diagnosed slowly.

2. **This is remote code execution by design.** OpenCode's `bash`, `edit`, and
   `write` tools run against the project directory the server was started in.
   Putting it behind relay means anyone with a relay client key can drive it.
   `require_client_key` should be mandatory for these endpoints, and this
   belongs in a `security-review` pass per `CLAUDE.md:113`.

3. **Long turns hold a concurrency slot.** `ProxyService._slots` is a semaphore
   of 18 (`config.py`). An agent turn running several minutes occupies one the
   whole time. Consider a separate, smaller gate for adapter endpoints.

4. **One server = one project.** `opencode serve` is bound to the directory it
   was launched in. Several projects means several servers means several relay
   endpoints. Worth stating plainly in the docs.

5. **`Router.create` inserts positionally.** `router.py:171-177` does
   `tuple(row.values())` against a hardcoded 15-column list, and
   `Router.patch` (`:206-209`) builds `SET` from every key in the in-memory row
   dict. The two row shapes **already** disagree about
   `active_tunnel_route_id`. Adding `protocol` requires touching both, carefully.

6. **Migrations are additive only** (`db.py:164-207`, `CLAUDE.md:86`). Add
   `protocol` to `SCHEMA` *and* an `if "protocol" not in cols` branch in
   `_migrate`. No down-migration exists.

7. **`Endpoints.tsx` is at 855 lines** and `CLAUDE.md:71` names ~850 as the
   ceiling. The add/edit forms must be extracted before adding fields, not
   after.

8. **The contract is written three times** (`CLAUDE.md:76`):
   `app/schemas/__init__.py`, `web/frontend/src/lib/types.ts`,
   `docs/api-contract.md`. `protocol` goes in all three.

---

## 5. Phased implementation

### Phase 0 — Verify (no code)

Run `opencode serve --port 4096` against a scratch project and `curl` it:
`GET /global/health`, `GET /config/providers`, `POST /session`,
`POST /session/{id}/message`. Save real response bodies into
`tests/backend/fixtures/opencode/` — the fake upstream should mirror observed
shapes, not the docs. Confirm `/event` delta behavior on your installed version.

### Phase 1 — Adapter seam (pure refactor)

- `services/adapters/{__init__,base,passthrough}.py`.
- `proxy._forward` dispatches through `get_adapter(endpoint)`.
- `health.py` calls `adapter.probe`.
- **All 91 existing tests pass unchanged.** That is the acceptance criterion.

### Phase 2 — `protocol` field, end to end

- `db.py`: `SCHEMA` + `_migrate` branch.
- `router.py`: include `protocol` in `create`'s column list; exclude
  non-`openai` from `_eligible`.
- `schemas/__init__.py`: `Protocol` literal on `EndpointCreate` / `Patch` / `Out`.
- `routes/admin_endpoints.py`: pass through.
- `types.ts` + `api.ts`: mirror.
- `Endpoints.tsx`: extract the shared add/edit form first, then add the
  `OpenCode` option; derive `protocol` from the type select.
- Still no OpenCode behavior — endpoints just carry the flag.

### Phase 3 — The OpenCode adapter

- `adapters/opencode.py`: `probe`, `complete`, synthesized `stream`.
- Basic-auth header construction.
- Session create → message → delete, with abort-on-disconnect.
- Route allowlist → `501` for unsupported routes.
- `_cost` guard for adapter-supplied `cost_usd`.

### Phase 4 — Tests

- Extend `tests/backend/fake_upstream.py` with an `opencode` host (it already
  routes by hostname: `good` / `flaky` / dead).
- New `tests/backend/test_adapter_opencode.py`: translation both directions,
  token + cost extraction, session cleanup on success/error/disconnect,
  synthesized SSE frame validity, `501` on `embeddings`, probe → `healthy`,
  auth header shape.
- `test_router.py`: OpenCode endpoints excluded from `auto` and from failover.

### Phase 5 — Docs

- `docs/architecture.md`: adapter layer in the subsystems table; a section on
  why the protocol seam sits where it does.
- `docs/api-contract.md`: `protocol` on the Endpoint object.
- `docs/opencode.md`: setup, the permission gotcha, the one-server-per-project
  constraint.
- `CLAUDE.md`: note that adding an adapter means touching `health.py` too.

### Phase 6 (optional) — Real streaming

Shared `/event` subscription with per-session demux, behind a runtime setting,
default off.

---

## 6. Open questions

- **Agent selection.** OpenCode has named agents (`build`, `plan`, custom). Pin
  one per endpoint via a new field, or let clients pass it? A dedicated
  `default_agent` column is cleanest; encoding it into `model_override` as
  `build:anthropic/claude-sonnet-4-5` avoids a migration but is uglier.
- **Tool calls.** Summarized as text in v1. Do you want real OpenAI `tool_calls`
  mapping, and if so, is the consumer an agent framework that will act on them?
- **Multi-turn fidelity.** Is flattening acceptable, or is a real client going
  to send long histories where labeled-transcript flattening degrades output?
- **Cost.** Take OpenCode's `info.cost` as authoritative (recommended), or
  re-price through relay's table for consistency with other endpoints?

---

## Sources

- [OpenCode — Server](https://opencode.ai/docs/server/)
- [OpenCode — SDK](https://opencode.ai/docs/sdk.md)
- [OpenCode — Config](https://opencode.ai/docs/config/)
- [anomalyco/opencode#27966 — `/event` SSE stops delivering SyncEvent publishes in 1.14.42+](https://github.com/anomalyco/opencode/issues/27966)
