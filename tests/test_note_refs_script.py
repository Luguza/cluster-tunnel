"""The bundled note_refs.sh runs for real against fake Slurm tools and a real git repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.test_records_scripts import SCRIPTS, _git

_FAKE_TOOLS = {
    "scontrol": "#!/bin/sh\nprintf 'ClusterName             = haic\\n'\n",
    # squeue knows job 7 only; sacct knows job 8 only.
    "squeue": '#!/bin/sh\ncase "$*" in *" 7 "*) echo 7 ;; esac\n',
    "sacct": '#!/bin/sh\ncase "$*" in *" 8 "*) echo 8 ;; esac\n',
}


def _posix_path(bash: str, path: Path) -> str:
    return subprocess.run(
        [bash, "-c", f"cd '{path.as_posix()}' && pwd"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def test_finds_jobs_and_commits_and_omits_the_rest(tmp_path: Path, bash: str) -> None:
    binp = tmp_path / "bin"
    binp.mkdir()
    for name, body in _FAKE_TOOLS.items():
        (binp / name).write_text(body, encoding="utf-8", newline="\n")
        (binp / name).chmod(0o755)
    records = tmp_path / "home" / ".ctun" / "jobs" / "haic"
    records.mkdir(parents=True)
    (records / "9.json").write_text("{}", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()

    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "home"))
    refs = ["job:7", "job:8", "job:9", "job:10", f"commit:{commit}", "commit:deadbee"]
    result = subprocess.run(
        [bash, str(SCRIPTS / "note_refs.sh"), _posix_path(bash, repo), "--", *refs],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["job:7", "job:8", "job:9", f"commit:{commit}"]
