"""Client-key helpers for telemetry attribution.

There is no auth plane. relay is an open OpenAI-compatible front door: no
admin token, no client key, no accounts. What survives here is labeling —
if a client happens to send ``Authorization: Bearer …`` (every OpenAI SDK
does, because the field is not optional in most of them), the last four
characters become the ``client_key`` column so the dashboard can tell two
callers apart. The value is never validated, never stored in full, and never
forwarded upstream.
"""

from fastapi import Request


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def mask_key(key: str | None) -> str | None:
    """A stable, non-reversible label for a caller. Never the key itself."""
    if not key:
        return None
    return "…" + key[-4:] if len(key) >= 4 else "…"
