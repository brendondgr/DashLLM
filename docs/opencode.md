# OpenCode endpoints

Relay can front a running [OpenCode](https://opencode.ai) agent server and
expose it through the ordinary OpenAI API, with full telemetry. Any OpenAI
client works unchanged:

```python
client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="...")
client.chat.completions.create(
    model="agent",                       # the endpoint's alias
    messages=[{"role": "user", "content": "what changed in this repo today?"}])
```

## What relay is actually doing

`opencode serve` is not OpenAI-compatible. It is a stateful agent server, so
relay translates rather than forwards:

| OpenAI (what your client sends) | OpenCode (what relay sends) |
| --- | --- |
| stateless `POST /v1/chat/completions` | `POST /session` → `POST /session/{id}/message` → `DELETE /session/{id}` |
| `"model": "anthropic/claude-sonnet-4-5"` | `{"model": {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"}}` |
| `messages[].content` | `parts: [{"type": "text", "text": "..."}]` |
| a `role: "system"` message | the message body's top-level `system` field |
| `choices[0].message.content` | concatenated `parts[*].text` |
| `usage.*_tokens` | `info.tokens` (+ `info.cost`, taken as authoritative) |
| `Authorization: Bearer <key>` | `Authorization: Basic base64(user:password)` |

Sessions are **ephemeral**: one per request, deleted in a `finally`, and
`abort`ed first if the client hung up or the turn hit its deadline. Relay
keeps no agent state.

## Setting one up

### The short way: `./launch.sh`

From the repo root, one file configures both servers and registers the
endpoint:

```bash
cp .env.example .env
```

Set `OPENCODE_SERVER_PASSWORD` (required), and usually `OPENCODE_PROJECT_DIR`
— `opencode serve` binds to one directory and its `bash`/`edit`/`write` tools
act there, so it defaults to this repo, which is rarely what you want. Then:

```bash
./launch.sh
```

It starts `opencode serve`, starts relay, waits for both to answer, and
creates or updates the relay endpoint from `.env` — idempotent, so re-running
after an edit converges rather than duplicating. Ctrl-C stops both. To fill in
`OPENCODE_MODELS`, ask the server what it has:

```bash
./launch.sh --list-models
```

If relay is already running as a systemd service on the same port, `launch.sh`
says so; `./launch.sh --takeover` stops the service first.

### Or as services: `relay start | stop | restart`

`./scripts/install-systemd.sh` installs two systemd *user* units —
`relay.service` and `opencode.service` — both reading the same `.env`:

```bash
./scripts/install-systemd.sh
```

`opencode.service` is `PartOf=relay.service` and pulled in by its `Wants=`, so
one command drives both:

| Command | Effect |
| --- | --- |
| `relay start` | starts relay + opencode, then registers the endpoint from `.env` |
| `relay stop` | stops both |
| `relay restart` | restarts both and re-registers |
| `relay status` | unit state for both, relay health, and each endpoint's health |
| `relay logs` | follows both journals |
| `relay enable` / `disable` | on-boot behavior for both |

Add `--relay-only` to any of them to leave the agent server alone.

`OPENCODE_ENABLED=0` in `.env` takes opencode out entirely: `relay` skips the
unit, and the unit itself exits without starting if something else launches it.
The wrapper also refuses to start (exit 78, no restart loop) when
`OPENCODE_SERVER_PASSWORD` is empty, rather than serving an unauthenticated
agent.

Both units take `RELAY_HOST`, `RELAY_PORT`, `OPENCODE_PORT`, and
`OPENCODE_PROJECT_DIR` from `.env` — nothing is baked into the unit files, so a
re-install cannot revert a local change. Re-run
`./scripts/install-systemd.sh` after pulling this change; it also refreshes
`~/.local/bin/relay` if you installed it there.

### Three different keys

Nothing about this setup is served by confusing them:

| Key | In `.env` | Who checks it |
| --- | --- | --- |
| Relay's client key | `RELAY_API_KEY` | relay, on `/v1/*`, when `RELAY_REQUIRE_CLIENT_KEY=1`. Leave empty to have one generated; `launch.sh` prints it either way. |
| OpenCode's server password | `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | `opencode serve`, as HTTP Basic. Relay presents it as the endpoint's `upstream_key`. |
| The provider's API key | `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY`, … | Anthropic/OpenAI/OpenCode Zen, when the agent calls the model. `launch.sh` exports these into the opencode process; the alternative is `opencode auth login`. |

`./launch.sh --list-models` prints ids as `provider/model`, and the provider
prefix tells you which of the third row you need — `opencode/…` wants
`OPENCODE_API_KEY`, `anthropic/…` wants `ANTHROPIC_API_KEY`. A locally hosted
provider usually needs none.

To register an endpoint against an already-running relay without launching
anything:

```bash
set -a; . ./.env; set +a; python3 scripts/register_opencode.py
```

### The manual way

1. Start the server in the project you want the agent to work in:

   ```bash
   OPENCODE_SERVER_USERNAME=me OPENCODE_SERVER_PASSWORD=s3cret \
     opencode serve --port 4096
   ```

2. In the dashboard, **Endpoints → + Add endpoint**:

   | Field | Value |
   | --- | --- |
   | type | `OpenCode (agent)` |
   | alias | e.g. `agent` — the routing name clients send as `model` |
   | url | `http://127.0.0.1:4096` (**no** `/v1` suffix) |
   | key | `user:password`, or a bare password for the default user `opencode` |
   | available models | one `provider/model` id per line |

   The key must match the server's `OPENCODE_SERVER_USERNAME` /
   `OPENCODE_SERVER_PASSWORD`.

3. **Test connection** hits `/global/health` and reads `/config/providers`.

## Choosing models

An OpenCode server fronts many provider models, so an alias alone is a blunt
handle. The **available models** list makes each one individually addressable:

```
anthropic/claude-sonnet-4-5
anthropic/claude-haiku-4-5
openai/gpt-5
```

With that list:

- `GET /v1/models` advertises all three alongside the `agent` alias, so a
  client's model picker shows them.
- `"model": "openai/gpt-5"` routes to this endpoint and forwards **exactly**
  that model — the list outranks `model_override`.
- `"model": "agent"` uses the endpoint's default: `model_override` (set from
  `OPENCODE_MODEL` in `.env`) if present, otherwise the first allowlisted id
  the probe confirmed. Set `OPENCODE_MODEL` — the discovered order is
  OpenCode's, and the model it happens to list first may not be one your
  account can bill.
- The endpoint will not serve a model that is not on the list, and the ids
  cannot collide with another endpoint's alias or allowlist.

Leave it empty to serve only the alias, on whatever model OpenCode defaults
to. An id with no `provider/` prefix is not sent at all — OpenCode picks.

Clients may also pass a non-standard `"agent"` field (`build`, `plan`, a
custom agent) in the request body; it is forwarded when present.

## Things that will bite you

- **Agent turns are not cheap in prompt tokens.** OpenCode sends its system
  prompt and tool definitions with every turn, so a one-word question bills
  5,000–10,000 prompt tokens before your text is counted. That is inherent to
  driving an agent, not relay overhead, but it shows up in the dashboard and
  it is worth knowing before you point a chat UI at it.

- **A provider error arrives as a `200` with `finish_reason: "error"`.** The
  agent turn itself succeeded; the model call inside it failed. The detail is
  in `relay.agent_error` on the response body (e.g. "No payment method"), and
  the row lands in telemetry with zero tokens.

- **Set `permission` explicitly.** A permission left at `"ask"` parks the turn
  waiting for a human answer that relay cannot give. Relay bounds it with a
  15-minute deadline (`_TURN_TIMEOUT`), then aborts the session and returns
  `504` — but the request has burned a concurrency slot until then. Configure
  the permissions you actually want instead of relying on the deadline.

- **This is remote code execution by design.** OpenCode's `bash`, `edit`, and
  `write` tools run against the directory the server was started in. Anyone
  who can reach relay can drive them. Run relay with
  `RELAY_REQUIRE_CLIENT_KEY=1`; it logs a warning at boot if an agent endpoint
  is registered without it.

- **One server = one project.** `opencode serve` is bound to its launch
  directory. Several projects means several servers means several relay
  endpoints, each with its own alias.

- **Long turns hold a concurrency slot.** `max_concurrency` (18) is shared
  with every other endpoint; agent turns can occupy a slot for minutes.

- **Agent endpoints are alias-only.** They are never chosen for
  `model: "auto"`, never a failover target, and never the default pin. Marking
  one "active" in the dashboard does not redirect `auto` traffic to it.

- **`/v1/embeddings` and `/v1/completions` return `501`.** An agent server
  implements neither.

## Streaming

`stream: true` works and emits valid OpenAI SSE, but it is **synthesized**:
relay awaits the blocking turn, then re-emits it as a role chunk, a content
chunk, a finish chunk, a `usage` chunk, and `[DONE]`. TTFT therefore equals
total latency, which is honest — the upstream produced nothing earlier.

Real deltas would mean subscribing to OpenCode's `GET /event` stream, which is
*global*, not per-session: one shared subscription demultiplexed across
concurrent requests. That is real state in an otherwise stateless proxy, and
it stays out until it earns its keep.

## Telemetry

Everything the dashboard shows works for agent turns. Token counts come from
`info.tokens` (reasoning folded into completion tokens), and **cost comes from
`info.cost`** rather than relay's price table — OpenCode already priced the
turn against the real provider's rates. Tool invocations appear inline in the
completion text as `[tool: bash]` markers; they are not mapped to OpenAI
`tool_calls`, because a guessed mapping would look valid to a client and be
wrong.
