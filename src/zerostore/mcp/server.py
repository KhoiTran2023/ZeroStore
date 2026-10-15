"""The one door: an MCP server that checks a token on every call.

Agents, the web UI and the CLI are all MCP clients, so every request passes
through here, is checked against its token's access level, and is logged.
Nothing here can change a fact: facts only change when sources change.

How the token arrives depends on the transport:
- stdio: the agent's MCP config sets ZEROSTORE_TOKEN for the server process.
- HTTP: each request carries `Authorization: Bearer <token>`.
The token is re-checked on every call, so revoking one takes effect at once.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from zerostore import __version__
from zerostore.access.tokens import AccessLevel, TokenError, TokenInfo
from zerostore.app import ZeroStore
from zerostore.mcp import descriptions as d

# Returns the raw token for the current request, or None.
TokenSource = Callable[[Context], "str | None"]

# Audit actions that describe a change to company knowledge (as opposed to
# housekeeping such as token changes), which is all `whats_changed` reports.
FACT_ACTIONS = ("document_added", "document_updated", "document_retired", "document_moved")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

_SPAN = re.compile(r"^\s*(\d+)\s*([dhw])\s*$", re.IGNORECASE)


def env_token_source(_: Context) -> str | None:
    return os.environ.get("ZEROSTORE_TOKEN")


def bearer_token_source(ctx: Context) -> str | None:
    headers = ctx.headers or {}
    value = headers.get("authorization") or headers.get("Authorization")
    return parse_bearer(value)


def parse_bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def parse_since(since: str, now: datetime | None = None) -> str:
    """Turn '7d', '24h', '2w', a date or a timestamp into a UTC ISO timestamp."""
    now = now or datetime.now(timezone.utc)
    span = _SPAN.match(since)
    if span:
        amount, unit = int(span.group(1)), span.group(2).lower()
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[
            unit
        ]
        moment = now - delta
    else:
        text = since.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise ToolError(
                f"couldn't read {since!r} as a time; use a date like 2026-10-01 or a span like 7d or 24h"
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _fact_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "document": row["title"],
        "source": row["source"],
        "source_url": row["source_url"],
        "updated_at": row["updated_at"],
    }


def tools_for(level: AccessLevel) -> set[str]:
    names = {"search", "get_fact", "whats_changed", "list_sources"}
    if level.can_propose:
        names.add("propose_fact")
    if level.is_owner:
        names |= {"token_create", "token_list", "token_revoke", "review_list", "review_decide", "sync_now"}
    return names


def build_server(
    zs: ZeroStore,
    token_source: TokenSource,
    *,
    only_level: AccessLevel | None = None,
) -> MCPServer:
    """Build the MCP server.

    `only_level` registers just the tools that level may use (stdio, where one
    process serves one token). Without it every tool is registered and each
    call is checked (HTTP, where one server serves many tokens).
    """
    server = MCPServer(
        name=d.SERVER_NAME,
        instructions=d.SERVER_INSTRUCTIONS,
        version=__version__,
    )
    allowed = tools_for(only_level) if only_level else None

    def register(name: str, description: str, read_only: bool):
        def decorator(fn):
            if allowed is None or name in allowed:
                server.add_tool(
                    fn, name=name, description=description, annotations=READ_ONLY if read_only else None
                )
            return fn

        return decorator

    def authorize(ctx: Context, action: str, detail: str | None = None) -> TokenInfo:
        try:
            caller = zs.tokens.verify(token_source(ctx))
        except TokenError as exc:
            zs.state.log_access(None, action, detail, allowed=False)
            raise ToolError(f"{exc}. Ask the ZeroStore owner for a token.") from None
        if action not in tools_for(caller.level):
            zs.state.log_access(caller.id, action, detail, allowed=False)
            raise ToolError(f"this token has the {caller.level.value} access level, which can't use {action}")
        zs.state.log_access(caller.id, action, detail, allowed=True)
        return caller

    # Read tools -----------------------------------------------------------

    @register("search", d.SEARCH, read_only=True)
    def search(query: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "search", query[:200])
        rows = zs.facts.search(
            query,
            include_confidential=caller.level.sees_confidential,
            limit=zs.config.retrieval.max_facts,
        )
        if not rows:
            return {"answer": "not sure", "facts": []}
        return {"facts": [_fact_view(r) for r in rows]}

    @register("get_fact", d.GET_FACT, read_only=True)
    def get_fact(id: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "get_fact", id)
        row = zs.facts.get(id, include_confidential=caller.level.sees_confidential)
        if row is None:
            raise ToolError(f"no fact with id {id}")
        return _fact_view(row)

    @register("whats_changed", d.WHATS_CHANGED, read_only=True)
    def whats_changed(since: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "whats_changed", since)
        changes = zs.state.audit_since(
            parse_since(since),
            actions=FACT_ACTIONS,
            include_confidential=caller.level.sees_confidential,
        )
        return {
            "changes": [
                {k: c[k] for k in ("at", "action", "doc_id", "source", "detail", "confidence")}
                for c in changes
            ]
        }

    @register("list_sources", d.LIST_SOURCES, read_only=True)
    def list_sources(ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "list_sources")
        return {"documents": zs.facts.sources(include_confidential=caller.level.sees_confidential)}

    # Agent tool -----------------------------------------------------------

    @register("propose_fact", d.PROPOSE_FACT, read_only=False)
    def propose_fact(fact: str, quote: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "propose_fact", fact[:200])
        if not zs.config.agent_proposals:
            raise ToolError("agent proposals are turned off in this ZeroStore")
        if not fact.strip():
            raise ToolError("the proposed fact is empty")
        item_id = zs.state.add_review_item(
            "agent_proposal", {"fact": fact.strip(), "quote": quote.strip()}, created_by=caller.id
        )
        return {
            "status": "waiting for approval",
            "review_id": item_id,
            "note": "A person will review this. It is not a fact until they approve it.",
        }

    # Setup tools (owner) --------------------------------------------------

    @register("token_create", d.TOKEN_CREATE, read_only=False)
    def token_create(name: str, level: AccessLevel, ctx: Context) -> dict[str, Any]:
        authorize(ctx, "token_create", f"{name} ({level.value})")
        try:
            raw, info = zs.tokens.create(name, level)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"id": info.id, "name": info.name, "level": info.level.value, "token": raw}

    @register("token_list", d.TOKEN_LIST, read_only=True)
    def token_list(ctx: Context) -> dict[str, Any]:
        authorize(ctx, "token_list")
        return {
            "tokens": [
                {
                    "id": t.id,
                    "name": t.name,
                    "level": t.level.value,
                    "created_at": t.created_at,
                    "last_used_at": t.last_used_at,
                }
                for t in zs.tokens.list()
            ]
        }

    @register("token_revoke", d.TOKEN_REVOKE, read_only=False)
    def token_revoke(id: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "token_revoke", id)
        if id == caller.id:
            raise ToolError(
                "you can't revoke the token you're using here; use `zerostore token revoke` instead"
            )
        if not zs.tokens.revoke(id):
            raise ToolError(f"no active token with id {id}")
        return {"revoked": id}

    @register("review_list", d.REVIEW_LIST, read_only=True)
    def review_list(ctx: Context) -> dict[str, Any]:
        authorize(ctx, "review_list")
        return {"items": zs.state.review_items("pending")}

    @register("review_decide", d.REVIEW_DECIDE, read_only=False)
    def review_decide(id: int, approve: bool, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "review_decide", f"{id} {'approve' if approve else 'reject'}")
        if not zs.state.decide_review_item(id, approve, decided_by=caller.id):
            raise ToolError(f"no pending review item with id {id}")
        zs.state.audit("review_approved" if approve else "review_rejected", detail=str(id))
        return {"id": id, "status": "approved" if approve else "rejected"}

    @register("sync_now", d.SYNC_NOW, read_only=False)
    def sync_now(ctx: Context) -> dict[str, Any]:
        from zerostore.librarian.ingestion import Ingestor, SyncBusy

        authorize(ctx, "sync_now")
        if not zs.config.sources.enabled():
            raise ToolError("no sources are set up yet")
        try:
            report = Ingestor(zs).sync()
        except SyncBusy:
            raise ToolError("a sync is already running; try again when it finishes") from None
        return {
            "summary": report.summary(),
            "changes": [
                {"action": c.action, "document": c.title, "source": c.source} for c in report.changes
            ],
            "problems": [
                {"source": r.source, "error": r.error, "warning": r.warning, "failed": r.failed}
                for r in report.sources
                if r.error or r.warning or r.failed
            ],
        }

    return server
