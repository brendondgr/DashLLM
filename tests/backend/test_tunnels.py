"""Tunnel manager: argv command generation, injection rejection, and a real
subprocess lifecycle driven by a fake 'tunnel' (a python TCP listener)."""

import shlex
import sys

import httpx
import pytest

from app.schemas import TunnelCreate, TunnelPatch
from app.services.tunnels import TunnelManager, build_argv, command_string


def _row(**kw) -> dict:
    return {
        "ssh_user": "sander", "ssh_host": "gpu-box.lan", "ssh_port": 22,
        "key_path": "~/.ssh/id_ed25519", "remote_host": "127.0.0.1",
        "remote_port": 8000, "local_port": 8443, "compress": 1,
        "keepalive": 1, "extra_opts": None, **kw,
    }


def test_command_matches_design():
    argv = build_argv(_row())
    assert argv[0:2] == ["ssh", "-N"]
    assert "-C" in argv
    assert argv[argv.index("-L") + 1] == "8443:127.0.0.1:8000"
    assert argv[argv.index("-p") + 1] == "22"
    assert "ServerAliveInterval=30" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[-1] == "sander@gpu-box.lan"
    # displayed string round-trips to the same argv (what you see is what runs)
    assert shlex.split(command_string(_row())) == argv


def test_command_toggles():
    argv = build_argv(_row(compress=0, keepalive=0, key_path=None))
    assert "-C" not in argv and "-i" not in argv
    assert "ServerAliveInterval=30" not in argv


def test_extra_opts_tokenized():
    argv = build_argv(_row(extra_opts="-o ProxyJump=bastion.lan"))
    assert argv[argv.index("ProxyJump=bastion.lan") - 1] == "-o"


@pytest.mark.parametrize("field,value", [
    ("ssh_user", "u; rm -rf /"),
    ("ssh_user", "-oProxyCommand=evil"),
    ("ssh_host", "host$(boom)"),
    ("ssh_host", "-evil.com"),
    ("remote_host", "127.0.0.1; nc"),
    ("ssh_port", 0),
    ("local_port", 99999),
    ("key_path", "-oProxyCommand=evil"),
    ("extra_opts", "-o ProxyCommand=`curl evil`"),
])
def test_injection_rejected(field, value):
    with pytest.raises(ValueError):
        build_argv(_row(**{field: value}))


# ---- lifecycle with a fake tunnel process --------------------------------

_LISTENER = (
    "import socket,time\n"
    "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,1)\n"
    "s.bind(('127.0.0.1', {port})); s.listen(4)\n"
    "time.sleep(60)\n"
)


def _fake_spawn(row: dict) -> list[str]:
    return [sys.executable, "-c", _LISTENER.format(port=row["local_port"])]


@pytest.fixture
async def manager(db):
    m = TunnelManager(db, httpx.AsyncClient(), spawn_argv=_fake_spawn,
                      up_timeout=8.0)
    yield m
    await m.stop_supervisor()


def _spec(local_port=18443, **kw) -> TunnelCreate:
    return TunnelCreate(name="gpu-box", ssh_host="127.0.0.1", ssh_user="u",
                        remote_port=8000, local_port=local_port, **kw)


async def test_tunnel_lifecycle(manager):
    row = manager.create(_spec())
    tid = row["id"]
    assert manager.out(tid).status == "stopped"

    out = await manager.start(tid)
    assert out.status == "up"
    assert out.pid is not None
    assert out.uptime_s >= 0

    # test level 1: forward accepting; level 2 fails (no HTTP behind it)
    result = await manager.test(tid, probe_timeout=1.0)
    assert result.ssh_ok is True
    assert result.endpoint_ok is False

    out = await manager.stop(tid)
    assert out.status == "stopped" and out.pid is None


async def test_change_port_restarts_child(manager):
    row = manager.create(_spec(local_port=18444))
    tid = row["id"]
    await manager.start(tid)
    pid1 = manager.out(tid).pid
    patched = await manager.patch(tid, TunnelPatch(local_port=18445))
    assert patched["local_port"] == 18445
    out = manager.out(tid)
    assert out.status == "up" and out.pid != pid1
    ok = await manager.test(tid)
    assert ok.ssh_ok is True
    await manager.stop(tid)


async def test_start_failure_reports_error(db):
    def broken_spawn(row):
        return [sys.executable, "-c", "import sys; sys.exit(3)"]
    m = TunnelManager(db, httpx.AsyncClient(), spawn_argv=broken_spawn)
    row = m.create(_spec(local_port=18446))
    out = await m.start(row["id"])
    assert out.status == "error"
    assert out.last_error


async def test_delete_stops_process(manager):
    row = manager.create(_spec(local_port=18447))
    await manager.start(row["id"])
    live = manager.live[row["id"]]
    assert await manager.delete(row["id"]) is True
    assert live.proc.returncode is not None
    assert manager.rows() == []
