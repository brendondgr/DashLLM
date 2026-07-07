"""Read the user's ``~/.ssh/config`` so the dashboard can offer real host
aliases and resolve them to their *actual* effective settings.

A "shorthand" tunnel command like ``ssh -N -L 9090:localhost:9090 skynet-alt``
relies entirely on ``~/.ssh/config`` for the hostname, user, port, identity
file, and any ``ProxyJump``. We resolve those with ``ssh -G <alias>`` — the
same resolution ssh itself performs — instead of guessing, so the UI shows
(and connections use) the real ``IdentityFile`` rather than a random default.

``ssh -G`` prints one ``identityfile`` line for an explicitly configured key,
but the full default fan-out (``id_rsa``/``id_ecdsa``/``id_ed25519``/…) when a
host has none. We surface that distinction as ``identity_explicit`` so the UI
can tell "this host has a specific key" from "ssh will try every default".
"""

import asyncio
import os
import re
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("ssh-config")

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@%:-]*$")
# The default identity files ssh falls back to when a host configures none.
_DEFAULT_KEY_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ecdsa_sk",
    "id_ed25519", "id_ed25519_sk", "id_xmss",
}


def config_path() -> Path:
    return Path(os.path.expanduser("~/.ssh/config"))


def list_host_aliases(path: Path | None = None) -> list[str]:
    """First concrete alias of every ``Host`` block (skips wildcard patterns
    like ``class1*``, which can't be connected to by name)."""
    p = path or config_path()
    try:
        text = p.read_text()
    except OSError:
        return []
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("=", " ").split()
        if len(parts) < 2 or parts[0].lower() != "host":
            continue
        for tok in parts[1:]:
            if any(c in tok for c in "*?!"):
                continue
            if tok not in seen:
                seen.add(tok)
                aliases.append(tok)
            break  # one representative alias per Host line is enough
    return aliases


def _expand(path: str) -> str:
    return os.path.expanduser(path) if path.startswith("~") else path


async def resolve(alias: str, timeout: float = 5.0) -> dict:
    """Effective config for ``alias`` via ``ssh -G`` (no network I/O).

    Returns hostname/user/port, the resolved identity files, whether a
    specific key is configured (vs. the default fan-out), and any proxy jump.
    Raises ``ValueError`` on a bad alias or if ssh can't resolve it.
    """
    alias = (alias or "").strip()
    if not _ALIAS_RE.match(alias):
        raise ValueError("invalid host alias")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-G", alias,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise ValueError(f"ssh not available: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("ssh -G timed out")
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()
        raise ValueError(msg[-1] if msg else f"ssh -G exited {proc.returncode}")

    fields: dict[str, str] = {}
    identity_files: list[str] = []
    for line in out.decode(errors="replace").splitlines():
        key, _, val = line.strip().partition(" ")
        if not val:
            continue
        key = key.lower()
        if key == "identityfile":
            identity_files.append(_expand(val))
        elif key not in fields:
            fields[key] = val

    # ssh -G returns ONLY the configured key(s) when a host sets IdentityFile,
    # but the full default fan-out (id_rsa/id_ecdsa/id_ed25519/…) when it does
    # not. So a specific key is configured unless we got the whole default set.
    non_default = any(
        Path(f).name not in _DEFAULT_KEY_NAMES for f in identity_files)
    identity_explicit = bool(identity_files) and (
        non_default or len(identity_files) == 1)
    return {
        "alias": alias,
        "hostname": fields.get("hostname"),
        "user": fields.get("user"),
        "port": int(fields["port"]) if fields.get("port", "").isdigit() else 22,
        "identity_files": identity_files,
        "identity_file": identity_files[0] if identity_files else None,
        "identity_explicit": identity_explicit,
        "proxyjump": fields.get("proxyjump") or None,
    }


async def list_hosts(timeout: float = 5.0) -> list[dict]:
    """Every concrete alias, each resolved to its effective settings.

    Resolution failures are skipped rather than failing the whole list."""
    aliases = list_host_aliases()
    if not aliases:
        return []
    results = await asyncio.gather(
        *(resolve(a, timeout) for a in aliases), return_exceptions=True)
    hosts: list[dict] = []
    for alias, res in zip(aliases, results):
        if isinstance(res, dict):
            hosts.append(res)
        else:
            log.debug("ssh host resolve failed",
                      extra={"data": {"alias": alias, "error": str(res)}})
    return hosts
