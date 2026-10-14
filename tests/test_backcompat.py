"""Backward compatibility: configs written for ctun 0.5.0 keep working unchanged.

String descriptions are the original form. They must load, show and serialise
exactly as before, nothing may rewrite them unasked, and the opt-in
`ctun config --migrate` must convert them without losing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cluster_tunnel import configedit
from cluster_tunnel.cli import cli
from cluster_tunnel.config import load_config

# Shaped like a config written for 0.5.0: comments everywhere, string
# descriptions in `|` and quoted form (one with two paragraphs), restrictions,
# a budget, and clusters without a description.
LEGACY = """\
# ctun configuration — see DESIGN.md for the full schema.

defaults:
  control_persist: "12h"           # master lifetime
  server_alive_interval: 60

agent:
  preamble: |
    You operate on shared academic HPC clusters via `ctun`.

clusters:

  # HOREKA @ KIT
  horeka:
    host: horeka.scc.kit.edu
    user: ab1234
    requires_otp: true             # advisory: surfaced via `ctun info`
    description: |
      HoreKa @ KIT (NHR). GPU jobs -> partition 'accelerated'.
    restrictions:                  # advisory only; surfaced via `ctun info`
      max_job_runtime: "48:00:00"
      allowed_partitions: [accelerated, cpuonly]
    budget:
      script: budget/horeka.sh
      session_limit: 500
      unit: jobh
      guard_commands: [sbatch, srun, salloc]
      fail_mode: closed

  # KCIST @ KIT — no OTP.
  kcist:
    host: login2.kcist.kit.edu
    user: ab1234
    description: |
      KCIST @ KIT — 18 GPU nodes total.
      Partitions (batch* = default):
        small   13× node-s   64 CPU   4× RTX 4090
        large    3× node-l  128 CPU   8× RTX 6000 Ada
      No walltime limit — always set a realistic --time yourself.

      Prefer the 'small' partition unless told otherwise.

  bwuni:
    ssh_alias: bwuni
    description: "bwUniCluster 2.0 @ KIT. GPU partitions gpu_h100 / gpu_a100."

  plain:
    host: plain.example

webui:
  host: 127.0.0.1
  port: 8765
"""

# What `ctun -t <cluster> info --json` printed in 0.5.0: no key added or removed.
INFO_KEYS_0_5_0 = {
    "ctun_version", "preamble", "cluster", "description", "host", "ssh_alias",
    "user", "requires_otp", "requires_password", "restrictions", "tunnel_live",
    "session", "budget",
}


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch) -> Path:
    """The legacy config (+ its budget script), with no ssh and a private cache."""
    p = tmp_path / "config.yaml"
    p.write_text(LEGACY, encoding="utf-8")
    (tmp_path / "budget").mkdir()
    (tmp_path / "budget" / "horeka.sh").write_text("#!/usr/bin/env bash\necho 0\n", encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: False)
    monkeypatch.setattr("cluster_tunnel.paths.cache_dir", lambda: tmp_path / "cache")
    return p


def _invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(cli, list(args), input=input)


def _raw_descriptions(text: str) -> dict[str, object]:
    """Each cluster's description exactly as YAML reads it ("" when absent)."""
    clusters = yaml.safe_load(text)["clusters"]
    return {name: entry.get("description", "") for name, entry in clusters.items()}


def test_legacy_config_loads_unchanged(legacy: Path) -> None:
    config = load_config(str(legacy))
    raw = _raw_descriptions(LEGACY)
    assert {name: c.description for name, c in config.clusters.items()} == raw
    assert all(isinstance(c.description, str) for c in config.clusters.values())
    horeka = config.clusters["horeka"]
    assert horeka.restrictions["allowed_partitions"] == ["accelerated", "cpuonly"]
    assert horeka.budget is not None and horeka.budget.session_limit == 500


def test_legacy_info_json_is_shaped_as_in_0_5_0(legacy: Path) -> None:
    raw = _raw_descriptions(LEGACY)
    for name in raw:
        r = _invoke("-t", name, "info", "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)
        assert set(data) == INFO_KEYS_0_5_0
        # 0.5.0 emitted `cluster.description.strip()`: still exactly that.
        assert data["description"] == raw[name].strip()
    every = json.loads(_invoke("info", "--json").output)
    assert [c["description"] for c in every["clusters"]] == [
        raw[name].strip() for name in sorted(raw)
    ]


def test_legacy_info_text_shows_the_description_as_a_paragraph(legacy: Path) -> None:
    r = _invoke("-t", "kcist", "info")
    assert r.exit_code == 0
    lines = [line.strip("│ ").rstrip() for line in r.output.splitlines()]
    assert "KCIST @ KIT — 18 GPU nodes total." in lines
    assert "small   13× node-s   64 CPU   4× RTX 4090" in lines  # rows keep their spacing
    assert "•" not in r.output


def test_legacy_validate_passes_with_an_optional_tip(legacy: Path) -> None:
    r = _invoke("config", "--validate")
    assert r.exit_code == 0, r.output
    assert "warning" not in r.output
    assert "tip: horeka, kcist, bwuni have a single-string description" in " ".join(r.output.split())
    assert "is valid" in r.output


def test_show_keeps_the_string_form(legacy: Path) -> None:
    shown = yaml.safe_load(_invoke("config", "--show").output)
    assert shown["clusters"]["kcist"]["description"] == _raw_descriptions(LEGACY)["kcist"]


def test_nothing_rewrites_a_legacy_config_unasked(legacy: Path) -> None:
    before = legacy.read_bytes()
    for args in (
        ["info"], ["info", "--json"], ["-t", "horeka", "info"], ["status"],
        ["status", "--json"], ["config", "--validate"], ["config", "--show"],
        ["config", "--path"], ["config", "--migrate"], ["-t", "kcist", "note", "x"],
    ):
        _invoke(*args)
    assert legacy.read_bytes() == before
    # ... and leaves nothing next to it: no backup, no temp file.
    assert {p.name for p in legacy.parent.iterdir()} <= {"budget", "cache", "config.yaml"}


def test_note_refuses_a_legacy_description_and_names_the_fix(legacy: Path) -> None:
    r = _invoke("-t", "kcist", "note", "Use -A hk-test.")
    assert r.exit_code != 0
    assert "ctun config --migrate" in " ".join(r.output.split())
    assert legacy.read_text(encoding="utf-8") == LEGACY


def test_note_works_on_a_legacy_cluster_without_a_description(legacy: Path) -> None:
    # No description means nothing old to convert: the cluster just gets a list.
    r = _invoke("-t", "plain", "note", "Login node only; no Slurm here.")
    assert r.exit_code == 0, r.output
    assert load_config(str(legacy)).clusters["plain"].description == ["Login node only; no Slurm here."]
    assert load_config(str(legacy)).clusters["kcist"].description == _raw_descriptions(LEGACY)["kcist"]


def test_migration_is_lossless_and_keeps_every_comment(legacy: Path) -> None:
    r = _invoke("config", "--migrate", "--yes")
    assert r.exit_code == 0, r.output
    assert (legacy.parent / "config.yaml.bak").read_text(encoding="utf-8") == LEGACY

    new = legacy.read_text(encoding="utf-8")
    old_data, new_data = yaml.safe_load(LEGACY), yaml.safe_load(new)
    assert new_data["clusters"]["kcist"]["description"][-1] == "Prefer the 'small' partition unless told otherwise."
    raw = _raw_descriptions(LEGACY)
    for name, text in raw.items():
        if text:
            assert new_data["clusters"][name]["description"] == configedit.split_paragraphs(text)
            del old_data["clusters"][name]["description"], new_data["clusters"][name]["description"]
    assert new_data == old_data  # nothing but the descriptions changed

    comments = [line.strip() for line in LEGACY.splitlines() if line.strip().startswith("#")]
    assert comments == [line.strip() for line in new.splitlines() if line.strip().startswith("#")]


def test_migrated_info_shows_the_same_text_as_notes(legacy: Path) -> None:
    _invoke("config", "--migrate", "--yes")
    data = json.loads(_invoke("-t", "kcist", "info", "--json").output)
    assert set(data) == INFO_KEYS_0_5_0
    assert data["description"] == [
        "KCIST @ KIT — 18 GPU nodes total.\nPartitions (batch* = default):\n"
        "  small   13× node-s   64 CPU   4× RTX 4090\n  large    3× node-l  128 CPU   8× RTX 6000 Ada\n"
        "No walltime limit — always set a realistic --time yourself.",
        "Prefer the 'small' partition unless told otherwise.",
    ]
    text = _invoke("-t", "kcist", "info").output
    assert text.count("•") == 2


def test_full_upgrade_path(legacy: Path) -> None:
    # 0.5.0 config -> note refused -> user migrates -> note accepted -> info shows it.
    assert _invoke("-t", "horeka", "note", "Use -A hk-test.").exit_code != 0
    assert _invoke("config", "--migrate", "--yes").exit_code == 0
    r = _invoke("-t", "horeka", "note", "Use -A hk-test.")
    assert r.exit_code == 0, r.output
    data = json.loads(_invoke("-t", "horeka", "info", "--json").output)
    assert data["description"] == [
        "HoreKa @ KIT (NHR). GPU jobs -> partition 'accelerated'.",
        "Use -A hk-test.",
    ]
    assert _invoke("config", "--migrate", "--yes").output.startswith("Nothing to migrate")
    assert _invoke("config", "--validate").exit_code == 0
