# Route Map

## Frontend pages (Astro)

| Route | File | Purpose |
| --- | --- | --- |
| `/` | `web/frontend/src/pages/index.astro` | Single-page dashboard; all six screens are client-side state within the React island (matches the prototype's sidebar navigation, no URL routing) |

Screens inside the island: Dashboard, Requests, Endpoints, SSH Tunnel,
Proxy Info, Settings.

## Backend — OpenAI-compatible client plane

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/chat/completions` | primary; streaming + non-streaming, fully instrumented |
| POST | `/v1/completions` | instrumented |
| POST | `/v1/embeddings` | instrumented |
| GET | `/v1/models` | synthesized: `auto` + every enabled endpoint alias (model-alias routing) |
| ANY | `/v1/{path}` | generic passthrough, recorded coarsely |

## Backend — control plane

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/admin/endpoints` | list + live health |
| POST | `/admin/endpoints` | create |
| PATCH | `/admin/endpoints/{id}` | edit url/key/priority/enabled |
| DELETE | `/admin/endpoints/{id}` | remove |
| POST | `/admin/endpoints/{id}/activate` | hot-swap: pin as active |
| POST | `/admin/endpoints/{id}/test` | probe now → `{ok, latency_ms, models[]}` |
| GET | `/admin/endpoints/health` | pool snapshot |
| GET/PUT | `/admin/router` | policy (manual|priority) + resolved target |
| GET | `/admin/tunnels` | list with live status |
| POST | `/admin/tunnels` | create |
| PATCH | `/admin/tunnels/{id}` | edit (incl. ssh_port / local_port) |
| DELETE | `/admin/tunnels/{id}` | remove |
| POST | `/admin/tunnels/{id}/start` · `/stop` | lifecycle |
| POST | `/admin/tunnels/{id}/test` | `{ssh_ok, endpoint_ok, models[], error}` |
| GET | `/admin/tunnels/{id}/command` | exact `ssh -N -L …` display string |
| GET/PUT | `/admin/settings` | toggles, retention, proxy port |
| GET | `/admin/proxy` | base url, masked client key, uptime, totals |
| POST | `/admin/proxy/key` | regenerate client API key |
| POST | `/admin/logs/frontend` | frontend UI event ingestion |
| GET | `/health` | service liveness |

## Backend — stats plane (ECharts-shaped)

| Path | Feeds |
| --- | --- |
| `/admin/stats/summary` | KPI row |
| `/admin/stats/volume` | Request volume chart |
| `/admin/stats/tokens/timeseries` | Tokens in/out area chart |
| `/admin/stats/tokens/by-hour` | Tokens by hour-of-day bars |
| `/admin/stats/tokens/by-day` | Tokens per day bars |
| `/admin/stats/by-model` | model breakdown |
| `/admin/stats/by-endpoint` | endpoint breakdown bars |
| `/admin/stats/latency` | TTFT / tok/s trend |
| `/admin/stats/recent` | Requests screen table |
| `/admin/stats/live` | in-flight gauge + concurrency series |

Common query params: `window` ∈ {`1h`,`24h`,`7d`,`30d`,`all`} or
`from`/`to` (unix seconds) for custom ranges; optional `endpoint_id`, `model`.
