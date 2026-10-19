"""Session-scoped compute-budget guard.

Before a job-submitting command, run the cluster's budget script on the login
node (over the tunnel), compare the compute used since the session started to the
session limit, and hard-block if at/over. Threshold only — no per-job forecasting.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cluster_tunnel import config as config_mod
from cluster_tunnel import session, ssh
from cluster_tunnel.config import Cluster, Config
from cluster_tunnel.ssh import ConnSpec


@dataclass
class Decision:
    allowed: bool
    reason: str
    used: Optional[float] = None
    limit: Optional[float] = None
    unit: str = "units"


_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def first_command_token(tokens: list[str]) -> str:
    """The first real command token, skipping leading ``VAR=value`` env assignments.

    ``ctun run -- FOO=1 sbatch job.sh`` should still be recognised as an ``sbatch``
    submission, so leading shell-style environment assignments are ignored.
    """
    for tok in tokens:
        if _ENV_ASSIGN.fullmatch(tok):
            continue
        return tok
    return ""


def first_token_name(tokens: list[str]) -> str:
    """The bare command name of the first real token (basename, no path)."""
    return os.path.basename(first_command_token(tokens))


def is_guarded(tokens: list[str], guard_commands: list[str], scope: str = "first-command") -> bool:
    """True if the command is a job submission subject to the budget guard.

    Each entry in ``guard_commands`` is treated as a **regex** matched (fullmatch)
    against the command's bare name, so robust patterns like ``aslurmx?`` or
    ``s(batch|run|alloc)`` work. An entry that isn't valid regex falls back to a
    literal comparison, so plain names like ``sbatch`` keep working. Matching is
    case-insensitive and ignores any path prefix or leading env assignments.

    The ``scope`` ``"first-command"`` looks at the first word only;
    ``"every-command"`` at every word where a command may start (see
    :func:`command_names`).
    """
    names = command_names(tokens) if scope == "every-command" else [first_token_name(tokens)]
    return any(_is_guard_command(name, guard_commands) for name in names if name)


def _is_guard_command(name: str, guard_commands: list[str]) -> bool:
    for pattern in guard_commands:
        try:
            if re.fullmatch(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            if pattern == name:  # invalid regex -> literal match
                return True
    return False


#: Commands that run the command given in their arguments: after one of these,
#: every word of the same command may be the one that runs (``timeout 60 sbatch``,
#: ``xargs -n1 sbatch``, ``nice -n 5 srun``).
_WRAPPERS = frozenset({
    "env", "nohup", "nice", "ionice", "time", "timeout", "exec", "command", "builtin",
    "setsid", "stdbuf", "xargs", "flock", "sudo", "doas", "watch", "unbuffer", "chronic",
    "taskset", "numactl", "strace", "ltrace", "parallel", "ssh", "su", "script",
})
#: Shells whose ``-c`` argument is a script to search.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "csh", "tcsh"})
#: Shell words that can precede a command in a script.
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "coproc"})
#: Command substitutions, which run even inside double quotes.
_SUBSTITUTION = re.compile(r"\$\(|`")
#: Bounds the search through nested shells and wrappers.
_MAX_DEPTH = 8


def command_names(tokens: list[str]) -> list[str]:
    """The bare names of every command ``tokens`` may run, as far as its words tell.

    That is the first word (after ``VAR=value`` assignments), every word after a
    wrapper such as ``env``, ``timeout`` or ``xargs``, the command after
    ``find -exec``, and the commands in a script handed to ``bash -c``,
    ``sh -c`` or ``eval``: split where a command can start (``;``, ``&&``,
    ``||``, ``|``, ``&``, parentheses, newlines; quotes respected) and inside
    every ``$(...)`` or backtick substitution, even a quoted one. Where in doubt
    it finds too many rather than too few. A script file run as
    ``bash submit.sh`` is not looked into.
    """
    return list(_simple_command(list(tokens), 0))


def _simple_command(words: list[str], depth: int):
    """The command names of one simple command (no separators in ``words``)."""
    i = 0
    while i < len(words) and (_ENV_ASSIGN.fullmatch(words[i]) or words[i] in _KEYWORDS):
        i += 1
    if i < len(words) and depth <= _MAX_DEPTH:
        yield from _command_at(words, i, depth)


def _command_at(words: list[str], i: int, depth: int):
    """The command named by ``words[i]``, and what it runs in turn."""
    name = os.path.basename(words[i])
    yield name
    lowered = name.lower()
    if lowered in _SHELLS:
        for j in range(i + 1, len(words) - 1):
            # -c, or combined short options such as -lc / -ec
            if words[j].startswith("-") and not words[j].startswith("--") and "c" in words[j][1:]:
                yield from _script(words[j + 1], depth + 1)
                break
    elif lowered == "eval":
        yield from _script(" ".join(words[i + 1:]), depth + 1)
    elif lowered == "find":
        for j in range(i + 1, len(words)):
            if words[j] in ("-exec", "-execdir", "-ok", "-okdir"):
                yield from _simple_command(words[j + 1:], depth + 1)
    elif lowered in _WRAPPERS:
        # Any later word may be the command that runs; one pass, so nested
        # wrappers (`nohup timeout 60 sbatch`) add no work.
        for j in range(i + 1, len(words)):
            if words[j].startswith("-") or _ENV_ASSIGN.fullmatch(words[j]):
                continue
            inner = os.path.basename(words[j]).lower()
            if inner in _SHELLS or inner in ("eval", "find"):
                yield from _command_at(words, j, depth + 1)
            else:
                yield os.path.basename(words[j])


def _script(text: str, depth: int):
    """The command names in shell code."""
    for piece in _SUBSTITUTION.split(text):
        for words in _commands(piece.replace("\n", ";")):
            yield from _simple_command(words, depth)


def _commands(code: str) -> list[list[str]]:
    """The words of each command in ``code``, split at the shell's operators.

    Quotes are respected. Where a substitution split cut a quote in two, the
    words are split at operators and spaces regardless, quote marks dropped.
    """
    lexer = shlex.shlex(code, posix=True, punctuation_chars=True)
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = [w.strip("'\"") for w in re.split(r"([;&|()<>]+|\s+)", code) if w.strip()]
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|()<>"):
            commands.append([])
        else:
            commands[-1].append(token)
    return [words for words in commands if words]


def remote_user(spec: ConnSpec, cluster: Cluster) -> str:
    """The cluster username: from config, else queried over the tunnel."""
    if cluster.user:
        return cluster.user
    res = ssh.capture(spec, ["id", "-un"])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "could not determine remote user")
    return res.stdout.strip()


def used_since(spec: ConnSpec, script_path: Path, start_epoch: int, user: str) -> float:
    """Run the budget script remotely and parse the single number it prints."""
    res = ssh.probe(spec, script_path, start_epoch, user)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"probe exited {res.returncode}")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("budget script produced no output")
    try:
        return float(lines[-1].split()[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"could not parse budget number from: {res.stdout!r}") from exc


def _failure(cluster: Cluster, msg: str, limit: Optional[float], unit: str) -> Decision:
    """Apply fail_mode when the probe can't produce a number."""
    fail_mode = cluster.budget.fail_mode if cluster.budget else "closed"
    if fail_mode == "open":
        return Decision(True, f"{msg} (fail-open: allowed)", None, limit, unit)
    return Decision(False, f"{msg} (fail-closed: blocked)", None, limit, unit)


def decide(
    config: Config, name: str, spec: ConnSpec, tokens: list[str], config_path: Path
) -> Decision:
    """Decide whether `tokens` may run on cluster `name` under the budget guard."""
    cluster = config_mod.get_cluster(config, name)
    budget = cluster.budget

    if budget is None or not is_guarded(tokens, budget.guard_commands, budget.guard_scope):
        return Decision(True, "not a guarded command")
    return check(config, name, spec, config_path)


def check(config: Config, name: str, spec: ConnSpec, config_path: Path) -> Decision:
    """The session budget as it stands: may a job be submitted to `name` now?

    What :func:`decide` concludes for a guarded command, without looking at a
    command: probe the usage since the session started and compare it to the
    session limit. A cluster without a ``budget`` section is never blocked.
    """
    cluster = config_mod.get_cluster(config, name)
    budget = cluster.budget
    if budget is None:
        return Decision(True, "no budget configured")

    sess = session.load(name)
    limit = sess.get("limit") if sess else None
    unit = (sess.get("unit") if sess else None) or budget.unit

    if sess is None:
        return _failure(cluster, "no active session", limit, unit)
    # A null or negative limit (e.g. -1) means "infinite" — no budget guard.
    if limit is None or limit < 0:
        return Decision(True, "no session limit set", unit=unit)

    script = config_mod.budget_script_path(config_path, name, budget.script)
    if not script.exists():
        return _failure(cluster, f"budget script not found: {script}", limit, unit)

    try:
        user = remote_user(spec, cluster)
        used = used_since(spec, script, int(sess["start_epoch"]), user)
    except Exception as exc:  # noqa: BLE001
        return _failure(cluster, f"budget probe failed: {exc}", limit, unit)

    if used >= limit:
        return Decision(
            False, f"session budget exhausted: {used} >= {limit} {unit}", used, limit, unit
        )
    return Decision(True, f"within budget: {used} < {limit} {unit}", used, limit, unit)
