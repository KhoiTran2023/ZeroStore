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
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import anyio
import mcp_types as types
from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, create_model

from zerostore import __version__
from zerostore.access.roles import hidden_tags, key_tags, role_problem
from zerostore.access.tokens import AccessLevel, TokenError, TokenInfo
from zerostore.app import ZeroStore
from zerostore.decisions.select import Engines
from zerostore.mcp import descriptions as d
from zerostore.store.embeddings import EmbeddingError

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
    view = {
        "id": row["id"],
        "text": row["text"],
        "document": row["title"],
        "source": row["source"],
        "source_url": row["source_url"],
        "updated_at": row["updated_at"],
    }
    if "score" in row:
        view["score"] = round(float(row["score"]), 3)
    if row.get("superseded_by"):
        view["status"] = "outdated: a newer document replaced this fact; search again for the current one"
    return view


class NotAsked(BaseModel):
    """The person wasn't asked about a proposal (see `ask_the_person`)."""

    reason: str


def _supports_elicitation(ctx: Context) -> bool:
    try:
        session = ctx.request_context.session
        return session.check_client_capability(
            types.ClientCapabilities(elicitation=types.ElicitationCapability())
        )
    except Exception:  # no session details (e.g. some test harnesses): assume it can't ask
        return False


def tools_for(level: AccessLevel) -> set[str]:
    names = {"search", "get_fact", "fetch", "whats_changed", "list_sources"}
    if level.can_propose:
        names.add("propose_fact")
    if level.is_owner:
        names |= {
            "token_create",
            "token_list",
            "token_revoke",
            "token_set_role",
            "review_list",
            "review_decide",
            "sync_now",
            "status",
            "activity",
        }
    return names


def build_server(
    zs: ZeroStore,
    token_source: TokenSource,
    *,
    only_level: AccessLevel | None = None,
    engines: Engines | None = None,
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

    from zerostore.librarian.retrieval import Retriever
    from zerostore.librarian.review import ReviewError, before_after, decide, describe

    retriever = Retriever(zs, engines)

    def register(name: str, description: str, read_only: bool):
        def decorator(fn):
            if allowed is None or name in allowed:
                server.add_tool(
                    fn, name=name, description=description, annotations=READ_ONLY if read_only else None
                )
            return fn

        return decorator

    def authorize_logged(ctx: Context, action: str, detail: str | None = None) -> tuple[TokenInfo, int]:
        """Check the token and its level, and log the request. Returns the caller and the log row."""
        try:
            caller = zs.tokens.verify(token_source(ctx))
        except TokenError as exc:
            zs.state.log_access(None, action, detail, allowed=False)
            raise ToolError(f"{exc}. Ask the ZeroStore owner for a token.") from None
        if action not in tools_for(caller.level):
            zs.state.log_access(caller.id, action, detail, allowed=False)
            raise ToolError(f"this token has the {caller.level.value} access level, which can't use {action}")
        return caller, zs.state.log_access(caller.id, action, detail, allowed=True)

    def authorize(ctx: Context, action: str, detail: str | None = None) -> TokenInfo:
        return authorize_logged(ctx, action, detail)[0]

    def hidden(caller: TokenInfo) -> list[str]:
        """The tags whose facts this caller's role doesn't cover."""
        known = {t for key in zs.state.access_keys() for t in key_tags(key)}
        return hidden_tags(zs.config, caller.role, owner=caller.level.is_owner, known=known)

    # Read tools -----------------------------------------------------------

    @register("search", d.SEARCH, read_only=True)
    def search(query: str, ctx: Context) -> dict[str, Any]:
        caller, row = authorize_logged(ctx, "search", query[:200])
        started = time.perf_counter()
        try:
            answer = retriever.answer(
                query, include_confidential=caller.level.sees_confidential, hidden_tags=hidden(caller)
            )
        except EmbeddingError as exc:
            raise ToolError(f"search is unavailable right now: {exc}") from None
        finally:
            zs.state.access_took(row, round((time.perf_counter() - started) * 1000))
        if answer.not_sure:
            return {
                "answer": "not sure",
                "facts": [],
                "results": [],
                "note": "Nothing in ZeroStore answers this reliably. Ask the user instead of guessing.",
            }
        # `results` is the list ChatGPT's search and deep research read (id, title, url).
        results = [{"id": r["id"], "title": r["title"], "url": r["source_url"]} for r in answer.facts]
        return {"facts": [_fact_view(r) for r in answer.facts], "results": results}

    @register("get_fact", d.GET_FACT, read_only=True)
    def get_fact(id: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "get_fact", id)
        row = zs.facts.get(
            id, include_confidential=caller.level.sees_confidential, hidden_tags=hidden(caller)
        )
        if row is None:
            raise ToolError(f"no fact with id {id}")
        return _fact_view(row)

    @register("fetch", d.FETCH, read_only=True)
    def fetch(id: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "fetch", id)
        row = zs.facts.get(
            id, include_confidential=caller.level.sees_confidential, hidden_tags=hidden(caller)
        )
        if row is None:
            raise ToolError(f"no fact with id {id}")
        return {
            "id": row["id"],
            "title": row["title"],
            "text": row["text"],
            "url": row["source_url"],
            "metadata": {"source": row["source"], "updated_at": row["updated_at"]},
        }

    @register("whats_changed", d.WHATS_CHANGED, read_only=True)
    def whats_changed(since: str, ctx: Context) -> dict[str, Any]:
        caller = authorize(ctx, "whats_changed", since)
        changes = zs.state.audit_since(
            parse_since(since),
            actions=FACT_ACTIONS,
            include_confidential=caller.level.sees_confidential,
            hidden_tags=hidden(caller),
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
        documents = zs.facts.sources(
            include_confidential=caller.level.sees_confidential, hidden_tags=hidden(caller)
        )
        return {"documents": documents}

    # Agent tool -----------------------------------------------------------

    def ask_the_person(fact: str, quote: str, ctx: Context) -> Elicit[BaseModel] | NotAsked:
        """Ask the person in the agent app (not the model) whether to save the fact.

        Runs before the tool body, so it checks the token itself: a bad or
        auto-approving token, or an app that can't show the question, is not asked.
        """
        try:
            caller = zs.tokens.verify(token_source(ctx))
        except TokenError:
            return NotAsked(reason="no valid token")
        if "propose_fact" not in tools_for(caller.level):
            return NotAsked(reason="this level can't propose")
        if caller.auto_approve or not zs.config.agent_proposals or not fact.strip():
            return NotAsked(reason="not needed")
        if not _supports_elicitation(ctx):
            return NotAsked(reason="the app can't show the question")
        form = create_model(
            "SaveFact",
            fact=(str, Field(default=fact.strip(), description="The fact to save. Edit it if needed.")),
        )
        prompt = f"Save this to company facts?\n\n{fact.strip()}"
        if quote.strip():
            prompt += f'\n\nFrom the conversation: "{quote.strip()}"'
        return Elicit(prompt, form)

    async def propose_fact(fact: str, quote: str, answer: Any, ctx: Context) -> dict[str, Any]:
        caller = await anyio.to_thread.run_sync(lambda: authorize(ctx, "propose_fact", fact[:200]))
        if not zs.config.agent_proposals:
            raise ToolError("agent proposals are turned off in this ZeroStore")
        if not fact.strip():
            raise ToolError("the proposed fact is empty")
        # Proposals through a confidential token stay confidential once approved.
        proposal = {
            "fact": fact.strip(),
            "quote": quote.strip(),
            "confidential": caller.level.sees_confidential,
        }
        item_id = zs.state.add_review_item("agent_proposal", proposal, created_by=caller.name)

        async def settle(approve: bool, by: str, text: str | None = None) -> str:
            outcome = await anyio.to_thread.run_sync(lambda: decide(zs, item_id, approve, by, fact=text))
            return outcome.message

        if caller.auto_approve:
            message = await settle(True, f"auto-approve on token {caller.name}")
            return {"status": "saved", "review_id": item_id, "note": message}
        asked = not (answer.action == "accept" and isinstance(answer.data, NotAsked))
        if asked and answer.action == "accept":
            message = await settle(
                True, f"the person using {caller.name}", getattr(answer.data, "fact", None)
            )
            return {"status": "saved", "review_id": item_id, "note": message}
        if answer.action == "decline":
            await settle(False, f"the person using {caller.name}")
            return {"status": "not saved", "review_id": item_id, "note": "The user chose not to save it."}
        return {
            "status": "waiting for approval",
            "review_id": item_id,
            "note": "A person will review this. It is not a fact until they approve it.",
        }

    # The resolver is a closure, which string annotations can't name: set the real object.
    propose_fact.__annotations__["answer"] = Annotated[ElicitationResult[BaseModel], Resolve(ask_the_person)]
    register("propose_fact", d.PROPOSE_FACT, read_only=False)(propose_fact)

    # Setup tools (owner) --------------------------------------------------

    @register("token_create", d.TOKEN_CREATE, read_only=False)
    def token_create(name: str, level: AccessLevel, ctx: Context, role: str | None = None) -> dict[str, Any]:
        authorize(ctx, "token_create", f"{name} ({level.value}{f', role {role}' if role else ''})")
        problem = None if level is AccessLevel.OWNER else role_problem(zs.config, role)
        if problem:
            raise ToolError(problem)
        try:
            raw, info = zs.tokens.create(name, level, role=role)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"id": info.id, "name": info.name, "level": info.level.value, "role": info.role, "token": raw}

    @register("token_list", d.TOKEN_LIST, read_only=True)
    def token_list(ctx: Context) -> dict[str, Any]:
        authorize(ctx, "token_list")
        return {
            "tokens": [
                {
                    "id": t.id,
                    "name": t.name,
                    "level": t.level.value,
                    "role": t.role,
                    "created_at": t.created_at,
                    "last_used_at": t.last_used_at,
                }
                for t in zs.tokens.list()
            ]
        }

    @register("token_set_role", d.TOKEN_SET_ROLE, read_only=False)
    def token_set_role(id: str, ctx: Context, role: str | None = None) -> dict[str, Any]:
        authorize(ctx, "token_set_role", f"{id} -> {role or 'no role'}")
        problem = role_problem(zs.config, role)
        if problem:
            raise ToolError(problem)
        if not zs.tokens.set_role(id, role):
            raise ToolError(f"no active default or confidential token with id {id}")
        return {"id": id, "role": role or None}

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
        return {
            "items": [
                {**describe(item), **before_after(zs, item)} for item in zs.state.review_items("pending")
            ]
        }

    @register("review_decide", d.REVIEW_DECIDE, read_only=False)
    def review_decide(id: int, approve: bool, ctx: Context, fact: str | None = None) -> dict[str, Any]:
        caller = authorize(ctx, "review_decide", f"{id} {'approve' if approve else 'reject'}")
        try:
            outcome = decide(zs, id, approve, decided_by=caller.name, fact=fact)
        except ReviewError as exc:
            raise ToolError(str(exc)) from None
        return {"id": id, "status": outcome.status, "result": outcome.message}

    @register("sync_now", d.SYNC_NOW, read_only=False)
    def sync_now(ctx: Context) -> dict[str, Any]:
        from zerostore.librarian.ingestion import Ingestor, SyncBusy, source_names

        authorize(ctx, "sync_now")
        if not source_names(zs.config):
            raise ToolError("no sources are set up yet")
        try:
            report = Ingestor(zs).sync()
        except SyncBusy:
            raise ToolError("a sync is already running; try again when it finishes") from None
        return {
            "summary": report.summary(),
            "changes": [
                {
                    k: v
                    for k, v in {
                        "action": c.action,
                        "document": c.title,
                        "source": c.source,
                        "confidence": c.confidence,
                        "note": c.note or None,
                    }.items()
                    if v is not None
                }
                for c in report.changes
            ],
            "problems": [
                {"source": r.source, "error": r.error, "warning": r.warning, "failed": r.failed}
                for r in report.sources
                if r.error or r.warning or r.failed
            ],
        }

    @register("status", d.STATUS, read_only=True)
    def status(ctx: Context) -> dict[str, Any]:
        from zerostore import insights

        authorize(ctx, "status")
        return insights.status(zs)

    @register("activity", d.ACTIVITY, read_only=True)
    def activity(ctx: Context, limit: int = 50, kind: str = "all") -> dict[str, Any]:
        from zerostore import insights

        authorize(ctx, "activity", kind)
        if kind not in ("all", "changes", "questions"):
            raise ToolError("kind must be 'all', 'changes' or 'questions'")
        return {"items": insights.activity(zs, limit, kind)}

    return server
