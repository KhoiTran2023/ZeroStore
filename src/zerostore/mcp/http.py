"""Serving the MCP server over HTTP (streamable HTTP at /mcp), with the web UI at /.

Every request to /mcp must carry a valid `Authorization: Bearer <token>`;
anything else gets a 401 before it reaches the MCP layer. Each tool call then
re-checks the token and its access level (see server.py). Claude's connectors get
their token by signing in (OAuth, see access/oauth.py); every 401 says where.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import (
    build_metadata,
    build_resource_metadata_url,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from zerostore.access.oauth import ConnectorAuth
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

    def __init__(
        self,
        app: ASGIApp,
        tokens: TokenStore,
        protected_prefix: str = MCP_PATH,
        resource_metadata: str | None = None,
    ):
        self.app = app
        self.tokens = tokens
        self.prefix = protected_prefix
        # Where Claude's connectors find out how to sign in (RFC 9728), named in every 401.
        self.challenge = 'Bearer realm="zerostore"' + (
            f', resource_metadata="{resource_metadata}"' if resource_metadata else ""
        )

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
            await _unauthorized(send, str(exc), self.challenge)
            return
        await self.app(scope, receive, send)


async def _unauthorized(send: Send, message: str, challenge: str) -> None:
    body = json.dumps({"error": "unauthorized", "message": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", challenge.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# The connector sign-in's endpoints (OAuth 2.1 for MCP), served next to /mcp.
OAUTH_PATHS = ("/authorize", "/token", "/register", "/revoke")
WELL_KNOWN = "/.well-known/oauth-"


class Router:
    """/mcp (and the server's lifespan) go to the MCP server, the connector sign-in's
    endpoints to the OAuth app, and every other path to the web UI."""

    def __init__(self, mcp: ASGIApp, web: ASGIApp, oauth: ASGIApp | None = None):
        self.mcp = mcp
        self.web = web
        self.oauth = oauth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "lifespan" or path == MCP_PATH or path.startswith(MCP_PATH + "/"):
            await self.mcp(scope, receive, send)
        elif self.oauth is not None and (path in OAUTH_PATHS or path.startswith(WELL_KNOWN)):
            await self.oauth(scope, receive, send)
        else:
            await self.web(scope, receive, send)


def connector_sign_in(zs: ZeroStore, mcp_url: str | None) -> tuple[ConnectorAuth, ASGIApp, str] | None:
    """The OAuth app that lets Claude's connectors sign in, with its provider and metadata URL.

    None when the address can't host it: OAuth needs HTTPS, except on this computer.
    """
    if not mcp_url:
        return None
    base = mcp_url[: -len(MCP_PATH)] if mcp_url.endswith(MCP_PATH) else mcp_url
    url = urlparse(base)
    if url.scheme != "https" and not is_loopback(url.hostname or ""):
        return None
    # AuthSettings keeps a path-less issuer without a trailing slash; issuers are compared exactly.
    urls = AuthSettings(issuer_url=base, resource_server_url=mcp_url, validate_token_resource=False)
    provider = ConnectorAuth(zs.state, zs.tokens, base, issuer=str(urls.issuer_url))
    registration = ClientRegistrationOptions(enabled=True)
    revocation = RevocationOptions(enabled=True)
    # Besides registering, apps may name a client ID metadata document (ChatGPT, Grok), and
    # every answer carries the issuer (RFC 9207), which ChatGPT checks.
    metadata = build_metadata(urls.issuer_url, None, registration, revocation).model_copy(
        update={
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        }
    )
    auth_routes = create_auth_routes(
        provider,
        issuer_url=urls.issuer_url,
        client_registration_options=registration,
        revocation_options=revocation,
    )
    routes = [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        *(r for r in auth_routes if getattr(r, "path", "") != "/.well-known/oauth-authorization-server"),
        *create_protected_resource_routes(
            resource_url=urls.resource_server_url,
            authorization_servers=[urls.issuer_url],
            resource_name="ZeroStore",
        ),
    ]
    return provider, Starlette(routes=routes), str(build_resource_metadata_url(urls.resource_server_url))


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
    scheduler_alive: Callable[[], bool] | None = None,
) -> ASGIApp:
    """The MCP server at /mcp, plus the web UI at / when `web_ui` is on and the config file is known."""
    server = build_server(zs, bearer_token_source)
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH, host=host, transport_security=host_check(host, behind_proxy, mcp_url)
    )
    if not (zs.config.web_ui and config_path):
        return BearerAuth(inner, zs.tokens)
    from zerostore.web.app import create_web_app

    # Signing in as a connector asks the owner on a web UI page, so it comes with the web UI.
    sign_in = connector_sign_in(zs, mcp_url)
    provider, oauth, metadata = sign_in if sign_in else (None, None, None)
    mcp = BearerAuth(inner, zs.tokens, resource_metadata=metadata)
    web = create_web_app(
        zs,
        config_path,
        bound_host=host,
        mcp_url=mcp_url,
        behind_proxy=behind_proxy,
        scheduled=scheduled,
        scheduler_alive=scheduler_alive,
        connectors=provider,
    )
    return Router(mcp, web, oauth)
