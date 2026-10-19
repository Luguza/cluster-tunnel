"""Python API: what the ``ctun`` CLI does, as functions a program can call.

Every function names its cluster as ``ctun -t <cluster>`` does and reads the
same config: ``config_path`` (default: ``$CTUN_CONFIG``, else the per-user
``config.yaml``), or an already loaded ``config``. In that case
``config_path`` only locates the budget scripts, which sit next to the config
file.

The CLI is built on these functions, so the two behave the same: the same
tunnel check, the same budget guard, the same session and command log.
Failures raise :class:`~cluster_tunnel.errors.ClusterTunnelError` subclasses;
the ones ``ctun`` reports with a stable exit code carry it as ``exit_code``.

This module and :mod:`cluster_tunnel.errors` are the public interface and
follow semantic versioning; the other modules are internal.

Example::

    from cluster_tunnel import api

    if not api.is_live("horeka"):
        ...  # a person runs `ctun -t horeka login`
    result = api.run("horeka", ["squeue", "--me"], capture=True)
    print(result.stdout)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import pydantic
import yaml

from cluster_tunnel import budget as budget_mod
from cluster_tunnel import cmdlog
from cluster_tunnel import config as config_mod
from cluster_tunnel import jobs as jobs_mod
from cluster_tunnel import session, ssh, wsl
from cluster_tunnel import transfer as transfer_mod
from cluster_tunnel.budget import Decision
from cluster_tunnel.config import Cluster, Config
from cluster_tunnel.errors import (
    BudgetBlocked,
    BudgetExhausted,
    BudgetGuardError,
    ClusterTunnelError,
    ConfigError,
    LoginError,
    LoginRequired,
    TransportError,
    UnknownClusterError,
    budget_blocked,
)
from cluster_tunnel.jobs import Job
from cluster_tunnel.ssh import ConnSpec

__all__ = [
    # operations
    "load_config", "clusters", "connection", "is_live", "status", "login", "logout",
    "preflight", "budget", "run", "feed_script", "upload", "download", "jobs",
    # results
    "Result", "ClusterStatus", "ClusterJobs", "LoginResult", "LogoutResult",
    "Decision", "Job", "ConnSpec", "Config", "Cluster",
    # errors
    "ClusterTunnelError", "ConfigError", "UnknownClusterError", "LoginRequired",
    "LoginError", "BudgetBlocked", "BudgetExhausted", "BudgetGuardError", "TransportError",
]

PathLike = Union[str, Path]

#: The finished-job window :func:`jobs` uses unless told otherwise (``ctun jobs``: 24h).
DEFAULT_SINCE_MINUTES = 24 * 60

#: Seconds an interactive :func:`login` waits for ssh (``ctun login --timeout``).
DEFAULT_LOGIN_TIMEOUT = 120


@dataclass
class Result:
    """What a command, script or transfer returned.

    ``stdout``/``stderr`` are ``None`` when the output streamed to this
    process's own stdout/stderr instead of being captured.
    """

    exit_code: int
    stdout: Optional[str] = None
    stderr: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class ClusterStatus:
    """One cluster's tunnel and session: a row of ``ctun status``."""

    cluster: str
    target: str
    live: bool
    session: Optional[dict]
    limit: Optional[float]
    unit: Optional[str]
    commands: dict  # {"count": int, "last_epoch": int | None}, from the command log
    used: Optional[float] = None
    used_error: Optional[str] = None

    def as_dict(self) -> dict:
        """The row as ``ctun status --json`` prints it."""
        return asdict(self)


@dataclass
class ClusterJobs:
    """One cluster's Slurm jobs, as ``ctun jobs`` reports them."""

    cluster: str
    live: bool
    error: Optional[str] = None
    jobs: list[Job] = field(default_factory=list)

    def as_dict(self) -> dict:
        """The entry as ``ctun jobs --json`` prints it."""
        return {
            "cluster": self.cluster,
            "live": self.live,
            "error": self.error,
            "jobs": [job.as_dict() for job in self.jobs],
        }


@dataclass
class LoginResult:
    """What :func:`login` did."""

    cluster: str
    target: str
    already_live: bool  # the tunnel was up already, so nothing was opened
    interactive: bool  # opened through the password/OTP dialog
    via: Optional[str]  # Windows: the WSL distro carrying the tunnel
    session: dict
    session_kept: bool  # the running session, and its budget window, was kept


@dataclass
class LogoutResult:
    """One cluster :func:`logout` closed."""

    cluster: str
    was_live: bool


# --- config --------------------------------------------------------------------


def load_config(config_path: Optional[PathLike] = None) -> Config:
    """Load and validate the config; raise :class:`ConfigError` if it is missing or invalid."""
    try:
        return config_mod.load_config(_path_arg(config_path))
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    except (pydantic.ValidationError, yaml.YAMLError) as exc:
        path = config_mod.resolve_config_path(_path_arg(config_path))
        raise ConfigError(f"invalid config {path}: {exc}") from exc


def clusters(*, config: Optional[Config] = None, config_path: Optional[PathLike] = None) -> list[str]:
    """The configured clusters' names, sorted."""
    config, _ = _load(config, config_path)
    return sorted(config.clusters)


def connection(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> ConnSpec:
    """How ctun reaches ``cluster``: its ssh target, control socket and keepalives."""
    config, _ = _load(config, config_path)
    return _spec(config, cluster)


# --- tunnel ----------------------------------------------------------------------


def is_live(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> bool:
    """True if the tunnel to ``cluster`` is up (its ssh master answers)."""
    return ssh.is_live(connection(cluster, config=config, config_path=config_path))


def status(
    cluster: Optional[str] = None,
    *,
    usage: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ClusterStatus]:
    """Tunnel and session state of ``cluster``, or of every configured cluster.

    With ``usage``, a live cluster whose session has a limit also gets its
    current budget usage, probed over the tunnel: ``used``, or ``used_error``
    when the probe fails. ``ctun status`` probes for a single ``-t`` cluster
    only, never as a side effect of the overview.
    """
    config, path = _load(config, config_path)
    rows: list[ClusterStatus] = []
    for name in _names(config, cluster):
        entry = config.clusters[name]
        spec = ssh.conn_spec(config, name)
        live = ssh.is_live(spec)
        sess = session.load(name)
        # The unit falls back to the cluster's configured unit; the limit comes
        # from the live session (None when unguarded or not logged in).
        unit = (sess.get("unit") if sess else None) or (entry.budget.unit if entry.budget else None)
        limit = sess.get("limit") if sess else None

        used: Optional[float] = None
        used_error: Optional[str] = None
        if usage and live and sess and limit is not None and entry.budget:
            try:
                script = config_mod.budget_script_path(path, name, entry.budget.script)
                user = budget_mod.remote_user(spec, entry)
                used = budget_mod.used_since(spec, script, int(sess["start_epoch"]), user)
            except Exception as exc:  # noqa: BLE001 — reported in the row, not raised
                used_error = str(exc)

        rows.append(
            ClusterStatus(
                cluster=name,
                target=spec.target,
                live=live,
                session=sess,
                limit=limit,
                unit=unit,
                commands=cmdlog.summary(name),
                used=used,
                used_error=used_error,
            )
        )
    return rows


def login(
    cluster: str,
    *,
    interactive: bool = False,
    limit: Optional[float] = None,
    timeout: int = DEFAULT_LOGIN_TIMEOUT,
    verbose: int = 0,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> LoginResult:
    """Open the tunnel to ``cluster`` and start a budget session, as ``ctun login``.

    Without ``interactive``, ssh asks for the password and OTP in this process's
    terminal; with it, a dialog lets a present person enter them, and they go
    straight to ssh. A tunnel that is already up is kept, and so is its session.
    A new tunnel starts a new session (and an empty command log) whose limit is
    ``limit``, else the dialog's, else the cluster's ``session_limit``; a
    negative limit means unguarded. ``verbose`` (0-3) adds ssh's ``-v`` flags.

    Raises :class:`LoginError` when the tunnel can't be opened or the dialog is
    cancelled or can't be shown.
    """
    config, _ = _load(config, config_path)
    entry = _entry(config, cluster)
    spec = ssh.conn_spec(config, cluster)

    via = None  # the WSL distro carrying the tunnel, on Windows
    if wsl.enabled():
        ready = wsl.preflight(spec.wsl_distro)
        if not ready.ok:
            raise LoginError("\n".join(ready.problems), cluster)
        via = ready.distro

    # Default limit: the argument, else the cluster's configured session_limit.
    default_limit = limit
    if default_limit is None and entry.budget is not None:
        default_limit = entry.budget.session_limit
    unit = entry.budget.unit if entry.budget else "units"

    already_live = ssh.is_live(spec)
    chosen_limit = default_limit
    if already_live:
        pass
    elif interactive:
        from cluster_tunnel import popup

        if not popup.gui_available():
            raise LoginError(
                f"No display for the login dialog; run `ctun -t {cluster} login` from a terminal.",
                cluster,
            )
        creds = popup.prompt_credentials(
            cluster, spec.target, default_limit, unit,
            entry.requires_otp, entry.requires_password,
        )
        if creds is None:
            raise LoginError("Login cancelled.", cluster)
        try:
            ok = popup.login_with_password(spec, creds.password, creds.otp, timeout, verbose)
        except wsl.WslError as exc:
            raise LoginError(str(exc), cluster) from exc
        if not ok:
            raise LoginError(
                f"Interactive login to '{cluster}' failed or timed out.", cluster, ssh_failed=True
            )
        chosen_limit = creds.limit
    else:
        try:
            rc = ssh.open_master(spec, verbose)
        except wsl.WslError as exc:
            raise LoginError(str(exc), cluster) from exc
        if rc != 0 or not ssh.is_live(spec):
            diagnostics = (ssh.check(spec).stderr or "").strip() if verbose else ""
            raise LoginError(
                f"Failed to open tunnel to '{cluster}' (ssh exit {rc}).",
                cluster,
                ssh_failed=True,
                diagnostics=diagnostics or None,
            )

    # A session limit of -1 (or any negative) means "infinite": no budget guard
    # at all, same as leaving the limit unset.
    if chosen_limit is not None and chosen_limit < 0:
        chosen_limit = None

    # A session begins on a fresh authentication; re-auth resets the window.
    sess = None if not already_live else session.load(cluster)
    if sess is None:
        cmdlog.clear(cluster)  # fresh session: start the command log empty
        sess = session.start(cluster, limit=chosen_limit, unit=unit)
        kept = False
    else:
        kept = True

    return LoginResult(
        cluster=cluster,
        target=spec.target,
        already_live=already_live,
        interactive=interactive and not already_live,
        via=via,
        session=sess,
        session_kept=kept,
    )


def logout(
    cluster: Optional[str] = None,
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[LogoutResult]:
    """Close the tunnel to ``cluster``, or to every configured cluster, as ``ctun logout``.

    Each cluster's session and command log are cleared, live tunnel or not.
    """
    config, _ = _load(config, config_path)
    closed: list[LogoutResult] = []
    for name in _names(config, cluster):
        spec = ssh.conn_spec(config, name)
        was_live = ssh.is_live(spec)
        ssh.close(spec)
        session.clear(name)
        cmdlog.clear(name)
        closed.append(LogoutResult(name, was_live))
    return closed


# --- commands ----------------------------------------------------------------------


def preflight(
    cluster: str,
    command: Sequence[str],
    *,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Decision:
    """Check that ``command`` may run on ``cluster`` now, without running it (``ctun run -n``).

    Raises :class:`LoginRequired` without a live tunnel, and
    :class:`BudgetExhausted` or :class:`BudgetGuardError` when the budget guard
    blocks the command; otherwise returns the guard's decision.
    """
    tokens = _tokens(command)
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    return _preflight(config, path, cluster, spec, tokens)


def budget(
    cluster: str, *, config: Optional[Config] = None, config_path: Optional[PathLike] = None
) -> Decision:
    """The session budget of ``cluster`` as it stands, probed over the tunnel now.

    The verdict the guard gives any job submission at this moment, whatever the
    command: ask it before submitting what the guard can't recognise as a
    submission, such as a script that calls ``sbatch``. A cluster without a
    budget is never blocked. Raises :class:`LoginRequired` without a live
    tunnel; a blocking verdict is returned (``allowed`` false), not raised.
    """
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    return budget_mod.check(config, cluster, spec, path)


def run(
    cluster: str,
    command: Sequence[str],
    *,
    capture: bool = False,
    tty: bool = False,
    timeout: Optional[float] = None,
    on_preflight: Optional[Callable[[Decision], None]] = None,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Run ``command`` (an argv list) on ``cluster``'s login node, as ``ctun run``.

    A live tunnel is required (:class:`LoginRequired`), a job submission must
    pass the budget guard (:class:`BudgetExhausted`, :class:`BudgetGuardError`),
    and the command goes into the session's command log before it runs.
    ``on_preflight`` is called with the guard's decision just before that.

    The output streams to this process's stdout and stderr, or, with
    ``capture``, comes back in the :class:`Result`; ``tty`` needs streaming.
    ``timeout`` (seconds, captured output only) stops waiting with exit code
    124; the remote command may keep running on the cluster.
    """
    if capture and tty:
        raise ValueError("a TTY needs streamed output; pass capture=False")
    if timeout is not None and not capture:
        raise ValueError("a timeout needs captured output; pass capture=True")
    tokens = _tokens(command)
    config, path = _load(config, config_path)
    spec = _spec(config, cluster)
    decision = _preflight(config, path, cluster, spec, tokens)
    if on_preflight is not None:
        on_preflight(decision)
    # Log the command (not its output) so `status` can show activity.
    cmdlog.record(cluster, tokens)
    if capture:
        res = ssh.capture(spec, tokens, timeout=timeout)
        return Result(res.returncode, res.stdout, res.stderr)
    return Result(ssh.run(spec, tokens, tty=tty))


def feed_script(
    cluster: str,
    script: PathLike,
    args: Sequence[object] = (),
    *,
    timeout: Optional[float] = None,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Run a local bash ``script`` on ``cluster``'s login node, fed over the tunnel.

    The script travels on stdin to ``bash -s -- <args>`` and is never written on
    the cluster; its output is captured. Like ctun's own probes it is neither
    budget-guarded nor logged, so keep it to read-only checks. ``timeout`` works
    as in :func:`run`. Raises :class:`LoginRequired` without a live tunnel.
    """
    config, _ = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    res = ssh.feed_script(spec, Path(script), list(args), timeout=timeout)
    return Result(res.returncode, res.stdout, res.stderr)


# --- transfers ---------------------------------------------------------------------


def upload(
    cluster: str,
    src: PathLike,
    dest: str,
    *,
    dry_run: bool = False,
    extra: Sequence[str] = (),
    capture: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Copy local ``src`` to ``dest`` on ``cluster`` (rsync -r over the tunnel), as ``ctun upload``.

    ``dest`` is a remote path, relative to the remote $HOME; trailing slashes
    follow rsync (``dir/`` copies the contents). ``extra`` are further rsync
    flags, ``dry_run`` only reports what would be copied, and ``capture``
    returns rsync's output instead of streaming it. Transfers are not
    budget-guarded: moving data isn't compute. Raises :class:`LoginRequired`
    without a live tunnel, and :class:`TransportError` when, on Windows, the
    local path can't be translated for WSL.
    """
    return _transfer("upload", cluster, src, dest, dry_run, extra, capture, config, config_path)


def download(
    cluster: str,
    src: str,
    dest: PathLike,
    *,
    dry_run: bool = False,
    extra: Sequence[str] = (),
    capture: bool = False,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> Result:
    """Copy ``src`` on ``cluster`` to local ``dest``, as ``ctun download``; see :func:`upload`."""
    return _transfer("download", cluster, src, dest, dry_run, extra, capture, config, config_path)


# --- jobs --------------------------------------------------------------------------


def jobs(
    cluster: Optional[str] = None,
    *,
    since_minutes: int = DEFAULT_SINCE_MINUTES,
    config: Optional[Config] = None,
    config_path: Optional[PathLike] = None,
) -> list[ClusterJobs]:
    """Your Slurm jobs on ``cluster``, or on every configured cluster, as ``ctun jobs``.

    On each cluster with a live tunnel a bundled read-only probe lists the
    running and pending jobs and those that finished within ``since_minutes``
    (``0``: active jobs only). A cluster whose tunnel is down is reported, never
    logged in to; a probe that fails is reported in ``error``.
    """
    config, _ = _load(config, config_path)
    results: list[ClusterJobs] = []
    for name in _names(config, cluster):
        entry = config.clusters[name]
        spec = ssh.conn_spec(config, name)
        item = ClusterJobs(name, ssh.is_live(spec))
        if item.live:
            try:
                item.jobs = jobs_mod.query(spec, entry.user, since_minutes)
            except Exception as exc:  # noqa: BLE001 — surface, don't abort the sweep
                item.error = str(exc)
        results.append(item)
    return results


# --- helpers -----------------------------------------------------------------------


def _path_arg(config_path: Optional[PathLike]) -> Optional[str]:
    return None if config_path is None else str(config_path)


def _load(config: Optional[Config], config_path: Optional[PathLike]) -> tuple[Config, Path]:
    """The config to use, and the config file path its budget scripts are relative to."""
    path = config_mod.resolve_config_path(_path_arg(config_path))
    return (config if config is not None else load_config(config_path)), path


def _entry(config: Config, name: str) -> Cluster:
    try:
        return config_mod.get_cluster(config, name)
    except KeyError as exc:
        raise UnknownClusterError(exc.args[0]) from None


def _spec(config: Config, name: str) -> ConnSpec:
    _entry(config, name)
    return ssh.conn_spec(config, name)


def _names(config: Config, cluster: Optional[str]) -> list[str]:
    """The cluster asked for, or every configured cluster (sorted) for ``None``."""
    if cluster is None:
        return sorted(config.clusters)
    _entry(config, cluster)
    return [cluster]


def _tokens(command: Sequence[str]) -> list[str]:
    if isinstance(command, str):
        raise TypeError("command is an argv list, e.g. ['squeue', '--me'], not a string")
    return list(command)


def _preflight(config: Config, path: Path, name: str, spec: ConnSpec, tokens: list[str]) -> Decision:
    """Require a live tunnel, then the budget guard's consent; return its decision."""
    if not ssh.is_live(spec):
        raise LoginRequired(name)
    decision = budget_mod.decide(config, name, spec, tokens, path)
    if not decision.allowed:
        raise budget_blocked(name, tokens, decision)
    return decision


def _transfer(
    direction: transfer_mod.Direction,
    cluster: str,
    src: PathLike,
    dest: PathLike,
    dry_run: bool,
    extra: Sequence[str],
    capture: bool,
    config: Optional[Config],
    config_path: Optional[PathLike],
) -> Result:
    config, _ = _load(config, config_path)
    spec = _spec(config, cluster)
    if not ssh.is_live(spec):
        raise LoginRequired(cluster)
    try:
        if capture:
            res = transfer_mod.capture_transfer(
                spec, direction, str(src), str(dest), dry_run=dry_run, extra=extra
            )
            return Result(res.returncode, res.stdout, res.stderr)
        return Result(
            transfer_mod.run_transfer(spec, direction, str(src), str(dest), dry_run=dry_run, extra=extra)
        )
    except wsl.WslError as exc:
        raise TransportError(str(exc)) from exc
