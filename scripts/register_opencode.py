#!/usr/bin/env python3
"""Register (or update) relay's OpenCode endpoint from environment variables.

Idempotent by endpoint *name*: run it as often as you like and the endpoint
converges on what ``.env`` says. Called by ``launch.sh`` once both servers are
up; also useful on its own after editing ``.env``:

    set -a; . ./.env; set +a; python3 scripts/register_opencode.py

``--list-models`` instead prints the ``provider/model`` ids the OpenCode server
offers, for pasting into ``OPENCODE_MODELS``.

Standard library only — this runs before/outside the backend's venv.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 15.0


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def opencode_get(path: str) -> dict:
    """GET from the OpenCode server with the Basic credentials from .env."""
    user = env("OPENCODE_SERVER_USERNAME", "opencode")
    password = env("OPENCODE_SERVER_PASSWORD")
    url = f"http://127.0.0.1:{env('OPENCODE_PORT', '4096')}{path}"
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def list_models() -> int:
    try:
        data = opencode_get("/config/providers")
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        print(f"could not read the provider catalog: {e}", file=sys.stderr)
        return 1

    ids: list[str] = []
    for provider in data.get("providers") or []:
        pid = provider.get("id")
        models = provider.get("models")
        names = (list(models) if isinstance(models, dict)
                 else [m.get("id") for m in (models or []) if isinstance(m, dict)])
        ids.extend(f"{pid}/{m}" for m in names if pid and m)

    if not ids:
        print("  (this server reports no models — is a provider configured?)")
        return 0
    for mid in sorted(ids):
        print(f"  {mid}")
    default = data.get("default")
    if isinstance(default, dict) and default:
        pid, mid = next(iter(default.items()))
        print(f"\n  server default: {pid}/{mid}")
    print(f"\n  OPENCODE_MODELS={','.join(sorted(ids))}")
    return 0


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list | None]:
    url = f"http://127.0.0.1:{env('RELAY_PORT', '4000')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = env("RELAY_ADMIN_TOKEN")
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        return e.code, {"detail": detail}


def main() -> int:
    if "--list-models" in sys.argv[1:]:
        return list_models()

    password = env("OPENCODE_SERVER_PASSWORD")
    if not password:
        print("OPENCODE_SERVER_PASSWORD is empty; refusing to register an "
              "unauthenticated agent endpoint", file=sys.stderr)
        return 2

    user = env("OPENCODE_SERVER_USERNAME", "opencode")
    name = env("OPENCODE_ENDPOINT_NAME", "opencode")
    alias = env("OPENCODE_ALIAS", "agent")
    models = [m.strip() for m in env("OPENCODE_MODELS").split(",") if m.strip()]

    spec = {
        "name": name,
        "alias": alias,
        "base_url": f"http://127.0.0.1:{env('OPENCODE_PORT', '4096')}",
        "protocol": "opencode",
        "server_type": "opencode",
        # Relay stores this and presents it as HTTP Basic; see
        # adapters/opencode.py::_basic_auth.
        "upstream_key": f"{user}:{password}",
        "available_models": models,
    }

    status, existing = api("GET", "/admin/endpoints")
    if status != 200 or not isinstance(existing, list):
        print(f"could not list endpoints (HTTP {status}): {existing}",
              file=sys.stderr)
        return 1

    match = next((e for e in existing if e.get("name") == name), None)
    if match is None:
        status, body = api("POST", "/admin/endpoints", spec)
        verb = "registered"
    else:
        # PATCH, not delete-and-recreate: the endpoint id is referenced by
        # every telemetry row already written against it.
        status, body = api("PATCH", f"/admin/endpoints/{match['id']}", spec)
        verb = "updated"

    if status >= 400:
        print(f"could not register endpoint (HTTP {status}): "
              f"{(body or {}).get('detail', body)}", file=sys.stderr)
        return 1

    served = body.get("available_models") or []
    print(f"  {verb} endpoint {name!r} · alias {alias!r} · health "
          f"{body.get('health', '?')}")
    print(f"  models: {', '.join(served) if served else '(alias only)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
