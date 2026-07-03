"""Interactive tunnel session manager: prompt → respond → forward opens.

Uses a fake program *named* ``ssh`` (the manager requires an ssh command) that
emulates a password prompt, then binds the -L local port once the right answer
is typed — so the whole connecting → awaiting_input → up path is exercised
without a real network.
"""

import asyncio
import os
import socket
import stat
import sys
import textwrap

import pytest

from app.services.tunnel_sessions import (
    TunnelSessionManager,
    parse_local_port,
    validate_command,
)

# Fake "ssh": if invoked with a password prompt it asks, checks the answer,
# then binds 127.0.0.1:<lport> (parsed from -L) and blocks, like `ssh -N`.
_FAKE_SSH = textwrap.dedent(
    """
    import socket, sys, time, termios
    argv = sys.argv[1:]
    lport = None
    for i, a in enumerate(argv):
        if a == "-L":
            lport = int(argv[i+1].split(":")[-3])
    need_pw = "--needpw" in argv
    if need_pw:
        # Disable echo during password entry, exactly like real ssh/getpass.
        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass
        sys.stdout.write("skynet@host's password: ")
        sys.stdout.flush()
        ans = sys.stdin.readline().strip()
        if ans != "hunter2":
            sys.stdout.write("\\nPermission denied\\n")
            sys.stdout.flush()
            sys.exit(1)
        sys.stdout.write("\\n")
        sys.stdout.flush()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", lport))
    s.listen(8)
    while True:
        time.sleep(0.2)
    """
)


@pytest.fixture
def fake_ssh(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    script = d / "ssh"
    # A shebang wrapper so os.path.basename(argv[0]) == "ssh".
    script.write_text(f"#!{sys.executable}\n{_FAKE_SSH}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(script)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_parse_local_port_forms():
    assert parse_local_port("ssh -N -L 127.0.0.1:9090:localhost:9090 h") == 9090
    assert parse_local_port("ssh -N -L 8443:localhost:8000 h") == 8443
    assert parse_local_port("ssh -N h") is None
    assert parse_local_port("ssh -N h", explicit=1234) == 1234


def test_validate_rejects_non_ssh():
    with pytest.raises(ValueError):
        validate_command("rm -rf /")
    with pytest.raises(ValueError):
        validate_command("")
    assert validate_command("ssh -N host")[0].endswith("ssh") or \
        validate_command("ssh -N host")[0] == "ssh"


async def _wait_status(mgr, eid, target, timeout=8.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        st = mgr.status(eid).status
        if st == target:
            return True
        if st == "error":
            return False
        await asyncio.sleep(0.1)
    return False


async def test_key_auth_connects_without_prompt(fake_ssh):
    mgr = TunnelSessionManager(up_grace=8.0)
    port = _free_port()
    cmd = f"{fake_ssh} -N -L {port}:localhost:1"
    await mgr.connect("e1", cmd)
    assert await _wait_status(mgr, "e1", "up"), mgr.status("e1")
    out = mgr.status("e1")
    assert out.local_port == port and out.pid
    await mgr.disconnect("e1")
    assert mgr.status("e1").status == "stopped"


async def test_password_prompt_then_respond(fake_ssh):
    mgr = TunnelSessionManager(up_grace=8.0)
    port = _free_port()
    cmd = f"{fake_ssh} -N --needpw -L {port}:localhost:1"
    await mgr.connect("e2", cmd)
    assert await _wait_status(mgr, "e2", "awaiting_input"), mgr.status("e2")
    st = mgr.status("e2")
    assert "password" in (st.prompt or "").lower()
    assert st.prompt_secret is True
    out = await mgr.respond("e2", "hunter2")
    assert out is not None
    assert await _wait_status(mgr, "e2", "up"), mgr.status("e2")
    # the typed secret must not appear in the redacted output
    assert not any("hunter2" in line for line in mgr.status("e2").output)
    await mgr.disconnect("e2")


async def test_wrong_password_errors(fake_ssh):
    mgr = TunnelSessionManager(up_grace=4.0)
    port = _free_port()
    cmd = f"{fake_ssh} -N --needpw -L {port}:localhost:1"
    await mgr.connect("e3", cmd)
    assert await _wait_status(mgr, "e3", "awaiting_input")
    await mgr.respond("e3", "wrong")
    # ssh exits non-zero → session goes to error
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        if mgr.status("e3").status in ("error", "stopped"):
            break
        await asyncio.sleep(0.1)
    assert mgr.status("e3").status == "error"
    await mgr.disconnect("e3")
