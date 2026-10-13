"""Command execution through the tunnel: run."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli.errors import emit_marker, fail
from cluster_tunnel.constants import ExitCode


#: Fraction of the session budget at/above which `run` warns before the hard block.
_NEAR_LIMIT_FRACTION = 0.8


def _num(x: float) -> str:
    """Format a budget figure compactly (1 decimal, no trailing .0)."""
    return f"{round(x, 1):g}"


def _block_code(decision) -> ExitCode:
    """Classify a blocked budget decision into its exit code.

    A decision carrying a usage *number* (``used``/``limit`` both set) was blocked
    because the session is at/over budget; otherwise the budget could not be
    verified and was fail-closed.
    """
    if decision.used is not None and decision.limit is not None:
        return ExitCode.BUDGET_EXHAUSTED
    return ExitCode.BUDGET_GUARD_ERROR


class ExecutionCommandsMixin:
    """Mixin providing the `run` command."""

    @click.command("run", context_settings={"ignore_unknown_options": True})
    @click.pass_obj
    @click.option("--tty", is_flag=True, help="Allocate a pseudo-TTY (for interactive remote programs).")
    @click.option("-n", "--dry-run", is_flag=True, help="Run the budget pre-flight check only; don't execute.")
    @click.option("--project", default=None, help="Project name stored in the record of each job this command submits.")
    @click.option("--note", default=None, help="Note stored in the record of each job this command submits.")
    @click.argument("command", nargs=-1, type=click.UNPROCESSED)
    def run_command(
        self,
        tty: bool,
        dry_run: bool,
        project: str | None,
        note: str | None,
        command: tuple[str, ...],
    ) -> None:
        """Run a command on the cluster: ctun -t <cluster> run -- <cmd...>.

        For each `Submitted batch job <N>` in the output, ctun stores a job record
        on the cluster: `--project`, `--note`, and the facts it collects itself
        (commit, uncommitted files, working directory, ...), and adds the job ID
        to the command's line in `ctun logs`. The command's output and exit code
        stay as they are.
        """
        import shlex

        from cluster_tunnel import budget as budget_mod
        from cluster_tunnel import cmdlog
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import records, ssh

        if not command:
            raise click.UsageError("Provide a command after `--`, e.g. `run -- squeue --me`.")

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)

        if not ssh.is_live(spec):
            fail(
                f"No live tunnel for '{name}'. Run `ctun -t {name} login` "
                f"(or `ctun -t {name} login --interactive`) first.",
                ExitCode.LOGIN_REQUIRED,
            )

        if note:
            _check_note(spec, note, project)

        tokens = list(command)
        config_path = config_mod.resolve_config_path(self.config_path)
        decision = budget_mod.decide(config, name, spec, tokens, config_path)

        if dry_run:
            verdict = "ALLOW" if decision.allowed else "BLOCK"
            click.echo(f"[dry-run] {verdict} on {name}: {shlex.join(tokens)}")
            if decision.used is not None:
                click.echo(f"  budget: {decision.used} / {decision.limit} {decision.unit}")
            click.echo(f"  reason: {decision.reason}")
            # Exit with the would-be failure code so `run -n` is a usable preflight.
            if not decision.allowed:
                code = _block_code(decision)
                emit_marker(code)
                raise SystemExit(int(code))
            return

        if not decision.allowed:
            # Budget exhausted (probe returned a number at/over the limit) gets a
            # specific, actionable message; other blocks (e.g. probe fail-closed)
            # fall back to the reason text.
            if decision.used is not None and decision.limit is not None:
                over = decision.used - decision.limit
                fail(
                    f"Submission blocked on '{name}': compute budget exhausted.\n"
                    f"  used {_num(decision.used)} / {_num(decision.limit)} {decision.unit} "
                    f"(over by {_num(over)} {decision.unit})\n"
                    f"  '{shlex.join(tokens)}' was NOT submitted.\n"
                    f"  Free budget by cancelling queued jobs, or start a fresh "
                    f"session with `ctun -t {name} login`.",
                    ExitCode.BUDGET_EXHAUSTED,
                )
            fail(
                f"Submission blocked on '{name}': {decision.reason}. Command not submitted.",
                ExitCode.BUDGET_GUARD_ERROR,
            )

        # When a probe actually ran (guarded command + a limit), show remaining
        # budget before the command's own output. Goes to stderr so it never
        # mixes into the command's stdout. Once usage crosses the near-limit
        # threshold, switch to a yellow warning so an agent can pace itself
        # before the hard block — but still let the command run.
        if decision.used is not None and decision.limit is not None:
            remaining = decision.limit - decision.used
            base = (
                f"{_num(decision.used)} / {_num(decision.limit)} {decision.unit} used"
                f" · {_num(remaining)} {decision.unit} remaining"
            )
            if decision.limit > 0 and decision.used / decision.limit >= _NEAR_LIMIT_FRACTION:
                pct = 100 * decision.used / decision.limit
                click.secho(f"budget: approaching limit — {pct:.0f}% used · {base}",
                            fg="yellow", err=True)
            else:
                click.secho(f"budget: {base}", fg="bright_black", err=True)

        # Log the command (not its output) so `status` can show activity.
        entry_id = cmdlog.record(name, tokens)
        records_on = config.clusters[name].job_records and not tty
        if (project or note) and not records_on:
            reason = "--tty hides the output" if tty else "job_records is off for this cluster"
            click.secho(f"ctun: no job record written ({reason}).", fg="yellow", err=True)
        if tty:
            raise SystemExit(ssh.run(spec, tokens, tty=True))

        returncode, output = ssh.run_capturing(spec, tokens)
        job_ids = records.submitted_job_ids(output)
        if job_ids:
            cmdlog.record_jobs(name, entry_id, job_ids)
        if records_on:
            _write_records(spec, job_ids, project=project, note=note, command=tokens)
        raise SystemExit(returncode)


def _check_note(spec, note: str, project: str | None) -> None:
    """Refuse the run (exit 13) if ``note`` names a commit or job the cluster doesn't know."""
    from cluster_tunnel import notes

    try:
        unknown = notes.unknown_references(spec, note, project)
    except RuntimeError as exc:
        fail(f"Not run: {exc}", ExitCode.NOTE_REFERENCE_INVALID)
    if unknown:
        fail(
            "Not run: the note names references the cluster does not know: "
            + ", ".join(unknown)
            + ".\n  Commit references are looked up in the repos of this project's earlier"
            " records. Fix or drop them, or leave the note out.",
            ExitCode.NOTE_REFERENCE_INVALID,
        )


def _write_records(spec, job_ids: list[str], *, project: str | None, note: str | None, command: list[str]) -> None:
    """Store a record for each of ``job_ids``; a failure only warns, never fails the run."""
    from cluster_tunnel import facts as facts_mod
    from cluster_tunnel import records

    for job_id in job_ids:
        facts = facts_mod.collect(spec, job_id)
        for warning in facts.warnings:
            click.secho(f"ctun: {warning}", fg="yellow", err=True)
        record = records.build(job_id, facts, project=project, note=note, command=command)
        try:
            records.write(spec, record)
        except RuntimeError as exc:
            click.secho(f"ctun: {exc}", fg="yellow", err=True)
            continue
        click.secho(f"ctun: recorded job {job_id}", fg="bright_black", err=True)
