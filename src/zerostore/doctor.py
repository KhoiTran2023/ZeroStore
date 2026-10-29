"""`zerostore doctor`: checks everything an agent needs before you add ZeroStore to it.

Each check says what it found and, when something is wrong, the one thing to do.
The address checks call the server the way ChatGPT, Grok and Claude do: over the
address you give them, with the public certificate authorities only, so a
self-signed certificate or a private `root.crt` shows up here first.
"""

from __future__ import annotations

import re
import socket
import ssl
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from zerostore.app import ZeroStore

OK, WARN, FAIL = "ok", "warn", "fail"
TIMEOUT = 10.0


@dataclass
class Check:
    group: str
    level: str
    says: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _settings(zs: ZeroStore, config_path: Path | None) -> list[Check]:
    from zerostore.librarian.ingestion import source_names

    cfg, group = zs.config, "settings"
    out = [Check(group, OK, "zerostore.yaml is valid")]
    if not source_names(cfg):
        out.append(
            Check(
                group,
                WARN,
                "no sources yet",
                "add a folder, Google Drive, GitHub or Linear on the setup page",
            )
        )
    if cfg.demo:
        out.append(Check(group, OK, "the demo's sources are simulated from local files; no keys needed"))
        return out + _service_settings(cfg, config_path)
    drive = cfg.sources.google_drive
    if drive and drive.folders:
        creds = Path(drive.credentials).expanduser() if drive.credentials else None
        if not creds or not creds.exists():
            out.append(
                Check(
                    group,
                    FAIL,
                    f"Google Drive has no credentials file ({drive.credentials or 'none set'})",
                    "use Sign in with Google on the setup page",
                )
            )
        else:
            out.append(Check(group, OK, f"Google Drive reads {len(drive.folders)} folder(s)"))
    github = cfg.sources.github
    if github and github.repos:
        if github.token:
            out.append(Check(group, OK, f"GitHub reads {len(github.repos)} repo(s) with a token"))
        else:
            out.append(
                Check(
                    group,
                    WARN,
                    "GitHub has no token, so only public repos can be read",
                    "set sources.github.token (for example ${GITHUB_TOKEN} in secrets.env)",
                )
            )
    linear = cfg.sources.linear
    if linear and linear.teams:
        if linear.api_key:
            out.append(Check(group, OK, f"Linear reads {len(linear.teams)} team(s)"))
        else:
            out.append(
                Check(group, FAIL, "Linear has no API key", "set sources.linear.api_key, kept in secrets.env")
            )
    return out + _service_settings(cfg, config_path)


def _service_settings(cfg, config_path: Path | None) -> list[Check]:
    from zerostore.service import shell_only_variables

    group, out = "settings", []
    if config_path and (names := shell_only_variables(config_path)):
        out.append(
            Check(
                group,
                WARN,
                f"only this terminal has {', '.join('$' + n for n in names)}; the background service won't",
                "save them on the settings page, which keeps them in secrets.env",
            )
        )
    if not cfg.web_ui:
        out.append(
            Check(
                group,
                WARN,
                "the web UI is off, so connectors (Claude, ChatGPT, Grok) can't be allowed in",
                "set web_ui: true",
            )
        )
    return out


def _services(zs: ZeroStore) -> list[Check]:
    from zerostore.services import OFF, RUNNING, STOPPED, check

    rows = check(zs)
    out = []
    if all(r["state"] == STOPPED for r in rows if r["name"] != "vector db"):
        out.append(
            Check(
                "services",
                FAIL,
                "the server isn't running, so agents can't reach ZeroStore and timed syncs don't run",
                "zerostore service install (keeps it running), or zerostore serve --http",
            )
        )
        rows = [r for r in rows if r["name"] == "vector db"]
    for row in rows:
        if row["state"] == RUNNING:
            out.append(Check("services", OK, f"{row['name']} is running ({row['detail']})"))
        elif row["state"] == OFF:
            out.append(Check("services", WARN, f"{row['name']} is off: {row['detail']}"))
        else:
            says = f"{row['name']} is {row['state']}: {row['detail']}"
            out.append(Check("services", FAIL, says, "restart the server"))
    return out


def _access(zs: ZeroStore) -> list[Check]:
    cfg, group = zs.config, "access"
    tokens = [t for t in zs.tokens.list() if t.level.value != "owner"]
    out = [Check(group, OK, f"{len(tokens)} agent token(s); every call needs one")]
    if cfg.tags and not cfg.roles:
        out.append(
            Check(
                group,
                WARN,
                f"tags exist ({', '.join(cfg.tags)}) but no roles, "
                "so only owner tokens see tagged information",
                "add a role under 'who sees what' on the settings page",
            )
        )
    for role, tags in cfg.roles.items():
        holders = sum(1 for t in tokens if t.role == role)
        out.append(Check(group, OK, f"role {role} sees {', '.join(tags)}; {holders} token(s) have it"))
    unused = [t for t in cfg.tags if not any(t in tags for tags in cfg.roles.values())]
    if cfg.roles and unused:
        out.append(
            Check(
                group,
                WARN,
                f"no role sees {', '.join(unused)}, so only owner tokens do",
                "add the tag to a role, or remove it",
            )
        )
    return out


def known_address(zs: ZeroStore) -> str | None:
    """Where the running server says it is, from its heartbeat."""
    from zerostore.services import last_beat

    beat = last_beat(zs)
    return beat.get("base") if beat else None


def _caused_by(exc: BaseException, kind: type[BaseException], text: str = "") -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind) or (text and text in str(seen)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


DOMAIN_FIX = (
    "point a domain name at this server and install with --domain <name>, or install with --ip "
    "on a server with a fixed public IP address (docs/connect-agents.md)"
)


def _address(base: str | None, client: httpx.Client) -> list[Check]:
    from zerostore.mcp.http import is_loopback

    group = "address"
    if not base:
        return [
            Check(
                group,
                WARN,
                "the address isn't checked while the server is stopped",
                "start it and run this again, or pass the address agents use: --url https://...",
            )
        ]
    base = base.rstrip("/")
    parts = urlparse(base)
    local = is_loopback(parts.hostname or "")
    out = []
    if parts.scheme == "https" and not local:
        out.append(Check(group, OK, f"agents use {base}/mcp over HTTPS"))
    elif local:
        out.append(
            Check(
                group,
                WARN,
                f"{base} works only for agents on this computer (Claude Code, Claude Desktop, Cursor)",
                "for ChatGPT, Grok and Claude's connectors, serve a public HTTPS address "
                "(docs/connect-agents.md)",
            )
        )
    else:
        out.append(
            Check(
                group,
                FAIL,
                f"{base} is plain HTTP on a network address; connectors refuse it",
                DOMAIN_FIX,
            )
        )
        return out

    try:
        mcp = client.get(f"{base}/mcp", headers={"Accept": "application/json, text/event-stream"})
    except httpx.HTTPError as exc:
        if _caused_by(exc, ssl.SSLCertVerificationError, "CERTIFICATE_VERIFY_FAILED"):
            out.append(
                Check(
                    group,
                    FAIL,
                    "the certificate isn't from a public authority (self-signed or a private root.crt), "
                    "and agents can't be told to trust it",
                    f"get a Let's Encrypt certificate: {DOMAIN_FIX}",
                )
            )
        elif _caused_by(exc, socket.gaierror):
            out.append(
                Check(
                    group,
                    FAIL,
                    f"{parts.hostname} has no DNS record, so no agent can find it",
                    "add an A record pointing at the server's public IP address (an Elastic IP on AWS)",
                )
            )
        else:
            out.append(
                Check(
                    group,
                    FAIL,
                    f"can't reach {base}/mcp: {exc}",
                    "check the server runs and the address, port and firewall are right",
                )
            )
        return out
    challenge = mcp.headers.get("www-authenticate", "")
    if mcp.status_code == 421:
        out.append(
            Check(
                group,
                FAIL,
                f"the server refuses the host name {parts.netloc} (421)",
                f"restart it with --public-url {base}",
            )
        )
        return out
    if mcp.status_code != 401:
        out.append(
            Check(
                group,
                FAIL,
                f"{base}/mcp answered {mcp.status_code} without asking for a token",
                "make sure this address reaches `zerostore serve --http`, not another program",
            )
        )
        return out
    out.append(Check(group, OK, f"{base}/mcp answers and asks every caller for a token"))
    if "resource_metadata=" not in challenge:
        out.append(
            Check(
                group,
                FAIL,
                "connector sign-in is off, so ChatGPT, Grok and Claude's connectors can't sign in",
                f"restart with --public-url {base}, with the web UI on"
                if parts.scheme == "https"
                else "it turns on over HTTPS, or on this computer",
            )
        )
        return out
    out.extend(_sign_in(base, challenge, client))
    return out


def _sign_in(base: str, challenge: str, client: httpx.Client) -> list[Check]:
    group = "sign-in"
    found = re.search(r'resource_metadata="([^"]+)"', challenge)
    try:
        where = found.group(1) if found else f"{base}/.well-known/oauth-protected-resource"
        resource = client.get(where).json()
        meta = client.get(f"{base}/.well-known/oauth-authorization-server").json()
    except (httpx.HTTPError, ValueError) as exc:
        return [Check(group, FAIL, f"the sign-in details can't be read: {exc}", "restart the server")]
    issuer = str(meta.get("issuer", "")).rstrip("/")
    if issuer != base or str(resource.get("resource", "")).rstrip("/") != f"{base}/mcp":
        return [
            Check(
                group,
                FAIL,
                f"the server calls itself {issuer or 'nothing'}, not {base}; agents stop at the first step",
                f"restart with --public-url {base}",
            )
        ]
    if not (
        meta.get("client_id_metadata_document_supported")
        and meta.get("authorization_response_iss_parameter_supported")
    ):
        return [
            Check(
                group,
                WARN,
                "the sign-in lacks what ChatGPT needs (client documents, iss)",
                "update ZeroStore and restart",
            )
        ]
    return [Check(group, OK, "Claude, ChatGPT and Grok can sign in here; you allow each one on the web UI")]


def run(
    zs: ZeroStore,
    config_path: Path | None = None,
    url: str | None = None,
    client: httpx.Client | None = None,
) -> list[Check]:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    try:
        return [
            *_settings(zs, config_path),
            *_services(zs),
            *_address(url or known_address(zs), client),
            *_access(zs),
        ]
    finally:
        if own:
            client.close()
