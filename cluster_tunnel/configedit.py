"""Surgical edits of the user's YAML config: `ctun note` and `ctun config --migrate`.

The config is a hand-edited, commented file, so ctun never re-serialises it.
Each edit replaces or inserts only the lines of one ``description`` value, at
the exact positions PyYAML's composer reports, and leaves every other byte of
the file alone. Before anything is written, the edited text is parsed again and
must equal the old data with exactly the intended change applied (and still be a
valid config); otherwise the edit is refused and the file stays untouched.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import yaml


class EditError(Exception):
    """The config could not be edited safely; nothing was written."""


class OldFormatError(EditError):
    """The cluster's description is a single string; notes need the list form."""


@dataclass
class Edit:
    """The edited text of a config file, ready to diff or write."""

    path: Path
    old: str
    new: str
    newline: str  # the file's line ending, kept on write
    changes: list[str]  # one human-readable line per changed cluster

    def diff(self) -> list[str]:
        """The change as unified-diff lines (no trailing newlines)."""
        return list(
            difflib.unified_diff(
                self.old.splitlines(),
                self.new.splitlines(),
                fromfile=f"{self.path.name} (now)",
                tofile=f"{self.path.name} (migrated)",
                lineterm="",
            )
        )


# --- public API -----------------------------------------------------------------


def add_note(path: Path, cluster: str, note: str, today: date | None = None) -> int:
    """Append `note` to `cluster`'s description list and write the file.

    A missing (or empty) description becomes a new one-item list. Raises
    :class:`OldFormatError` for a single-string description — that cluster
    has to be migrated first — and :class:`EditError` if the file can't be
    edited safely. Returns the number of (non-blank) notes afterwards.
    """
    note = note.strip()
    if not note:
        raise EditError("the note is empty")
    old, newline = _read(path)
    node = _cluster_node(_compose(old), cluster)
    comment = f"# added via ctun note, {(today or date.today()).isoformat()}"

    entry = _entry(node, "description")
    if entry is None:
        new = _insert_description(old, node, note, comment)
    else:
        key, value = entry
        if isinstance(value, yaml.ScalarNode):
            if value.value.strip():
                raise OldFormatError(
                    f"'{cluster}' has a single-string description, which can't take notes"
                )
            new = _splice(old, [_replace_value(old, key, value, [note], comment)])
        elif isinstance(value, yaml.SequenceNode) and not value.flow_style:
            new = _splice(old, [_append_item(old, value, note, comment)])
        elif isinstance(value, yaml.SequenceNode):
            if value.start_mark.line != value.end_mark.line:
                raise EditError(
                    f"'{cluster}' has a multi-line [...] description; turn it into "
                    "a '- item' list by hand first"
                )
            items = [item.value for item in value.value] + [note]
            new = _splice(old, [_replace_value(old, key, value, items, comment)])
        else:
            raise EditError(f"'{cluster}'s description is neither text nor a list")

    def change(data: dict) -> dict:
        settings = data["clusters"][cluster]
        current = settings.get("description")
        settings["description"] = (current if isinstance(current, list) else []) + [note]
        return data

    _verify(old, new, change)
    _write(path, new, newline)
    return sum(1 for item in yaml.safe_load(new)["clusters"][cluster]["description"] if item.strip())


def plan_migration(path: Path) -> Edit:
    """Compute (without writing) the migration of `path` to the current format."""
    old, newline = _read(path)
    new, changes = old, []
    for step in _MIGRATIONS:
        new, step_changes = step(new)
        changes += step_changes
    return Edit(path=path, old=old, new=new, newline=newline, changes=changes)


def apply(edit: Edit, backup: bool = False) -> Path | None:
    """Write `edit`; with `backup`, first copy the current file to ``<name>.bak``."""
    copy = None
    if backup:
        copy = edit.path.with_name(edit.path.name + ".bak")
        shutil.copy2(edit.path, copy)
    _write(edit.path, edit.new, edit.newline)
    return copy


def split_paragraphs(text: str) -> list[str]:
    """Split an old-style description at blank lines: one list item per paragraph.

    Lines within a paragraph stay together, so tables and wrapped sentences are
    never cut apart. A multi-line item keeps a final newline, so it is written
    as a plain ``|`` block.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return ["\n".join(p) + ("\n" if len(p) > 1 else "") for p in paragraphs]


# --- migrations -----------------------------------------------------------------


def _descriptions_to_lists(text: str) -> tuple[str, list[str]]:
    """Turn every single-string cluster description into a list (split at blank lines)."""
    clusters = _entry(_compose(text), "clusters")
    if clusters is None or not isinstance(clusters[1], yaml.MappingNode):
        return text, []

    edits: list[tuple[int, int, str]] = []
    converted: dict[str, list[str]] = {}
    for name_node, node in clusters[1].value:
        entry = _entry(node, "description") if isinstance(node, yaml.MappingNode) else None
        if entry is None:
            continue
        key, value = entry
        if not isinstance(value, yaml.ScalarNode) or not value.value.strip():
            continue
        items = split_paragraphs(value.value)
        edits.append(_replace_value(text, key, value, items))
        converted[name_node.value] = items
    if not converted:
        return text, []

    new = _splice(text, edits)

    def change(data: dict) -> dict:
        for name, items in converted.items():
            data["clusters"][name]["description"] = items
        return data

    _verify(text, new, change)
    changes = [
        f"{name}: description string -> {len(items)} item{'s' if len(items) != 1 else ''}"
        for name, items in converted.items()
    ]
    return new, changes


# Applied in order by `plan_migration`; each takes and returns the config text.
# A future format change adds its step here.
_MIGRATIONS: list[Callable[[str], tuple[str, list[str]]]] = [_descriptions_to_lists]


# --- YAML positions -------------------------------------------------------------


def _compose(text: str) -> yaml.Node:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise EditError(f"the config does not parse as YAML ({exc})") from exc
    if not isinstance(root, yaml.MappingNode):
        raise EditError("the config is not a YAML mapping")
    return root


def _entry(node: yaml.MappingNode, key: str) -> tuple[yaml.Node, yaml.Node] | None:
    """The (key, value) node pair for `key` in a mapping node, if present."""
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def _cluster_node(root: yaml.MappingNode, cluster: str) -> yaml.MappingNode:
    clusters = _entry(root, "clusters")
    entry = None
    if clusters and isinstance(clusters[1], yaml.MappingNode):
        entry = _entry(clusters[1], cluster)
    if entry is None:
        raise EditError(f"no cluster '{cluster}' in the config")
    node = entry[1]
    if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
        raise EditError(f"cluster '{cluster}' is not a block mapping ('key: value' lines)")
    return node


def _content_end(text: str, node: yaml.Node) -> int:
    """Index just past the last character of `node`'s own content.

    PyYAML's end marks of block scalars and block collections run on over the
    blank lines and comments that follow them; this stops at the content.
    """
    if isinstance(node, yaml.ScalarNode):
        if node.style in ("|", ">"):
            return len(text[: node.end_mark.index].rstrip())
        return node.end_mark.index
    if node.flow_style or not node.value:
        return node.end_mark.index
    last = node.value[-1]
    return _content_end(text, last[1] if isinstance(node, yaml.MappingNode) else last)


def _line_end(text: str, index: int) -> int:
    """Index of the line break ending the line that contains `index`."""
    end = text.find("\n", index)
    return len(text) if end < 0 else end


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply (start, end, replacement) edits; they must not overlap."""
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


# --- the edits ------------------------------------------------------------------


def _replace_value(
    text: str,
    key: yaml.Node,
    value: yaml.Node,
    items: list[str],
    comment: str | None = None,
) -> tuple[int, int, str]:
    """Edit replacing a scalar or one-line flow value with a block list of `items`.

    The list goes below the key, one level deeper. A comment on the old value's
    line (after a quoted string, or on a ``|`` header) moves to the key's line.
    """
    start = text.index(":", key.end_mark.index) + 1
    content_end = _content_end(text, value)
    end = _line_end(text, content_end)
    if isinstance(value, yaml.ScalarNode) and value.style in ("|", ">"):
        header = text[value.start_mark.index : _line_end(text, value.start_mark.index)]
        kept = re.search(r"\s+#.*$", header)
    else:
        kept = re.search(r"\s+#.*$", text[content_end:end])
    block = _render_items(items, key.start_mark.column + 2, comment)
    return start, end, (kept.group(0) if kept else "") + "\n" + block


def _append_item(text: str, seq: yaml.SequenceNode, note: str, comment: str) -> tuple[int, int, str]:
    """Edit appending `note` to a block list, aligned with its dashes."""
    at = _line_end(text, _content_end(text, seq.value[-1]))
    return at, at, "\n" + _render_items([note], seq.start_mark.column, comment)


def _insert_description(text: str, node: yaml.MappingNode, note: str, comment: str) -> str:
    """Add a ``description`` list to a cluster that has none.

    It goes in before ``restrictions``/``budget`` (where the example config keeps
    it), else after the last key. Comments sitting above the following key stay
    above that key.
    """
    column = node.value[0][0].start_mark.column
    block = " " * column + "description:\n" + _render_items([note], column + 2, comment)
    keys = [k.value if isinstance(k, yaml.ScalarNode) else None for k, _ in node.value]
    later = [i for i, k in enumerate(keys) if k in ("restrictions", "budget")]
    position = later[0] if later else len(keys)
    if position == 0:
        at = _line_start(text, node.value[0][0].start_mark.index)
        return _splice(text, [(at, at, block + "\n")])
    at = _line_end(text, _content_end(text, node.value[position - 1][1]))
    return _splice(text, [(at, at, "\n" + block)])


class _Dumper(yaml.SafeDumper):
    """Writes multi-line strings as ``|`` blocks, like a hand-written config."""


_Dumper.add_representer(
    str,
    lambda dumper, data: dumper.represent_scalar(
        "tag:yaml.org,2002:str", data, style="|" if "\n" in data else None
    ),
)


def _render_items(items: list[str], column: int, comment: str | None = None) -> str:
    """Block-list lines for `items`, dashes at `column`; `comment` on the last item."""
    rendered = []
    for i, item in enumerate(items):
        out = yaml.dump([item], Dumper=_Dumper, allow_unicode=True, width=float("inf"))
        lines = out.rstrip("\n").split("\n")
        if comment and i == len(items) - 1:
            lines[0] += "  " + comment
        rendered += [" " * column + line if line else line for line in lines]
    return "\n".join(rendered)


# --- verify and write -----------------------------------------------------------


def _verify(old: str, new: str, change: Callable[[dict], dict]) -> None:
    """Refuse unless `new` parses to `old`'s data with exactly `change` applied."""
    from cluster_tunnel.config import Config

    expected = change(yaml.safe_load(old) or {})
    try:
        actual = yaml.safe_load(new) or {}
    except yaml.YAMLError as exc:
        raise EditError(f"the edited file would not parse ({exc}); nothing was written") from exc
    if actual != expected:
        raise EditError("the edit would change more than intended; nothing was written")
    try:
        Config(**actual)
    except Exception as exc:  # noqa: BLE001 — pydantic's ValidationError
        raise EditError(f"the edited config would be invalid ({exc}); nothing was written") from exc


def _read(path: Path) -> tuple[str, str]:
    """The file's text with ``\\n`` line breaks, and the line ending it uses."""
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), newline


def _write(path: Path, text: str, newline: str) -> None:
    """Replace the file atomically, keeping its mode and following a symlink."""
    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
