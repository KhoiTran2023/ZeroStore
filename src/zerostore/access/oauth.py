"""Sign-in for Claude's connectors: the OAuth flow the MCP spec uses.

claude.ai, Claude Desktop and the Claude phone apps add an MCP server the way they
add Gmail: you paste its address under Settings, Connectors, and Claude sends you to
the server to sign in. ZeroStore is that sign-in server. Claude registers itself
(dynamic client registration), sends your browser to `/authorize`, and ZeroStore's
web UI asks you, the owner, whether to allow it and at which access level. Claude
then swaps the one-time code (with PKCE) for an access token, which is an ordinary
ZeroStore token: it shows in your token list and revoking it there disconnects Claude.

Clients are kept in the state database so they survive restarts. Requests waiting
for your answer and the one-time codes live in memory for a few minutes.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from zerostore.access.tokens import AccessLevel, TokenStore
from zerostore.access.tokens import TokenError as ZeroStoreTokenError
from zerostore.store.state import StateDB

APPROVE_PATH = "/connect/approve"
REQUEST_TTL = 600  # seconds to answer the "Allow Claude?" page
CODE_TTL = 300  # seconds Claude has to swap the code for a token
MAX_CLIENTS = 200  # registration is open to anyone, so keep it bounded
LEVELS = (AccessLevel.DEFAULT, AccessLevel.CONFIDENTIAL)


class ApprovalError(Exception):
    """The sign-in request is unknown, expired or already answered."""


@dataclass
class Pending:
    """A connector's sign-in, waiting for the owner to allow or deny it."""

    id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created: float

    @property
    def name(self) -> str:
        return (self.client.client_name or "An app").strip()[:80]

    @property
    def returns_to(self) -> str:
        """Where the browser goes after the answer (the app's sign-in callback)."""
        return str(self.params.redirect_uri)


class ConnectorAuth:
    """The OAuth authorization server behind ZeroStore's MCP endpoint (the SDK's provider protocol)."""

    def __init__(self, state: StateDB, tokens: TokenStore, base_url: str):
        self.state = state
        self.tokens = tokens
        self.base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}
        self._codes: dict[str, tuple[AuthorizationCode, AccessLevel, str, str | None]] = {}

    # Clients --------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.state.query("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0]["info"]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if self.state.query("SELECT COUNT(*) AS n FROM oauth_clients")[0]["n"] >= MAX_CLIENTS:
            raise RegistrationError(
                "invalid_client_metadata", "too many apps are registered with this ZeroStore"
            )
        self.state.save_oauth_client(client_info.client_id or "", client_info.model_dump_json())

    # Asking the owner -----------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending = Pending(secrets.token_urlsafe(24), client, params, time.time())
        with self._lock:
            self._drop_old()
            self._pending[pending.id] = pending
        return f"{self.base_url}{APPROVE_PATH}?request={pending.id}"

    def pending(self, request_id: str) -> Pending:
        with self._lock:
            self._drop_old()
            found = self._pending.get(request_id or "")
        if found is None:
            raise ApprovalError("this sign-in expired or was already answered; start again from the app")
        return found

    def answer(self, request_id: str, *, allow: bool, level: str = "default", role: str | None = None) -> str:
        """The owner's answer: where to send the browser back to the app, with a code or a refusal."""
        if allow and level not in [x.value for x in LEVELS]:
            raise ApprovalError("a connector gets default or confidential access")
        with self._lock:
            self._drop_old()
            pending = self._pending.pop(request_id or "", None)
        if pending is None:
            raise ApprovalError("this sign-in expired or was already answered; start again from the app")
        params = pending.params
        if not allow:
            return construct_redirect_uri(
                pending.returns_to,
                error="access_denied",
                error_description="The ZeroStore owner didn't allow it.",
                state=params.state,
            )
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client.client_id or "",
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        with self._lock:
            self._codes[code.code] = (code, AccessLevel(level), pending.name, role or None)
        return construct_redirect_uri(pending.returns_to, code=code.code, state=params.state)

    def _drop_old(self) -> None:
        now = time.time()
        for key in [k for k, p in self._pending.items() if now - p.created > REQUEST_TTL]:
            del self._pending[key]
        for key in [k for k, (c, *_) in self._codes.items() if c.expires_at < now]:
            del self._codes[key]

    # Codes and tokens -----------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._lock:
            self._drop_old()
            found = self._codes.get(authorization_code)
        if found is None or found[0].client_id != client.client_id:
            return None
        return found[0]

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            found = self._codes.pop(authorization_code.code, None)  # each code works once
        if found is None:
            raise TokenError("invalid_grant", "this code was already used or has expired")
        _, level, name, role = found
        raw, _ = self.tokens.create(f"{name} (connector)", level, role=role)
        # A ZeroStore token doesn't expire; revoke it in the token list to disconnect the app.
        return OAuthToken(
            access_token=raw, token_type="Bearer", scope=" ".join(authorization_code.scopes) or None
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None  # access tokens don't expire, so none are handed out

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        raise TokenError("invalid_grant", "ZeroStore doesn't use refresh tokens")

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            info = self.tokens.verify(token)
        except ZeroStoreTokenError:
            return None
        return AccessToken(token=token, client_id=info.id, scopes=[info.level.value])

    async def revoke_token(self, token: Any) -> None:
        try:
            info = self.tokens.verify(token.token)
        except ZeroStoreTokenError:
            return
        if info.level != AccessLevel.OWNER:  # an app may disconnect itself, never the owner
            self.tokens.revoke(info.id)
