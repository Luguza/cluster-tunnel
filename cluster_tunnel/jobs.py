"""Job overview: run the bundled ``jobs.sh`` probe over the tunnel and parse it.

``ctun jobs`` ships :data:`SCRIPT_PATH` to a cluster's login node via
``bash -s`` (see :func:`cluster_tunnel.ssh.feed_script`) and reads back one
pipe-delimited line per job. This module owns the wire format — the field order,
the split, and the active/finished ordering — so the CLI layer only renders.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from cluster_tunnel import constants, ssh
from cluster_tunnel.ssh import ConnSpec

PACKAGE_DIR = Path(__file__).parent

_SUBMITTED_JOB = re.compile(r"^Submitted batch job (\d+)", re.MULTILINE)

#: The bundled probe shipped over the tunnel; universal across Slurm clusters.
SCRIPT_PATH = PACKAGE_DIR / "scripts" / "jobs.sh"

#: Number of ``|``-separated columns each data line carries. The name is last,
#: so it is captured with ``split('|', _N_FIELDS - 1)`` and may itself contain
#: a stray ``|`` without shifting the earlier columns.
_N_FIELDS = 9

#: The probe's line naming the Slurm ClusterName: ``cluster|<name>``.
_CLUSTER_PREFIX = "cluster|"


@dataclass
class Job:
    """One Slurm job as reported by the probe."""

    source: str  # "active" (live queue) or "done" (recently finished)
    jobid: str
    state: str
    elapsed: str
    timelimit: str
    partition: str
    nodes: str
    start: str  # as Slurm prints it; for a pending job its estimate, or N/A
    name: str

    @property
    def active(self) -> bool:
        return self.source == "active"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Overview:
    """What the probe reports for one cluster."""

    #: Slurm's ``ClusterName`` (what jobs see as ``SLURM_CLUSTER_NAME``); ``None`` if unknown.
    slurm_cluster: str | None
    jobs: list[Job]


def submitted_job_ids(output: str) -> list[str]:
    """The job IDs of every ``Submitted batch job <N>`` line in ``output``."""
    return _SUBMITTED_JOB.findall(output)


def parse_cluster_name(lines: list[str]) -> str | None:
    """The ClusterName from the probe's ``cluster|<name>`` line, or ``None`` without one."""
    for line in lines:
        if line.startswith(_CLUSTER_PREFIX):
            return line[len(_CLUSTER_PREFIX):].strip() or None
    return None


def parse_line(line: str) -> Job | None:
    """Parse one probe output line into a :class:`Job`, or ``None`` if malformed.

    The name field is last and split-limited, so job names containing ``|`` stay
    intact. Lines with too few fields (unexpected noise on stdout) are dropped.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return None
    parts = line.split("|", _N_FIELDS - 1)
    if len(parts) != _N_FIELDS:
        return None
    source, jobid, state, elapsed, timelimit, partition, nodes, start, name = parts
    if source not in ("active", "done") or not jobid:
        return None
    return Job(source, jobid, state, elapsed, timelimit, partition, nodes, start, name)


def _order(jobs: list[Job]) -> list[Job]:
    """Active jobs first (in queue order), then finished jobs most-recent-first.

    The probe prints active lines in ``squeue`` order and finished lines in
    ``sacct`` chronological order, so reversing the finished group yields
    newest-finished-first. A job seen as both active and finished (it ended
    between the two probe calls) is kept only in its active form.
    """
    seen: set[str] = set()
    active: list[Job] = []
    done: list[Job] = []
    for job in jobs:
        if job.active:
            seen.add(job.jobid)
            active.append(job)
    for job in jobs:
        if not job.active and job.jobid not in seen:
            seen.add(job.jobid)
            done.append(job)
    done.reverse()
    return active + done


def query(spec: ConnSpec, user: str | None, since_minutes: int) -> Overview:
    """Run the probe on ``spec``'s login node and return the ClusterName and its jobs, ordered.

    ``user`` may be ``None`` (or empty) to let the script resolve it remotely via
    ``id -un``. ``since_minutes`` is the finished-job history window; ``0`` skips
    accounting and returns only the live queue. Raises :class:`RuntimeError` if
    the probe cannot run (e.g. no ``squeue`` on the host).
    """
    res = ssh.feed_script(
        spec, SCRIPT_PATH, [user or "", since_minutes], timeout=constants.PROBE_TIMEOUT
    )
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"probe exited {res.returncode}"
        raise RuntimeError(detail)
    lines = res.stdout.splitlines()
    jobs = [job for line in lines if (job := parse_line(line))]
    return Overview(parse_cluster_name(lines), _order(jobs))
