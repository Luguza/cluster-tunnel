"""Config and cache locations, with appdirs' Windows branch faked so they run on any OS."""

from __future__ import annotations

import appdirs

from cluster_tunnel import paths


def _fake_windows(monkeypatch, local_appdata) -> None:
    """Make appdirs take its Windows branch, with %LOCALAPPDATA% at local_appdata."""
    monkeypatch.setattr(appdirs, "system", "win32")
    monkeypatch.setattr(appdirs, "_get_win_folder", lambda _: str(local_appdata), raising=False)


def test_windows_config_dir_has_no_author_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert paths.config_dir() == tmp_path / "cluster-tunnel"


def test_windows_cache_dir_has_no_author_folder(monkeypatch, tmp_path):
    _fake_windows(monkeypatch, tmp_path)
    assert paths.cache_dir() == tmp_path / "cluster-tunnel" / "Cache"
