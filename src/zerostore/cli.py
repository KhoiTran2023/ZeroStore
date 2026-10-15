"""The `zerostore` command.

Commands so far: init, serve, sync, token, status and connect. Demo, report,
service and reindex arrive with later stages.

Admin commands (init, token, status) work directly on this machine's state
file: whoever can read ~/.zerostore already holds the owner token, and the
very first token has to be made somehow. Everything agents do goes through
the MCP server.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import typer

from zerostore import __version__
from zerostore.access.tokens import AccessLevel
from zerostore.agents import rules
from zerostore.config import CONFIG_FILENAME, Config, ConfigError, home_dir, load_config
from zerostore.mcp.descriptions import AGENT_RULE

app = typer.Typer(
    help="ZeroStore: tell your sources once. Every agent knows.",
    no_args_is_help=True,
    add_completion=False,
)
token_app = typer.Typer(help="Create, list and revoke access tokens.", no_args_is_help=True)
app.add_typer(token_app, name="token")

OWNER_TOKEN_FILE = "owner-token"

CONFIG_TEMPLATE = """\
# ZeroStore settings. Only `sources` is required.
# Full reference: zerostore.example.yaml in the ZeroStore repository.

sources: {{}}
  # google_drive: {{ folders: ["Company/Decisions"] }}
  # github:       {{ repos: ["acme/app"] }}
  # linear:       {{ teams: ["Product"] }}

sync:
  schedule: "0 6 * * *"   # daily at 06:00

ai:
  decision_engine: llm      # jev once you add a Jev key
  # jev_api_key: ${{JEV_API_KEY}}
  # llm_provider: anthropic
  # llm_api_key: ${{LLM_API_KEY}}

servers:
  host: localhost
  vectordb: {home}/facts.lance
  statedb: {home}/state.sqlite
"""


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _open(config_path: Path | None, quiet: bool = True):
    from zerostore.app import ZeroStore

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
    try:
        return ZeroStore.open(config, quiet=quiet)
    except RuntimeError as exc:
        _fail(str(exc))


def _open_tokens(config_path: Path | None):
    """Just the token store, for commands that don't touch facts."""
    from zerostore.access.tokens import TokenStore
    from zerostore.store.state import StateDB

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc))
    return TokenStore(StateDB(config.servers.statedb_path()))


def _write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


ConfigOption = typer.Option(None, "--config", "-c", help="Path to zerostore.yaml.")


@app.callback(invoke_without_command=True)
def _root(version: bool = typer.Option(False, "--version", help="Show the version and exit.")) -> None:
    if version:
        typer.echo(f"zerostore {__version__}")
        raise typer.Exit()


@app.command()
def init(
    home: Path = typer.Option(None, help="Where ZeroStore keeps its files (default ~/.zerostore)."),
    rule: bool = typer.Option(
        None,
        "--rule/--no-rule",
        help="Add the 'check ZeroStore first' line to agent settings files here, without asking.",
    ),
) -> None:
    """Set up ZeroStore: settings file, data folders and your owner token."""
    home = (home or home_dir()).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, stat.S_IRWXU)
    config_path = home / CONFIG_FILENAME

    if config_path.exists():
        typer.echo(f"Using existing settings at {config_path}")
    else:
        _write_private(config_path, CONFIG_TEMPLATE.format(home=home))
        typer.echo(f"Created settings at {config_path}")

    zs = _open(config_path)
    token_path = home / OWNER_TOKEN_FILE
    owners = [t for t in zs.tokens.list() if t.level is AccessLevel.OWNER]
    if owners and token_path.exists():
        typer.echo(f"Your owner token is already saved at {token_path}")
    else:
        raw, info = zs.tokens.create("owner", AccessLevel.OWNER)
        _write_private(token_path, raw + "\n")
        typer.echo(
            f"Created your owner token ({info.id}) and saved it at {token_path} (readable only by you)."
        )
    zs.close()

    _offer_rule(Path.cwd(), rule)

    typer.echo("")
    typer.echo("Next: give each agent its own token, then connect it. For Claude Code:")
    typer.echo("  zerostore token create --name claude-code --level confidential")
    typer.echo("  claude mcp add zerostore -e ZEROSTORE_TOKEN=<that token> -- zerostore serve")


def _offer_rule(directory: Path, rule: bool | None) -> None:
    targets = rules.detect_agent_files(directory) or [directory / "CLAUDE.md"]
    pending = [p for p in targets if not rules.has_rule(p)]
    if not pending:
        return
    if rule is None:
        if not sys.stdin.isatty():
            return
        names = ", ".join(p.name for p in pending)
        typer.echo(
            f'\nAgents work better when told to check ZeroStore. Add this line to {names}?\n  "{AGENT_RULE}"'
        )
        rule = typer.confirm("Add it", default=True)
    if rule:
        for path in pending:
            rules.add_rule(path)
            typer.echo(f"Added the ZeroStore line to {path} (remove it with `zerostore connect --undo`).")


@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Serve over HTTP at /mcp instead of stdio."),
    host: str = typer.Option(None, help="HTTP address to bind (default from config: localhost)."),
    port: int = typer.Option(None, help="HTTP port (default from config: 8765)."),
    tls_cert: Path = typer.Option(None, help="TLS certificate file, to serve HTTPS directly."),
    tls_key: Path = typer.Option(None, help="TLS private key file."),
    behind_https_proxy: bool = typer.Option(
        False, help="Allow plain HTTP on a non-local address because a proxy in front terminates HTTPS."
    ),
    schedule: bool = typer.Option(True, "--schedule/--no-schedule", help="Run timed syncs in this process."),
    config: Path = ConfigOption,
) -> None:
    """Run the MCP server (stdio for one agent, or HTTP for many), plus timed syncs."""
    zs = _open(config, quiet=False)
    if schedule and zs.config.sources.enabled():
        from zerostore.librarian.ingestion import Ingestor
        from zerostore.scheduler.runner import SyncScheduler

        SyncScheduler(Ingestor(zs), zs.config.sync.schedule).start()
    if not http:
        _serve_stdio(zs)
        return

    from zerostore.mcp.http import create_http_app, is_loopback

    host = host or zs.config.servers.host
    port = port or zs.config.servers.port
    if bool(tls_cert) != bool(tls_key):
        _fail("pass both --tls-cert and --tls-key, or neither")
    if not is_loopback(host) and not tls_cert and not behind_https_proxy:
        _fail(
            f"refusing to serve plain HTTP on {host}: tokens would cross the network unencrypted. "
            "Pass --tls-cert/--tls-key, or --behind-https-proxy if Caddy, a load balancer or a tunnel "
            "handles HTTPS in front."
        )
    import uvicorn

    scheme = "https" if tls_cert else "http"
    typer.echo(f"ZeroStore MCP server on {scheme}://{host}:{port}/mcp", err=True)
    uvicorn.run(
        create_http_app(zs, host=host),
        host=host,
        port=port,
        ssl_certfile=str(tls_cert) if tls_cert else None,
        ssl_keyfile=str(tls_key) if tls_key else None,
        log_level="warning",
    )


def _serve_stdio(zs) -> None:
    from zerostore.access.tokens import TokenError
    from zerostore.mcp.server import build_server, env_token_source

    try:
        caller = zs.tokens.verify(os.environ.get("ZEROSTORE_TOKEN"))
    except TokenError as exc:
        _fail(
            f"{exc}. Set ZEROSTORE_TOKEN in the agent's MCP settings "
            "(create one with `zerostore token create`)."
        )
    build_server(zs, env_token_source, only_level=caller.level).run("stdio")


@app.command()
def sync(
    source: list[str] = typer.Option(
        None, "--source", "-s", help="Only this source (google_drive, github, linear). Repeatable."
    ),
    config: Path = ConfigOption,
) -> None:
    """Check every source for changes now and update the fact store."""
    from zerostore.librarian.ingestion import Ingestor, SyncBusy

    zs = _open(config, quiet=False)
    if not zs.config.sources.enabled():
        _fail(f"no sources are set up yet; add them under `sources:` in {CONFIG_FILENAME}")
    try:
        report = Ingestor(zs).sync(only=source or None)
    except SyncBusy as exc:
        _fail(f"{exc}; try again when it finishes")
    except ValueError as exc:
        _fail(str(exc))
    for change in report.changes:
        mark = {"added": "+", "updated": "~", "retired": "-"}[change.action]
        typer.echo(f"  {mark} {change.title}  ({change.source}, {change.action})")
    for result in report.sources:
        if result.error:
            typer.secho(f"  ! {result.source}: {result.error}", fg=typer.colors.RED, err=True)
        if result.warning:
            typer.secho(f"  ! {result.source}: {result.warning}", fg=typer.colors.YELLOW, err=True)
        for failure in result.failed:
            typer.secho(f"  ! {result.source}: couldn't read {failure}", fg=typer.colors.YELLOW, err=True)
    typer.echo(report.summary())
    if not report.ok:
        raise typer.Exit(1)


@token_app.command("create")
def token_create(
    name: str = typer.Option(..., help="A label for who uses it, e.g. cursor-laptop."),
    level: AccessLevel = typer.Option(AccessLevel.DEFAULT, help="default, confidential or owner."),
    config: Path = ConfigOption,
) -> None:
    """Create a token. It is shown once; store it in the agent's settings."""
    tokens = _open_tokens(config)
    try:
        raw, info = tokens.create(name, level)
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(
        f"Created token {info.id} ({info.name}, {info.level.value}). Copy it now; it won't be shown again:"
    )
    typer.echo(raw)


@token_app.command("list")
def token_list(
    all_: bool = typer.Option(False, "--all", help="Include revoked tokens."),
    config: Path = ConfigOption,
) -> None:
    """List tokens (never their secret values)."""
    tokens = _open_tokens(config).list(include_revoked=all_)
    if not tokens:
        typer.echo("No tokens yet. Run `zerostore init`.")
        return
    for t in tokens:
        state = f"revoked {t.revoked_at}" if t.revoked_at else f"last used {t.last_used_at or 'never'}"
        typer.echo(f"{t.id}  {t.level.value:<12} {t.name:<24} {state}")


@token_app.command("revoke")
def token_revoke(
    token_id: str = typer.Argument(..., help="The token's id from `zerostore token list`."),
    config: Path = ConfigOption,
) -> None:
    """Revoke a token; anything using it loses access immediately."""
    if not _open_tokens(config).revoke(token_id):
        _fail(f"no active token with id {token_id}")
    typer.echo(f"Revoked {token_id}.")


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    config: Path = ConfigOption,
) -> None:
    """Show what ZeroStore holds and what's waiting for you."""
    zs = _open(config)
    cfg: Config = zs.config
    info = {
        "version": __version__,
        "sources_configured": cfg.sources.enabled(),
        "documents": len(zs.facts.sources(include_confidential=True)),
        "chunks": zs.facts.count(),
        "active_tokens": len(zs.tokens.list()),
        "pending_reviews": len(zs.state.review_items("pending")),
        "sync_schedule": cfg.sync.schedule,
        "last_sync": _last_sync(zs),
        "fact_store": cfg.servers.vectordb_uri(),
        "state_db": str(cfg.servers.statedb_path()),
    }
    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return
    typer.echo(f"ZeroStore {info['version']}")
    typer.echo(f"  Sources:          {', '.join(info['sources_configured']) or 'none yet'}")
    typer.echo(f"  Documents:        {info['documents']} ({info['chunks']} chunks)")
    typer.echo(f"  Active tokens:    {info['active_tokens']}")
    typer.echo(f"  Waiting review:   {info['pending_reviews']}")
    typer.echo(f"  Sync schedule:    {info['sync_schedule']}")
    last = info["last_sync"]
    typer.echo(f"  Last sync:        {last['finished_at'] + ' · ' + last['summary'] if last else 'never'}")
    typer.echo(f"  Fact store:       {info['fact_store']}")


def _last_sync(zs) -> dict | None:
    raw = zs.state.get_meta("last_sync")
    if not raw:
        return None
    from zerostore.librarian.ingestion import SyncReport

    report = SyncReport.from_json(raw)
    return {"finished_at": report.finished_at, "summary": report.summary(), "ok": report.ok}


@app.command()
def connect(
    agent: str = typer.Argument("claude", help="claude, codex, cursor or agents."),
    undo: bool = typer.Option(False, "--undo", help="Remove the ZeroStore line instead."),
    directory: Path = typer.Option(
        Path("."), "--dir", help="Project folder holding the agent's settings file."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask first."),
) -> None:
    """Add (or remove) the 'check ZeroStore first' line in an agent's settings file."""
    if agent not in rules.AGENT_FILES:
        _fail(f"unknown agent {agent!r}; choose from {', '.join(rules.AGENT_FILES)}")
    path = directory.expanduser() / rules.AGENT_FILES[agent]
    if undo:
        if rules.remove_rule(path):
            typer.echo(f"Removed the ZeroStore line from {path}.")
        else:
            typer.echo(f"No ZeroStore line in {path}; nothing to remove.")
        return
    if rules.has_rule(path):
        typer.echo(f"{path} already has the ZeroStore line.")
        return
    if not yes:
        typer.echo(f'This adds one line to {path}:\n  "{AGENT_RULE}"')
        if not typer.confirm("Add it", default=True):
            raise typer.Exit()
    rules.add_rule(path)
    typer.echo(f"Added the ZeroStore line to {path}.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
