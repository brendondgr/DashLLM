"""The upstream-protocol seam.

Relay's hot path is OpenAI-shaped end to end: an inbound
``POST /v1/chat/completions`` is forwarded as-is, usage is read from
``usage.*_tokens``, health is a ``GET /models``. That is true of every
llama.cpp / vLLM / ollama box, and for those the adapter is a passthrough.

It is *not* true of an agent server like OpenCode, which speaks sessions and
parts instead of messages and choices. Rather than sprinkle protocol branches
through ``proxy.py``, each protocol implements this two-method interface:

- :meth:`UpstreamAdapter.probe` — replaces the hardcoded ``GET /models`` in
  ``health.py``; returns the same ``EndpointTestResult`` the state machine and
  the dashboard's "Test connection" button already consume.
- :meth:`UpstreamAdapter.forward` — owns one forwarding attempt and returns an
  OpenAI-shaped response.

Deliberately narrow. Everything above a single attempt — the concurrency gate,
alias pinning, the failover loop, the telemetry call sites — stays in
``ProxyService`` and is shared by every protocol. An adapter that owned
``handle()`` would fork all of it.
"""

from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from fastapi.responses import Response

from app.schemas import EndpointTestResult
from app.services.telemetry import RequestRecord

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime only
    from app.services.proxy import ProxyService

# Hop-by-hop headers never forwarded either direction.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "authorization", "x-admin-token",
}


class RetryableUpstreamError(Exception):
    """This attempt failed before any byte reached the client, so the dispatch
    layer may transparently try another endpoint."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class UpstreamSaturated(RetryableUpstreamError):
    """This attempt failed because *relay* ran out of local capacity — the
    shared httpx connection pool had no slot free — not because the upstream
    misbehaved.

    The distinction is load-bearing. A ``PoolTimeout`` raised while 300
    requests are in flight says nothing about the model server, but the
    health state machine cannot tell the difference: three of them in a row
    marked a perfectly healthy endpoint FAILED and took the whole pool down
    with it. Failing over is equally pointless — every endpoint shares the
    same pool — so the dispatch layer answers 503 + Retry-After instead of
    burning attempts and health credit.
    """


def classify_httpx_error(e: httpx.HTTPError) -> RetryableUpstreamError:
    """Map an httpx exception to the right retryable class.

    ``PoolTimeout`` is the local-saturation signal; everything else
    (connect/read timeouts, resets, protocol errors) is genuinely the
    upstream's problem and should feed the health state machine.
    """
    message = f"{type(e).__name__}: {e}"
    if isinstance(e, httpx.PoolTimeout):
        return UpstreamSaturated(
            "relay connection pool exhausted: " + message, status=503)
    return RetryableUpstreamError(message)


class StaleModelOverride(Exception):
    """Upstream rejected the endpoint's pinned model_override as unknown — the
    model on that port was swapped out. Signals the dispatch layer to clear
    the override and retry the same endpoint with the discovered model."""

    def __init__(self, model: str):
        super().__init__(model)
        self.model = model


class UpstreamAdapter:
    """Base class. Subclasses are stateless singletons (see ``get_adapter``)."""

    name: str = "openai"

    def supports(self, route: str) -> bool:
        """Whether this protocol can serve an OpenAI route name (as produced by
        ``proxy._route_name``). Unsupported routes get a 501 rather than being
        forwarded into a shape the upstream can't answer."""
        return True

    async def probe(self, http: httpx.AsyncClient, endpoint: dict,
                    timeout: float) -> EndpointTestResult:
        raise NotImplementedError

    async def forward(self, proxy: "ProxyService", request: Request,
                      endpoint: dict, path: str, record: RequestRecord,
                      body: bytes, t0: float) -> Response:
        raise NotImplementedError
