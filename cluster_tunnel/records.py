"""Job records: one write-once JSON file per submitted job, stored on the cluster.

``ctun run`` writes a record for each ``Submitted batch job <N>`` in its output.
The record holds what the agent said about the job (``project``, ``note``) and
the facts ctun collected itself (:mod:`cluster_tunnel.facts`). ``records.sh``
keeps them in ``~/.ctun/jobs/<ClusterName>/<job id>.json`` on the cluster, so
every machine that submits there sees the same records.
"""

from __future__ import annotations

import base64
import json
import re
import socket
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cluster_tunnel import ssh
from cluster_tunnel.facts import JobFacts
from cluster_tunnel.ssh import ConnSpec

SCRIPT_PATH = Path(__file__).parent / "scripts" / "records.sh"

SCHEMA_VERSION = 1

_SUBMITTED_JOB = re.compile(r"^Submitted batch job (\d+)", re.MULTILINE)


@dataclass
class JobRecord:
    """One submitted job. Never changed after it is written."""

    schema_version: int
    slurm_cluster: Optional[str]
    job_id: str
    job_name: Optional[str]
    project: Optional[str]
    note: Optional[str]
    submitted_at: str
    submitted_from: str
    #: ``{workdir, commit, branch, pushed, uncommitted}``; each may be ``None`` (unknown).
    code: dict[str, Any]
    command: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "JobRecord":
        """Parse a record; keys this ctun does not know are ignored, missing ones are ``None``."""
        raw = json.loads(text)
        known = {f.name for f in fields(cls)}
        values = {name: raw.get(name) for name in known}
        values["code"] = values["code"] or {}
        values["command"] = values["command"] or []
        values["job_id"] = str(raw["job_id"])
        return cls(**values)


def submitted_job_ids(output: str) -> list[str]:
    """The job IDs of every ``Submitted batch job <N>`` line in ``output``."""
    return _SUBMITTED_JOB.findall(output)


def build(
    job_id: str,
    facts: JobFacts,
    *,
    project: Optional[str],
    note: Optional[str],
    command: list[str],
) -> JobRecord:
    """A record for ``job_id`` from the agent's ``project``/``note`` and ctun's ``facts``."""
    return JobRecord(
        schema_version=SCHEMA_VERSION,
        slurm_cluster=facts.slurm_cluster,
        job_id=job_id,
        job_name=facts.job_name,
        project=project,
        note=note,
        submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        submitted_from=socket.gethostname(),
        code={
            "workdir": facts.workdir,
            "commit": facts.commit,
            "branch": facts.branch,
            "pushed": facts.pushed,
            "uncommitted": facts.uncommitted,
        },
        command=command,
    )


def write(spec: ConnSpec, record: JobRecord) -> None:
    """Store ``record`` on the cluster; raises :class:`RuntimeError` if that fails."""
    payload = base64.b64encode(record.to_json().encode("utf-8")).decode("ascii")
    res = ssh.feed_script(spec, SCRIPT_PATH, ["write", record.job_id, payload])
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"exit code {res.returncode}"
        raise RuntimeError(f"could not write the record of job {record.job_id}: {detail}")


def list_recent(spec: ConnSpec, since_minutes: int, job_ids: list[str]) -> list[JobRecord]:
    """Records written in the last ``since_minutes`` minutes, plus those of ``job_ids``."""
    return _read(spec, ["list", since_minutes, *job_ids])


def list_all(spec: ConnSpec) -> list[JobRecord]:
    """Every record on the cluster."""
    return _read(spec, ["all"])


def _read(spec: ConnSpec, args: list[object]) -> list[JobRecord]:
    res = ssh.feed_script(spec, SCRIPT_PATH, args)
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"exit code {res.returncode}"
        raise RuntimeError(f"could not read the job records: {detail}")
    records = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            records.append(JobRecord.from_json(line))
        except (ValueError, TypeError, KeyError):
            continue  # a damaged file must not hide the others
    return records
