"""Config loading, validation, and resolution helpers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cluster_tunnel import config as cfg
from cluster_tunnel import wsl
from cluster_tunnel.config import Cluster


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_and_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    user: u\n")
    c = cfg.load_config(str(p))
    assert "c" in c.clusters
    assert c.defaults.socket_dir  # filled by the loader
    assert c.defaults.control_persist == "12h"


def test_resolve_target_variants() -> None:
    assert cfg.resolve_target(Cluster(ssh_alias="al", host="h", user="u")) == "al"
    assert cfg.resolve_target(Cluster(host="h", user="u")) == "u@h"
    assert cfg.resolve_target(Cluster(host="h")) == "h"


def test_requires_password_defaults_true_and_overridable() -> None:
    # Default: a cluster uses a service password unless told otherwise.
    assert Cluster(host="h").requires_password is True
    assert Cluster(host="h", requires_password=False).requires_password is False


def test_description_is_a_string_or_a_list(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "clusters:\n"
        "  old:\n    host: h\n    description: |\n      One string.\n"
        "  new:\n    host: h\n    description:\n      - first\n      - second\n"
        "  none:\n    host: h\n",
    )
    clusters = cfg.load_config(str(p)).clusters
    assert clusters["old"].description == "One string.\n"
    assert clusters["new"].description == ["first", "second"]
    assert clusters["none"].description == ""


@pytest.mark.parametrize(
    "item, kind",
    [
        ("Account: hk-x", "a 'key: value' mapping"),  # the most likely slip
        ("[a, b]", "a nested list"),
        ("yes", "a true/false value"),
        ("42", "a number"),
        ("1.5", "a number"),
        ("", "empty"),
        ("2026-10-14", "a date"),
    ],
)
def test_description_items_must_be_text(tmp_path: Path, item: str, kind: str) -> None:
    p = _write(tmp_path, f"clusters:\n  c:\n    host: h\n    description:\n      - ok\n      - {item}\n")
    with pytest.raises(Exception, match=re.escape(f"description item 2 is {kind}, not text")):
        cfg.load_config(str(p))


def test_budget_script_path(tmp_path: Path) -> None:
    cp = tmp_path / "config.yaml"
    assert cfg.budget_script_path(cp, "horeka", None) == tmp_path / "budget" / "horeka.sh"
    absolute = tmp_path / "abs" / "x.sh"  # "/abs/x.sh" is not absolute on Windows
    assert cfg.budget_script_path(cp, "horeka", str(absolute)) == absolute
    assert cfg.budget_script_path(cp, "horeka", "budget/custom.sh") == tmp_path / "budget" / "custom.sh"


def test_missing_host_or_alias(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  bad:\n    description: x\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_extra_forbidden(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    nope: 1\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_bad_fail_mode(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    budget:\n      fail_mode: maybe\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        cfg.load_config("/nonexistent/does-not-exist.yaml")


# --- validation_warnings -------------------------------------------------------


def test_warn_missing_budget_script(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n      script: budget/nope.sh\n",
    )
    config = cfg.load_config(str(p))
    warnings = cfg.validation_warnings(config, p)
    assert len(warnings) == 1
    assert "budget script not found" in warnings[0]
    assert "nope.sh" in warnings[0]


def test_warn_invalid_guard_regex(tmp_path: Path) -> None:
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n"
        "      script: x.sh\n      guard_commands: ['sbatch', 's(run']\n",
    )
    config = cfg.load_config(str(p))
    warnings = cfg.validation_warnings(config, p)
    assert len(warnings) == 1
    assert "not a valid regex" in warnings[0]
    assert "s(run" in warnings[0]


def test_no_warnings_when_script_present_and_regex_valid(tmp_path: Path) -> None:
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n"
        "      script: x.sh\n      guard_commands: ['sbatch', 'aslurmx?']\n",
    )
    config = cfg.load_config(str(p))
    assert cfg.validation_warnings(config, p) == []


def test_no_warnings_when_no_budget(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    user: u\n")
    config = cfg.load_config(str(p))
    assert cfg.validation_warnings(config, p) == []


def test_ssh_config_and_wsl_distro_keys(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "defaults:\n  ssh_config: /x/cfg\n  wsl_distro: Ubuntu\n"
        "clusters:\n  c:\n    host: h\n    ssh_config: /y/cfg\n",
    )
    c = cfg.load_config(str(p))
    assert (c.defaults.ssh_config, c.defaults.wsl_distro) == ("/x/cfg", "Ubuntu")
    assert c.clusters["c"].ssh_config == "/y/cfg"


def test_windows_socket_dir_defaults_inside_wsl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    c = cfg.load_config(str(_write(tmp_path, "clusters:\n  c:\n    host: h\n")))
    assert c.defaults.socket_dir == wsl.DEFAULT_SOCKET_DIR


_WINDOWS_STYLE = (
    "defaults:\n  ssh_config: '~/.ssh/config'\n"
    "clusters:\n  c:\n    host: h\n    identity_file: 'C:\\Users\\me\\.ssh\\id'\n"
)


def test_windows_validation_flags_paths_wsl_cannot_use(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    p = _write(tmp_path, _WINDOWS_STYLE)
    warnings = cfg.validation_warnings(cfg.load_config(str(p)), p)
    assert any("identity_file is a Windows path" in w for w in warnings)
    assert any("does not expand for -F" in w for w in warnings)


def test_wsl_path_rules_only_apply_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: False)
    p = _write(tmp_path, _WINDOWS_STYLE)
    assert cfg.validation_warnings(cfg.load_config(str(p)), p) == []


def test_host_key_checking_defaults_to_accept_new(tmp_path: Path) -> None:
    c = cfg.load_config(str(_write(tmp_path, "clusters:\n  c:\n    host: h\n")))
    assert c.clusters["c"].strict_host_key_checking == "accept-new"
    assert c.clusters["c"].known_hosts_file is None


@pytest.mark.parametrize("value", ["yes", "'yes'", "accept-new"])
def test_host_key_checking_values(tmp_path: Path, value: str) -> None:
    # An unquoted `yes` is a YAML boolean; it still means "yes".
    p = _write(tmp_path, f"clusters:\n  c:\n    host: h\n    strict_host_key_checking: {value}\n")
    assert cfg.load_config(str(p)).clusters["c"].strict_host_key_checking == value.strip("'")


@pytest.mark.parametrize("value", ["no", "false", "ask", "off"])
def test_host_key_checking_refuses_weaker_values(tmp_path: Path, value: str) -> None:
    p = _write(tmp_path, f"clusters:\n  c:\n    host: h\n    strict_host_key_checking: {value}\n")
    with pytest.raises(ValueError, match="strict_host_key_checking"):
        cfg.load_config(str(p))


def test_windows_validation_flags_a_windows_known_hosts_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    known_hosts_file: 'C:\\Users\\me\\kh'\n")
    warnings = cfg.validation_warnings(cfg.load_config(str(p)), p)
    assert any("known_hosts_file is a Windows path" in w for w in warnings)


@pytest.mark.parametrize("path", ["~/.config/k", "/abs/k", "rel/k"])
def test_known_hosts_file_takes_plain_paths(tmp_path: Path, path: str) -> None:
    p = _write(tmp_path, f"clusters:\n  c:\n    host: h\n    known_hosts_file: {path}\n")
    assert cfg.load_config(str(p)).clusters["c"].known_hosts_file == path


@pytest.mark.parametrize("path", ["~/%d/k", "${HOME}/k", "~bob/k"])
def test_known_hosts_file_refuses_what_only_ssh_would_expand(tmp_path: Path, path: str) -> None:
    p = _write(tmp_path, f"clusters:\n  c:\n    host: h\n    known_hosts_file: '{path}'\n")
    with pytest.raises(ValueError, match="plain path"):
        cfg.load_config(str(p))
