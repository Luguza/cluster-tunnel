"""Session-scoped compute-budget guard.

Before a job-submitting command, run the cluster's budget script on the login
node (over the tunnel), compare the compute used since the session started to the
session limit, and hard-block if at/over. Threshold only — no per-job forecasting.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cluster_tunnel import config as config_mod
from cluster_tunnel import constants, session, ssh
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
    :func:`command_names`), and treats a command nested too deeply to follow
    as a submission.
    """
    if scope != "every-command":
        name = first_token_name(tokens)
        return bool(name) and _is_guard_command(name, guard_commands)
    return any(
        name == _TOO_DEEP or _is_guard_command(name, guard_commands)
        for name in command_names(tokens)
        if name
    )


def _is_guard_command(name: str, guard_commands: list[str]) -> bool:
    for pattern in guard_commands:
        try:
            if re.fullmatch(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            if pattern == name:  # invalid regex -> literal match
                return True
    return False


#: Stands for a command nested too deeply to follow; :func:`is_guarded` guards it.
_TOO_DEEP = "\x00too-deep"
#: Bounds the search through nested shells, wrappers and substitutions.
_MAX_DEPTH = 8
#: Shells whose ``-c`` argument is a script.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "csh", "tcsh"})
#: Shell options whose value is the next word (``bash -o pipefail -c ...``).
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
#: Shell words that can come before a command.
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "coproc", "function"})
#: Commands that run a command given in their arguments: the options whose
#: value is the next word, and how many plain words (a duration, a lock file, a
#: CPU mask, a host, a user) come before that command.
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "builtin": (frozenset(), 0),
    "chronic": (frozenset(), 0),
    "command": (frozenset(), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir"}), 0),
    "exec": (frozenset({"-a"}), 0),
    "flock": (frozenset({"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}), 1),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}), 0),
    "ltrace": (frozenset({"-a", "-e", "-l", "-n", "-o", "-p", "-s", "-u"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "nohup": (frozenset(), 0),
    "numactl": (frozenset({"-C", "--cpunodebind", "-N", "-m", "--membind", "-i", "--interleave",
                           "-p", "--preferred", "--physcpubind"}), 0),
    "parallel": (frozenset({"-j", "--jobs", "-N", "-n", "-S", "--sshlogin", "-a", "--arg-file", "-d",
                            "--delimiter", "--joblog", "--results", "-I", "--colsep", "-C",
                            "--timeout", "--tmpdir", "--wd"}), 0),
    "script": (frozenset({"-E", "--echo", "-I", "--log-in", "-O", "--log-out", "-B", "--log-io",
                          "-T", "--log-timing", "-m", "--logging-format"}), 1),
    "setsid": (frozenset(), 0),
    "ssh": (frozenset({"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m",
                       "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"}), 1),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "strace": (frozenset({"-a", "-b", "-e", "-E", "-I", "-O", "-o", "-P", "-p", "-S", "-s", "-u", "-X"}), 0),
    "su": (frozenset({"-g", "--group", "-G", "--supp-group", "-s", "--shell", "-w",
                      "--whitelist-environment"}), 1),
    "sudo": (frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir",
                        "-h", "--host", "-p", "--prompt", "-r", "--role", "-t", "--type", "-T",
                        "--command-timeout", "-U", "--other-user"}), 0),
    "taskset": (frozenset(), 1),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "unbuffer": (frozenset(), 0),
    "watch": (frozenset({"-n", "--interval"}), 0),
    "xargs": (frozenset({"-a", "--arg-file", "-d", "--delimiter", "-E", "--eof", "-I", "--replace",
                         "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs", "-s",
                         "--max-chars", "--process-slot-var"}), 0),
}
#: Wrapper options whose value is a command line for a shell (``su -c '...'``).
_SCRIPT_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-S", "--split-string"}),
    "flock": frozenset({"-c", "--command"}),
    "script": frozenset({"-c", "--command"}),
    "su": frozenset({"-c", "--command"}),
}
#: Wrappers that hand the rest of their words to a shell as one command line.
_JOINED = frozenset({"ssh", "watch", "parallel"})


def command_names(tokens: list[str]) -> list[str]:
    """The bare names of every command ``tokens`` may run, as far as its words tell.

    That is the first word (after ``VAR=value`` assignments); the command a
    wrapper such as ``env``, ``timeout``, ``xargs``, ``sudo`` or ``ssh host``
    runs, after its options and leading arguments; the command after
    ``find -exec``; and every command in a script handed to ``bash -c``,
    ``sh -c``, ``eval``, ``su -c``, ``env -S`` or ``watch``, including the
    code in ``$(...)`` and backticks, even quoted. Scripts are split the way a
    shell splits them (see :func:`_lex`). A script file (``bash submit.sh``),
    a program's own code and commands fed on stdin are not looked into.
    """
    return list(_simple_command(list(tokens), 0))


def _simple_command(words: list[str], depth: int):
    """The command names of one simple command (no separators in ``words``)."""
    if depth > _MAX_DEPTH:
        yield _TOO_DEEP
        return
    i = 0
    while i < len(words) and (_ENV_ASSIGN.fullmatch(words[i]) or words[i] in _KEYWORDS):
        i += 1
    if i < len(words):
        yield from _command_at(words, i, depth)


def _command_at(words: list[str], i: int, depth: int):
    """The command named by ``words[i]``, and what it runs in turn."""
    name = os.path.basename(words[i])
    yield name
    lowered = name.lower()
    if lowered in _SHELLS:
        yield from _shell(words, i, depth)
    elif lowered == "eval":
        yield from _script(" ".join(words[i + 1:]), depth + 1)
    elif lowered == "find":
        j = i + 1
        while j < len(words):
            if words[j] in ("-exec", "-execdir", "-ok", "-okdir"):
                end = j + 1
                while end < len(words) and words[end] not in (";", "+"):
                    end += 1
                yield from _simple_command(words[j + 1:end], depth + 1)
                j = end
            j += 1
    elif lowered in _WRAPPERS:
        yield from _wrapped(words, i, depth)


def _shell(words: list[str], i: int, depth: int):
    """The script a shell runs with ``-c``: its first plain word after the options."""
    has_c, j = False, i + 1
    while j < len(words):
        word = words[j]
        if word == "--":
            j += 1
            break
        if word in _SHELL_VALUE_OPTIONS:
            j += 2
            continue
        if len(word) > 1 and word[0] in "-+":
            if not word.startswith("--") and "c" in word[1:]:
                has_c = True  # -c, or a cluster such as -lc / -ec
            j += 1
            continue
        break
    if has_c and j < len(words):
        yield from _script(words[j], depth + 1)


def _wrapped(words: list[str], i: int, depth: int):
    """What a wrapper runs: the command after its options and leading plain words,
    or the command line in a script option (``su -c '...'``)."""
    name = os.path.basename(words[i]).lower()
    values, plain = _WRAPPERS[name]
    scripts = _SCRIPT_OPTIONS.get(name, frozenset())
    j, options = i + 1, True
    while j < len(words):
        word = words[j]
        if options and word == "--":
            options = False
        elif options and len(word) > 1 and word.startswith("-"):
            option, eq, value = word.partition("=")
            if option in scripts:
                yield from _script(value if eq else (words[j + 1] if j + 1 < len(words) else ""), depth + 1)
                return
            if name == "command" and option in ("-v", "-V"):
                return  # it only looks the command up
            if option in values and not eq:
                j += 1  # the option's value is the next word
        elif name == "env" and _ENV_ASSIGN.fullmatch(word):
            pass
        elif options and name == "su" and word == "-":
            pass  # a login shell (`su - user`), not the user
        elif plain:
            plain -= 1
        else:
            break
        j += 1
    if j < len(words):
        if name in _JOINED:
            yield from _script(" ".join(words[j:]), depth + 1)
        else:
            yield from _simple_command(words[j:], depth + 1)


def _script(code: str, depth: int):
    """The command names in shell code, its substitutions included."""
    if depth > _MAX_DEPTH:
        yield _TOO_DEEP
        return
    commands, substitutions = _lex(code)
    for words in commands:
        yield from _simple_command(words, depth)
    for inner in substitutions:
        yield from _script(inner, depth + 1)


#: Characters that end a command outside quotes.
_SEPARATORS = frozenset(";&|(){}\n")


def _lex(code: str) -> tuple[list[list[str]], list[str]]:
    """Split shell code into the words of each command, as a POSIX shell would.

    Commands end at ``;``, ``&``, ``|``, parentheses, braces and newlines outside
    quotes. A ``#`` that starts a word comments out the rest of its line, a
    backslash-newline joins two lines, ``${...}`` stays one word, ``$'...'``
    takes backslash escapes, a redirection (``> out``, ``2>&1``) is no word of
    the command, and a here-document's lines are skipped. The code of every
    ``$(...)`` and backtick substitution, quoted or not, comes back separately,
    and so does a ``${...}`` that holds one. An unclosed quote or bracket runs
    to the end.
    """
    commands: list[list[str]] = [[]]
    substitutions: list[str] = []
    heredocs: list[tuple[str, bool]] = []  # (delimiter, leading tabs stripped)
    word: list[str] = []
    started = False  # a word is open (it may still be empty, as in '')
    target = ""  # the open word: "" a command's, ">" a redirection target, "<<"/"<<-" a here-doc delimiter
    quote = ""
    i, n = 0, len(code)

    def finish() -> None:
        nonlocal word, started, target
        if started:
            text = "".join(word)
            if not target:
                commands[-1].append(text)
            elif target != ">":
                heredocs.append((text, target == "<<-"))
        word, started, target = [], False, ""

    while i < n:
        c = code[i]
        if quote == "'":
            if c == "'":
                quote = ""
            else:
                word.append(c)
            i += 1
            continue
        if quote == "$'":
            if c == "'":
                quote = ""
            else:
                if c == "\\" and i + 1 < n:
                    i += 1  # $'it\'s': an escaped quote doesn't end it
                word.append(code[i])
            i += 1
            continue
        if c == "`" or (c == "$" and code.startswith("(", i + 1)):
            start = i + 1 if c == "`" else i + 2
            end = code.find("`", start) if c == "`" else _closing(code, start, "(", ")")
            end = n if end < 0 else end
            substitutions.append(code[start:end])
            word.append("$")  # keeps `x=$(...)` an assignment
            started = True
            i = end + 1
            continue
        if quote == '"':
            if c == '"':
                quote = ""
            elif c == "\\" and i + 1 < n:
                i += 1
                word.append(code[i])
            else:
                word.append(c)
            i += 1
            continue
        if c == "\\":
            if i + 1 < n and code[i + 1] != "\n":
                word.append(code[i + 1])
                started = True
            i += 2
            continue
        if c in "'\"":
            quote, started = c, True
        elif c == "$" and code.startswith("'", i + 1):
            quote, started = "$'", True
            i += 2
            continue
        elif c == "$" and code.startswith("{", i + 1):
            end = _closing(code, i + 2, "{", "}")
            body = code[i + 2:end]
            if "$(" in body or "`" in body:
                substitutions.append(body)  # ${x:-$(sbatch j)} runs it
            word.append(code[i:end + 1])
            started = True
            i = end + 1
            continue
        elif c == "#" and not started:
            end = code.find("\n", i)
            i = n if end < 0 else end
            continue
        elif c in " \t\r":
            finish()
        elif c in "<>" or (c == "&" and code.startswith(">", i + 1)):
            if not target and word and code[i - 1].isdigit() and "".join(word).isdigit():
                word, started = [], False  # a file descriptor (2>, 1>&2), not a word
            finish()
            start = i
            while i < n and code[i] in "<>&|-":
                i += 1
            op = code[start:i]
            target = op[:3] if op.startswith("<<-") else op[:2] if op.startswith("<<") and op != "<<<" else ">"
            while i < n and code[i] in " \t":
                i += 1
            continue
        elif c in _SEPARATORS:
            finish()
            if commands[-1]:
                commands.append([])
            if c == "\n" and heredocs:
                i = _skip_heredocs(code, i + 1, heredocs)
                heredocs.clear()
                continue
        else:
            word.append(c)
            started = True
        i += 1
    finish()
    return [words for words in commands if words], substitutions


def _closing(code: str, start: int, opening: str, closing: str) -> int:
    """Index of the ``closing`` bracket that ends one opened just before ``start``."""
    depth, quote, i = 1, "", start
    while i < len(code):
        c = code[i]
        if quote:
            if c == quote[-1]:
                quote = ""
            elif c == "\\" and quote != "'":
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "$" and code.startswith("'", i + 1):
            quote = "$'"
            i += 1
        elif c == "\\":
            i += 1
        elif c == opening:
            depth += 1
        elif c == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(code)


def _skip_heredocs(code: str, start: int, heredocs: list[tuple[str, bool]]) -> int:
    """Where the code goes on after the here-documents that begin at ``start``."""
    pos = start
    for delimiter, tabs in heredocs:
        while pos < len(code):
            end = code.find("\n", pos)
            end = len(code) if end < 0 else end
            line = code[pos:end].rstrip("\r")
            pos = end + 1
            if (line.lstrip("\t") if tabs else line) == delimiter:
                break
    return pos


def remote_user(spec: ConnSpec, cluster: Cluster) -> str:
    """The cluster username: from config, else queried over the tunnel."""
    if cluster.user:
        return cluster.user
    res = ssh.capture(spec, ["id", "-un"], timeout=constants.PROBE_TIMEOUT)
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
