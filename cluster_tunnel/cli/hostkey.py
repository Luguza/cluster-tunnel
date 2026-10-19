"""Host-key pinning: hostkey."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli import configcmd


class HostKeyCommandsMixin:
    """Mixin providing the `hostkey` command."""

    @click.command("hostkey")
    @click.pass_obj
    @click.option(
        "--pin", "do_pin", is_flag=True,
        help="Pin the key the server presents now (shows the change, asks first).",
    )
    @click.option("-y", "--yes", "assume_yes", is_flag=True, help="With --pin: pin without asking.")
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def hostkey_command(self, do_pin: bool, assume_yes: bool, as_json: bool) -> None:
        """Show the host key a cluster presents, or pin it: ctun -t <cluster> hostkey [--pin].

        The key is fetched over a fresh connection that stops before logging in,
        so no password or OTP is needed, and compared with the key pinned for
        the cluster. Until a key is pinned, logins accept the key on first
        contact (StrictHostKeyChecking=accept-new).

        `--pin` stores the key in ctun's known_hosts file and sets
        `strict_host_key_checking: yes` and `known_hosts_file` in the cluster's
        config entry (keeping config.yaml.bak), so later logins refuse any other
        key. Compare the fingerprint with the one your cluster's operators
        publish before you confirm. Without a terminal, `--pin` only shows the
        change, unless -y/--yes; a key that differs from the pinned one can only
        be re-pinned from a terminal.

        Exits 1 when the server presents a key other than the pinned one.
        """
        from cluster_tunnel import api

        if assume_yes and not do_pin:
            raise click.UsageError("-y/--yes only goes with --pin.")
        if as_json and do_pin:
            raise click.UsageError("--json shows the key; use it without --pin.")

        config = self.load_config()
        name, cluster = self.resolve_cluster(config)
        try:
            status = api.host_key(name, config=config, config_path=self.config_path)
        except api.HostKeyError as exc:
            raise click.ClickException(str(exc)) from exc
        changed = bool(status.pinned) and not status.matches

        if as_json:
            import json

            click.echo(json.dumps(status.as_dict(), indent=2))
            if changed:
                raise SystemExit(1)
            return

        self._show_host_key(name, status)
        if not do_pin:
            if changed:
                raise click.ClickException(
                    f"'{name}' presents a different host key than the pinned one. Don't log in "
                    "until you know why; if its operators announced a new key, re-pin it with "
                    f"`ctun -t {name} hostkey --pin` from a terminal."
                )
            return
        self._pin_host_key(name, cluster, status, assume_yes, changed)

    def _show_host_key(self, name: str, status) -> None:
        offered = status.offered
        click.echo(f"Host key of '{name}' ({offered[0].host}):")
        for key in offered:
            click.echo(f"  {key.kind:<8} {key.fingerprint}")
        if status.pinned and status.matches:
            if status.strict:
                click.secho("pinned: logins accept only this key.", fg="green")
            else:
                click.secho(
                    f"matches the key in {status.known_hosts_file}, but logins still accept a new "
                    f"key on first contact; `ctun -t {name} hostkey --pin` makes them strict.",
                    fg="yellow",
                )
        elif status.pinned:
            click.secho("DIFFERENT from the pinned key:", fg="red", bold=True)
            for key in status.pinned:
                click.secho(f"  {key.kind:<8} {key.fingerprint}", fg="red")
        elif status.strict and status.known_hosts_file:
            click.secho(
                f"not in {status.known_hosts_file}: logins are refused until the key is pinned "
                f"with `ctun -t {name} hostkey --pin`.",
                fg="yellow",
            )
        elif status.strict:
            click.echo("checked strictly against ssh's own known_hosts; not pinned by ctun.")
        else:
            click.secho(
                "not pinned: logins accept the key on first contact. Pin it with "
                f"`ctun -t {name} hostkey --pin`.",
                fg="yellow",
            )

    def _pin_host_key(self, name: str, cluster, status, assume_yes: bool, changed: bool) -> None:
        """Show what pinning changes, then pin (after asking, unless --yes)."""
        from cluster_tunnel import api, configedit, hostkey
        from cluster_tunnel import config as config_mod

        if status.matches and status.strict:
            click.echo("Already pinned; nothing to do.")
            return

        path = config_mod.resolve_config_path(self.config_path)
        file = cluster.known_hosts_file or hostkey.default_file()
        try:
            edit = configedit.plan_host_key_pin(path, name, file)
        except configedit.EditError as exc:
            raise click.ClickException(f"Cannot pin the host key in {path}: {exc}.") from exc

        fingerprint = status.offered[0].fingerprint
        click.echo()
        click.echo(f"Pinning {fingerprint} for '{name}' in {file}.")
        if edit.changes:
            click.echo(f"Changing {path}:")
            click.echo()
            configcmd._echo_diff(edit.diff())
        click.echo()
        click.secho(
            "Compare the fingerprint with the one your cluster's operators publish before you confirm.",
            fg="yellow",
        )

        tty = configcmd._stdin_is_tty()
        if changed and not tty:
            raise click.ClickException(
                "The key differs from the pinned one; re-pin it from a terminal (--yes does "
                "not apply to a changed key). Nothing was pinned."
            )
        if not assume_yes or changed:
            if not tty:
                click.echo(f"Nothing pinned. Run `ctun -t {name} hostkey --pin --yes` to pin it.")
                return
            question = "Replace the pinned key with this one?" if changed else "Pin this key?"
            if not click.confirm(question, default=False):
                click.echo("Nothing pinned.")
                return

        try:
            api.pin_host_key(name, fingerprint, config_path=self.config_path)
        except (api.HostKeyError, api.ConfigError) as exc:
            raise click.ClickException(str(exc)) from exc
        backup = f" The previous config is in {path.name}.bak." if edit.changes else ""
        click.echo(f"✓ Pinned: logins to '{name}' now accept only {fingerprint}.{backup}")
