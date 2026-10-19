"""`ctun hostkey`: show the key a cluster presents, and pin it (host-key helper stubbed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import paths
from cluster_tunnel.cli import cli, configcmd
from cluster_tunnel.hostkey import HostKey

from tests.test_cli import _norm

UNPINNED = "clusters:\n  bw:\n    host: uc3.example  # login node\n"
PINNED = (
    "clusters:\n  bw:\n    host: uc3.example\n"
    "    strict_host_key_checking: 'yes'\n    known_hosts_file: /kh\n"
)


def _key(fingerprint: str) -> HostKey:
    return HostKey("uc3.example", "ED25519", 256, fingerprint, f"uc3.example ssh-ed25519 {fingerprint[-3:]}")


@pytest.fixture
def server(tmp_path: Path, monkeypatch) -> dict:
    """A stubbed server presenting SHA256:new; set ``pinned`` for the key file's key."""
    state: dict = {"offered": [_key("SHA256:new")], "pinned": [], "pins": []}
    monkeypatch.setattr("cluster_tunnel.hostkey.scan", lambda spec: state["offered"])
    monkeypatch.setattr("cluster_tunnel.hostkey.pinned", lambda spec, file, host: state["pinned"])
    monkeypatch.setattr(
        "cluster_tunnel.hostkey.pin", lambda spec, file, keys: state["pins"].append((file, keys))
    )
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "conf")
    return state


def _config(tmp_path: Path, monkeypatch, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def _tty(monkeypatch, tty: bool) -> None:
    monkeypatch.setattr(configcmd, "_stdin_is_tty", lambda: tty)


def _hostkey(*args: str, input: str | None = None):
    return CliRunner().invoke(cli, ["-t", "bw", "hostkey", *args], input=input)


# --- showing --------------------------------------------------------------------


def test_an_unpinned_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    r = _hostkey()
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "Host key of 'bw' (uc3.example): ED25519 SHA256:new" in out
    assert "not pinned: logins accept the key on first contact" in out
    assert server["pins"] == []


def test_a_pinned_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:new")]
    r = _hostkey()
    assert r.exit_code == 0, r.output
    assert "pinned: logins accept only this key." in _norm(r)


def test_a_matching_key_that_is_not_yet_checked_strictly(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED.replace("'yes'", "accept-new"))
    server["pinned"] = [_key("SHA256:new")]
    r = _hostkey()
    assert r.exit_code == 0
    assert "but logins still accept a new key on first contact" in _norm(r)


def test_strict_checking_without_a_pinned_key(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    r = _hostkey()
    assert r.exit_code == 0
    assert "not in /kh: logins are refused until the key is pinned" in _norm(r)


def test_a_changed_key_fails(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:old")]
    r = _hostkey()
    assert r.exit_code == 1
    out = _norm(r)
    assert "DIFFERENT from the pinned key: ED25519 SHA256:old" in out
    assert "Don't log in until you know why" in out


def test_json(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:new")]
    r = _hostkey("--json")
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["matches"] is True and payload["strict"] is True
    assert payload["offered"][0]["fingerprint"] == "SHA256:new"

    server["pinned"] = [_key("SHA256:old")]
    assert _hostkey("--json").exit_code == 1


def test_an_unreachable_server(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)

    def refused(spec):
        raise RuntimeError("ssh: connect to host uc3.example port 22: Connection refused")

    monkeypatch.setattr("cluster_tunnel.hostkey.scan", refused)
    r = _hostkey()
    assert r.exit_code == 1
    assert "cannot fetch the host key of 'bw': ssh: connect to host" in _norm(r)


@pytest.mark.parametrize("args", [["--yes"], ["--json", "--pin"]])
def test_option_combinations_that_make_no_sense(tmp_path: Path, monkeypatch, server, args) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    r = _hostkey(*args)
    assert r.exit_code == 2


# --- pinning --------------------------------------------------------------------


def test_pin_without_a_terminal_only_shows_the_change(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, False)
    r = _hostkey("--pin")
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert f"Pinning SHA256:new for 'bw' in {tmp_path / 'conf' / 'known_hosts'}." in out
    assert "+ strict_host_key_checking: 'yes'" in out
    assert "Compare the fingerprint with the one your cluster's operators publish" in out
    assert "Nothing pinned. Run `ctun -t bw hostkey --pin --yes` to pin it." in out
    assert p.read_text(encoding="utf-8") == UNPINNED and server["pins"] == []


def test_pin_with_yes(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 0, r.output
    file = str(tmp_path / "conf" / "known_hosts")
    assert server["pins"] == [(file, server["offered"])]
    text = p.read_text(encoding="utf-8")
    assert "uc3.example  # login node\n    strict_host_key_checking: 'yes'\n" in text
    assert f"known_hosts_file: {file}" in text
    assert "✓ Pinned: logins to 'bw' now accept only SHA256:new." in _norm(r)
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == UNPINNED


@pytest.mark.parametrize("answer, pinned", [("y\n", True), ("n\n", False)])
def test_pin_asks_on_a_terminal(tmp_path: Path, monkeypatch, server, answer, pinned) -> None:
    _config(tmp_path, monkeypatch, UNPINNED)
    _tty(monkeypatch, True)
    r = _hostkey("--pin", input=answer)
    assert r.exit_code == 0, r.output
    assert "Pin this key?" in r.output
    assert bool(server["pins"]) is pinned


def test_a_changed_key_needs_a_terminal_even_with_yes(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:old")]
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 1
    assert "re-pin it from a terminal (--yes does not apply to a changed key)" in _norm(r)
    assert server["pins"] == [] and p.read_text(encoding="utf-8") == PINNED


def test_a_changed_key_is_re_pinned_after_asking_on_a_terminal(tmp_path: Path, monkeypatch, server) -> None:
    p = _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:old")]
    _tty(monkeypatch, True)
    r = _hostkey("--pin", "--yes", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Replace the pinned key with this one?" in r.output
    assert server["pins"] == [("/kh", server["offered"])]
    assert p.read_text(encoding="utf-8") == PINNED  # already strict with this file
    assert "previous config" not in _norm(r)


def test_pin_when_already_pinned(tmp_path: Path, monkeypatch, server) -> None:
    _config(tmp_path, monkeypatch, PINNED)
    server["pinned"] = [_key("SHA256:new")]
    r = _hostkey("--pin")
    assert r.exit_code == 0
    assert "Already pinned; nothing to do." in _norm(r)
    assert server["pins"] == []


def test_pin_refuses_when_the_key_changes_while_asking(tmp_path: Path, monkeypatch, server) -> None:
    # The key is fetched again right before pinning; a different one pins nothing.
    p = _config(tmp_path, monkeypatch, UNPINNED)
    answers = iter([[_key("SHA256:new")], [_key("SHA256:swapped")]])
    monkeypatch.setattr("cluster_tunnel.hostkey.scan", lambda spec: next(answers))
    _tty(monkeypatch, False)
    r = _hostkey("--pin", "--yes")
    assert r.exit_code == 1
    assert "presents SHA256:swapped, not SHA256:new; nothing was pinned." in _norm(r)
    assert server["pins"] == [] and p.read_text(encoding="utf-8") == UNPINNED
