"""Budget guard logic (ssh.probe and session.load are mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cluster_tunnel import budget
from cluster_tunnel import config as cfg
from cluster_tunnel.ssh import ConnSpec


def _spec() -> ConnSpec:
    return ConnSpec("k", "k", Path("/tmp/k"), "12h", 60, 3, None)


def _config_with_budget(tmp_path: Path, *, fail_mode="closed", script="x.sh", limit=100):
    body = (
        "clusters:\n  k:\n    host: h\n    user: u\n    budget:\n"
        f"      script: {script}\n      session_limit: {limit}\n      unit: core-hours\n"
        f"      fail_mode: {fail_mode}\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return cfg.load_config(str(p)), p


def _dummy_script(tmp_path: Path) -> str:
    s = tmp_path / "x.sh"
    s.write_text("#!/bin/sh\n")
    return str(s)


def _patch(monkeypatch, *, sess, probe_stdout=None, probe_rc=0):
    monkeypatch.setattr("cluster_tunnel.session.load", lambda name: sess)
    if probe_stdout is not None:
        cp = subprocess.CompletedProcess([], probe_rc, probe_stdout, "")
        monkeypatch.setattr("cluster_tunnel.ssh.probe", lambda *a, **k: cp)


def test_first_token_and_guarded() -> None:
    assert budget.first_token_name(["/usr/bin/sbatch", "x"]) == "sbatch"
    assert budget.is_guarded(["sbatch"], ["sbatch", "srun"]) is True
    assert budget.is_guarded(["squeue"], ["sbatch"]) is False
    assert budget.is_guarded([], ["sbatch"]) is False


def test_is_guarded_robust() -> None:
    cmds = ["sbatch", "srun", "salloc", "aslurm", "aslurmx"]
    # path prefix + leading env assignments are ignored
    assert budget.is_guarded(["/opt/slurm/bin/sbatch", "j.sh"], cmds) is True
    assert budget.is_guarded(["FOO=1", "BAR=2", "aslurm", "x"], cmds) is True
    # both aslurm and aslurmx match; case-insensitive
    assert budget.is_guarded(["aslurmx"], cmds) is True
    assert budget.is_guarded(["ASLURM"], cmds) is True
    # a regex pattern entry works
    assert budget.is_guarded(["aslurm"], ["aslurmx?"]) is True
    assert budget.is_guarded(["aslurmx"], ["aslurmx?"]) is True
    # non-submission stays unguarded; no accidental substring match
    assert budget.is_guarded(["squeue"], cmds) is False
    assert budget.is_guarded(["sbatch_helper"], cmds) is False


def test_not_guarded(tmp_path: Path) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    d = budget.decide(c, "k", _spec(), ["squeue", "--me"], p)
    assert d.allowed and "not a guarded" in d.reason


def test_over_budget(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="150\n")
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and d.used == 150.0


def test_under_budget(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="5\n")
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and d.used == 5.0


def test_no_limit_allowed(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": None, "unit": "core-hours"})
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and "no session limit" in d.reason


def test_negative_limit_means_infinite(tmp_path: Path, monkeypatch) -> None:
    # A stored limit of -1 means "infinite" — submissions are always allowed,
    # and the budget script is never even probed.
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": -1.0, "unit": "core-hours"})
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and "no session limit" in d.reason


def test_fail_closed(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, fail_mode="closed", script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="", probe_rc=1)
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and "fail-closed" in d.reason


def test_fail_open(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, fail_mode="open", script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="", probe_rc=1)
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and "fail-open" in d.reason


def test_script_missing(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script="does_not_exist.sh")
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"})
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and "not found" in d.reason


def test_check_is_decides_verdict_for_any_command(tmp_path: Path, monkeypatch) -> None:
    # `check` judges the budget as it stands, without looking at a command:
    # what `decide` concludes for a guarded one, even for a command it doesn't guard.
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="150\n")
    d = budget.check(c, "k", _spec(), p)
    assert not d.allowed and d.used == 150.0
    assert d == budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert budget.decide(c, "k", _spec(), ["bash", "submit_all.sh"], p).allowed


def test_check_without_a_budget_section_allows(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("clusters:\n  k:\n    host: h\n", encoding="utf-8")
    d = budget.check(cfg.load_config(str(p)), "k", _spec(), p)
    assert d.allowed and d.reason == "no budget configured"


# --- guard_scope: every-command ---------------------------------------------------


GUARDS = ["sbatch", "srun", "salloc"]

#: (command, found by every-command, found by first-command)
SCOPE_CASES = [
    (["sbatch", "j.sh"], True, True),
    (["/opt/slurm/bin/sbatch", "j"], True, True),
    (["squeue", "--me"], False, False),
    (["grep", "sbatch", "slurm.log"], False, False),
    (["cat", "sbatch.log"], False, False),
    (["echo", "x; sbatch"], False, False),  # echo's argument is not run
    (["bash", "-c", "cd run && sbatch job.sh"], True, False),
    (["bash", "-lc", "module load x; srun ./a"], True, False),
    (["sh", "-c", "for f in *.sh; do sbatch $f; done"], True, False),
    (["sh", "-c", "echo $(sbatch --parsable j.sh)"], True, False),
    (["sh", "-c", 'echo "id: $(sbatch --parsable j.sh)"'], True, False),  # runs inside quotes
    (["sh", "-c", "echo `salloc -N1`"], True, False),
    (["sh", "-c", "squeue\nsbatch j.sh"], True, False),
    (["sh", "-c", "{ sbatch j.sh; }"], True, False),
    (["sh", "-c", "(cd x; srun y) > out.log 2>&1"], True, False),
    (["bash", "-c", "echo 'not; sbatch'"], False, False),  # quoted text, not run
    (["bash", "-c", "grep sbatch log | wc -l"], False, False),
    (["A=1", "sbatch", "j.sh"], True, True),  # a leading VAR= is skipped by both
    (["env", "A=1", "sbatch", "j.sh"], True, False),  # `env` is the first word
    (["env", "-i", "sbatch", "j.sh"], True, False),
    (["timeout", "60", "srun", "x"], True, False),
    (["nice", "-n", "5", "srun", "x"], True, False),
    (["nohup", "timeout", "60", "bash", "-c", "sbatch j"], True, False),
    (["xargs", "-n1", "sbatch"], True, False),
    (["find", ".", "-name", "*.sh", "-exec", "sbatch", "{}", ";"], True, False),
    (["eval", "sbatch j.sh"], True, False),
    (["bash", "submit_all.sh"], False, False),  # a script file is not looked into
    # Scripts are split the way a shell splits them.
    (["bash", "-c", "# go\nsbatch j.sh"], True, False),  # a comment ends at its line
    (["bash", "-c", "cd x # go\nsbatch j"], True, False),
    (["bash", "-c", "n=${#f[@]}; sbatch j"], True, False),  # not a comment
    (["bash", "-c", "[ $# -eq 0 ] && sbatch j"], True, False),
    (["bash", "-c", "module load x && \\\nsbatch j"], True, False),  # a continued line
    (["bash", "-c", "cat > j.sh <<'EOF'\n#SBATCH -N1\necho it's\nsrun ./a\nEOF\nsbatch j.sh"], True, False),
    (["bash", "-c", "cat > j.sh <<'EOF'\nsrun ./a\nEOF\nls"], False, False),  # a here-doc is data
    (["bash", "-c", 'echo "a $(sbatch x) b"; srun y'], True, False),
    (["bash", "-c", "function f { sbatch x; }; f"], True, False),
    (["bash", "-c", "f() { srun y; }; f"], True, False),
    (["bash", "-c", "echo x > sbatch"], False, False),  # a redirection's target, not a command
    (["bash", "-c", "sbatch x 2>&1 | tee log"], True, False),
    (["bash", "-c", "2>/dev/null sbatch x"], True, False),  # 2> is a redirection, not a command
    (["bash", "-c", "2>&1 sbatch x"], True, False),
    (["bash", "-c", "echo 2>err; ls 1>out"], False, False),
    (["bash", "-c", "echo $'it\\'s'; sbatch x"], True, False),  # $'...' takes escapes
    (["bash", "-c", "echo $'a; sbatch x'"], False, False),  # ...and is quoted text
    (["bash", "-c", "echo $(printf $'\\'') && srun y"], True, False),
    (["bash", "-c", "echo ${x:-$(sbatch j)}"], True, False),  # a substitution in ${...} runs
    (["bash", "-c", "echo ${x:-`sbatch j`}"], True, False),
    (["bash", "-c", "echo ${x:-sbatch}"], False, False),
    # Shells and wrappers are read the way they read their arguments.
    (["bash", "-c", "--", "sbatch x"], True, False),
    (["bash", "-c", "-e", "sbatch x"], True, False),
    (["bash", "-o", "pipefail", "-c", "sbatch x"], True, False),
    (["ssh", "login2", "cd x && sbatch j"], True, False),
    (["ssh", "-p", "2222", "login2", "squeue"], False, False),
    (["su", "-c", "sbatch x"], True, False),
    (["su", "-", "alice", "-c", "sbatch x"], True, False),  # `-` is a login shell, not the user
    (["su", "-", "alice"], False, False),
    (["flock", "/tmp/l", "-c", "sbatch x"], True, False),
    (["flock", "/tmp/l", "sbatch", "j"], True, False),
    (["watch", "squeue; sbatch x"], True, False),
    (["env", "-S", "sbatch x"], True, False),
    (["xargs", "-a", "files.txt", "sbatch"], True, False),
    (["parallel", "sbatch {}", ":::", "a.sh"], True, False),
    (["sudo", "-u", "bob", "srun", "x"], True, False),
    (["nohup", "timeout", "-k", "5", "60", "nice", "-n", "5", "srun", "x"], True, False),
    (["command", "-v", "sbatch"], False, False),  # only looks it up
    (["timeout", "30", "grep", "-rn", "sbatch", "."], False, False),  # grep's argument
    (["xargs"] * 9 + ["sbatch"], True, False),  # nested too deeply to follow: guarded
]


@pytest.mark.parametrize("tokens, every, first", SCOPE_CASES, ids=lambda v: " ".join(v) if isinstance(v, list) else None)
def test_guard_scope(tokens: list, every: bool, first: bool) -> None:
    assert budget.is_guarded(tokens, GUARDS, "every-command") is every
    assert budget.is_guarded(tokens, GUARDS, "first-command") is first
    assert budget.is_guarded(tokens, GUARDS) is first  # the default scope


def test_guard_scope_keeps_regex_patterns() -> None:
    assert budget.is_guarded(["bash", "-c", "ASLURMX job"], ["aslurmx?"], "every-command")


def test_guard_scope_stays_fast_on_pathological_commands() -> None:
    import time

    start = time.monotonic()
    assert budget.is_guarded(["xargs"] * 5000 + ["sbatch"], GUARDS, "every-command")
    nested_finds = ["find", "."] + ["-exec", "find", "."] * 30 + ["-exec", "true", ";"]
    assert budget.is_guarded(nested_finds, GUARDS, "every-command")  # too deep: guarded
    assert budget.is_guarded(["bash", "-c", "$(" * 300 + "sbatch"], GUARDS, "every-command")
    assert not budget.is_guarded(["bash", "-c", "x" * 200_000 + "; ls"], GUARDS, "every-command")
    assert time.monotonic() - start < 2


def test_decide_follows_the_clusters_guard_scope(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="150\n")
    hidden = ["bash", "-c", "cd run && sbatch j.sh"]
    assert budget.decide(c, "k", _spec(), hidden, p).allowed  # first-command: not seen
    c.clusters["k"].budget.guard_scope = "every-command"
    d = budget.decide(c, "k", _spec(), hidden, p)
    assert not d.allowed and d.used == 150.0


def test_guard_scope_values(tmp_path: Path) -> None:
    body = "clusters:\n  k:\n    host: h\n    budget:\n      guard_scope: {}\n"
    p = tmp_path / "config.yaml"
    p.write_text(body.format("every-command"), encoding="utf-8")
    assert cfg.load_config(str(p)).clusters["k"].budget.guard_scope == "every-command"
    p.write_text(body.format("anywhere"), encoding="utf-8")
    with pytest.raises(ValueError, match="guard_scope"):
        cfg.load_config(str(p))
    p.write_text("clusters:\n  k:\n    host: h\n    budget: {}\n", encoding="utf-8")
    assert cfg.load_config(str(p)).clusters["k"].budget.guard_scope == "first-command"
