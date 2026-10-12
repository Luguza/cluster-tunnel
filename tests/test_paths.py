"""Config and cache locations, with appdirs' Windows branch faked so they run on any OS."""

from __future__ import annotations

import appdirs
from click.testing import CliRunner

from cluster_tunnel import config as config_mod
from cluster_tunnel import paths
from cluster_tunnel.cli import cli


def _fake_windows(monkeypatch, local_appdata) -> None:
    """Make appdirs take its Windows branch, with %LOCALAPPDATA% at local_appdata."""
    monkeypatch.setattr(appdirs, "system", "win32")
    monkeypatch.setattr(appdirs, "_get_win_folder", lambda _: str(local_appdata), raising=False)


def _legacy_install(local_appdata):
    """Lay out a 0.5.0 Windows install: config.yaml in the author folder."""
    legacy = local_appdata / "jonas" / "cluster-tunnel"
    legacy.mkdir(parents=True)
    (legacy / "config.yaml").write_text("clusters: {}\n", encoding="utf-8")
    return legacy


def test_windows_config_dir_has_no_author_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert paths.config_dir() == tmp_path / "cluster-tunnel"


def test_windows_cache_dir_has_no_author_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert paths.cache_dir() == tmp_path / "cluster-tunnel" / "Cache"


def test_windows_legacy_install_keeps_its_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    legacy = _legacy_install(tmp_path)
    assert paths.using_legacy_dirs()
    assert paths.config_dir() == legacy
    assert paths.cache_dir() == legacy / "Cache"


def test_windows_legacy_install_finds_its_config(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    monkeypatch.delenv("CTUN_CONFIG", raising=False)
    legacy = _legacy_install(tmp_path)
    assert config_mod.resolve_config_path() == legacy / "config.yaml"
    assert paths.budget_dir() == legacy / "budget"


def test_windows_legacy_runtime_dirs_stay_in_legacy_folder(monkeypatch, tmp_path):
    # Creating the new folder as a side effect would silently switch the
    # install over on the next run and orphan its config.
    _fake_windows(monkeypatch, tmp_path)
    legacy = _legacy_install(tmp_path)
    paths.ensure_runtime_dirs()
    assert (legacy / "Cache" / "sessions").is_dir()
    assert not (tmp_path / "cluster-tunnel").exists()
    assert paths.using_legacy_dirs()


def test_windows_new_folder_wins_over_legacy(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    _legacy_install(tmp_path)
    (tmp_path / "cluster-tunnel").mkdir()
    assert not paths.using_legacy_dirs()
    assert paths.config_dir() == tmp_path / "cluster-tunnel"
    assert paths.cache_dir() == tmp_path / "cluster-tunnel" / "Cache"


def test_windows_legacy_file_is_not_an_install(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    (tmp_path / "jonas").mkdir()
    (tmp_path / "jonas" / "cluster-tunnel").write_text("", encoding="utf-8")
    assert not paths.using_legacy_dirs()


def test_posix_has_no_legacy_location(monkeypatch, tmp_path):
    # appdirs ignores the author off Windows, so both names are the same folder.
    monkeypatch.setattr(appdirs, "system", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "cluster-tunnel").mkdir()
    assert paths.legacy_config_dir() == paths.default_config_dir()
    assert not paths.using_legacy_dirs()
    assert paths.config_dir() == tmp_path / "cluster-tunnel"


def test_config_command_notes_legacy_folder_on_stderr(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    monkeypatch.delenv("CTUN_CONFIG", raising=False)
    legacy = _legacy_install(tmp_path)
    r = CliRunner().invoke(cli, ["config", "--path"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == str(legacy / "config.yaml")
    stderr = " ".join(r.stderr.split())
    assert "legacy folder" in stderr
    assert str(tmp_path / "cluster-tunnel") in stderr


def test_config_command_is_quiet_without_legacy_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    monkeypatch.delenv("CTUN_CONFIG", raising=False)
    r = CliRunner().invoke(cli, ["config", "--path"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == str(tmp_path / "cluster-tunnel" / "config.yaml")
    assert "legacy" not in r.stderr
