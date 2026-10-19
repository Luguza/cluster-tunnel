"""Job records: `run` writes them, `jobs --json` attaches them (ssh mocked)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel import facts, notes, records
from cluster_tunnel.cli import cli
from cluster_tunnel.constants import ExitCode
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

    def __init__(self, facts_rc: int = 0, known_refs: tuple[str, ...] = ()) -> None:
        self.facts_rc = facts_rc
        self.known_refs = known_refs
        self.written: dict[str, dict] = {}
        self.note_checks: list[list] = []

    def feed_script(self, spec, script_path: Path, args: list) -> subprocess.CompletedProcess:
        if script_path == notes.SCRIPT_PATH:
            self.note_checks.append(args)
            return subprocess.CompletedProcess([], 0, "\n".join(self.known_refs), "")
        if script_path == facts.SCRIPT_PATH:
            if self.facts_rc:
                return subprocess.CompletedProcess([], self.facts_rc, "", "squeue: not found")
            return subprocess.CompletedProcess([], 0, FACTS_OUTPUT, "")
        command, *rest = args
        if command in ("write", "update"):
            job_id, payload = rest
            self.written[job_id] = json.loads(base64.b64decode(payload))
            return subprocess.CompletedProcess([], 0, "", "")
        lines = "\n".join(json.dumps(record) for record in self.written.values())
        return subprocess.CompletedProcess([], 0, lines, "")


def _patch_run(monkeypatch, cluster: FakeCluster, output: str, rc: int = 0) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)
    monkeypatch.setattr("cluster_tunnel.ssh.run_capturing", lambda spec, tokens: (rc, output))
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)
    monkeypatch.setattr("cluster_tunnel.cmdlog.record", lambda name, tokens: None)
    monkeypatch.setattr("cluster_tunnel.cmdlog.record_jobs", lambda name, entry_id, job_ids: None)


def test_run_writes_a_record_with_the_facts(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Run ID: x\nSubmitted batch job 4822010\n")
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "run", "--project", "htfm", "--note", "lr sweep", "--", "./submit.sh"]
    )
    assert r.exit_code == 0, r.output
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
    config.write_text("clusters:\n  localhost:\n    host: localhost\n    job_records: false\n")
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--note", "x", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "no job record written" in r.output
    assert cluster.written == {}


def test_from_json_ignores_unknown_keys_and_fills_missing() -> None:
    record = records.JobRecord.from_json('{"job_id": 5, "future_field": [1], "project": "p"}')
    assert record.job_id == "5" and record.project == "p"
    assert record.note is None and record.code == {} and record.command == []


def test_submitted_job_ids_reads_every_submission() -> None:
    output = "Submitted batch job 1\nnoise Submitted batch job 9\nSubmitted batch job 2\n"
    assert records.submitted_job_ids(output) == ["1", "2"]


def test_jobs_json_attaches_records(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[Job("active", "101", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "train")])
    stored = [
        records.JobRecord.from_json('{"job_id": "101", "project": "htfm"}'),
        records.JobRecord.from_json('{"job_id": "99", "project": "htfm"}'),
    ]
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda spec, since, ids: stored)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "list", "--json"])
    entry = json.loads(r.output)["clusters"][0]
    assert entry["jobs"][0]["record"]["project"] == "htfm"
    assert [record["job_id"] for record in entry["records_without_job"]] == ["99"]
    assert entry["records_error"] is None


def test_run_capturing_streams_and_keeps_stdout(monkeypatch, capsysbinary) -> None:
    import sys

    from cluster_tunnel import ssh, wsl

    program = "import sys; print('Submitted batch job 5'); sys.exit(2)"
    monkeypatch.setattr(wsl, "launch", lambda argv, distro=None: [sys.executable, "-c", program])
    from pathlib import PurePosixPath

    spec = ssh.ConnSpec("c", "host", PurePosixPath("/tmp/socket"), "1h", 30, 3, None)
    returncode, output = ssh.run_capturing(spec, ["sbatch", "x"])
    assert returncode == 2
    assert output.strip() == "Submitted batch job 5"
    assert b"Submitted batch job 5" in capsysbinary.readouterr().out



# --- note references (D9) -------------------------------------------------------


def test_references_finds_markers_once() -> None:
    note = "rerun of job:1695485 after commit:541b4cb; see job:1695485 and commit:zz"
    assert notes.references(note) == ["job:1695485", "commit:541b4cb"]


def test_note_with_unknown_reference_is_refused_before_running(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    cluster.written["1"] = {"job_id": "1", "project": "htfm", "code": {"workdir": "/work/repo"}}
    cluster.written["2"] = {"job_id": "2", "project": "other", "code": {"workdir": "/work/other"}}
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    note ="after job:7 and commit:abcdef1"
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--project", "htfm", "--note", note, "--", "sbatch", "x"])
    assert r.exit_code == ExitCode.NOTE_REFERENCE_INVALID
    assert "commit:abcdef1" in r.output and "note_reference_invalid" in r.output
    assert "8" not in cluster.written  # nothing ran, so nothing was recorded
    assert cluster.note_checks == [["/work/repo", "--", "job:7", "commit:abcdef1"]]


def test_note_with_known_references_runs(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    r =CliRunner().invoke(cli, ["-t", "localhost", "run", "--note", "rerun of job:7", "--", "sbatch", "x"])
    assert r.exit_code == 0, r.output
    assert cluster.written["8"]["note"] == "rerun of job:7"
    assert cluster.note_checks == [["--", "job:7"]]


# --- editing a record (ctun job) -------------------------------------------------


def test_a_new_note_is_unconfirmed_and_logged() -> None:
    current = {"job_id": "7", "note": "old", "note_confirmed": True, "code": {"commit": "abc"}, "future": 1}
    record = records.edited(current, "7", project=None, note="new", confirm_note=False)
    assert record["note"] == "new" and record["note_confirmed"] is False
    assert record["code"] == {"commit": "abc"} and record["future"] == 1
    (change,) = record["changes"]
    assert change["note"] == ["old", "new"] and change["note_confirmed"] == [True, False]
    assert change["at"] and change["from"]


def test_a_missing_confirmation_counts_as_unconfirmed() -> None:
    record = records.edited({"job_id": "7", "note": "old"}, "7", project=None, note="new", confirm_note=False)
    (change,) = record["changes"]
    assert "note_confirmed" not in change and record["note_confirmed"] is False


def test_empty_values_clear_and_confirm_needs_a_note() -> None:
    current = {"job_id": "7", "project": "htfm", "note": "x"}
    record = records.edited(current, "7", project="", note="", confirm_note=False)
    assert record["project"] is None and record["note"] is None
    try:
        records.edited(record, "7", project=None, note=None, confirm_note=True)
    except ValueError as exc:
        assert "no note to confirm" in str(exc)
    else:
        raise AssertionError("confirming a missing note must fail")


def test_an_edit_that_changes_nothing_returns_none() -> None:
    current = {"job_id": "7", "project": "htfm", "note": "x", "note_confirmed": True}
    assert records.edited(current, "7", project="htfm", note="x", confirm_note=True) is None


def _patch_job(monkeypatch, cluster: FakeCluster) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)


def test_jobs_edit_prints_the_resulting_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "htfm"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "other"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == [cluster.written["7"]] and cluster.written["7"]["project"] == "other"


def test_job_edit_gives_an_old_job_a_record_without_facts(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "htfm/mh", "--note", "lr 3e-4", "--confirm-note"])
    assert r.exit_code == 0, r.output
    record = cluster.written["7"]
    assert record["project"] == "htfm/mh" and record["note"] == "lr 3e-4" and record["note_confirmed"] is True
    assert "submitted_at" not in record and len(record["changes"]) == 1


def test_job_edit_refuses_an_unknown_job(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "htfm"])
    assert r.exit_code != 0 and "unknown" in r.output
    assert cluster.written == {}


def test_job_edit_checks_the_note_references(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "htfm", "code": {"workdir": "/work/repo"}}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--note", "after job:5"])
    assert r.exit_code == ExitCode.NOTE_REFERENCE_INVALID
    assert "Not saved" in r.output and "note" not in cluster.written["7"]


def test_jobs_edit_without_a_change_is_a_usage_error(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7"])
    assert r.exit_code == 2 and "--project, --note or --confirm-note" in r.output


def test_plain_jobs_prints_the_group_help(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs"])
    assert "list" in r.output and "edit" in r.output and "Job ID" not in r.output


def test_jobs_edit_gives_several_jobs_the_same_change(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "old"}
    cluster.written["8"] = {"job_id": "8", "project": "new"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "8", "--project", "new"])
    assert r.exit_code == 0, r.output
    assert [record["project"] for record in json.loads(r.stdout)] == ["new", "new"]
    assert "updated job 7" in r.stderr and "job 8 already has these values" in r.stderr


def test_jobs_edit_stops_at_the_first_failing_job(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "999", "--project", "new"])
    assert r.exit_code != 0 and "999 is unknown" in r.output
    assert cluster.written["7"]["project"] == "new" and "999" not in cluster.written
