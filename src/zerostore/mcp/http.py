"""Serving the MCP server over HTTP (streamable HTTP at /mcp), with the web UI at /.

Every request to /mcp must carry a valid `Authorization: Bearer <token>`;
anything else gets a 401 before it reaches the MCP layer. Each tool call then
re-checks the token and its access level (see server.py).
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from zerostore.access.tokens import TokenError, TokenStore
from zerostore.app import ZeroStore
from zerostore.mcp.server import bearer_token_source, build_server, parse_bearer

MCP_PATH = "/mcp"


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class BearerAuth:
    """ASGI middleware: reject requests without a valid ZeroStore token."""

    def __init__(self, app: ASGIApp, tokens: TokenStore, protected_prefix: str = MCP_PATH):
        self.app = app
        self.tokens = tokens
        self.prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        header = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        try:
            self.tokens.verify(parse_bearer(header))
        except TokenError as exc:
            await _unauthorized(send, str(exc))
            return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send, message: str) -> None:
    body = json.dumps({"error": "unauthorized", "message": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="zerostore"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class Router:
    """/mcp (and the server's lifespan) go to the MCP server; every other path to the web UI."""

    def __init__(self, mcp: ASGIApp, web: ASGIApp):
        self.mcp = mcp
        self.web = web

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "lifespan" or path == MCP_PATH or path.startswith(MCP_PATH + "/"):
            await self.mcp(scope, receive, send)
        else:
            await self.web(scope, receive, send)


LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def host_check(host: str, behind_proxy: bool, mcp_url: str | None) -> TransportSecuritySettings | None:
    """Which Host headers /mcp answers.

    On a loopback address the MCP library only answers requests addressed to localhost,
    against DNS rebinding. Behind an HTTPS proxy or tunnel (Tailscale, Cloudflare, Caddy)
    on this machine, requests arrive addressed to the public name, so that name is allowed
    too. None keeps the library's own rule.
    """
    if not (behind_proxy and is_loopback(host)):
        return None
    public = urlparse(mcp_url or "")
    if not public.netloc or is_loopback(public.hostname or ""):
        # The public name isn't known (no --public-url). Every request still needs a token.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        allowed_hosts=[*LOOPBACK_HOSTS, public.netloc, f"{public.netloc}:*"],
        allowed_origins=[*LOOPBACK_ORIGINS, f"{public.scheme}://{public.netloc}"],
    )


def create_http_app(
    zs: ZeroStore,
    host: str = "127.0.0.1",
    *,
    config_path: Path | None = None,
    mcp_url: str | None = None,
    behind_proxy: bool = False,
    scheduled: bool = True,
) -> ASGIApp:
    """The MCP server at /mcp, plus the web UI at / when `web_ui` is on and the config file is known."""
    server = build_server(zs, bearer_token_source)
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH, host=host, transport_security=host_check(host, behind_proxy, mcp_url)
    )
    mcp = BearerAuth(inner, zs.tokens)
    if not (zs.config.web_ui and config_path):
        return mcp
    from zerostore.web.app import create_web_app

    web = create_web_app(
        zs, config_path, bound_host=host, mcp_url=mcp_url, behind_proxy=behind_proxy, scheduled=scheduled
    )
    return Router(mcp, web)
