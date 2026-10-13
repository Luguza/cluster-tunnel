"""`login --credentials-stdin`: the secrets come through a pipe, never a window or argv."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel.cli import cli
from tests.test_cli import _cfg


def _patch_login(tmp_path: Path, monkeypatch) -> list:
    from cluster_tunnel import paths, popup, ssh

    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    live = iter([False, True])  # down before the login, up after it
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(live))
    logins = []

    def fake_login(spec, password, otp, timeout, verbose):
        logins.append((password, otp))
        return True

    monkeypatch.setattr(popup, "login_with_password", fake_login)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: (_ for _ in ()).throw(AssertionError("popup")))
    return logins


def test_logs_in_with_piped_secrets_and_limit(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import session

    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "login", "--credentials-stdin"],
        input='{"password": "s3cret", "otp": "123456", "limit": 20}',
    )
    assert r.exit_code == 0, r.output
    assert logins == [("s3cret", "123456")]
    assert session.load("localhost")["limit"] == 20.0
    assert "s3cret" not in r.output


def test_rejects_input_that_is_not_json(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "--credentials-stdin"], input="s3cret")
    assert r.exit_code != 0
    assert "not JSON" in r.output
    assert logins == []
