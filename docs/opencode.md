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
says so; `./launch.sh --takeover` stops the service first. The service reads
the same `.env` (via `EnvironmentFile`), so `relay start` and `./launch.sh`
agree on `RELAY_REQUIRE_CLIENT_KEY` and `RELAY_ADMIN_TOKEN` — but the unit's
`ExecStart` pins its own `--port`, so `RELAY_PORT` only moves the port for
`launch.sh`. Re-run `./scripts/install-systemd.sh` after pulling this change.

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
- `"model": "agent"` uses the endpoint's default: `model_override` if set,
  otherwise the first allowlisted id the probe confirmed.
- The endpoint will not serve a model that is not on the list, and the ids
  cannot collide with another endpoint's alias or allowlist.

Leave it empty to serve only the alias, on whatever model OpenCode defaults
to. An id with no `provider/` prefix is not sent at all — OpenCode picks.

Clients may also pass a non-standard `"agent"` field (`build`, `plan`, a
custom agent) in the request body; it is forwarded when present.

## Things that will bite you

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
