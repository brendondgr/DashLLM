"""SSH tunnel manager: the command we display is the command we run.

- ``build_argv`` produces the ssh invocation as an **argv list** (never a
  shell string) with validated fields, so the displayed string is cosmetic
  and the executed form is injection-safe.
- Child processes are spawned with ``ssh -N -L ...`` and supervised: if an
  enabled tunnel dies, it is restarted with backoff (autossh behavior
  without the dependency).
- Two-level connection test: (1) ssh/forward reachability, (2) an actual
  ``GET /v1/models`` through the forwarded local port.
"""

import asyncio
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.logging import get_logger
from app.db import Database
from app.schemas import TunnelCreate, TunnelOut, TunnelPatch, TunnelTestResult

log = get_logger("tunnels")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_EXTRA_TOKEN_RE = re.compile(r"^[A-Za-z0-9@=:,./_%+-]+$")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def validate_row(row: dict) -> None:
    _require(bool(_NAME_RE.match(row["ssh_user"] or "")),
             "invalid ssh user")
    _require(bool(_HOST_RE.match(row["ssh_host"] or "")),
             "invalid ssh host")
    _require(bool(_HOST_RE.match(row["remote_host"] or "")),
             "invalid remote host")
    for port_field in ("ssh_port", "remote_port", "local_port"):
        _require(1 <= int(row[port_field]) <= 65535,
                 f"invalid {port_field}")
    key = row.get("key_path")
    if key:
        _require(not key.startswith("-"), "invalid key path")
    for token in shlex.split(row.get("extra_opts") or ""):
        _require(bool(_EXTRA_TOKEN_RE.match(token)),
                 f"invalid extra option token: {token!r}")


def build_argv(row: dict) -> list[str]:
    """The exact ssh command for this tunnel, as argv."""
    validate_row(row)
    argv = ["ssh", "-N"]
    if row.get("compress"):
        argv.append("-C")
    argv += ["-L", f"{row['local_port']}:{row['remote_host']}:{row['remote_port']}"]
    argv += ["-p", str(row["ssh_port"])]
    if row.get("key_path"):
        argv += ["-i", str(Path(row["key_path"]).expanduser())]
    if row.get("keepalive"):
        argv += ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    argv += ["-o", "ExitOnForwardFailure=yes",
             "-o", "StrictHostKeyChecking=accept-new"]
    argv += shlex.split(row.get("extra_opts") or "")
    argv.append(f"{row['ssh_user']}@{row['ssh_host']}")
    return argv


def command_string(row: dict) -> str:
    """Display form of build_argv (what the dashboard shows/copies)."""
    return shlex.join(build_argv(row))


@dataclass
class LiveTunnel:
    status: str = "stopped"  # stopped|starting|up|error
    proc: asyncio.subprocess.Process | None = None
    started_at: float | None = None
    last_error: str | None = None
    backoff_s: float = 2.0
    manual_stop: bool = False
    stderr_tail: list[str] = field(default_factory=list)


async def _port_open(host: str, port: int, timeout: float = 3.0
                     ) -> tuple[bool, float]:
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, (time.perf_counter() - t0) * 1000
    except (OSError, asyncio.TimeoutError):
        return False, (time.perf_counter() - t0) * 1000


class TunnelManager:
    def __init__(self, db: Database, http: httpx.AsyncClient,
                 spawn_argv=build_argv, up_timeout: float = 12.0):
        self.db = db
        self.http = http
        self.spawn_argv = spawn_argv  # injectable for tests
        self.up_timeout = up_timeout
        self.live: dict[str, LiveTunnel] = {}
        self._supervisor: asyncio.Task | None = None
        for row in self.db.query("SELECT * FROM tunnels"):
            self.live[row["id"]] = LiveTunnel()

    # ---- CRUD -------------------------------------------------------------
    def rows(self) -> list[dict]:
        return self.db.query("SELECT * FROM tunnels ORDER BY created_ts")

    def row(self, tid: str) -> dict | None:
        return self.db.query_one("SELECT * FROM tunnels WHERE id = ?", (tid,))

    def create(self, spec: TunnelCreate) -> dict:
        row = {"id": str(uuid.uuid4()), **spec.model_dump(),
               "created_ts": time.time()}
        row["compress"] = int(row["compress"])
        row["keepalive"] = int(row["keepalive"])
        row["enabled"] = int(row["enabled"])
        validate_row(row)
        self.db.execute(
            "INSERT INTO tunnels (id, name, ssh_host, ssh_port, ssh_user,"
            " key_path, remote_host, remote_port, local_port, compress,"
            " keepalive, extra_opts, enabled, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["name"], row["ssh_host"], row["ssh_port"],
             row["ssh_user"], row["key_path"], row["remote_host"],
             row["remote_port"], row["local_port"], row["compress"],
             row["keepalive"], row["extra_opts"], row["enabled"],
             row["created_ts"]))
        self.live[row["id"]] = LiveTunnel()
        log.info("tunnel created", extra={"data": {
            "id": row["id"], "name": row["name"],
            "target": f"{row['ssh_user']}@{row['ssh_host']}:{row['ssh_port']}",
            "forward": f"{row['local_port']}->{row['remote_host']}:{row['remote_port']}"}})
        return row

    async def patch(self, tid: str, patch: TunnelPatch) -> dict | None:
        row = self.row(tid)
        if row is None:
            return None
        changes = patch.model_dump(exclude_none=True)
        for k in ("compress", "keepalive", "enabled"):
            if k in changes:
                changes[k] = int(changes[k])
        merged = {**row, **changes}
        validate_row(merged)
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.db.execute(f"UPDATE tunnels SET {sets} WHERE id = ?",
                        [*changes.values(), tid])
        log.info("tunnel updated", extra={"data": {"id": tid, **changes}})
        # Port/connection changes take effect by restarting the child.
        needs_restart = self.live[tid].status in ("up", "starting") and any(
            k in changes for k in ("ssh_host", "ssh_port", "ssh_user",
                                   "key_path", "remote_host", "remote_port",
                                   "local_port", "compress", "keepalive",
                                   "extra_opts"))
        if needs_restart:
            log.info("tunnel restarting to apply changes",
                     extra={"data": {"id": tid}})
            await self.stop(tid)
            await self.start(tid)
        return self.row(tid)

    async def delete(self, tid: str) -> bool:
        if self.row(tid) is None:
            return False
        await self.stop(tid)
        self.db.execute("DELETE FROM tunnels WHERE id = ?", (tid,))
        self.live.pop(tid, None)
        log.info("tunnel deleted", extra={"data": {"id": tid}})
        return True

    # ---- lifecycle ----------------------------------------------------------
    async def start(self, tid: str) -> TunnelOut | None:
        row = self.row(tid)
        if row is None:
            return None
        live = self.live.setdefault(tid, LiveTunnel())
        if live.proc and live.proc.returncode is None:
            return self.out(tid)  # already running
        argv = self.spawn_argv(row)
        live.status = "starting"
        live.last_error = None
        live.manual_stop = False
        live.stderr_tail = []
        log.info("tunnel starting", extra={"data": {
            "id": tid, "argv": argv}})
        try:
            live.proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            live.status = "error"
            live.last_error = str(e)
            log.error("tunnel spawn failed",
                      extra={"data": {"id": tid, "error": str(e)}})
            return self.out(tid)
        asyncio.create_task(self._drain_stderr(tid, live.proc))
        # up when the local forward accepts a TCP connection
        deadline = time.time() + self.up_timeout
        while time.time() < deadline:
            if live.proc.returncode is not None:
                live.status = "error"
                live.last_error = (
                    "; ".join(live.stderr_tail[-3:]) or
                    f"ssh exited with code {live.proc.returncode}")
                log.error("tunnel died during startup", extra={"data": {
                    "id": tid, "error": live.last_error}})
                return self.out(tid)
            ok, _ = await _port_open("127.0.0.1", row["local_port"], 1.0)
            if ok:
                live.status = "up"
                live.started_at = time.time()
                live.backoff_s = 2.0
                self.db.execute(
                    "UPDATE tunnels SET enabled = 1 WHERE id = ?", (tid,))
                log.info("tunnel up", extra={"data": {
                    "id": tid, "local_port": row["local_port"],
                    "pid": live.proc.pid}})
                return self.out(tid)
            await asyncio.sleep(0.25)
        live.status = "error"
        live.last_error = "timed out waiting for local forward to accept"
        log.error("tunnel start timed out", extra={"data": {"id": tid}})
        await self._kill(live)
        return self.out(tid)

    async def _drain_stderr(self, tid: str, proc) -> None:
        try:
            assert proc.stderr is not None
            async for line in proc.stderr:
                text = line.decode(errors="replace").strip()
                if text:
                    live = self.live.get(tid)
                    if live is not None:
                        live.stderr_tail = (live.stderr_tail + [text])[-10:]
                    log.debug("ssh stderr", extra={"data": {
                        "id": tid, "line": text}})
        except Exception:
            pass

    async def _kill(self, live: LiveTunnel) -> None:
        if live.proc and live.proc.returncode is None:
            live.proc.terminate()
            try:
                await asyncio.wait_for(live.proc.wait(), 5)
            except asyncio.TimeoutError:
                live.proc.kill()
                await live.proc.wait()

    async def stop(self, tid: str) -> TunnelOut | None:
        row = self.row(tid)
        if row is None:
            return None
        live = self.live.setdefault(tid, LiveTunnel())
        live.manual_stop = True
        await self._kill(live)
        live.status = "stopped"
        live.started_at = None
        self.db.execute("UPDATE tunnels SET enabled = 0 WHERE id = ?", (tid,))
        log.info("tunnel stopped", extra={"data": {"id": tid}})
        return self.out(tid)

    async def start_supervisor(self) -> None:
        self._supervisor = asyncio.create_task(
            self._supervise(), name="tunnel-supervisor")
        # bring enabled tunnels up at boot
        for row in self.rows():
            if row["enabled"]:
                await self.start(row["id"])
        log.info("tunnel supervisor started")

    async def stop_supervisor(self) -> None:
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        for tid, live in self.live.items():
            if live.proc and live.proc.returncode is None:
                await self._kill(live)
                live.status = "stopped"
        log.info("tunnel supervisor stopped")

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            for row in self.rows():
                live = self.live.get(row["id"])
                if (live is None or not row["enabled"] or live.manual_stop
                        or live.status not in ("up", "error")):
                    continue
                died = (live.status == "error"
                        or live.proc is None
                        or live.proc.returncode is not None)
                if not died:
                    continue
                log.warning("tunnel down; restarting with backoff",
                            extra={"data": {"id": row["id"],
                                            "backoff_s": live.backoff_s}})
                await asyncio.sleep(live.backoff_s)
                live.backoff_s = min(live.backoff_s * 2, 30.0)
                await self.start(row["id"])

    # ---- test ---------------------------------------------------------------
    async def test(self, tid: str, probe_timeout: float = 5.0
                   ) -> TunnelTestResult | None:
        row = self.row(tid)
        if row is None:
            return None
        live = self.live.setdefault(tid, LiveTunnel())
        result = TunnelTestResult(ssh_ok=False)
        if live.status == "up" and live.proc and live.proc.returncode is None:
            ok, ms = await _port_open("127.0.0.1", row["local_port"])
            result.ssh_ok = ok
            result.ssh_latency_ms = round(ms, 1)
            if not ok:
                result.error = "tunnel process running but forward not accepting"
        else:
            # tunnel not running: check the ssh server is reachable at all
            ok, ms = await _port_open(row["ssh_host"], row["ssh_port"])
            result.ssh_ok = ok
            result.ssh_latency_ms = round(ms, 1)
            if not ok:
                result.error = (
                    f"cannot reach {row['ssh_host']}:{row['ssh_port']}")
        if result.ssh_ok and live.status == "up":
            url = f"http://127.0.0.1:{row['local_port']}/v1/models"
            t0 = time.perf_counter()
            try:
                resp = await self.http.get(url, timeout=probe_timeout)
                result.endpoint_latency_ms = round(
                    (time.perf_counter() - t0) * 1000, 1)
                if resp.status_code == 200:
                    result.endpoint_ok = True
                    result.models = [
                        m.get("id", "?")
                        for m in resp.json().get("data", [])]
                else:
                    result.error = f"endpoint HTTP {resp.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                result.endpoint_latency_ms = round(
                    (time.perf_counter() - t0) * 1000, 1)
                result.error = f"{type(e).__name__}: {e}"
        log.info("tunnel test", extra={"data": {
            "id": tid, **result.model_dump()}})
        return result

    # ---- views -----------------------------------------------------------------
    def out(self, tid: str) -> TunnelOut | None:
        row = self.row(tid)
        if row is None:
            return None
        live = self.live.setdefault(tid, LiveTunnel())
        running = live.proc is not None and live.proc.returncode is None
        return TunnelOut(
            id=row["id"], name=row["name"], ssh_host=row["ssh_host"],
            ssh_port=row["ssh_port"], ssh_user=row["ssh_user"],
            key_path=row["key_path"], remote_host=row["remote_host"],
            remote_port=row["remote_port"], local_port=row["local_port"],
            compress=bool(row["compress"]), keepalive=bool(row["keepalive"]),
            extra_opts=row["extra_opts"], enabled=bool(row["enabled"]),
            status=live.status, pid=live.proc.pid if running else None,
            last_error=live.last_error, started_at=live.started_at,
            uptime_s=round(time.time() - live.started_at, 1)
            if live.started_at else 0.0)

    def list_out(self) -> list[TunnelOut]:
        return [self.out(r["id"]) for r in self.rows()]
