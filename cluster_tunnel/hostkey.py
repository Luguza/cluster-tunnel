"""Host-key pinning: the key a cluster's server presents, and the key pinned for it.

A tunnel's master accepts an unknown host key on first contact
(``StrictHostKeyChecking=accept-new``) unless its cluster is pinned:
``strict_host_key_checking: yes`` with a ``known_hosts_file`` that holds the key
a person confirmed. That file is read and written where ssh runs (inside WSL on
Windows), by one POSIX sh helper fed on stdin, so nothing here depends on which
side of WSL ctun itself runs on.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass

from cluster_tunnel import paths, ssh, wsl
from cluster_tunnel.ssh import ConnSpec

#: Seconds to wait for the helper; covers a cold WSL start and a slow server.
TIMEOUT = 90

# Operations (the first argument):
#   scan <target> <ssh option>...  fetch the target's host key into a throwaway
#       known_hosts file; print its lines, a "--" line, then their fingerprints.
#   pinned <file> <host>  the same for the lines of <file> that name <host>.
#   pin <file> <host> <line>...  replace <file>'s lines for <host> with <line>s.
# A leading "~/" in <file> means $HOME, as in the config. Child commands read
# /dev/null, not this script on stdin.
_HELPER = r"""
expand() { case $1 in "~"/*) printf '%s\n' "$HOME/${1#??}" ;; *) printf '%s\n' "$1" ;; esac; }
show() { cat "$1" && echo -- && ssh-keygen -l -f "$1" </dev/null; }
op=$1; shift
case $op in
scan)
    target=$1; shift
    d=$(mktemp -d) || exit 1
    trap 'rm -rf "$d"' EXIT
    ssh "$@" -o "UserKnownHostsFile=\"$d/known_hosts\"" "$target" true </dev/null >/dev/null 2>"$d/err"
    rc=$?
    if [ -s "$d/known_hosts" ]; then show "$d/known_hosts"; exit; fi
    cat "$d/err" >&2
    [ "$rc" -ne 0 ] && exit "$rc"
    echo "the server sent no host key" >&2
    exit 1
    ;;
pinned)
    f=$(expand "$1")
    [ -f "$f" ] || exit 0
    d=$(mktemp -d) || exit 1
    trap 'rm -rf "$d"' EXIT
    awk -v h="$2" '$1 == h' "$f" > "$d/known_hosts" || exit 1
    [ -s "$d/known_hosts" ] || exit 0
    show "$d/known_hosts"
    ;;
pin)
    f=$(expand "$1"); h=$2; shift 2
    mkdir -p "$(dirname "$f")" || exit 1
    t="$f.tmp.$$"
    trap 'rm -f "$t"' EXIT
    if [ -f "$f" ]; then awk -v h="$h" '$1 != h' "$f" > "$t" || exit 1; else : > "$t"; fi
    printf '%s\n' "$@" >> "$t" && mv "$t" "$f"
    ;;
*)
    echo "unknown operation: $op" >&2
    exit 2
    ;;
esac
"""


@dataclass
class HostKey:
    """One host key: its known_hosts line and its fingerprint."""

    host: str  # the name ssh looks the key up by: the host, [host]:port, or a HostKeyAlias
    kind: str  # ED25519, ECDSA, RSA, ...
    bits: int
    fingerprint: str  # SHA256:...
    line: str  # the known_hosts line, "<host> <key type> <base64 key>"

    def as_dict(self) -> dict:
        return asdict(self)


def default_file() -> str:
    """Where pinned keys go unless a cluster names its own file (a WSL path on Windows)."""
    return wsl.DEFAULT_KNOWN_HOSTS if wsl.enabled() else str(paths.known_hosts_file())


def scan(spec: ConnSpec) -> list[HostKey]:
    """The host key ``spec``'s server presents now, fetched without logging in.

    Raises :class:`RuntimeError` with ssh's own words when the server can't be
    reached or sends no key.
    """
    keys = parse(_checked(spec, ["scan", spec.target, *ssh.scan_opts(spec)]))
    if not keys:
        raise RuntimeError("the server sent no host key")
    return keys


def pinned(spec: ConnSpec, file: str, host: str) -> list[HostKey]:
    """The keys pinned for ``host`` in ``file``; empty if there are none (or no file)."""
    return parse(_checked(spec, ["pinned", file, host]))


def pin(spec: ConnSpec, file: str, keys: list[HostKey]) -> None:
    """Make ``keys`` the only keys ``file`` holds for their host."""
    hosts = {key.host for key in keys}
    if len(hosts) != 1:
        raise ValueError(f"pin the keys of one host at a time, not {sorted(hosts)}")
    _checked(spec, ["pin", file, hosts.pop(), *(key.line for key in keys)])


def parse(output: str) -> list[HostKey]:
    """Pair the helper's known_hosts lines with the fingerprints after its ``--`` line.

    ``ssh-keygen -l`` prints one line per key, in file order, e.g.
    ``256 SHA256:nThb... uc3.scc.kit.edu (ED25519)``.
    """
    if not output.strip():
        return []
    head, sep, tail = output.partition("\n--\n")
    if not sep:
        raise RuntimeError(f"unexpected output from the host-key helper: {output!r}")
    lines = [ln for ln in head.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    prints = [ln for ln in tail.splitlines() if ln.strip()]
    if len(lines) != len(prints):
        raise RuntimeError("could not match the fingerprints to the host keys")
    keys = []
    for line, printed in zip(lines, prints):
        bits, fingerprint = printed.split()[:2]
        kind = printed.rsplit("(", 1)[-1].rstrip(")") if printed.endswith(")") else "?"
        keys.append(HostKey(line.split()[0], kind, int(bits), fingerprint, line))
    return keys


def _checked(spec: ConnSpec, args: list[str]) -> str:
    """Run the helper with ``args`` where ssh runs; return its stdout or raise."""
    res = _run_helper(spec, args)
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"exit code {res.returncode}"
        raise RuntimeError(detail)
    return res.stdout


def _run_helper(spec: ConnSpec, args: list[str]) -> subprocess.CompletedProcess:
    """The helper's output as text. The script goes in as bytes: a text pipe on
    Windows turns ``\\n`` into ``\\r\\n``, which breaks it on the Linux side."""
    argv = wsl.launch(["sh", "-s", "--", *args], spec.wsl_distro)
    try:
        res = subprocess.run(
            argv, input=_HELPER.encode("utf-8"), capture_output=True, timeout=TIMEOUT
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc.strerror or exc}\n")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"no answer after {TIMEOUT}s\n")
    return subprocess.CompletedProcess(
        argv, res.returncode, res.stdout.decode("utf-8", "replace"), res.stderr.decode("utf-8", "replace")
    )
