"""References in a job note: ``commit:<hash>`` and ``job:<id>``, checked on the cluster.

A note may point at commits and jobs only through these explicit markers, so
the reader (and the VS Code extension) can link them, and ctun can refuse a
note that points at something that does not exist.
"""

from __future__ import annotations

import re
from typing import Optional

from cluster_tunnel import records, ssh
from cluster_tunnel.ssh import ConnSpec

_REFERENCE = re.compile(r"\b(?:commit:[0-9a-fA-F]{7,40}|job:\d+(?:_\d+)?)\b")


def references(note: str) -> list[str]:
    """Every distinct ``commit:<hash>`` / ``job:<id>`` marker in ``note``, in order."""
    return list(dict.fromkeys(_REFERENCE.findall(note)))


def unknown_references(spec: ConnSpec, note: str, project: Optional[str]) -> list[str]:
    """The references in ``note`` that the cluster does not know.

    Commits are looked up in the repos (working directories) of the earlier
    records of ``project``, or of every record when no project is given.
    Raises :class:`RuntimeError` when the check itself cannot run.
    """
    refs = references(note)
    if not refs:
        return []
    repos: list[str] = []
    if any(ref.startswith("commit:") for ref in refs):
        repos = _record_repos(spec, project)
    res = records.call(spec, ["refs", *repos, "--", *refs])
    if res.returncode != 0:
        raise RuntimeError(f"could not check the note's references: {ssh.failure(res)}")
    found = set(res.stdout.split())
    return [ref for ref in refs if ref not in found]


def _record_repos(spec: ConnSpec, project: Optional[str]) -> list[str]:
    workdirs = (
        record.code.get("workdir")
        for record in records.list_all(spec)
        if project is None or record.project == project
    )
    return list(dict.fromkeys(workdir for workdir in workdirs if workdir))
