"""The Python API on its own: results, errors and side effects (no real ssh)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cluster_tunnel import api, cmdlog, paths, popup, session, ssh, wsl
from cluster_tunnel.budget import Decision
from cluster_tunnel.constants import ExitCode
from cluster_tunnel.jobs import Job

GUARDED = (
    "clusters:\n"
    "  c:\n"
    "    host: h\n"
    "    user: u\n"
    "    budget:\n"
    "      session_limit: 3\n"
    "      unit: jobh\n"
    "  d:\n"
    "    host: d.example\n"
)


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(GUARDED, encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def _live(monkeypatch, live: bool = True) -> None:
    monkeypatch.setattr(ssh, "is_live", lambda spec: live)


def _decide(monkeypatch, decision: Decision) -> list:
    calls: list = []

    def fake(config, name, spec, tokens, config_path):
        calls.append((name, list(tokens), Path(config_path)))
        return decision

    monkeypatch.setattr("cluster_tunnel.budget.decide", fake)
    return calls


def _forbid(monkeypatch, *targets: str) -> None:
    """Make each target fail the test if it is called."""
    for target in targets:
        def boom(*args, _target=target, **kwargs):
            raise AssertionError(f"{_target} must not be called")

        monkeypatch.setattr(target, boom)


# --- config ------------------------------------------------------------------


def test_load_config_without_a_file(tmp_path: Path) -> None:
    with pytest.raises(api.ConfigError, match="No config at"):
        api.load_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize("text", ["clusters:\n  c:\n    hots: typo\n", "clusters: [\n"])
def test_load_config_that_is_invalid(tmp_path: Path, text: str) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(api.ConfigError, match="invalid config"):
        api.load_config(p)


def test_unknown_cluster(cfg: Path) -> None:
    with pytest.raises(api.UnknownClusterError) as info:
        api.connection("nope")
    assert str(info.value) == "Unknown cluster 'nope'. Configured: c, d"
    assert isinstance(info.value, api.ClusterTunnelError)


def test_clusters_and_connection(cfg: Path, tmp_path: Path) -> None:
    assert api.clusters() == ["c", "d"]
    spec = api.connection("c")
    assert spec.target == "u@h"
    # The control socket: whoever holds it has the logged-in session.
    assert spec.socket == tmp_path / "cache" / "sockets" / "c"


def test_an_explicit_config_path_beats_the_environment(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "other.yaml"
    other.write_text("clusters:\n  x:\n    host: x.example\n", encoding="utf-8")
    assert api.clusters(config_path=other) == ["x"]
    assert api.clusters(config=api.load_config(other)) == ["x"]


def test_is_live(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    assert api.is_live("c") is False
    _live(monkeypatch, True)
    assert api.is_live("c") is True


# --- run, preflight, budget, feed_script --------------------------------------


def test_run_order_preflight_hook_log_then_command(cfg: Path, monkeypatch) -> None:
    events: list = []
    _live(monkeypatch)
    decision = Decision(True, "within budget: 1.0 < 3.0 jobh", 1.0, 3.0, "jobh")
    calls = _decide(monkeypatch, decision)
    monkeypatch.setattr(cmdlog, "record", lambda name, tokens: events.append(("record", name, tokens)))
    monkeypatch.setattr(ssh, "run", lambda spec, tokens, tty=False: events.append(("run", tokens, tty)) or 7)

    result = api.run("c", ("sbatch", "j.sh"), on_preflight=lambda d: events.append(("hook", d)))
    assert result == api.Result(7, None, None) and not result.ok
    assert calls == [("c", ["sbatch", "j.sh"], cfg)]
    assert events == [
        ("hook", decision),
        ("record", "c", ["sbatch", "j.sh"]),
        ("run", ["sbatch", "j.sh"], False),
    ]


def test_run_capture_returns_the_output(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)
    _decide(monkeypatch, Decision(True, "not a guarded command"))
    seen: dict = {}

    def fake_capture(spec, tokens, timeout=None):
        seen.update(tokens=tokens, timeout=timeout)
        return subprocess.CompletedProcess([], 0, "JOBID\n", "")

    monkeypatch.setattr(ssh, "capture", fake_capture)
    result = api.run("c", ["squeue", "--me"], capture=True, timeout=30)
    assert result == api.Result(0, "JOBID\n", "") and result.ok
    assert seen == {"tokens": ["squeue", "--me"], "timeout": 30}
    assert cmdlog.entries("c")[0]["command"] == ["squeue", "--me"]


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"capture": True, "tty": True}, ValueError),
        ({"timeout": 5}, ValueError),
    ],
)
def test_run_rejects_contradictory_options(cfg: Path, monkeypatch, kwargs, error) -> None:
    _forbid(monkeypatch, "cluster_tunnel.ssh.is_live", "cluster_tunnel.ssh.run")
    with pytest.raises(error):
        api.run("c", ["ls"], **kwargs)


def test_a_command_is_an_argv_list(cfg: Path, monkeypatch) -> None:
    _forbid(monkeypatch, "cluster_tunnel.ssh.is_live")
    with pytest.raises(TypeError, match="argv list"):
        api.run("c", "squeue --me")
    with pytest.raises(TypeError):
        api.preflight("c", "sbatch j.sh")


def test_without_a_tunnel_nothing_runs(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    _forbid(monkeypatch, "cluster_tunnel.budget.decide", "cluster_tunnel.cmdlog.record", "cluster_tunnel.ssh.run")
    with pytest.raises(api.LoginRequired) as info:
        api.run("c", ["squeue"])
    exc = info.value
    assert exc.exit_code == ExitCode.LOGIN_REQUIRED and exc.marker == "login_required"
    assert exc.cluster == "c"
    assert "ctun -t c login" in str(exc)


@pytest.mark.parametrize(
    "decision, error, code, marker",
    [
        (Decision(False, "session budget exhausted: 4.0 >= 3.0 jobh", 4.0, 3.0, "jobh"),
         api.BudgetExhausted, ExitCode.BUDGET_EXHAUSTED, "budget_exhausted"),
        (Decision(False, "budget probe failed: boom (fail-closed: blocked)", None, 3.0, "jobh"),
         api.BudgetGuardError, ExitCode.BUDGET_GUARD_ERROR, "budget_guard_error"),
    ],
)
def test_a_blocked_submission_raises_and_runs_nothing(cfg: Path, monkeypatch, decision, error, code, marker) -> None:
    _live(monkeypatch)
    _decide(monkeypatch, decision)
    _forbid(monkeypatch, "cluster_tunnel.cmdlog.record", "cluster_tunnel.ssh.run")
    with pytest.raises(error) as info:
        api.run("c", ["sbatch", "j.sh"])
    exc = info.value
    assert isinstance(exc, api.BudgetBlocked)
    assert exc.exit_code == code and exc.marker == marker
    assert exc.decision is decision and exc.command == ["sbatch", "j.sh"]
    assert str(exc) == f"Submission blocked on 'c': {decision.reason}"
    with pytest.raises(error):
        api.preflight("c", ["sbatch", "j.sh"])


def test_preflight_returns_the_decision_and_runs_nothing(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)
    decision = Decision(True, "within budget: 1.0 < 3.0 jobh", 1.0, 3.0, "jobh")
    _decide(monkeypatch, decision)
    _forbid(monkeypatch, "cluster_tunnel.cmdlog.record", "cluster_tunnel.ssh.run")
    assert api.preflight("c", ["sbatch", "j.sh"]) is decision


def test_budget_returns_a_blocking_verdict(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)
    blocked = Decision(False, "session budget exhausted: 4.0 >= 3.0 jobh", 4.0, 3.0, "jobh")
    seen: list = []
    monkeypatch.setattr(
        "cluster_tunnel.budget.check",
        lambda config, name, spec, path: seen.append((name, Path(path))) or blocked,
    )
    assert api.budget("c") is blocked
    assert seen == [("c", cfg)]


def test_budget_needs_a_tunnel(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    _forbid(monkeypatch, "cluster_tunnel.budget.check")
    with pytest.raises(api.LoginRequired):
        api.budget("c")


def test_budget_probes_the_session(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    # The real check, down to the probe: usage since the session start vs its limit.
    (tmp_path / "budget").mkdir()
    (tmp_path / "budget" / "c.sh").write_text("echo 2.5\n", encoding="utf-8")
    sess = session.start("c", limit=3.0, unit="jobh")
    _live(monkeypatch)
    probes: list = []

    def fake_probe(spec, script, start_epoch, user):
        probes.append((Path(script).name, start_epoch, user))
        return subprocess.CompletedProcess([], 0, "2.5\n", "")

    monkeypatch.setattr(ssh, "probe", fake_probe)
    decision = api.budget("c")
    assert decision.allowed and decision.used == 2.5 and decision.limit == 3.0
    assert probes == [("c.sh", sess["start_epoch"], "u")]


def test_feed_script_is_captured_but_neither_guarded_nor_logged(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    _live(monkeypatch)
    _forbid(monkeypatch, "cluster_tunnel.budget.decide", "cluster_tunnel.cmdlog.record")
    script = tmp_path / "probe.sh"
    script.write_text("echo hi\n", encoding="utf-8")
    seen: dict = {}

    def fake_feed(spec, script_path, args, timeout=None):
        seen.update(script=script_path, args=args, timeout=timeout)
        return subprocess.CompletedProcess([], 0, "hi\n", "")

    monkeypatch.setattr(ssh, "feed_script", fake_feed)
    result = api.feed_script("c", str(script), ("a", 1), timeout=9)
    assert result == api.Result(0, "hi\n", "")
    assert seen == {"script": script, "args": ["a", 1], "timeout": 9}


def test_feed_script_needs_a_tunnel(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    _forbid(monkeypatch, "cluster_tunnel.ssh.feed_script")
    with pytest.raises(api.LoginRequired):
        api.feed_script("c", tmp_path / "probe.sh")


# --- transfers -----------------------------------------------------------------


def test_upload_streams_and_returns_the_exit_code(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    _live(monkeypatch)
    seen: list = []

    def fake_run_transfer(spec, direction, src, dest, *, dry_run=False, extra=()):
        seen.append((spec.name, direction, src, dest, dry_run, tuple(extra)))
        return 23

    monkeypatch.setattr("cluster_tunnel.transfer.run_transfer", fake_run_transfer)
    result = api.upload("c", tmp_path / "in", "data", dry_run=True, extra=["-z"])
    assert result == api.Result(23, None, None)
    assert seen == [("c", "upload", str(tmp_path / "in"), "data", True, ("-z",))]


def test_download_capture_returns_rsync_output(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)
    _forbid(monkeypatch, "cluster_tunnel.transfer.run_transfer")
    seen: list = []

    def fake_capture(spec, direction, src, dest, *, dry_run=False, extra=()):
        seen.append((direction, src, dest))
        return subprocess.CompletedProcess([], 0, "receiving file list\n", "")

    monkeypatch.setattr("cluster_tunnel.transfer.capture_transfer", fake_capture)
    result = api.download("c", "results", Path("out"), capture=True)
    assert result.stdout == "receiving file list\n" and result.ok
    assert seen == [("download", "results", "out")]


def test_a_transfer_needs_a_tunnel(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    _forbid(monkeypatch, "cluster_tunnel.transfer.run_transfer")
    with pytest.raises(api.LoginRequired):
        api.upload("c", "a", "b")


def test_a_wsl_translation_failure_is_a_transport_error(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)

    def fail(*args, **kwargs):
        raise wsl.WslError("cannot translate 'Z:\\x' to a WSL path")

    monkeypatch.setattr("cluster_tunnel.transfer.run_transfer", fail)
    with pytest.raises(api.TransportError, match="cannot translate") as info:
        api.upload("c", "Z:\\x", "data")
    assert info.value.exit_code is None and info.value.marker is None


# --- status, jobs, login, logout ------------------------------------------------


def test_status_probes_usage_only_when_asked(cfg: Path, monkeypatch) -> None:
    session.start("c", limit=3.0, unit="jobh")
    _live(monkeypatch)
    monkeypatch.setattr("cluster_tunnel.budget.remote_user", lambda spec, cluster: "u")
    monkeypatch.setattr("cluster_tunnel.budget.used_since", lambda *a: 1.5)
    [quiet] = api.status("c")
    assert quiet.used is None and quiet.used_error is None
    [probed] = api.status("c", usage=True)
    assert probed.used == 1.5 and probed.limit == 3.0 and probed.unit == "jobh"
    assert [row.cluster for row in api.status(usage=True)] == ["c", "d"]


def test_jobs_returns_typed_jobs(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch)
    job = Job("active", "42", "RUNNING", "0:05", "1:00:00", "cpu", "1", "run.sh")
    monkeypatch.setattr("cluster_tunnel.jobs.query", lambda spec, user, since: [job])
    [entry] = api.jobs("c", since_minutes=0)
    assert entry.jobs == [job] and entry.live and entry.error is None
    assert entry.as_dict()["jobs"] == [job.as_dict()]


def test_login_results(cfg: Path, monkeypatch) -> None:
    # Fresh tunnel: a new session with the configured limit.
    answers = iter([False, True])
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(answers, True))
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: 0)
    fresh = api.login("c")
    assert (fresh.already_live, fresh.interactive, fresh.session_kept) == (False, False, False)
    assert fresh.session["limit"] == 3.0 and fresh.target == "u@h" and fresh.via is None

    # The tunnel is up now: logging in again keeps that session.
    again = api.login("c", limit=99)
    assert (again.already_live, again.session_kept) == (True, True)
    assert again.session == fresh.session


def test_interactive_login_result(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a: popup.Credentials("pw", "1", -1))
    monkeypatch.setattr(popup, "login_with_password", lambda *a: True)
    result = api.login("c", interactive=True)
    assert result.interactive and not result.already_live
    assert result.session["limit"] is None  # a negative limit means unguarded


def test_login_error_carries_the_ssh_diagnostics(cfg: Path, monkeypatch) -> None:
    _live(monkeypatch, False)
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: 255)
    monkeypatch.setattr(ssh, "check", lambda spec: subprocess.CompletedProcess([], 255, "", "Permission denied\n"))
    with pytest.raises(api.LoginError) as quiet:
        api.login("c")
    assert str(quiet.value) == "Failed to open tunnel to 'c' (ssh exit 255)."
    assert quiet.value.ssh_failed and quiet.value.diagnostics is None
    with pytest.raises(api.LoginError) as loud:
        api.login("c", verbose=1)
    assert loud.value.diagnostics == "Permission denied"
    assert session.load("c") is None


def test_logout_reports_each_cluster(cfg: Path, monkeypatch) -> None:
    session.start("c", limit=None, unit="units")
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "c")
    monkeypatch.setattr(ssh, "close", lambda spec: True)
    assert api.logout() == [api.LogoutResult("c", True), api.LogoutResult("d", False)]
    assert session.load("c") is None


def test_unknown_clusters_are_refused_everywhere(cfg: Path, monkeypatch) -> None:
    _forbid(monkeypatch, "cluster_tunnel.ssh.is_live")
    calls = [
        lambda: api.status("nope"), lambda: api.jobs("nope"), lambda: api.logout("nope"),
        lambda: api.login("nope"), lambda: api.run("nope", ["ls"]), lambda: api.budget("nope"),
        lambda: api.upload("nope", "a", "b"), lambda: api.feed_script("nope", "x.sh"),
    ]
    for call in calls:
        with pytest.raises(api.UnknownClusterError):
            call()


# --- host keys -------------------------------------------------------------------


def _key(fingerprint: str, host: str = "h") -> api.HostKey:
    return api.HostKey(host, "ED25519", 256, fingerprint, f"{host} ssh-ed25519 KEY-{fingerprint}")


def _server(monkeypatch, *, trust: str, fingerprint: str = "SHA256:a", conflicts=(), pinned=None) -> dict:
    """Stub ssh's verdict, the key fetch and the key file; record what is asked."""
    from cluster_tunnel import hostkey as hostkey_mod

    calls: dict = {"scan": [], "pinned": [], "pin": []}
    monkeypatch.setattr(
        "cluster_tunnel.hostkey.check",
        lambda spec: hostkey_mod.Check(trust, "ssh-ed25519", fingerprint, list(conflicts)),
    )

    def fake_scan(spec, algorithm=None):
        calls["scan"].append(algorithm)
        return [_key(fingerprint)]

    def fake_pinned(spec, file, host):
        calls["pinned"].append((file, host))
        return pinned or []

    monkeypatch.setattr("cluster_tunnel.hostkey.scan", fake_scan)
    monkeypatch.setattr("cluster_tunnel.hostkey.pinned", fake_pinned)
    monkeypatch.setattr("cluster_tunnel.hostkey.pin", lambda spec, file, keys: calls["pin"].append((file, keys)))
    return calls


def _pinned_config(tmp_path: Path) -> Path:
    p = tmp_path / "pinned.yaml"
    p.write_text(
        "clusters:\n  bw:\n    host: h\n    strict_host_key_checking: 'yes'\n    known_hosts_file: /kh\n",
        encoding="utf-8",
    )
    return p


def test_host_key_of_an_unpinned_cluster(cfg: Path, monkeypatch) -> None:
    calls = _server(monkeypatch, trust="new")
    status = api.host_key("c")
    assert status.offered.fingerprint == "SHA256:a" and status.trust == "new"
    assert status.pinned == [] and not status.strict and not status.matches
    assert calls["scan"] == ["ssh-ed25519"]  # the key a login negotiates
    assert calls["pinned"] == []  # no known_hosts_file to read


def test_host_key_of_a_pinned_cluster(tmp_path: Path, monkeypatch) -> None:
    p = _pinned_config(tmp_path)
    calls = _server(monkeypatch, trust="known", pinned=[_key("SHA256:a")])
    status = api.host_key("bw", config_path=p)
    assert status.trust == "known" and status.strict and status.matches
    assert calls["pinned"] == [("/kh", "h")]
    assert status.as_dict()["matches"] is True and status.as_dict()["trust"] == "known"


def test_host_key_reports_where_a_different_key_is_trusted(cfg: Path, monkeypatch) -> None:
    _server(monkeypatch, trust="changed", conflicts=["/home/u/.ssh/known_hosts:3"])
    status = api.host_key("c")
    assert status.trust == "changed" and status.conflicts == ["/home/u/.ssh/known_hosts:3"]


def test_host_key_refuses_a_server_that_presents_two_keys(cfg: Path, monkeypatch) -> None:
    _server(monkeypatch, trust="new", fingerprint="SHA256:first")
    monkeypatch.setattr("cluster_tunnel.hostkey.scan", lambda spec, algorithm=None: [_key("SHA256:second")])
    with pytest.raises(api.HostKeyError, match="two different keys"):
        api.host_key("c")


def test_host_key_of_an_unreachable_server(cfg: Path, monkeypatch) -> None:
    def refused(spec):
        raise RuntimeError("ssh: connect to host h port 22: Connection refused")

    monkeypatch.setattr("cluster_tunnel.hostkey.check", refused)
    with pytest.raises(api.HostKeyError, match="cannot check the host key of 'c': ssh: connect"):
        api.host_key("c")


def test_pin_host_key_refuses_a_key_other_than_the_confirmed_one(cfg: Path, monkeypatch) -> None:
    calls = _server(monkeypatch, trust="new", fingerprint="SHA256:now")
    before = cfg.read_text(encoding="utf-8")
    with pytest.raises(api.HostKeyError, match="presents SHA256:now, not SHA256:confirmed"):
        api.pin_host_key("c", "SHA256:confirmed")
    assert calls["pin"] == [] and cfg.read_text(encoding="utf-8") == before


def test_pin_host_key_does_not_replace_a_trusted_key_unless_told(cfg: Path, monkeypatch) -> None:
    # ssh trusts another key for the host (e.g. in ~/.ssh/known_hosts): pinning the
    # one presented now would replace it, which is a person's decision.
    calls = _server(monkeypatch, trust="changed", conflicts=["/home/u/.ssh/known_hosts:3"])
    with pytest.raises(api.HostKeyError, match="trust a different key"):
        api.pin_host_key("c", "SHA256:a")
    assert calls["pin"] == []
    api.pin_host_key("c", "SHA256:a", replace=True)
    assert len(calls["pin"]) == 1


def test_pin_host_key_stores_the_key_next_to_the_config(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    calls = _server(monkeypatch, trust="new")
    status = api.pin_host_key("c", "SHA256:a")
    file = str(tmp_path / "known_hosts")  # next to config.yaml
    assert calls["pin"] == [(file, [_key("SHA256:a")])]
    entry = api.load_config().clusters["c"]
    assert entry.strict_host_key_checking == "yes" and entry.known_hosts_file == file
    assert status.strict and status.matches and status.trust == "known"
    backup = tmp_path / "config.yaml.bak"
    assert "strict_host_key_checking" not in backup.read_text(encoding="utf-8")

    # Pinning the same key again rewrites the key file only, not the config.
    backup.unlink()
    api.pin_host_key("c", "SHA256:a")
    assert not backup.exists() and len(calls["pin"]) == 2


def test_pin_host_key_pins_nothing_when_the_config_cant_be_edited(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "flow.yaml"
    p.write_text("clusters: {bw: {host: h}}\n", encoding="utf-8")
    calls = _server(monkeypatch, trust="new")
    with pytest.raises(api.ConfigError, match="cannot pin the host key"):
        api.pin_host_key("bw", "SHA256:a", config_path=p)
    assert calls["pin"] == []


# --- review fixes: config errors, empty commands, stdin, probe timeouts ----------


@pytest.mark.parametrize("raw", [b"- a\n- b\n", b"just text\n", b"clusters:\n  c:\n    host: \xff\n"])
def test_every_broken_config_is_a_config_error(tmp_path: Path, raw: bytes) -> None:
    p = tmp_path / "config.yaml"
    p.write_bytes(raw)
    with pytest.raises(api.ConfigError, match="invalid config"):
        api.load_config(p)


def test_an_empty_command_is_refused(cfg: Path, monkeypatch) -> None:
    _forbid(monkeypatch, "cluster_tunnel.ssh.is_live")
    with pytest.raises(ValueError, match="empty"):
        api.run("c", [])
    with pytest.raises(ValueError, match="empty"):
        api.preflight("c", [])


def test_captured_commands_read_nothing(cfg: Path, monkeypatch) -> None:
    # A program embedding the API (e.g. a terminal UI) keeps its own stdin.
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ssh.capture(api.connection("c"), ["hostname"])
    assert seen["stdin"] is subprocess.DEVNULL


def test_probes_are_bounded(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import budget as budget_mod
    from cluster_tunnel import constants
    from cluster_tunnel import jobs as jobs_mod

    timeouts: list = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "u\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    spec = api.connection("d")
    script = tmp_path / "probe.sh"
    script.write_text("echo 1\n", encoding="utf-8")
    budget_mod.remote_user(spec, api.load_config().clusters["d"])
    ssh.probe(spec, script, 0, "u")
    jobs_mod.query(spec, "u", 0)
    assert timeouts == [constants.PROBE_TIMEOUT] * 3
