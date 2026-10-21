"""Job records: `run` writes them, `jobs --json` attaches them (ssh mocked)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel import api, facts, jobs, records
from cluster_tunnel.cli import cli
from tests.test_cli import _cfg, _patch_jobs

FACTS_OUTPUT = (
    "slurm_cluster=haic\n"
    "job_name=train\n"
    "workdir=/work/repo\n"
    "commit=0123456789abcdef0123456789abcdef01234567\n"
    "branch=main\n"
    "pushed=no\n"
    "dirty= M models/train.py\n"
)


class FakeCluster:
    """Answers `feed_script` for facts.sh and records.sh like a cluster would."""

    def __init__(self, facts_rc: int = 0) -> None:
        self.facts_rc = facts_rc
        self.written: dict[str, dict] = {}

    def feed_script(self, spec, script_path: Path, args: list, timeout=None) -> subprocess.CompletedProcess:
        if script_path == facts.SCRIPT_PATH:
            if self.facts_rc:
                return subprocess.CompletedProcess([], self.facts_rc, "", "squeue: not found")
            return subprocess.CompletedProcess([], 0, FACTS_OUTPUT, "")
        command, *rest = args
        if command == "write":
            job_id, payload = rest
            self.written[job_id] = json.loads(base64.b64decode(payload))
            return subprocess.CompletedProcess([], 0, "", "")
        lines = "\n".join(json.dumps(record) for record in self.written.values())
        return subprocess.CompletedProcess([], 0, lines, "")


def _patch_run(monkeypatch, cluster: FakeCluster, output: str, rc: int = 0) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)

    def fake_run(spec, tokens, tty=False, on_line=None):
        if on_line is not None:
            for line in output.splitlines():
                on_line(line)
        return rc

    monkeypatch.setattr("cluster_tunnel.ssh.run", fake_run)
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)
    monkeypatch.setattr("cluster_tunnel.cmdlog.record", lambda name, tokens: "0")
    monkeypatch.setattr("cluster_tunnel.cmdlog.record_jobs", lambda name, entry_id, job_ids: None)


def test_run_writes_a_record_with_the_facts(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Run ID: x\nSubmitted batch job 4822010\n")
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "run", "--project", "htfm", "--note", "lr sweep", "--", "./submit.sh"]
    )
    assert r.exit_code == 0, r.output
    assert "recorded job 4822010" in r.output
    record = cluster.written["4822010"]
    assert record["schema_version"] == records.SCHEMA_VERSION
    assert record["slurm_cluster"] == "haic"
    assert record["job_name"] == "train"
    assert record["project"] == "htfm" and record["note"] == "lr sweep"
    assert record["command"] == ["./submit.sh"]
    assert record["code"] == {
        "workdir": "/work/repo",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "branch": "main",
        "pushed": False,
        "uncommitted": [" M models/train.py"],
    }
    assert record["submitted_at"] and record["submitted_from"]


def test_api_run_returns_the_records_it_wrote(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    result = api.run("localhost", ["sbatch", "job.sh"], project="htfm")
    assert [record.job_id for record in result.records] == ["7"]
    assert result.records[0].project == "htfm" and result.warnings == []


def test_run_without_a_job_id_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "JOBID PARTITION\n", rc=3)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "squeue", "--me"])
    assert r.exit_code == 3
    assert cluster.written == {}


def test_a_failing_fact_warns_but_still_records(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(facts_rc=1)
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "could not collect the facts of job 7" in r.output
    assert cluster.written["7"]["code"]["commit"] is None
    assert cluster.written["7"]["slurm_cluster"] is None


def test_records_off_writes_no_record(tmp_path: Path, monkeypatch) -> None:
    config = _cfg(tmp_path, monkeypatch)
    config.write_text("clusters:\n  localhost:\n    host: localhost\n    job_records: false\n", encoding="utf-8")
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--note", "x", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "no job record written" in r.output
    assert cluster.written == {}


def test_a_tty_run_writes_no_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    monkeypatch.setattr("cluster_tunnel.ssh.run", lambda spec, tokens, tty=False: 0)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--tty", "--project", "htfm", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "no job record written (a TTY hides the output)" in r.output
    assert cluster.written == {}


def test_from_json_ignores_unknown_keys_and_fills_missing() -> None:
    record = records.JobRecord.from_json('{"job_id": 5, "future_field": [1], "project": "p"}')
    assert record.job_id == "5" and record.project == "p"
    assert record.note is None and record.code == {} and record.command == []


def test_submitted_job_ids_reads_every_submission() -> None:
    output = "Submitted batch job 1\nnoise Submitted batch job 9\nSubmitted batch job 2\n"
    assert jobs.submitted_job_ids(output) == ["1", "2"]


def test_jobs_json_attaches_records(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[Job("active", "101", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "train")])
    stored = [
        records.JobRecord.from_json('{"job_id": "101", "project": "htfm"}'),
        records.JobRecord.from_json('{"job_id": "99", "project": "htfm"}'),
    ]
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda spec, since, ids: stored)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"])
    entry = json.loads(r.output)["clusters"][0]
    assert entry["jobs"][0]["record"]["project"] == "htfm"
    assert [record["job_id"] for record in entry["records_without_job"]] == ["99"]
    assert entry["records_error"] is None


def test_jobs_json_reports_unreadable_records_and_still_lists_the_jobs(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[Job("active", "101", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "train")])

    def unreadable(spec, since, ids):
        raise RuntimeError("could not read the job records: scontrol: not found")

    monkeypatch.setattr("cluster_tunnel.records.list_recent", unreadable)
    entry = json.loads(CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"]).output)["clusters"][0]
    assert [job["jobid"] for job in entry["jobs"]] == ["101"]
    assert entry["records_error"].endswith("scontrol: not found")


def test_api_jobs_reads_no_records_unless_asked(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch)
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda *a: (_ for _ in ()).throw(AssertionError))
    [entry] = api.jobs("localhost")
    assert entry.records is None and "records_without_job" not in entry.as_dict()
