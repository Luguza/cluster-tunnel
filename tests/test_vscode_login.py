"""`login -i` inside a VS Code window: the window logs in, the popup stays shut."""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import vscode_login
from cluster_tunnel.cli import cli
from tests.test_cli import _cfg


def _serve_once(address: Path, reply: bytes) -> list:
    """Answer one request on a Unix socket with `reply`; return the list the request lands in."""
    requests = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(address))
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as pipe:
            requests.append(json.loads(pipe.readline()))
            pipe.write(reply)
        server.close()

    threading.Thread(target=answer, daemon=True).start()
    return requests


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="the test server is a Unix socket")


@unix_only
def test_sends_the_cluster_and_limit_and_returns_the_outcome(tmp_path: Path, monkeypatch) -> None:
    address = tmp_path / "bridge.sock"
    requests = _serve_once(address, b'{"status": "live"}\n')
    monkeypatch.setenv(vscode_login.BRIDGE_VARIABLE, str(address))
    outcome = vscode_login.log_in("haicore", 20.0)
    assert outcome.status == "live"
    assert requests == [{"cluster": "haicore", "session_limit": 20.0}]


@unix_only
def test_a_window_that_closes_without_reply_declines(tmp_path: Path, monkeypatch) -> None:
    address = tmp_path / "bridge.sock"
    _serve_once(address, b"")
    monkeypatch.setenv(vscode_login.BRIDGE_VARIABLE, str(address))
    with pytest.raises(vscode_login.Unavailable):
        vscode_login.log_in("haicore", None)


def test_without_a_window_it_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv(vscode_login.BRIDGE_VARIABLE, raising=False)
    with pytest.raises(vscode_login.Unavailable):
        vscode_login.log_in("haicore", None)


def test_a_window_that_has_closed_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    gone = r"\\.\pipe\ctun-vscode-gone" if sys.platform == "win32" else str(tmp_path / "gone.sock")
    monkeypatch.setenv(vscode_login.BRIDGE_VARIABLE, gone)
    with pytest.raises(vscode_login.Unavailable):
        vscode_login.log_in("haicore", None)


def _patch_login(tmp_path: Path, monkeypatch, outcome) -> None:
    from cluster_tunnel import paths, popup, ssh

    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(ssh, "is_live", lambda spec: False)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: (_ for _ in ()).throw(AssertionError("popup")))
    monkeypatch.setattr(vscode_login, "log_in", lambda name, limit: outcome)
    monkeypatch.setenv(vscode_login.BRIDGE_VARIABLE, "the-window")


def test_login_keeps_the_session_the_window_started(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import session

    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, vscode_login.Outcome("live"))
    started = session.start("localhost", limit=20.0, unit="jobh")  # by the window's own login
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i"])
    assert r.exit_code == 0, r.output
    assert "in VS Code" in r.output
    assert session.load("localhost") == started


def test_login_reports_a_failure_in_the_window(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, vscode_login.Outcome("failed", "wrong OTP"))
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i"])
    assert r.exit_code != 0
    assert "wrong OTP" in r.output
