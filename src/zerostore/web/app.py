"""The web UI's pages and routes (FastAPI + HTMX, rendered on the server).

Every page works without JavaScript; htmx (vendored in static/) makes links
and forms swap the page body in place and polls a running sync.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from zerostore import __version__
from zerostore.app import ZeroStore
from zerostore.config import CONFIG_FILENAME, home_dir
from zerostore.web import auth, charts
from zerostore.web.owner import OwnerClient, OwnerError
from zerostore.web.settings import (
    SettingsError,
    apply_form,
    form_values,
    read_text,
    save_text,
    version_of,
)

log = logging.getLogger("zerostore.web")

HERE = Path(__file__).parent
COOKIE = "zerostore_session"
AGENT_RULE_FILE = "CLAUDE.md"

ACTION_LABELS = {
    "document_added": "add",
    "document_updated": "update",
    "document_retired": "retire",
    "document_moved": "move",
    "document_kept": "keep",
    "document_skipped": "skip",
    "change_queued": "review",
    "delete_queued": "review",
    "review_approved": "approved",
    "review_rejected": "rejected",
    "sync_failed": "sync failed",
    "token_created": "new token",
    "token_revoked": "token revoked",
}
WARN_ACTIONS = {"change_queued", "delete_queued", "sync_failed"}
SOURCE_NAMES = {
    "google_drive": "Google Drive",
    "github": "GitHub",
    "linear": "Linear",
    "decisions": "decisions.md",
}


class SignIn(Exception):
    """The request needs a signed-in owner."""


class Forbidden(Exception):
    pass


def local_time(at: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not at:
        return ""
    try:
        return datetime.fromisoformat(at).astimezone().strftime(fmt)
    except ValueError:
        return at


class SyncRunner:
    """Runs `sync_now` in a background thread so the page can poll it."""

    def __init__(self, owner: OwnerClient):
        self.owner = owner
        self._lock = threading.Lock()
        self.running = False
        self.result: dict[str, Any] | None = None

    def start(self, token: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.result = None
        threading.Thread(target=self._run, args=(token,), name="zerostore-web-sync", daemon=True).start()
        return True

    def snapshot(self) -> dict[str, Any]:
        """Whether a sync runs, and the finished one's outcome, shown once."""
        with self._lock:
            if self.running:
                return {"syncing": True, "sync_result": None}
            result, self.result = self.result, None
            return {"syncing": False, "sync_result": result}

    def _run(self, token: str) -> None:
        try:
            report = self.owner.call(token, "sync_now")
            result = {"ok": not report.get("problems"), "text": report.get("summary", "Sync finished.")}
        except OwnerError as exc:
            result = {"ok": False, "text": str(exc)}
        except Exception as exc:  # shown on the page; the details go to the log
            log.exception("sync from the web UI failed")
            result = {"ok": False, "text": f"the sync failed: {exc}"}
        with self._lock:
            self.result = result
            self.running = False


def connect_commands(token: str, config_path: Path, http_url: str | None = None) -> dict[str, str]:
    """What to paste into Claude Code, Claude Desktop, Cursor and other agents."""
    default = (home_dir() / CONFIG_FILENAME).expanduser().resolve()
    extra = [] if config_path.expanduser().resolve() == default else ["-c", str(config_path)]
    shell_extra = f' -c "{config_path}"' if extra else ""
    entry = {"command": "zerostore", "args": ["serve", *extra], "env": {"ZEROSTORE_TOKEN": token}}
    out = {
        "claude_code": f"claude mcp add zerostore -e ZEROSTORE_TOKEN={token} -- zerostore serve{shell_extra}",
        "json": json.dumps({"mcpServers": {"zerostore": entry}}, indent=2),
    }
    if http_url:
        out["http"] = json.dumps(
            {"mcpServers": {"zerostore": {"url": http_url, "headers": {"Authorization": f"Bearer {token}"}}}},
            indent=2,
        )
    return out


def review_view(item: dict[str, Any], auto_apply_at: float) -> dict[str, Any]:
    """Plain words for one review item: the question, why it waits, and the two answers."""
    kind = item["kind"]
    conf = item.get("confidence")
    sure = f"{conf:.2f}" if isinstance(conf, (int, float)) else "needs a yes"
    source = SOURCE_NAMES.get(item.get("source", ""), item.get("source", ""))
    if kind == "change":
        doc = item["document"]
        title = f"Add {doc}" if item.get("change") == "added" else f"Apply the edit to {doc}"
        title += "" if title.endswith("?") else "?"
        why = f"ZeroStore was {sure} sure, below your auto-apply level of {auto_apply_at:.2f}."
        if item.get("note"):
            why = f"{item['note'][0].upper()}{item['note'][1:]}. {why}"
        tag, yes, no, verb = (
            "conflict" if item.get("note") else "change",
            "Apply it",
            "Keep current",
            "approve",
        )
    elif kind == "delete":
        doc = item["document"]
        if item.get("moved_to"):
            title = f"Did {doc} move to {item['moved_to']}?"
            yes = "Record the move"
        else:
            title = f"Retire {doc}?"
            yes = "Retire it"
        lean = f" ZeroStore leaned towards {item['suggestion']}." if item.get("suggestion") else ""
        why = f"It was deleted from {source}.{lean} Its facts stay live until you decide."
        tag, no, verb = "deleted", "Keep its facts", "approve"
    else:
        fact = item.get("fact", "")
        title = f"Save “{fact if len(fact) <= 90 else fact[:87] + '...'}”?"
        who = item.get("proposed_by") or "An agent"
        why = f"{who} suggested this" + (f", from: “{item['quote']}”" if item.get("quote") else ".")
        tag, yes, no, verb = "proposal", "Approve", "Reject", "approve"
    url = item.get("url") or ""
    link = url if urlparse(url).scheme in ("http", "https", "file") else ""
    meta = " · ".join(
        p
        for p in (
            item.get("engine"),
            source if kind != "agent_proposal" else "",
            local_time(item.get("created_at")),
        )
        if p
    )
    return {
        **item,
        "tag": tag,
        "sure": sure,
        "title": title,
        "why": why,
        "yes": yes,
        "no": no,
        "verb": verb,
        "link": link,
        "meta": meta,
    }


def create_web_app(
    zs: ZeroStore,
    config_path: Path,
    *,
    bound_host: str = "127.0.0.1",
    mcp_url: str | None = None,
    behind_proxy: bool = False,
    scheduled: bool = True,
) -> FastAPI:
    """The web UI for one ZeroStore.

    `behind_proxy`: a reverse proxy in front forwards requests, so the Host header
    may be the public name and the public origin comes from X-Forwarded-Host.
    `scheduled`: this process runs timed syncs (shown on the status page).
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        version=__version__, local_time=local_time, labels=ACTION_LABELS, source_names=SOURCE_NAMES
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    sessions = auth.Sessions()
    owner = OwnerClient(zs)
    syncer = SyncRunner(owner)
    loopback_only = _is_loopback(bound_host) and not behind_proxy
    failures = auth.FailedSignIns()
    app.state.sessions = sessions
    app.state.owner = owner
    app.state.syncer = syncer

    async def call(token: str, tool: str, **args: Any) -> dict[str, Any]:
        try:
            return await anyio.to_thread.run_sync(functools.partial(owner.call, token, tool, **args))
        except OwnerError as exc:
            if auth.is_token_problem(str(exc)):  # revoked or deleted while signed in
                raise SignIn() from None
            raise

    def public_host(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-host") if behind_proxy else None
        return (forwarded or request.headers.get("host", "")).split(",")[0].strip()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = auth.host_name(request.headers.get("host", ""))
        if loopback_only and host not in auth.LOOPBACK_NAMES:
            return Response(
                "This address isn't allowed. Open ZeroStore at http://localhost, or start it with "
                "--behind-https-proxy if a proxy forwards to it.",
                status_code=400,
            )
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlparse(origin).netloc != public_host(request):
                return Response("Cross-site requests are refused.", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(SignIn)
    async def _sign_in(request: Request, exc: SignIn):
        sessions.end(request.cookies.get(COOKIE))
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return Response("This form expired. Reload the page and try again.", status_code=403)

    def session_of(request: Request) -> auth.Session:
        session = sessions.get(request.cookies.get(COOKIE))
        if session is None:
            raise SignIn()
        return session

    async def posted(request: Request) -> tuple[auth.Session, dict[str, str]]:
        session = session_of(request)
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        if not auth.csrf_ok(session, form.get("csrf") or request.headers.get("x-csrf-token")):
            raise Forbidden()
        return session, form

    async def render(request: Request, name: str, session: auth.Session | None, page: str, **ctx: Any):
        waiting = ctx.pop("waiting", None)
        if waiting is None and "status" in ctx:
            waiting = ctx["status"].get("waiting")
        if waiting is None and session is not None:
            waiting = (await call(session.token, "status")).get("waiting")
        flash = []
        if session is not None:
            flash, session.flash = session.flash, []
        return templates.TemplateResponse(
            request,
            name,
            {
                "page": page,
                "session": session,
                "csrf": session.csrf if session else "",
                "waiting": waiting,
                "flash": flash,
                "config_path": str(config_path),
                "loopback": loopback_only,
                **ctx,
            },
        )

    def say(session: auth.Session, kind: str, text: str, **extra: Any) -> None:
        session.flash.append({"kind": kind, "text": text, **extra})

    # Signing in ---------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, code: str | None = None):
        # A link only shows a button: the code is used up by the POST, so link previews
        # and prefetching can't spend it.
        return await render(request, "login.html", None, "login", code=code or "")

    @app.post("/login")
    async def login(request: Request):
        who = request.client.host if request.client else "?"
        if failures.blocked(who):
            return await render(
                request, "login.html", None, "login", error="Too many tries. Wait a minute and try again."
            )
        form = await request.form()
        code = str(form.get("code") or "").strip()
        token = str(form.get("token") or "").strip()
        try:
            if code:
                await anyio.to_thread.run_sync(auth.redeem_login_code, zs, code)
                token = await anyio.to_thread.run_sync(auth.saved_owner_token, zs, config_path)
            else:
                await anyio.to_thread.run_sync(auth.check_owner, zs, token)
        except auth.LoginError as exc:
            failures.add(who)
            log.warning("web UI sign-in refused from %s: %s", who, exc)
            return await render(request, "login.html", None, "login", error=str(exc))
        return _signed_in(request, sessions, token)

    @app.post("/logout")
    async def logout(request: Request):
        await posted(request)
        sessions.end(request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    # Pages ----------------------------------------------------------------------

    @app.get("/")
    async def home(request: Request):
        session = session_of(request)
        status = await call(session.token, "status")
        return RedirectResponse("/status" if status["sources"] else "/setup", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        session = session_of(request)
        status = await call(session.token, "status")
        review = (await call(session.token, "review_list"))["items"] if status["waiting"] else []
        recent = (await call(session.token, "activity", limit=8))["items"]
        cfg = zs.config
        c = status["charts"]
        figures = {
            "changes": charts.stacked_columns(
                c["days"],
                [
                    ("applied", charts.BLUE, c["changes_applied"]),
                    ("sent to review", charts.ORANGE, c["changes_queued"]),
                ],
                "Changes per day, last two weeks",
                "No changes in the last two weeks.",
            ),
            "outcomes": charts.bars(
                list(c["outcomes"].items()), "Decisions by outcome, last two weeks", "No decisions yet."
            ),
            "confidence": charts.histogram(
                c["confidence"],
                cfg.review.auto_apply_at,
                "How sure the librarian was",
                "No judged changes yet. Add an AI key to turn on judgment.",
            ),
            "questions": charts.columns(
                c["days"], c["questions"], "Questions agents asked per day", "questions", "No questions yet."
            ),
            "latency": charts.line(
                c["days"], c["median_ms"], "Median search time per day", "ms", "No timed searches yet."
            ),
        }
        return await render(
            request,
            "status.html",
            session,
            "status",
            status=status,
            review=[review_view(i, cfg.review.auto_apply_at) for i in review[:5]],
            recent=recent,
            figures=figures,
            **syncer.snapshot(),
            scheduled=scheduled,
        )

    @app.post("/sync")
    async def sync(request: Request):
        session, _ = await posted(request)
        if not syncer.start(session.token):
            say(session, "warn", "A sync is already running.")
        return RedirectResponse("/status", status_code=303)

    @app.get("/sync/state", response_class=HTMLResponse)
    async def sync_state(request: Request):
        session_of(request)
        if syncer.running:
            return HTMLResponse(SYNC_POLL)
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, item: int | None = None):
        session = session_of(request)
        items = (await call(session.token, "review_list"))["items"]
        views = [review_view(i, zs.config.review.auto_apply_at) for i in items]
        current = next((v for v in views if v["id"] == item), views[0] if views else None)
        return await render(
            request, "review.html", session, "review", items=views, current=current, waiting=len(views)
        )

    @app.post("/review/{item_id}")
    async def decide(request: Request, item_id: int):
        session, form = await posted(request)
        approve = form.get("decision") == "approve"
        args: dict[str, Any] = {"id": item_id, "approve": approve}

        def clean(text: str | None) -> str:
            return (text or "").replace("\r\n", "\n").strip()  # browsers send CRLF from a textarea

        fact = clean(form.get("fact"))
        if approve and fact and fact != clean(form.get("original_fact")):
            args["fact"] = fact
        try:
            result = await call(session.token, "review_decide", **args)
            say(session, "ok", f"✓ {result['result']}.")
        except OwnerError as exc:
            say(session, "warn", str(exc))
            return RedirectResponse(f"/review?item={item_id}", status_code=303)
        return RedirectResponse("/review", status_code=303)

    @app.get("/log", response_class=HTMLResponse)
    async def log_page(request: Request, kind: str = "all", q: str = ""):
        session = session_of(request)
        kind = kind if kind in ("all", "changes", "questions") else "all"
        rows = (await call(session.token, "activity", limit=300, kind=kind))["items"]
        needle = q.strip().lower()
        if needle:
            rows = [r for r in rows if needle in " ".join(str(v) for v in r.values() if v).lower()]
        return await render(
            request, "log.html", session, "log", rows=rows[:200], kind=kind, q=q, warn=WARN_ACTIONS
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, view: str = "form"):
        session = session_of(request)
        tokens = (await call(session.token, "token_list"))["tokens"]
        status = await call(session.token, "status")
        try:
            values = await anyio.to_thread.run_sync(form_values, config_path)
            problem = None
        except SettingsError as exc:
            values, problem, view = None, str(exc), "yaml"
        text = await anyio.to_thread.run_sync(read_text, config_path)
        caller = await anyio.to_thread.run_sync(auth.check_owner, zs, session.token)
        return await render(
            request,
            "settings.html",
            session,
            "settings",
            status=status,
            view="yaml" if view == "yaml" else "form",
            v=values,
            problem=problem,
            yaml_text=text,
            yaml_version=version_of(text),
            tokens=tokens,
            me=caller.id,
            servers=_servers(zs, bound_host, mcp_url),
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        session, form = await posted(request)
        return await _save(session, lambda: apply_form(config_path, form), form.get("back") or "/settings")

    @app.post("/settings/yaml")
    async def save_yaml(request: Request):
        session, form = await posted(request)
        return await _save(
            session,
            lambda: save_text(config_path, form.get("yaml", ""), form.get("version")),
            "/settings?view=yaml",
        )

    async def _save(session: auth.Session, action, back: str):
        if back not in ("/settings", "/settings?view=yaml", "/setup?step=2", "/setup?step=3"):
            back = "/settings"  # only pages of this app, never a redirect elsewhere
        try:
            config = await anyio.to_thread.run_sync(action)
        except SettingsError as exc:
            say(session, "warn", f"Not saved: {exc}")
            return RedirectResponse(back, status_code=303)
        zs.config = config  # syncs, review and search thresholds use it from now on
        say(
            session,
            "ok",
            f"Saved to {config_path.name}. Syncs use it now; restart `zerostore serve` for new "
            "server addresses or new AI keys in search.",
        )
        return RedirectResponse(back, status_code=303)

    @app.post("/tokens")
    async def new_token(request: Request):
        session, form = await posted(request)
        name = (form.get("name") or "").strip() or "agent"
        level = form.get("level") if form.get("level") in ("default", "confidential", "owner") else "default"
        back = form.get("back") if form.get("back") in ("/settings", "/setup?step=3") else "/settings"
        try:
            made = await call(session.token, "token_create", name=name, level=level)
        except OwnerError as exc:
            say(session, "warn", str(exc))
            return RedirectResponse(back, status_code=303)
        say(
            session,
            "token",
            f"Created {made['name']} ({made['level']}). Copy it now: it isn't shown again.",
            token=made["token"],
            commands=connect_commands(made["token"], config_path, mcp_url),
        )
        return RedirectResponse(back, status_code=303)

    @app.post("/tokens/{token_id}/revoke")
    async def revoke_token(request: Request, token_id: str):
        session, _ = await posted(request)
        try:
            await call(session.token, "token_revoke", id=token_id)
            say(session, "ok", "Revoked. Agents using that token are refused from now on.")
        except OwnerError as exc:
            say(session, "warn", str(exc))
        return RedirectResponse("/settings", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, step: int | None = None):
        session = session_of(request)
        status = await call(session.token, "status")
        try:
            values = await anyio.to_thread.run_sync(form_values, config_path)
        except SettingsError as exc:
            say(session, "warn", f"{exc}. Fix it in the YAML view.")
            return RedirectResponse("/settings?view=yaml", status_code=303)
        cfg = values["config"]
        done = {
            1: bool(cfg.sources.enabled()),
            2: cfg.ai.has_llm or cfg.ai.has_jev or cfg.ai.decision_engine == "demo",
            3: any(t["level"] != "owner" for t in status["tokens"]),
        }
        if step not in (1, 2, 3):
            step = next((k for k in (1, 2, 3) if not done[k]), 3)
        return await render(
            request,
            "setup.html",
            session,
            "setup",
            status=status,
            v=values,
            step=step,
            done=done,
            schedule=values["schedules"].get(cfg.sync.schedule, cfg.sync.schedule),
            rule_file=AGENT_RULE_FILE,
            scheduled=scheduled,
        )

    @app.post("/setup/sync")
    async def first_sync(request: Request):
        session, _ = await posted(request)
        syncer.start(session.token)
        return RedirectResponse("/status", status_code=303)

    return app


SYNC_POLL = (
    '<span id="sync-state" class="cmd" hx-get="/sync/state" hx-trigger="every 1s" hx-swap="outerHTML">'
    "syncing…</span>"
)


def _signed_in(request: Request, sessions: auth.Sessions, token: str) -> Response:
    sid, _ = sessions.start(token)
    response = RedirectResponse("/", status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE, sid, httponly=True, samesite="strict", secure=secure, path="/", max_age=auth.SESSION_TTL
    )
    return response


def _is_loopback(host: str) -> bool:
    from zerostore.mcp.http import is_loopback

    return is_loopback(host)


def _servers(zs: ZeroStore, host: str, mcp_url: str | None) -> list[dict[str, str]]:
    cfg = zs.config
    web = mcp_url[: -len("/mcp")] if mcp_url and mcp_url.endswith("/mcp") else f"http://{host}"
    return [
        {"name": "web UI", "where": "this process", "address": web},
        {"name": "mcp server", "where": "this process", "address": mcp_url or "stdio"},
        {"name": "librarian", "where": "inside zerostore", "address": f"syncs on '{cfg.sync.schedule}'"},
        {"name": "vector db", "where": "lancedb", "address": cfg.servers.vectordb},
        {"name": "state db", "where": "sqlite", "address": cfg.servers.statedb},
    ]
