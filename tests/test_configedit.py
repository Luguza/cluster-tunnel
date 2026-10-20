"""Surgical config edits behind `ctun note` and `ctun config --migrate`."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from cluster_tunnel import configedit
from cluster_tunnel.config import load_config

DAY = date(2026, 10, 14)
ADDED = "# added via ctun note, 2026-10-14"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _note(tmp_path: Path, text: str, cluster: str, note: str) -> str:
    """Add `note` to `cluster` in a config holding `text`; return the new text."""
    p = _write(tmp_path, text)
    configedit.add_note(p, cluster, note, DAY)
    return p.read_text(encoding="utf-8")


def _migrated(tmp_path: Path, text: str) -> configedit.Edit:
    return configedit.plan_migration(_write(tmp_path, text))


# --- ctun note ------------------------------------------------------------------


def test_note_appends_to_a_list_and_leaves_comments_in_place(tmp_path: Path) -> None:
    before = (
        "clusters:\n"
        "  a:\n"
        "    host: a.example\n"
        "    description:\n"
        "      - first\n"
        "      - second   # why\n"
        "    # about budgets\n"
        "    budget:\n"
        "      unit: jobh\n"
        "\n"
        "  # header of b\n"
        "  b:\n"
        "    host: b.example\n"
    )
    after = before.replace(
        "      - second   # why\n",
        f"      - second   # why\n      - third  {ADDED}\n",
    )
    assert _note(tmp_path, before, "a", "third") == after


def test_note_after_a_block_item_stays_out_of_the_block(tmp_path: Path) -> None:
    before = (
        "clusters:\n"
        "  a:\n"
        "    host: a.example\n"
        "    description:\n"
        "      - |\n"
        "        KCIST @ KIT — 18 GPU nodes.\n"
        "          small   13× node-s\n"
        "\n"
        "  # header of b\n"
        "  b:\n"
        "    host: b.example\n"
    )
    after = before.replace(
        "          small   13× node-s\n",
        f"          small   13× node-s\n      - Use -A hk-test  {ADDED}\n",
    )
    assert _note(tmp_path, before, "a", "Use -A hk-test") == after


def test_note_extends_an_indentless_list(tmp_path: Path) -> None:
    before = "clusters:\n  a:\n    host: a.example\n    description:\n    - first\n"
    after = before + f"    - second  {ADDED}\n"
    assert _note(tmp_path, before, "a", "second") == after


def test_note_creates_a_description_before_the_budget(tmp_path: Path) -> None:
    before = (
        "clusters:\n"
        "  a:\n"
        "    host: a.example\n"
        "    user: ab1234         # login name\n"
        "    # the budget guard\n"
        "    budget:\n"
        "      unit: jobh\n"
    )
    after = before.replace(
        "    user: ab1234         # login name\n",
        f"    user: ab1234         # login name\n    description:\n      - fact  {ADDED}\n",
    )
    assert _note(tmp_path, before, "a", "fact") == after


def test_note_creates_a_description_after_the_last_key(tmp_path: Path) -> None:
    before = (
        "clusters:\n"
        "  a:\n"
        "    host: a.example\n"
        "\n"
        "  # header of b\n"
        "  b:\n"
        "    host: b.example\n"
    )
    after = before.replace(
        "    host: a.example\n",
        f"    host: a.example\n    description:\n      - fact  {ADDED}\n",
    )
    assert _note(tmp_path, before, "a", "fact") == after


def test_note_creates_a_description_when_the_budget_comes_first(tmp_path: Path) -> None:
    before = "clusters:\n  a:\n    budget:\n      unit: jobh\n    host: a.example\n"
    after = before.replace(
        "  a:\n", f"  a:\n    description:\n      - fact  {ADDED}\n"
    )
    assert _note(tmp_path, before, "a", "fact") == after


def test_note_replaces_an_empty_description(tmp_path: Path) -> None:
    before = 'clusters:\n  a:\n    host: a.example\n    description: ""\n'
    after = f"clusters:\n  a:\n    host: a.example\n    description:\n      - fact  {ADDED}\n"
    assert _note(tmp_path, before, "a", "fact") == after


def test_note_turns_a_flow_list_into_a_block_list(tmp_path: Path) -> None:
    before = "clusters:\n  a:\n    host: a.example\n    description: [x, y]   # short\n"
    after = (
        "clusters:\n  a:\n    host: a.example\n    description:   # short\n"
        f"      - x\n      - y\n      - z  {ADDED}\n"
    )
    assert _note(tmp_path, before, "a", "z") == after


def test_note_refuses_a_multi_line_flow_list(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  a:\n    host: a.example\n    description: [x,\n      y]\n")
    with pytest.raises(configedit.EditError, match="multi-line"):
        configedit.add_note(p, "a", "z", DAY)


def test_note_quotes_text_yaml_would_misread(tmp_path: Path) -> None:
    text = _note(tmp_path, "clusters:\n  a:\n    host: h\n", "a", "Account: -A hk-test")
    assert f"      - 'Account: -A hk-test'  {ADDED}\n" in text


def test_note_writes_a_multi_line_note_as_a_block(tmp_path: Path) -> None:
    text = _note(tmp_path, "clusters:\n  a:\n    host: h\n", "a", "line one\n  line two\n")
    assert text.endswith(f"    description:\n      - |-  {ADDED}\n        line one\n          line two\n")


def test_note_refuses_a_single_string_description(tmp_path: Path) -> None:
    before = "clusters:\n  a:\n    host: h\n    description: |\n      Old style.\n"
    p = _write(tmp_path, before)
    with pytest.raises(configedit.OldFormatError, match="single-string"):
        configedit.add_note(p, "a", "fact", DAY)
    assert p.read_text(encoding="utf-8") == before


def test_note_rejects_unknown_cluster_and_empty_text(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  a:\n    host: h\n")
    with pytest.raises(configedit.EditError, match="no cluster 'b'"):
        configedit.add_note(p, "b", "fact", DAY)
    with pytest.raises(configedit.EditError, match="empty"):
        configedit.add_note(p, "a", "   ", DAY)


def test_note_keeps_crlf_line_endings(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_bytes(b"clusters:\r\n  a:\r\n    host: h\r\n    description:\r\n      - first\r\n")
    configedit.add_note(p, "a", "second", DAY)
    raw = p.read_bytes()
    assert raw.endswith(f"      - second  {ADDED}\r\n".encode())
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_note_returns_the_number_of_notes(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  a:\n    host: h\n    description: [x]\n")
    assert configedit.add_note(p, "a", "y", DAY) == 2
    assert configedit.add_note(p, "a", "z", DAY) == 3


# Text that YAML would read as something else (a bool, a number, a mapping, a
# comment, an alias, ...) unless the note is quoted the right way.
TRICKY = [
    "yes", "no", "null", "~", "true", "123", "1.5", "0o17", "2026-10-14",
    "- dash", "* star", "& amp", "! bang", "% pct", "@ at", "`tick`", "{brace}",
    "[bracket]", "trailing colon:", "key: value", "has # hash", "#starts", "<<",
    "'single'", '"double"', "tab\there", "ünïcödé — ×", "line1\nline2", "?", "|", ">",
]


@pytest.mark.parametrize("note", TRICKY)
def test_note_round_trips_text_yaml_would_misread(tmp_path: Path, note: str) -> None:
    p = _write(tmp_path, "clusters:\n  a:\n    host: h\n    description:\n      - first\n")
    configedit.add_note(p, "a", note, DAY)
    assert load_config(str(p)).clusters["a"].description == ["first", note]


def test_note_keeps_a_utf8_bom(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_bytes("\ufeffclusters:\n  a:\n    host: h\n".encode("utf-8"))
    configedit.add_note(p, "a", "fact", DAY)
    assert p.read_bytes().startswith(b"\xef\xbb\xbfclusters:")
    assert load_config(str(p)).clusters["a"].description == ["fact"]


def test_note_refuses_a_description_shared_through_an_alias(tmp_path: Path) -> None:
    # Appending would change both clusters; the safety check must catch it.
    before = (
        "clusters:\n  a:\n    host: h\n    description: &shared\n      - common\n"
        "  b:\n    host: h\n    description: *shared\n"
    )
    p = _write(tmp_path, before)
    for cluster in ("a", "b"):
        with pytest.raises(configedit.EditError, match="more than intended"):
            configedit.add_note(p, cluster, "x", DAY)
    assert p.read_text(encoding="utf-8") == before


def test_note_refuses_a_flow_style_cluster(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  a: {host: h}\n")
    with pytest.raises(configedit.EditError, match="block mapping"):
        configedit.add_note(p, "a", "fact", DAY)


def test_failed_write_leaves_the_file_and_no_temp_file(tmp_path: Path, monkeypatch) -> None:
    before = "clusters:\n  a:\n    host: h\n"
    p = _write(tmp_path, before)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(configedit.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        configedit.add_note(p, "a", "fact", DAY)
    assert p.read_text(encoding="utf-8") == before
    assert [f.name for f in tmp_path.iterdir()] == ["config.yaml"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and file modes")
def test_write_follows_a_symlink_and_keeps_the_mode(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "config.yaml"
    real.parent.mkdir()
    real.write_text("clusters:\n  a:\n    host: h\n", encoding="utf-8")
    real.chmod(0o640)
    link = tmp_path / "config.yaml"
    os.symlink(real, link)
    configedit.add_note(link, "a", "fact", DAY)
    assert link.is_symlink()
    assert "- fact" in real.read_text(encoding="utf-8")
    assert real.stat().st_mode & 0o777 == 0o640


def test_verify_refuses_unintended_changes() -> None:
    with pytest.raises(configedit.EditError, match="more than intended"):
        configedit._verify("clusters:\n  a:\n    host: h\n", "clusters:\n  a:\n    host: x\n", lambda d: d)
    with pytest.raises(configedit.EditError, match="invalid"):
        configedit._verify("clusters: {}\n", "clusters:\n  a:\n    user: u\n", lambda d: {"clusters": {"a": {"user": "u"}}})


# --- ctun config --migrate ------------------------------------------------------

# Shaped like a real config: a commented header per cluster, unicode, a table.
OLD = """\
clusters:
  # KCIST @ KIT — no OTP.
  kcist:
    host: login2.kcist.kit.edu
    requires_otp: false            # advisory
    description: |
      KCIST @ KIT — 18 GPU nodes total.
      Partitions:
        small   13× node-s   4× RTX 4090
      No walltime limit — always set --time.

  # HoreKa @ KIT
  horeka:
    host: horeka.scc.kit.edu
    description: "HoreKa @ KIT (NHR)."   # short
    budget:
      unit: jobh
  plain:
    host: plain.example
"""

NEW = """\
clusters:
  # KCIST @ KIT — no OTP.
  kcist:
    host: login2.kcist.kit.edu
    requires_otp: false            # advisory
    description:
      - |
        KCIST @ KIT — 18 GPU nodes total.
        Partitions:
          small   13× node-s   4× RTX 4090
        No walltime limit — always set --time.

  # HoreKa @ KIT
  horeka:
    host: horeka.scc.kit.edu
    description:   # short
      - HoreKa @ KIT (NHR).
    budget:
      unit: jobh
  plain:
    host: plain.example
"""


def test_migrate_turns_string_descriptions_into_lists(tmp_path: Path) -> None:
    edit = _migrated(tmp_path, OLD)
    assert edit.new == NEW
    assert edit.changes == [
        "kcist: description string -> 1 item",
        "horeka: description string -> 1 item",
    ]


def test_migrate_splits_at_blank_lines_only(tmp_path: Path) -> None:
    old = (
        "clusters:\n  a:\n    host: h\n    description: |   # about a\n"
        "      Para one,\n      continued.\n\n      Para two.\n"
    )
    edit = _migrated(tmp_path, old)
    assert edit.new == (
        "clusters:\n  a:\n    host: h\n    description:   # about a\n"
        "      - |\n        Para one,\n        continued.\n      - Para two.\n"
    )
    assert edit.changes == ["a: description string -> 2 items"]


def test_migrate_keeps_an_indented_paragraph_intact(tmp_path: Path) -> None:
    old = (
        "clusters:\n  a:\n    host: h\n    description: |\n"
        "      Intro.\n\n        row 1\n        row 2\n"
    )
    new = _migrated(tmp_path, old).new
    assert new.endswith("    description:\n      - Intro.\n      - |2\n          row 1\n          row 2\n")


def test_migrate_moves_the_comment_of_a_multi_line_plain_string(tmp_path: Path) -> None:
    old = (
        "clusters:\n  a:\n    host: h\n    description: plain words\n"
        "      continued here   # tail\n    user: u\n"
    )
    new = _migrated(tmp_path, old).new
    assert new == (
        "clusters:\n  a:\n    host: h\n    description:   # tail\n"
        "      - plain words continued here\n    user: u\n"
    )


def test_migrate_leaves_lists_and_missing_descriptions_alone(tmp_path: Path) -> None:
    old = 'clusters:\n  a:\n    host: h\n    description: [x]\n  b:\n    host: h\n  c:\n    host: h\n    description: ""\n'
    edit = _migrated(tmp_path, old)
    assert edit.changes == []
    assert edit.new == old


def test_migrate_writes_a_backup_and_then_has_nothing_left(tmp_path: Path) -> None:
    edit = _migrated(tmp_path, OLD)
    backup = configedit.apply(edit, backup=True)
    assert backup == tmp_path / "config.yaml.bak"
    assert backup.read_text(encoding="utf-8") == OLD
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == NEW
    assert configedit.plan_migration(tmp_path / "config.yaml").changes == []


def test_migrate_changes_nothing_outside_the_descriptions(tmp_path: Path) -> None:
    import difflib

    old_lines, new_lines = OLD.splitlines(), _migrated(tmp_path, OLD).new.splitlines()
    # The old-side lines of every changed block must be description lines: the
    # `description:` key line or a line of its value (indented deeper than it).
    description_lines = set()
    for i, line in enumerate(old_lines):
        if line.strip().startswith("description:"):
            indent = len(line) - len(line.lstrip())
            description_lines.add(i)
            j = i + 1
            while j < len(old_lines) and (
                not old_lines[j].strip() or len(old_lines[j]) - len(old_lines[j].lstrip()) > indent
            ):
                description_lines.add(j)
                j += 1
    opcodes = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes()
    changed = {i for tag, i1, i2, _, _ in opcodes if tag != "equal" for i in range(i1, i2)}
    assert changed and changed <= description_lines


# Every way to write a string in YAML, with what the migration must turn it into.
STYLES = {
    "plain": "description: Plain text here",
    "single-quoted": "description: 'It''s quoted'",
    "double-quoted": 'description: "Tab\\tand \\u00e9 and \\"q\\""',
    "literal": "description: |\n      A\n      B",
    "literal-strip": "description: |-\n      A\n      B",
    "literal-keep": "description: |+\n      A\n\n      B\n\n",
    "literal-indent": "description: |2\n        indented first\n      then less",
    "folded": "description: >\n      one\n      two\n\n      three",
    "header-comment": "description: |   # about a\n      A",
    "quoted-key": '"description": |\n      Q',
}


@pytest.mark.parametrize("body", STYLES.values(), ids=STYLES.keys())
def test_migrate_handles_every_string_style(tmp_path: Path, body: str) -> None:
    p = _write(tmp_path, f"clusters:\n  a:\n    host: h\n    {body}\n  b:\n    host: h\n")
    old = load_config(str(p)).clusters["a"].description
    configedit.apply(configedit.plan_migration(p))
    migrated = load_config(str(p))
    assert migrated.clusters["a"].description == configedit.split_paragraphs(old)
    assert migrated.clusters["b"].host == "h"


def test_migrate_keeps_crlf_line_endings(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_bytes(b"clusters:\r\n  a:\r\n    host: h\r\n    description: |\r\n      One.\r\n")
    configedit.apply(configedit.plan_migration(p))
    raw = p.read_bytes()
    assert raw.endswith(b"    description:\r\n      - One.\r\n")
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_split_paragraphs() -> None:
    split = configedit.split_paragraphs
    assert split("One line.\n") == ["One line."]
    assert split("A\nB\n") == ["A\nB\n"]
    assert split("A\n\n\nB\n  b2\n") == ["A", "B\n  b2\n"]
    assert split("   \n") == []


# --- ctun hostkey --pin ---------------------------------------------------------


def _pinned(tmp_path: Path, text: str, file: str = "/kh") -> configedit.Edit:
    return configedit.plan_host_key_pin(_write(tmp_path, text), "bw", file)


def test_pin_adds_both_keys_after_the_connection_keys(tmp_path: Path) -> None:
    edit = _pinned(
        tmp_path,
        "clusters:\n  bw:\n    host: uc3.example  # login node\n    user: ab\n"
        "    requires_otp: true\n    description:\n      - note\n",
    )
    assert edit.new == (
        "clusters:\n  bw:\n    host: uc3.example  # login node\n    user: ab\n"
        "    requires_otp: true\n    strict_host_key_checking: 'yes'\n    known_hosts_file: /kh\n"
        "    description:\n      - note\n"
    )
    assert edit.changes == ["bw: strict_host_key_checking -> yes", "bw: known_hosts_file -> /kh"]
    assert "(pinned)" in edit.diff()[1]


def test_pin_replaces_values_in_place_and_keeps_their_comments(tmp_path: Path) -> None:
    edit = _pinned(
        tmp_path,
        "clusters:\n  bw:\n    host: h\n    strict_host_key_checking: accept-new   # default\n"
        "    known_hosts_file:\n",
    )
    assert edit.new == (
        "clusters:\n  bw:\n    host: h\n    strict_host_key_checking: 'yes'   # default\n"
        "    known_hosts_file: /kh\n"
    )


def test_pin_on_a_pinned_cluster_changes_nothing(tmp_path: Path) -> None:
    text = "clusters:\n  bw:\n    host: h\n    strict_host_key_checking: yes\n    known_hosts_file: /kh\n"
    edit = _pinned(tmp_path, text)
    assert edit.changes == [] and edit.new == text


def test_pin_puts_keys_first_when_there_are_no_connection_keys_before(tmp_path: Path) -> None:
    edit = _pinned(tmp_path, "clusters:\n  bw:\n    description: x\n    ssh_alias: bw\n")
    assert yaml.safe_load(edit.new)["clusters"]["bw"]["known_hosts_file"] == "/kh"
    assert edit.new.index("ssh_alias") < edit.new.index("strict_host_key_checking")


def test_pin_quotes_a_path_only_where_yaml_needs_it(tmp_path: Path) -> None:
    edit = _pinned(tmp_path, "clusters:\n  bw:\n    host: h\n", file="~/.config/cluster-tunnel/known_hosts")
    assert "    known_hosts_file: ~/.config/cluster-tunnel/known_hosts\n" in edit.new
    assert load_config(str(_write(tmp_path, edit.new))).clusters["bw"].known_hosts_file.startswith("~/")


def test_pin_refuses_a_block_value(tmp_path: Path) -> None:
    with pytest.raises(configedit.EditError, match="not a one-line value"):
        _pinned(tmp_path, "clusters:\n  bw:\n    host: h\n    known_hosts_file: |\n      /kh\n")


def test_pin_keeps_crlf_and_writes_a_backup(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_bytes(b"clusters:\r\n  bw:\r\n    host: h\r\n")
    edit = configedit.plan_host_key_pin(p, "bw", "/kh")
    backup = configedit.apply(edit, backup=True)
    assert backup.read_bytes() == b"clusters:\r\n  bw:\r\n    host: h\r\n"
    assert p.read_bytes() == (
        b"clusters:\r\n  bw:\r\n    host: h\r\n    strict_host_key_checking: 'yes'\r\n"
        b"    known_hosts_file: /kh\r\n"
    )
