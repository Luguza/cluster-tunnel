"""Your jobs: the overview across clusters (jobs list) and changing their records (jobs edit)."""

from __future__ import annotations

import re

import rich_click as click

from cluster_tunnel.cli.errors import fail
from cluster_tunnel.constants import ExitCode

#: `--since` duration units expressed in minutes. A bare number is minutes.
_SINCE_UNITS = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}
_SINCE_RE = re.compile(r"^\s*(\d+)\s*([mhdw]?)\s*$", re.IGNORECASE)


def _parse_since(text: str) -> int:
    """Parse a ``--since`` value like ``24h`` / ``90m`` / ``2d`` into minutes.

    A bare number is minutes; ``0`` (any unit) disables the finished-job window.
    Raises :class:`click.BadParameter` on anything else.
    """
    m = _SINCE_RE.match(text)
    if not m:
        raise click.BadParameter(
            f"invalid duration {text!r}; use e.g. 24h, 90m, 2d (or 0 for active only)."
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    return value * _SINCE_UNITS.get(unit, 1)


def _fmt_since(minutes: int) -> str:
    """Render a minute count back as a compact duration for messages."""
    if minutes % (60 * 24) == 0:
        return f"{minutes // (60 * 24)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _state_style(state: str) -> str:
    """Rich style for a Slurm state word (first token of the raw state).

    RUNNING (live) and COMPLETED (finished OK) get deliberately distinct hues —
    green vs. blue — so an active job never reads as a finished one at a glance.
    """
    head = state.split()[0].upper() if state else ""
    if head == "RUNNING":
        return "green"
    if head == "PENDING":
        return "yellow"
    if head == "COMPLETED":
        return "blue"
    if head in {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"}:
        return "red"
    if head == "CANCELLED":
        return "bright_red"
    return "white"


def _attach_records(spec, entry: dict, since_minutes: int) -> None:
    """Give each job in ``entry`` its ``record`` (or ``None``); list the rest as ``records_without_job``.

    A record without a listed job belongs to a job that finished before the
    window, or whose accounting is gone. A failure to read the records is
    reported in ``records_error`` and leaves the jobs as they are.
    """
    from cluster_tunnel import records as records_mod

    job_ids = [job["jobid"] for job in entry["jobs"]]
    try:
        records = records_mod.list_recent(spec, since_minutes, job_ids)
    except RuntimeError as exc:
        entry["records_error"] = str(exc)
        return
    by_job = {record.job_id: record.as_dict() for record in records}
    for job in entry["jobs"]:
        job["record"] = by_job.pop(job["jobid"], None)
    entry["records_without_job"] = list(by_job.values())
    entry["records_error"] = None


class JobsCommandsMixin:
    """Mixin providing the `jobs` group: `jobs list` and `jobs edit`."""

    @click.group("jobs")
    def jobs_command() -> None:
        """Your Slurm jobs: list them, or give them a project or note."""

    @jobs_command.command("list")
    @click.pass_obj
    @click.option(
        "-s", "--since", "since", default="24h", metavar="DURATION",
        help="Finished-job history window (e.g. 24h, 90m, 2d; 0 = active only).",
    )
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def jobs_list_command(self, since: str, as_json: bool) -> None:
        """List your Slurm jobs: running, pending, recently finished.

        With a target (`-t <cluster>`), reports that cluster; without one, sweeps
        every configured cluster and reports each with a live tunnel (down ones
        are noted, never logged in to). For each cluster a bundled probe runs
        `squeue` (your running + pending jobs) and, unless `--since 0`, `sacct`
        (jobs that finished within the window) on the login node over the live
        tunnel — read-only, so it is never budget-guarded. `--json` includes
        each job's record.
        """
        from cluster_tunnel import jobs as jobs_mod
        from cluster_tunnel import ssh

        since_minutes = _parse_since(since)

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)  # validate the target
            names = [self.target]
        else:
            names = sorted(config.clusters)

        results = []
        for name in names:
            cluster = config.clusters[name]
            spec = ssh.conn_spec(config, name)
            entry = {"cluster": name, "slurm_cluster": None, "live": ssh.is_live(spec), "error": None, "jobs": []}
            if entry["live"]:
                try:
                    overview = jobs_mod.query(spec, cluster.user, since_minutes)
                    entry["slurm_cluster"] = overview.slurm_cluster
                    entry["jobs"] = [j.as_dict() for j in overview.jobs]
                except Exception as exc:  # noqa: BLE001 — surface, don't abort the sweep
                    entry["error"] = str(exc)
            if as_json and entry["live"] and entry["error"] is None and cluster.job_records:
                _attach_records(spec, entry, since_minutes)
            results.append(entry)

        if as_json:
            import json

            click.echo(json.dumps(
                {"since_minutes": since_minutes, "clusters": results}, indent=2
            ))
            return

        self._render_jobs(results, since_minutes)

    @jobs_command.command("edit")
    @click.pass_obj
    @click.argument("job_ids", nargs=-1, required=True, metavar="JOB_ID...")
    @click.option("--project", default=None, help='New project; "" for Unassigned.')
    @click.option("--note", default=None, help='New note, unconfirmed until --confirm-note; "" removes it.')
    @click.option("--confirm-note", is_flag=True, help="Confirm the note: a human has reviewed it.")
    def jobs_edit_command(self, job_ids: tuple[str, ...], project: str | None, note: str | None, confirm_note: bool) -> None:
        """Give one or more jobs the same project or note.

        Each change is stored in the job's record on the cluster, so every
        machine sees it. Prints the changed records as JSON. Stops at the first
        job that fails; running the command again is safe.

        Example: ctun -t haicore jobs edit 51319 51322 --project htfm/mh
        """
        import json

        from cluster_tunnel import notes, records, ssh
        from cluster_tunnel.cli.execution import check_note

        if project is None and note is None and not confirm_note:
            raise click.UsageError("Give --project, --note or --confirm-note.")

        config = self.load_config()
        name, cluster = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)
        if not ssh.is_live(spec):
            fail(f"No live tunnel for '{name}'. Run `ctun -t {name} login` first.", ExitCode.LOGIN_REQUIRED)
        if not cluster.job_records:
            raise click.ClickException(f"job_records is off for '{name}'.")

        results = []
        for job_id in job_ids:
            try:
                current = records.read(spec, job_id)
                if current is None and notes.unknown_references(spec, f"job:{job_id}", None):
                    raise click.ClickException(f"Job {job_id} is unknown on '{name}' (not in squeue or sacct).")
                updated = records.edited(current, job_id, project=project, note=note, confirm_note=confirm_note)
                if updated is not None:
                    if note:
                        check_note(spec, note, updated.get("project"), refused="Not saved")
                    records.update(spec, updated)
            except (RuntimeError, ValueError) as exc:
                raise click.ClickException(str(exc)) from exc

            if updated is None:
                click.secho(f"ctun: job {job_id} already has these values", fg="bright_black", err=True)
            else:
                click.secho(f"ctun: updated job {job_id}", fg="bright_black", err=True)
            results.append(updated or current)
        click.echo(json.dumps(results, indent=2, ensure_ascii=False))

    def _render_jobs(self, results: list[dict], since_minutes: int) -> None:
        from rich.table import Table

        cons = self.cons

        if not results:
            click.echo("No clusters configured. Run `ctun config --init`.")
            return

        if since_minutes:
            cons.print(
                f"[dim]Your jobs — running, pending, and finished in the last "
                f"{_fmt_since(since_minutes)}.[/dim]\n"
            )
        else:
            cons.print("[dim]Your active jobs — running and pending.[/dim]\n")

        for r in results:
            name = r["cluster"]
            if not r["live"]:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  "
                    f"[red]tunnel down[/red] [dim]— run `ctun -t {name} login`[/dim]\n"
                )
                continue
            if r["error"]:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  "
                    f"[yellow]could not query jobs: {r['error']}[/yellow]\n"
                )
                continue

            jobs = r["jobs"]
            active = sum(1 for j in jobs if j["source"] == "active")
            done = len(jobs) - active
            counts = f"[dim]({active} active"
            counts += f", {done} finished)[/dim]" if since_minutes else ")[/dim]"

            if not jobs:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  [dim]no jobs[/dim]\n"
                )
                continue

            table = Table(
                title=f"[bold cyan]{name}[/bold cyan]  {counts}",
                title_justify="left",
                show_header=True,
                header_style="bold",
                border_style="bright_black",
                expand=True,
            )
            table.add_column("Job ID", style="orange3", no_wrap=True)
            table.add_column("Name", overflow="ellipsis", max_width=32)
            table.add_column("State", no_wrap=True)
            table.add_column("Elapsed", no_wrap=True)
            table.add_column("Time limit", no_wrap=True)
            table.add_column("Partition", no_wrap=True)
            table.add_column("Nodes", justify="right", no_wrap=True)
            for j in jobs:
                state = j["state"].split()[0] if j["state"] else "—"
                table.add_row(
                    j["jobid"],
                    j["name"] or "—",
                    f"[{_state_style(j['state'])}]{state}[/]",
                    j["elapsed"] or "—",
                    j["timelimit"] or "—",
                    j["partition"] or "—",
                    j["nodes"] or "—",
                )
            cons.print(table)
            cons.print()
