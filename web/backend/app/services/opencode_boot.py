"""Register the OpenCode agent endpoint at boot, from configuration alone.

This is what makes `git clone && ./launch.sh` produce a working agent endpoint
with nothing to fill in. There used to be a `scripts/register_opencode.py` that
launch.sh called over HTTP once relay was up; doing it in-process instead means
every start path (launch.sh, the systemd unit, a bare uvicorn) ends up with the
same endpoint, and there is no second place where the model list is decided.

Two phases, because `opencode serve` may not be listening yet when relay boots:

1. **Upsert, synchronously.** The endpoint exists before the first request can
   arrive, so the alias resolves and the dashboard shows it (unhealthy at
   worst) rather than "no endpoints".
2. **Discover, in the background.** Probe until the provider catalog answers,
   then publish the free models it reports as the endpoint's addressable
   model list. Only then can a client ask for `opencode/hy3-free` by name.

The endpoint is reconciled from configuration on every boot: edits made in the
dashboard to *this* endpoint's URL, credential, or model list last until the
next restart. Anything else you add through the UI is left alone.
"""

import asyncio

from app.core.logging import get_logger
from app.schemas import EndpointCreate, EndpointPatch

log = get_logger("opencode")

# Roughly two minutes of patience, front-loaded: `opencode serve` normally
# answers in a second or two, but a cold start that has to resolve providers
# can take a while, and giving up early leaves the endpoint model-less until
# someone notices.
_RETRY_DELAYS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0)


def ensure_endpoint(app) -> str | None:
    """Create or reconcile the OpenCode endpoint. Returns its id, or None when
    OpenCode is switched off or has no credential."""
    cfg = app.state.opencode
    if not cfg.enabled:
        log.info("opencode disabled (OPENCODE_ENABLED=0); not registering")
        return None

    credential = cfg.credential()
    if not credential:
        # `opencode serve` refuses anonymous callers, so an endpoint without a
        # credential could only ever be unhealthy. Say why instead.
        log.warning(
            "no OpenCode credential found; not registering the agent endpoint."
            " Start via ./launch.sh (it mints one) or set"
            " OPENCODE_SERVER_PASSWORD",
            extra={"data": {"auth_file": str(cfg.auth_file)}})
        return None

    router = app.state.router
    existing = next(
        (r for r in router.endpoints.values() if r["name"] == cfg.endpoint_name),
        None)
    try:
        if existing is None:
            row = router.create(EndpointCreate(
                name=cfg.endpoint_name, alias=cfg.alias,
                base_url=cfg.base_url, protocol="opencode",
                server_type="opencode", upstream_key=credential))
            log.info("registered the OpenCode endpoint", extra={"data": {
                "name": cfg.endpoint_name, "alias": cfg.alias,
                "url": cfg.base_url}})
            return row["id"]
        router.patch(existing["id"], EndpointPatch(
            alias=cfg.alias, base_url=cfg.base_url, protocol="opencode",
            server_type="opencode", upstream_key=credential, enabled=True))
        return existing["id"]
    except ValueError as e:
        # An alias collision with an endpoint someone added by hand. Leaving
        # the stack up without the agent beats refusing to boot.
        log.error("could not register the OpenCode endpoint", extra={"data": {
            "error": str(e)}})
        return None


async def discover_models(app, eid: str) -> None:
    """Probe until the catalog answers, then publish the free models."""
    for delay in _RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        result = await app.state.prober.probe_and_report(eid)
        if result is None:
            return  # endpoint was deleted underneath us
        if result.ok and result.models:
            app.state.router.sync_allowlist(eid, result.models)
            log.info("agent models published", extra={"data": {
                "models": result.models}})
            return
        if result.ok:
            log.warning(
                "OpenCode answered but offers no free models — relay only"
                " serves models with 'free' in the id, so the alias has"
                " nothing to run")
            return
    log.warning(
        "OpenCode never answered; the agent alias is registered but has no"
        " models. Is `opencode serve` running?",
        extra={"data": {"url": app.state.opencode.base_url}})
