"""Host-key pinning: the sh helper (run for real) and how its output is read.

The helper tests run ``sh``, ``awk`` and ``ssh-keygen`` for real, with a fake
``ssh`` that records a generated key the way a server's key exchange would.
``CTUN_TEST_SSHD=<host>`` adds a scan of a real SSH server (no login needed).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from cluster_tunnel import hostkey, paths, wsl
from cluster_tunnel.ssh import ConnSpec

needs_tools = pytest.mark.skipif(
    sys.platform == "win32" or not all(shutil.which(t) for t in ("sh", "awk", "ssh-keygen")),
    reason="runs the POSIX sh helper with awk and ssh-keygen",
)


def _spec(target: str = "fakehost") -> ConnSpec:
    return ConnSpec("k", target, Path("sock"), "12h", 60, 3, None)


def _key(tmp_path: Path, host: str, name: str) -> tuple[str, str]:
    """A fresh ed25519 key: its known_hosts line for `host`, and its fingerprint."""
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    kind, data = Path(f"{key}.pub").read_text(encoding="utf-8").split()[:2]
    printed = subprocess.run(
        ["ssh-keygen", "-l", "-f", f"{key}.pub"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return f"{host} {kind} {data}", printed.split()[1]


def _fake_ssh(tmp_path: Path, monkeypatch, *, line: str | None, stderr: str) -> None:
    """Put an ``ssh`` on PATH that writes `line` to its UserKnownHostsFile, then fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ssh"
    write = f"printf '%s\\n' '{line}' > \"$f\"\n" if line else ""
    script.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "  case $arg in UserKnownHostsFile=*) f=${arg#UserKnownHostsFile=}; f=${f#\\\"}; f=${f%\\\"} ;; esac\n"
        "done\n"
        f"{write}"
        f"echo '{stderr}' >&2\n"
        "exit 255\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


# --- the helper, for real ---------------------------------------------------------


@needs_tools
def test_scan_returns_the_key_the_server_offered(tmp_path: Path, monkeypatch) -> None:
    line, fingerprint = _key(tmp_path, "fakehost", "server")
    _fake_ssh(tmp_path, monkeypatch, line=line, stderr="Permission denied (publickey).")
    [key] = hostkey.scan(_spec())
    assert (key.host, key.kind, key.bits, key.fingerprint, key.line) == (
        "fakehost", "ED25519", 256, fingerprint, line,
    )


@needs_tools
def test_scan_reports_what_ssh_said_when_no_key_came(tmp_path: Path, monkeypatch) -> None:
    _fake_ssh(tmp_path, monkeypatch, line=None, stderr="ssh: connect to host fakehost port 22: Connection refused")
    with pytest.raises(RuntimeError, match="Connection refused"):
        hostkey.scan(_spec())


@needs_tools
def test_pin_replaces_only_the_hosts_own_lines(tmp_path: Path) -> None:
    file = str(tmp_path / "keys" / "known_hosts")  # its directory is created
    old, _ = _key(tmp_path, "bw", "old")
    other, other_fp = _key(tmp_path, "horeka", "other")
    new, new_fp = _key(tmp_path, "bw", "new")
    spec = _spec()

    hostkey.pin(spec, file, hostkey.parse(f"{old}\n--\n256 SHA256:x bw (ED25519)\n"))
    hostkey.pin(spec, file, hostkey.parse(f"{other}\n--\n256 {other_fp} horeka (ED25519)\n"))
    hostkey.pin(spec, file, hostkey.parse(f"{new}\n--\n256 {new_fp} bw (ED25519)\n"))

    assert Path(file).read_text(encoding="utf-8") == f"{other}\n{new}\n"
    assert [k.fingerprint for k in hostkey.pinned(spec, file, "bw")] == [new_fp]
    assert [k.fingerprint for k in hostkey.pinned(spec, file, "horeka")] == [other_fp]
    assert hostkey.pinned(spec, file, "nobody") == []
    assert hostkey.pinned(spec, str(tmp_path / "missing"), "bw") == []
    assert sorted(p.name for p in Path(file).parent.iterdir()) == ["known_hosts"]  # no temp left


@needs_tools
def test_a_leading_tilde_means_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    line, fingerprint = _key(tmp_path, "bw", "server")
    hostkey.pin(_spec(), "~/.config/ctun/known_hosts", hostkey.parse(f"{line}\n--\n256 {fingerprint} bw (ED25519)\n"))
    assert (tmp_path / ".config" / "ctun" / "known_hosts").read_text(encoding="utf-8") == f"{line}\n"
    assert hostkey.pinned(_spec(), "~/.config/ctun/known_hosts", "bw")[0].fingerprint == fingerprint


def test_pin_takes_one_host_at_a_time() -> None:
    keys = hostkey.parse("a ssh-ed25519 AAA\nb ssh-ed25519 BBB\n--\n256 SHA256:a a (ED25519)\n256 SHA256:b b (ED25519)\n")
    with pytest.raises(ValueError, match="one host"):
        hostkey.pin(_spec(), "kh", keys)


@pytest.mark.skipif(not os.environ.get("CTUN_TEST_SSHD"), reason="set CTUN_TEST_SSHD=<host> to scan a real server")
def test_scan_a_real_server() -> None:
    [key] = hostkey.scan(_spec(os.environ["CTUN_TEST_SSHD"]))
    assert key.fingerprint.startswith("SHA256:") and key.kind and key.bits > 0


# --- reading the helper's output ---------------------------------------------------


def test_parse_pairs_lines_with_fingerprints() -> None:
    out = (
        "[uc3.example]:2222 ssh-rsa AAAAB3\n"
        "--\n"
        "3072 SHA256:8szdMQTAZ [uc3.example]:2222 (RSA)\n"
    )
    [key] = hostkey.parse(out)
    assert (key.host, key.kind, key.bits, key.fingerprint) == ("[uc3.example]:2222", "RSA", 3072, "SHA256:8szdMQTAZ")
    assert key.line == "[uc3.example]:2222 ssh-rsa AAAAB3"
    assert hostkey.parse("") == []


@pytest.mark.parametrize("out", ["h ssh-ed25519 AAA\n", "h ssh-ed25519 AAA\n--\n"])
def test_parse_refuses_output_it_cannot_pair(out: str) -> None:
    with pytest.raises(RuntimeError):
        hostkey.parse(out)


# --- where it runs -------------------------------------------------------------------


def test_default_file(monkeypatch) -> None:
    assert hostkey.default_file() == str(paths.known_hosts_file())
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    assert hostkey.default_file() == wsl.DEFAULT_KNOWN_HOSTS


def test_windows_runs_the_helper_inside_wsl_with_unix_line_ends(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(wsl, "enabled", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    spec = ConnSpec("k", "h", PurePosixPath("~/s/k"), "12h", 60, 3, None, wsl_distro="Ubuntu")
    assert hostkey.pinned(spec, "~/.config/cluster-tunnel/known_hosts", "h") == []
    assert seen["argv"][:5] == ["wsl.exe", "-d", "Ubuntu", "-e", "sh"]
    assert seen["argv"][-3:] == ["pinned", "~/.config/cluster-tunnel/known_hosts", "h"]
    assert isinstance(seen["input"], bytes) and b"\r\n" not in seen["input"]


def test_a_missing_sh_is_reported(monkeypatch) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="sh"):
        hostkey.scan(_spec())
