"""Interactive, manually-controlled SSH tunnel sessions — one per endpoint.

Unlike ``tunnels.TunnelManager`` (structured, supervised, auto-restarting),
these sessions exist so a user can click **Connect** on an endpoint and drive
an ``ssh`` command that may *prompt* — for a host-key confirmation, a key
passphrase, or a password. The command runs inside a real pseudo-terminal so
those prompts appear; we surface the prompt text to the dashboard and feed the
user's typed answer back into the PTY. Nothing connects on its own: no boot
autostart, no restart-on-death. The command is parsed as an argv list and
exec'd directly (never through a shell), so the pasted string is injection-safe.

State machine per endpoint::

    idle → connecting ⇄ awaiting_input → up
                     ↘ error / stopped
"""

import asyncio
import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.schemas import TunnelSessionStatus

log = get_logger("tunnel-sessions")

# A trailing line that looks like ssh is waiting for the user to type something.
_PROMPT_RE = re.compile(
    r"(?:"
    r"password:|"
    r"password for [^:]+:|"
    r"enter passphrase[^:]*:|"
    r"\(yes/no(?:/\[fingerprint\])?\)\??|"
    r"verification code:|"
    r"otp:|"
    r"[Pp]asscode:"
    r")\s*$"
)
# Prompts whose answer should be masked in the UI and never logged/echoed.
_SECRET_RE = re.compile(r"passw|passphrase|passcode|verification code|otp", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\r\x00-\x08\x0b\x0c\x0e-\x1f]")

_MAX_OUTPUT_LINES = 60


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_local_port(command: str, explicit: int | None = None) -> int | None:
    """Local port a tunnel forwards, from an ``-L [bind:]lport:host:rport`` flag."""
    if explicit:
        return explicit
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for i, tok in enumerate(argv):
        spec = None
        if tok == "-L" and i + 1 < len(argv):
            spec = argv[i + 1]
        elif tok.startswith("-L") and len(tok) > 2:
            spec = tok[2:]
        if spec:
            parts = spec.split(":")
            # forms: lport:host:rport  |  bind:lport:host:rport
            if len(parts) >= 3:
                try:
                    return int(parts[-3])
                except ValueError:
                    return None
    return None


def validate_command(command: str) -> list[str]:
    """Parse an ssh command into argv, rejecting anything that isn't ssh."""
    command = (command or "").strip()
    if not command:
        raise ValueError("empty ssh command")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise ValueError(f"cannot parse command: {e}")
    if not argv:
        raise ValueError("empty ssh command")
    if os.path.basename(argv[0]) != "ssh":
        raise ValueError("command must invoke ssh")
    return argv


async def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


@dataclass
class Session:
    endpoint_id: str
    command: str = ""
    local_port: int | None = None
    status: str = "idle"  # idle|connecting|awaiting_input|up|error|stopped
    prompt: str | None = None
    prompt_secret: bool = False
    output: list[str] = field(default_factory=list)
    last_error: str | None = None
    started_at: float | None = None
    pid: int | None = None
    proc: asyncio.subprocess.Process | None = None
    master_fd: int | None = None
    task: asyncio.Task | None = None
    _tail: str = ""  # partial (unterminated) trailing line, for prompt detection
    _scrub: str = ""  # a just-sent secret to strip from the next echo, if any

    def record(self, text: str) -> None:
        if self._scrub:
            text = text.replace(self._scrub, "")
            self._scrub = ""
        clean = _clean(text)
        if not clean:
            return
        combined = self._tail + clean
        lines = combined.split("\n")
        self._tail = lines.pop()  # last element is the unterminated remainder
        for ln in lines:
            ln = ln.rstrip()
            if ln:
                self.output.append(ln)
        del self.output[:-_MAX_OUTPUT_LINES]


class TunnelSessionManager:
    """Owns the live interactive ssh processes, keyed by endpoint id."""

    def __init__(self, up_grace: float = 45.0):
        # up_grace: how long we keep polling for the forward to open before
        # giving up (only relevant once auth has cleared).
        self.up_grace = up_grace
        self.sessions: dict[str, Session] = {}

    def _get(self, eid: str) -> Session:
        return self.sessions.setdefault(eid, Session(endpoint_id=eid))

    def status(self, eid: str) -> TunnelSessionStatus:
        s = self._get(eid)
        running = s.proc is not None and s.proc.returncode is None
        return TunnelSessionStatus(
            endpoint_id=eid, status=s.status, prompt=s.prompt,
            prompt_secret=s.prompt_secret, output=list(s.output),
            last_error=s.last_error, local_port=s.local_port,
            pid=s.pid if running else None,
            uptime_s=round(time.time() - s.started_at, 1) if s.started_at and s.status == "up" else 0.0)

    def all_status(self) -> list[TunnelSessionStatus]:
        return [self.status(eid) for eid in self.sessions]

    async def connect(self, eid: str, command: str,
                      local_port: int | None = None) -> TunnelSessionStatus:
        s = self._get(eid)
        if s.proc is not None and s.proc.returncode is None:
            return self.status(eid)  # already running
        argv = validate_command(command)
        s.command = command
        s.local_port = parse_local_port(command, local_port)
        s.status = "connecting"
        s.prompt = None
        s.prompt_secret = False
        s.last_error = None
        s.output = []
        s._tail = ""
        s.started_at = None

        master, slave = os.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True)
        except OSError as e:
            os.close(master)
            os.close(slave)
            s.status = "error"
            s.last_error = str(e)
            log.error("tunnel session spawn failed",
                      extra={"data": {"endpoint": eid, "error": str(e)}})
            return self.status(eid)
        os.close(slave)
        os.set_blocking(master, False)
        s.proc = proc
        s.master_fd = master
        s.pid = proc.pid
        s.task = asyncio.create_task(self._pump(eid), name=f"tunnel-session-{eid}")
        log.info("tunnel session connecting", extra={"data": {
            "endpoint": eid, "pid": proc.pid, "local_port": s.local_port,
            "argv": argv}})
        return self.status(eid)

    async def _pump(self, eid: str) -> None:
        s = self.sessions[eid]
        proc = s.proc
        master = s.master_fd
        assert proc is not None and master is not None
        deadline_up: float | None = None
        try:
            while True:
                if proc.returncode is not None:
                    break
                try:
                    data = os.read(master, 4096)
                except (BlockingIOError, InterruptedError):
                    data = b""
                except OSError:
                    break
                if data:
                    text = data.decode(errors="replace")
                    s.record(text)
                    tail = _clean(s._tail).strip()
                    if tail and _PROMPT_RE.search(tail):
                        s.status = "awaiting_input"
                        s.prompt = tail
                        s.prompt_secret = bool(_SECRET_RE.search(tail))
                        log.info("tunnel session prompt", extra={"data": {
                            "endpoint": eid, "secret": s.prompt_secret,
                            "prompt": "<hidden>" if s.prompt_secret else tail}})
                # Once we're not blocked on a prompt, watch for the forward.
                if s.status in ("connecting", "up") and s.local_port:
                    if await _port_open(s.local_port, 0.5):
                        if s.status != "up":
                            s.status = "up"
                            s.started_at = time.time()
                            s.prompt = None
                            log.info("tunnel session up", extra={"data": {
                                "endpoint": eid, "local_port": s.local_port}})
                        deadline_up = None
                    elif s.status == "connecting":
                        if deadline_up is None:
                            deadline_up = time.time() + self.up_grace
                        elif time.time() > deadline_up:
                            s.status = "error"
                            s.last_error = (
                                "authenticated but the forwarded port never "
                                "opened (check the -L spec / remote service)")
                            await self._kill(s)
                            break
                if not data:
                    await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tunnel session pump error",
                          extra={"data": {"endpoint": eid}})
        finally:
            await self._finalize(eid)

    async def _finalize(self, eid: str) -> None:
        s = self.sessions.get(eid)
        if s is None:
            return
        if s.master_fd is not None:
            try:
                os.close(s.master_fd)
            except OSError:
                pass
            s.master_fd = None
        proc = s.proc
        rc = proc.returncode if proc else None
        if s.status in ("connecting", "awaiting_input") or (
                s.status == "up" and rc not in (0, None)):
            # died before/after coming up without a manual stop
            if s.status != "stopped":
                s.status = "error"
                if not s.last_error:
                    tail = " ".join(s.output[-4:]) or f"ssh exited ({rc})"
                    s.last_error = tail
                log.warning("tunnel session ended", extra={"data": {
                    "endpoint": eid, "rc": rc, "error": s.last_error}})
        s.prompt = None
        s.started_at = None

    async def respond(self, eid: str, text: str) -> TunnelSessionStatus | None:
        s = self.sessions.get(eid)
        if s is None or s.master_fd is None or s.proc is None or s.proc.returncode is not None:
            return None
        was_secret = s.prompt_secret
        # Defense-in-depth: if the tty echoes the answer back, scrub it from the
        # next chunk so a secret never reaches the output buffer or the logs.
        s._scrub = text if (was_secret and text) else ""
        try:
            os.write(s.master_fd, (text + "\n").encode())
        except OSError as e:
            s.last_error = f"could not send input: {e}"
            return self.status(eid)
        # Never keep the typed answer around; note that a reply was sent.
        s.output.append("· sent response" + ("" if not was_secret else " (hidden)"))
        del s.output[:-_MAX_OUTPUT_LINES]
        s.prompt = None
        s.prompt_secret = False
        s.status = "connecting"
        s._tail = ""
        log.info("tunnel session response sent",
                 extra={"data": {"endpoint": eid, "secret": was_secret}})
        return self.status(eid)

    async def _kill(self, s: Session) -> None:
        proc = s.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def disconnect(self, eid: str) -> TunnelSessionStatus:
        s = self._get(eid)
        s.status = "stopped"
        s.prompt = None
        s.prompt_secret = False
        if s.task:
            # let _kill drive termination; cancel the pump after
            await self._kill(s)
            s.task.cancel()
            try:
                await s.task
            except (asyncio.CancelledError, Exception):
                pass
            s.task = None
        s.started_at = None
        s.pid = None
        log.info("tunnel session disconnected", extra={"data": {"endpoint": eid}})
        return self.status(eid)

    async def shutdown(self) -> None:
        for eid in list(self.sessions):
            try:
                await self.disconnect(eid)
            except Exception:
                pass
