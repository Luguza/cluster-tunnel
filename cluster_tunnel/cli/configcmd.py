"""Configuration command: config."""

from __future__ import annotations

import sys

import rich_click as click


def _stdin_is_tty() -> bool:
    """Whether a person can answer a prompt (an agent's shell can't)."""
    return sys.stdin.isatty()


def _echo_diff(lines: list[str]) -> None:
    """Print a unified diff of the config, colored like `git diff`."""
    for line in lines:
        if line.startswith(("+++", "---")):
            click.secho(line, bold=True)
        elif line.startswith("@@"):
            click.secho(line, fg="cyan")
        elif line.startswith(("+", "-")):
            click.secho(line, fg="green" if line[0] == "+" else "red")
        else:
            click.echo(line)


class ConfigCommandsMixin:
    """Mixin providing the `config` command."""

    @click.command("config")
    @click.pass_obj
    @click.option("-p", "--path", "show_path", is_flag=True, help="Print the config file path and exit.")
    @click.option("-s", "--show", "show_contents", is_flag=True, help="Print the resolved config and exit.")
    @click.option("--validate", "do_validate", is_flag=True, help="Validate the config and exit.")
    @click.option("-i", "--init", "do_init", is_flag=True, help="Create a starter config if none exists.")
    @click.option(
        "--migrate", "do_migrate", is_flag=True,
        help="Convert old-style entries to the current format (shows the change, asks first).",
    )
    @click.option("-y", "--yes", "assume_yes", is_flag=True, help="With --migrate: write without asking.")
    def config_command(
        self,
        show_path: bool,
        show_contents: bool,
        do_validate: bool,
        do_init: bool,
        do_migrate: bool,
        assume_yes: bool,
    ) -> None:
        """Open the config file in $EDITOR (or inspect it).

        Configs written for older ctun versions keep working unchanged.
        `--migrate` converts them to the current format, but only when you
        run it: it shows the change as a diff, asks before writing, and keeps
        the previous file as `config.yaml.bak`. Today that means turning each
        single-string cluster `description` into a list, which `ctun note`
        can then add to.
        """
        import yaml

        from cluster_tunnel import config as config_mod
        from cluster_tunnel import paths

        if paths.using_legacy_dirs():
            # stderr, so `--path` and `--show` stay machine-readable.
            click.secho(
                f"note: using the legacy folder {paths.legacy_config_dir()}. "
                f"Move it to {paths.default_config_dir()} to adopt the new location.",
                fg="yellow",
                err=True,
            )

        path = config_mod.resolve_config_path(self.config_path)

        if assume_yes and not do_migrate:
            raise click.UsageError("--yes only applies to --migrate.")

        if do_init:
            created = config_mod.setup_if_necessary(self.config_path)
            click.echo(f"Config ready at {created}")
            return

        if show_path:
            click.echo(str(path))
            return

        if not path.exists():
            raise click.UsageError(f"No config at {path}. Run `ctun config --init` first.")

        if do_migrate:
            self._migrate_config(path, assume_yes)
            return

        if do_validate:
            try:
                config = config_mod.load_config(self.config_path)
            except Exception as exc:  # noqa: BLE001
                raise click.ClickException(f"Invalid config: {exc}") from exc
            warnings = config_mod.validation_warnings(config, path)
            for warning in warnings:
                click.secho(f"warning: {warning}", fg="yellow", err=True)
            strings = [
                name for name, cluster in config.clusters.items()
                if isinstance(cluster.description, str) and cluster.description.strip()
            ]
            if strings:
                verb = "has" if len(strings) == 1 else "have"
                click.echo(
                    f"tip: {', '.join(strings)} {verb} a single-string description; "
                    "`ctun config --migrate` turns it into a list that `ctun note` can "
                    "add to (optional).",
                    err=True,
                )
            if warnings:
                raise click.ClickException(
                    f"{path} is structurally valid but has {len(warnings)} "
                    f"warning(s) — see above."
                )
            click.echo(f"OK — {path} is valid.")
            return

        if show_contents:
            config = config_mod.load_config(self.config_path)
            click.echo(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False).rstrip())
            return

        click.edit(filename=str(path))

    def _migrate_config(self, path, assume_yes: bool) -> None:
        """Show the migration diff, then write it (after asking, unless --yes)."""
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import configedit

        try:
            config_mod.load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Invalid config: {exc}") from exc
        try:
            edit = configedit.plan_migration(path)
        except configedit.EditError as exc:
            raise click.ClickException(f"Cannot migrate {path}: {exc}.") from exc
        if not edit.changes:
            click.echo(f"Nothing to migrate: {path} already uses the current format.")
            return

        click.echo(f"Migrating {path}:")
        for change in edit.changes:
            click.echo(f"  {change}")
        click.echo()
        _echo_diff(edit.diff())
        click.echo()
        click.secho(
            "ctun 0.5.0 and older can't read the migrated file: update ctun on every "
            "machine that uses this config.",
            fg="yellow",
        )

        if not assume_yes:
            if not _stdin_is_tty():
                click.echo("Nothing written. Run `ctun config --migrate --yes` to apply.")
                return
            if not click.confirm("Write these changes?", default=False):
                click.echo("Nothing written.")
                return
        backup = configedit.apply(edit, backup=True)
        click.echo(f"✓ Migrated {path}; the previous version is in {backup}.")
