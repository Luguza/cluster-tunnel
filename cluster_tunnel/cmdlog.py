"""Per-cluster command log (commands only, never their output), in the cache.

A lightweight, temporary record of the commands sent to a cluster during the
current tunnel/session. It lives in the cache and is wiped on `logout` (and on a
fresh `login`). `status` reads it to show activity. Only the command tokens, a
timestamp and the IDs of the jobs a command submitted are stored — never
stdin/stdout/stderr.

Each command is one line, written before it runs. The job IDs follow, once the
command has finished, in a line of their own that names the command's ``id``:
appending never rewrites the file, so parallel `ctun run`s cannot race.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from cluster_tunnel import paths

#: Length of a command's ``id`` in hex characters.
_ID_HEX_LENGTH = 12


def _log_file(name: str) -> Path:
    return paths.cmdlog_dir() / f"{name}.jsonl"


def _append(name: str, line: dict) -> None:
    paths.cmdlog_dir().mkdir(parents=True, exist_ok=True)
    with _log_file(name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _lines(name: str) -> list[dict]:
    """Every readable line of the cluster's log, oldest first."""
    f = _log_file(name)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _is_jobs_line(line: dict) -> bool:
    return "jobs_of" in line


def record(name: str, command: list[str]) -> str:
    """Append one command (with a timestamp) to the cluster's log; return its ``id``."""
    entry_id = secrets.token_hex(_ID_HEX_LENGTH // 2)
    _append(name, {"id": entry_id, "epoch": int(time.time()), "command": list(command)})
    return entry_id


def record_jobs(name: str, entry_id: str, job_ids: list[str]) -> None:
    """Append the IDs of the jobs that the command ``entry_id`` submitted."""
    _append(name, {"jobs_of": entry_id, "job_ids": list(job_ids)})


def summary(name: str) -> dict:
    """Return ``{'count': int, 'last_epoch': int | None}`` for a cluster."""
    commands = [line for line in _lines(name) if not _is_jobs_line(line)]
    last_epoch: int | None = commands[-1].get("epoch") if commands else None
    return {"count": len(commands), "last_epoch": last_epoch}


def entries(name: str) -> list[dict]:
    """Return all logged commands (oldest first) as ``{'id', 'epoch', 'command'}``.

    A command that submitted jobs also carries ``job_ids``. Entries of older
    logs have no ``id``; job lines naming an unknown ``id`` are ignored.
    """
    lines = _lines(name)
    commands = [line for line in lines if not _is_jobs_line(line)]
    by_id = {command["id"]: command for command in commands if "id" in command}
    for line in lines:
        command = by_id.get(line["jobs_of"]) if _is_jobs_line(line) else None
        if command is not None:
            command.setdefault("job_ids", []).extend(line.get("job_ids", []))
    return commands


def clear(name: str) -> None:
    """Delete the command log for a cluster (on logout / fresh login)."""
    _log_file(name).unlink(missing_ok=True)
