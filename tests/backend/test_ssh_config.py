"""~/.ssh/config parsing + `ssh -G` resolution.

``resolve``/``list_hosts`` shell out to ``ssh -G``; we put a fake ``ssh`` on
PATH that emits canned ``-G`` output so the identity-file heuristic (explicit
key vs. default fan-out) is exercised without touching the real config.
"""

import stat
import sys
import textwrap

import pytest

from app.services import ssh_config

_CONFIG = textwrap.dedent(
    """
    Host login coaps-login
        HostName login.example.edu
        User alice
        IdentityFile ~/.ssh/id_rsa

    Host skynet-alt
        HostName skynet.example.edu
        User bob
        ProxyJump login

    Host class1*
        HostName %h.example.edu

    # a comment
    Host plain
        HostName plain.example.edu
    """
)

# Fake ssh: `ssh -G <alias>` prints canned effective config. An alias ending in
# "-nokey" gets the full default identity fan-out (no configured key).
_FAKE_SSH = textwrap.dedent(
    """
    import sys
    alias = sys.argv[-1]
    print("user", "bob")
    print("hostname", "skynet.example.edu")
    print("port", "22")
    if alias.endswith("-nokey"):
        for k in ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk"):
            print("identityfile", "~/.ssh/" + k)
    else:
        print("identityfile", "~/.ssh/id_rsa")
        print("proxyjump", "login")
    """
)


@pytest.fixture
def fake_ssh_path(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    script = d / "ssh"
    script.write_text(f"#!{sys.executable}\n{_FAKE_SSH}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    monkeypatch.setenv("PATH", str(d) + ":" + __import__("os").environ["PATH"])
    return d


def test_list_host_aliases_skips_wildcards(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(_CONFIG)
    aliases = ssh_config.list_host_aliases(cfg)
    assert aliases == ["login", "skynet-alt", "plain"]  # class1* skipped


def test_list_host_aliases_missing_file(tmp_path):
    assert ssh_config.list_host_aliases(tmp_path / "nope") == []


async def test_resolve_explicit_key(fake_ssh_path):
    r = await ssh_config.resolve("skynet-alt")
    assert r["hostname"] == "skynet.example.edu"
    assert r["user"] == "bob"
    assert r["identity_file"].endswith("id_rsa")
    assert r["identity_explicit"] is True
    assert r["proxyjump"] == "login"


async def test_resolve_default_fanout_is_not_explicit(fake_ssh_path):
    r = await ssh_config.resolve("host-nokey")
    assert len(r["identity_files"]) == 5
    assert r["identity_explicit"] is False


async def test_resolve_rejects_bad_alias():
    with pytest.raises(ValueError):
        await ssh_config.resolve("bad;rm -rf /")
