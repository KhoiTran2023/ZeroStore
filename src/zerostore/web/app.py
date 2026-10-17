"""The web UI's pages and routes (FastAPI + HTMX, rendered on the server).

Every page works without JavaScript; htmx (vendored in static/) makes links
and forms swap the page body in place and polls a running sync.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from zerostore import __version__
from zerostore.access.oauth import APPROVE_PATH, ApprovalError, ConnectorAuth
from zerostore.access.roles import role_problem
from zerostore.agents import commands as agent_commands
from zerostore.app import ZeroStore
from zerostore.config import config_env
from zerostore.connectors import google_signin as google
from zerostore.web import auth, charts
from zerostore.web.owner import OwnerClient, OwnerError
from zerostore.web.settings import (
    SettingsError,
    apply_form,
    form_values,
    read_text,
    save_secrets,
    save_text,
    update_drive,
    version_of,
)

log = logging.getLogger("zerostore.web")

HERE = Path(__file__).parent
COOKIE = "zerostore_session"
DRIVE_NAMES_KEY = "gdrive_folder_names"  # names to show for folders saved as id:...
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
    out = {
        "claude_code": agent_commands.claude_code_stdio(token, config_path),
        "json": agent_commands.stdio_json(token, config_path),
    }
    if http_url:
        out["claude_code_http"] = agent_commands.claude_code_http(token, http_url)
        out["http"] = agent_commands.http_json(token, http_url)
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
    google_http: Callable[[], httpx.Client] | None = None,
    connectors: ConnectorAuth | None = None,
) -> FastAPI:
    """The web UI for one ZeroStore.

    `behind_proxy`: a reverse proxy in front forwards requests, so the Host header
    may be the public name and the public origin comes from X-Forwarded-Host.
    `scheduled`: this process runs timed syncs (shown on the status page).
    `google_http`: makes the HTTP client for talking to Google (tests pass a fake one).
    `connectors`: the sign-in for Claude's connectors, which asks the owner on /connect/approve.
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
    flows = google.Flows()
    google_http = google_http or (lambda: httpx.Client(timeout=30))

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
        response.headers.setdefault("Content-Security-Policy", _csp())
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
                "roles": list(zs.config.roles),
                **ctx,
            },
        )

    def say(session: auth.Session, kind: str, text: str, **extra: Any) -> None:
        session.flash.append({"kind": kind, "text": text, **extra})

    # Signing in ---------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, code: str | None = None, next: str = ""):
        # A link only shows a button: the code is used up by the POST, so link previews
        # and prefetching can't spend it.
        return await render(request, "login.html", None, "login", code=code or "", next=_next_page(next))

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
        then = _next_page(str(form.get("next") or ""))
        try:
            if code:
                await anyio.to_thread.run_sync(auth.redeem_login_code, zs, code)
                token = await anyio.to_thread.run_sync(auth.saved_owner_token, zs, config_path)
            else:
                await anyio.to_thread.run_sync(auth.check_owner, zs, token)
        except auth.LoginError as exc:
            failures.add(who)
            log.warning("web UI sign-in refused from %s: %s", who, exc)
            return await render(request, "login.html", None, "login", error=str(exc), next=then)
        return _signed_in(request, sessions, token, then or "/")

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
            tag_counts=status.get("tags", {}),
            me=caller.id,
            servers=_servers(zs, bound_host, mcp_url),
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        session, form = await posted(request)
        return await _save(
            session,
            lambda: apply_form(config_path, form),
            form.get("back") or "/settings",
            access_changed="access" in form.get("sections", ""),
        )

    @app.post("/settings/yaml")
    async def save_yaml(request: Request):
        session, form = await posted(request)
        return await _save(
            session,
            lambda: save_text(config_path, form.get("yaml", ""), form.get("version")),
            "/settings?view=yaml",
        )

    async def _save(session: auth.Session, action, back: str, access_changed: bool = False):
        if back not in ("/settings", "/settings?view=yaml", "/setup?step=2", "/setup?step=3"):
            back = "/settings"  # only pages of this app, never a redirect elsewhere
        try:
            config = await anyio.to_thread.run_sync(action)
        except SettingsError as exc:
            say(session, "warn", f"Not saved: {exc}")
            return RedirectResponse(back, status_code=303)
        zs.config = config  # syncs, review, search thresholds and roles use it from now on
        say(
            session,
            "ok",
            f"Saved to {config_path.name}. Syncs use it now; restart `zerostore serve` for new "
            "server addresses or new AI keys in search."
            + (" Run a sync to apply tag changes to your documents." if access_changed else ""),
        )
        return RedirectResponse(back, status_code=303)

    @app.post("/tokens")
    async def new_token(request: Request):
        session, form = await posted(request)
        name = (form.get("name") or "").strip() or "agent"
        level = form.get("level") if form.get("level") in ("default", "confidential", "owner") else "default"
        back = form.get("back") if form.get("back") in ("/settings", "/setup?step=3") else "/settings"
        role = form.get("role") or None
        try:
            made = await call(session.token, "token_create", name=name, level=level, role=role)
        except OwnerError as exc:
            say(session, "warn", str(exc))
            return RedirectResponse(back, status_code=303)
        say(
            session,
            "token",
            f"Created {made['name']} ({made['level']}{', role ' + made['role'] if made.get('role') else ''})."
            " Copy it now: it isn't shown again.",
            token=made["token"],
            commands=connect_commands(made["token"], config_path, mcp_url),
        )
        return RedirectResponse(back, status_code=303)

    @app.post("/tokens/{token_id}/role")
    async def set_token_role(request: Request, token_id: str):
        session, form = await posted(request)
        role = form.get("role") or None
        try:
            await call(session.token, "token_set_role", id=token_id, role=role)
            say(
                session,
                "ok",
                f"Role set to {role}." if role else "Role removed: it sees untagged facts only.",
            )
        except OwnerError as exc:
            say(session, "warn", str(exc))
        return RedirectResponse("/settings", status_code=303)

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
    async def setup_page(request: Request, step: int | None = None, folder: str = "root", path: str = ""):
        session = session_of(request)
        status = await call(session.token, "status")
        try:
            values = await anyio.to_thread.run_sync(form_values, config_path)
        except SettingsError as exc:
            say(session, "warn", f"{exc}. Fix it in the YAML view.")
            return RedirectResponse("/settings?view=yaml", status_code=303)
        cfg = values["config"]
        s = cfg.sources
        done = {
            1: bool((s.google_drive and s.google_drive.folders) or s.github or s.linear),
            2: cfg.ai.has_llm or cfg.ai.has_jev or cfg.ai.decision_engine == "demo",
            3: any(t["level"] != "owner" for t in status["tokens"]),
        }
        if step not in (1, 2, 3):
            step = next((k for k in (1, 2, 3) if not done[k]), 3)
        drive = await anyio.to_thread.run_sync(drive_view, request, cfg, folder, path) if step == 1 else None
        return await render(
            request,
            "setup.html",
            session,
            "setup",
            status=status,
            v=values,
            step=step,
            done=done,
            drive=drive,
            schedule=values["schedules"].get(cfg.sync.schedule, cfg.sync.schedule),
            rule_file=AGENT_RULE_FILE,
            scheduled=scheduled,
            connector_url=mcp_url if connectors and mcp_url and mcp_url.startswith("https://") else None,
        )

    # Claude's connectors -----------------------------------------------------------
    # Claude (web, desktop, phone) adds ZeroStore under Settings, Connectors, then sends
    # the owner's browser here to allow it. See access/oauth.py for the rest of the flow.

    def connect_done(request: Request, message: str) -> Response:
        return templates.TemplateResponse(request, "connect_done.html", {"message": message}, status_code=400)

    @app.get(APPROVE_PATH, response_class=HTMLResponse)
    async def connect_page(request: Request, hop: int = 0):
        if connectors is None:
            raise HTTPException(404)
        try:
            pending = connectors.pending(request.query_params.get("request", ""))
        except ApprovalError as exc:
            return connect_done(request, f"Not connected: {exc}.")
        here = f"{APPROVE_PATH}?request={pending.id}"
        session = sessions.get(request.cookies.get(COOKIE))
        if session is None:
            if not hop:
                # Claude sends the browser here from its own site, which leaves the SameSite=Strict
                # session cookie behind; one step from ZeroStore itself brings it along again.
                return templates.TemplateResponse(request, "connect_hop.html", {"next": f"{here}&hop=1"})
            return RedirectResponse(f"/login?next={quote(here, safe='')}", status_code=303)
        response = await render(
            request,
            "connect_approve.html",
            session,
            "connect",
            p=pending,
            returns_to=urlparse(pending.returns_to).netloc or pending.returns_to,
        )
        # The answer's redirect goes back to the app, which form-action must allow on this page.
        response.headers["Content-Security-Policy"] = _csp(_origin(pending.returns_to))
        return response

    @app.post(APPROVE_PATH)
    async def connect_answer(request: Request):
        session, form = await posted(request)
        if connectors is None:
            raise HTTPException(404)
        allow = form.get("answer") == "allow"
        role = form.get("role") or None
        problem = role_problem(zs.config, role) if allow else None
        if problem:
            return connect_done(request, f"Not connected: {problem}.")
        try:
            pending = connectors.pending(form.get("request", ""))
            to = connectors.answer(pending.id, allow=allow, level=form.get("level") or "default", role=role)
        except ApprovalError as exc:
            return connect_done(request, f"Not connected: {exc}.")
        if allow:
            say(session, "ok", f"Connected {pending.name}. Revoke its token in Settings to disconnect it.")
        response = RedirectResponse(to, status_code=303)
        response.headers["Content-Security-Policy"] = _csp(_origin(pending.returns_to))
        return response

    # Sign in with Google ---------------------------------------------------------

    def on_this_computer(request: Request) -> bool:
        return not behind_proxy and auth.host_name(request.headers.get("host", "")) in auth.LOOPBACK_NAMES

    def credentials_file() -> Path:
        drive = zs.config.sources.google_drive
        if drive and drive.credentials:
            return Path(drive.credentials).expanduser()
        return config_path.parent / google.CREDENTIALS_FILENAME

    def folder_names() -> dict[str, str]:
        try:
            return json.loads(zs.state.get_meta(DRIVE_NAMES_KEY) or "{}")
        except ValueError:
            return {}

    def drive_view(request: Request, cfg, folder: str, path: str) -> dict[str, Any]:
        """What the setup page shows for Google Drive: sign-in state, chosen folders, a folder browser."""
        drive = cfg.sources.google_drive
        names = folder_names()
        view: dict[str, Any] = {
            "client": google.oauth_client(config_env(config_path)),
            "local": on_this_computer(request),
            "account": google.signed_in_as(credentials_file()),
            "chosen": [{"value": f, "label": names.get(f, f)} for f in (drive.folders if drive else [])],
            "folder": folder,
            "path": path,
            "redirect_uri": google.PASTE_REDIRECT,
        }
        if not view["account"]:
            return view
        from zerostore.connectors.base import ConnectorError
        from zerostore.connectors.google_auth import GoogleAuth

        top = folder in ("root", "shared", "drives")
        view["crumbs"] = [
            {"name": p, "path": "/".join(path.split("/")[: i + 1])}
            for i, p in enumerate(path.split("/"))
            if p
        ]
        with google_http() as http:
            try:
                rows = google.list_folders(GoogleAuth(credentials_file(), http).token(), folder, http)
            except (google.SignInError, ConnectorError) as exc:
                view["error"] = str(exc)
                return view
        in_my_drive = folder == "root" or (not top and bool(path))
        taken = {r["name"] for r in rows if [x["name"] for x in rows].count(r["name"]) > 1}
        for row in rows:
            child_path = f"{path}/{row['name']}" if path else row["name"]
            by_path = in_my_drive and row["name"] not in taken and "/" not in row["name"]
            row["value"] = child_path if by_path else f"id:{row['id']}"
            row["label"] = child_path if in_my_drive else row["name"]
            row["open"] = f"/setup?step=1&folder={quote(row['id'])}" + (
                f"&path={quote(child_path)}" if in_my_drive else ""
            )
        view["rows"] = rows
        return view

    def signed_in(session: auth.Session, flow: google.Flow, code: str | None) -> None:
        if not code:
            raise google.SignInError("Google didn't send a sign-in code back")
        with google_http() as http:
            credentials, email = google.finish(flow, code, http)
        target = config_path.parent / google.CREDENTIALS_FILENAME
        if target.exists() and not google.signed_in_as(target):
            target = config_path.parent / "google-signin.json"  # never overwrite a service-account key
        google.save_credentials(target, credentials, email)
        zs.config = update_drive(config_path, credentials=str(target))
        say(session, "ok", f"Signed in to Google as {email}. Now add the folders ZeroStore should read.")

    @app.get("/setup/google/start")
    async def google_start(request: Request):
        session = session_of(request)
        client = google.oauth_client(config_env(config_path))
        if client is None:
            say(session, "warn", "Add your Google sign-in app below first.")
            return RedirectResponse("/setup?step=1", status_code=303)
        local = on_this_computer(request)
        redirect_uri = (
            f"{request.url.scheme}://{request.headers['host']}/setup/google/callback"
            if local
            else google.PASTE_REDIRECT
        )
        flow = flows.start(client, redirect_uri, session)
        return RedirectResponse(flow.url(), status_code=303)

    @app.get("/setup/google/callback", response_class=HTMLResponse)
    async def google_callback(
        request: Request, state: str | None = None, code: str | None = None, error: str | None = None
    ):
        # Google's redirect is a cross-site navigation, so the SameSite=Strict session cookie
        # doesn't come with it: the one-time `state` proves which signed-in session asked.
        try:
            flow = flows.take(state)
            if error:
                raise google.SignInError(
                    "you didn't allow access on Google's page"
                    if error == "access_denied"
                    else f"Google said {error}"
                )
            await anyio.to_thread.run_sync(signed_in, flow.owner, flow, code)
            message = None
        except (google.SignInError, SettingsError) as exc:
            message = f"Not signed in: {exc}."
            if "flow" in locals():
                say(flow.owner, "warn", message)
        return templates.TemplateResponse(request, "google_done.html", {"message": message})

    @app.post("/setup/google/paste")
    async def google_paste(request: Request):
        session, form = await posted(request)
        code, state, error = google.read_pasted(form.get("address", ""))
        try:
            flow = flows.take(state)
            if flow.owner is not session:
                raise google.SignInError("that sign-in was started in another browser")
            if error:
                raise google.SignInError(f"Google said {error}")
            await anyio.to_thread.run_sync(signed_in, session, flow, code)
        except (google.SignInError, SettingsError) as exc:
            say(session, "warn", f"Not signed in: {exc}.")
        return RedirectResponse("/setup?step=1", status_code=303)

    @app.post("/setup/google/client")
    async def google_client(request: Request):
        session, form = await posted(request)
        try:
            client_id = google.check_client_id(form.get("client_id", ""))
            secret = (form.get("client_secret") or "").strip()
            if not secret or any(c.isspace() for c in secret):
                raise google.SignInError("paste the client secret too")
            await anyio.to_thread.run_sync(
                save_secrets,
                config_path,
                {google.CLIENT_ID_NAME: client_id, google.CLIENT_SECRET_NAME: secret},
            )
            say(session, "ok", "Saved your Google sign-in app. Now sign in with Google.")
        except (google.SignInError, SettingsError) as exc:
            say(session, "warn", f"Not saved: {exc}.")
        return RedirectResponse("/setup?step=1", status_code=303)

    @app.post("/setup/google/signout")
    async def google_signout(request: Request):
        session, _ = await posted(request)
        target = credentials_file()
        if google.signed_in_as(target):

            def forget() -> None:
                with google_http() as http:
                    google.sign_out(target, http)

            await anyio.to_thread.run_sync(forget)
            say(session, "ok", "Signed out of Google. Syncs skip Google Drive until you sign in again.")
        return RedirectResponse("/setup?step=1", status_code=303)

    @app.post("/setup/google/folders")
    async def google_folders(request: Request):
        session, form = await posted(request)
        value = (form.get("value") or "").strip()
        back = form.get("back") or "/setup?step=1"
        if not back.startswith("/setup?step=1"):
            back = "/setup?step=1"  # only this page, never a redirect elsewhere
        if value:
            try:
                if form.get("action") == "remove":
                    zs.config = await anyio.to_thread.run_sync(
                        functools.partial(update_drive, config_path, remove=value)
                    )
                else:
                    zs.config = await anyio.to_thread.run_sync(
                        functools.partial(update_drive, config_path, add=value)
                    )
                    if value.startswith("id:") and form.get("label"):
                        names = folder_names()
                        names[value] = form["label"][:200]
                        zs.state.set_meta(DRIVE_NAMES_KEY, json.dumps(names))
            except SettingsError as exc:
                say(session, "warn", f"Not saved: {exc}")
        return RedirectResponse(back, status_code=303)

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


def _signed_in(request: Request, sessions: auth.Sessions, token: str, to: str = "/") -> Response:
    sid, _ = sessions.start(token)
    response = RedirectResponse(to, status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE, sid, httponly=True, samesite="strict", secure=secure, path="/", max_age=auth.SESSION_TTL
    )
    return response


def _csp(also_post_to: str = "") -> str:
    form_action = f"'self' {also_post_to}".strip()
    return (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        f"frame-ancestors 'none'; form-action {form_action}; base-uri 'none'"
    )


def _origin(url: str) -> str:
    """A CSP source for where a URL goes: its origin, or its scheme for an app's own (cursor:)."""
    parts = urlparse(url)
    if parts.scheme in ("http", "https"):
        return f"{parts.scheme}://{parts.netloc}" if _HOST_PORT.fullmatch(parts.netloc) else ""
    return f"{parts.scheme}:" if _SCHEME.fullmatch(parts.scheme) else ""


_HOST_PORT = re.compile(r"([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(:\d+)?")
_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*")


def _next_page(value: str) -> str:
    """Where to go after signing in: only back to a connector's approval page."""
    prefix = f"{APPROVE_PATH}?request="
    if value.startswith(prefix) and re.fullmatch(r"[A-Za-z0-9_\-]+", value[len(prefix) :]):
        return value
    return ""


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
