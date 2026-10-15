"""Signing in to the web UI.

Two ways in, both ending in the owner token held server-side for the session:
- a one-time login link that `zerostore serve --http` and `zerostore ui`
  print. Its code is single-use, expires in ten minutes and is stored only as
  a hash (in state.sqlite, so another process can mint one). Redeeming it
  reads your owner token from the `owner-token` file `zerostore init` saved.
- pasting an owner token on the sign-in page.

The browser only ever holds a random session id in an HttpOnly, SameSite=Strict
cookie; every form also carries a per-session CSRF token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zerostore.access.tokens import AccessLevel, TokenError, TokenInfo
from zerostore.app import ZeroStore
from zerostore.config import OWNER_TOKEN_FILE, home_dir

CODE_TTL = 10 * 60
SESSION_TTL = 12 * 60 * 60
MAX_SESSIONS = 200
_CODE_PREFIX = "web_login:"


class LoginError(Exception):
    """Signing in didn't work; the message says why and what to do."""


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_login_code(zs: ZeroStore, now: float | None = None) -> str:
    now = time.time() if now is None else now
    for row in zs.state.query("SELECT key, value FROM meta WHERE key LIKE ?", (_CODE_PREFIX + "%",)):
        if float(row["value"]) < now:
            zs.state.delete_meta(row["key"])
    code = secrets.token_urlsafe(24)
    zs.state.set_meta(_CODE_PREFIX + _hash(code), str(now + CODE_TTL))
    return code


def redeem_login_code(zs: ZeroStore, code: str, now: float | None = None) -> None:
    """Use up a login code. Raises LoginError if it is unknown, used or expired."""
    now = time.time() if now is None else now
    key = _CODE_PREFIX + _hash(code)
    expires = zs.state.get_meta(key)
    if expires is None or not zs.state.delete_meta(key):
        raise LoginError("this sign-in link was already used or isn't valid")
    if float(expires) < now:
        raise LoginError("this sign-in link expired")


def owner_token_paths(config_path: Path) -> list[Path]:
    paths = [config_path.parent / OWNER_TOKEN_FILE, home_dir() / OWNER_TOKEN_FILE]
    return list(dict.fromkeys(p.expanduser() for p in paths))


def check_owner(zs: ZeroStore, raw: str) -> TokenInfo:
    try:
        info = zs.tokens.verify(raw)
    except TokenError as exc:
        raise LoginError(str(exc)) from None
    if info.level is not AccessLevel.OWNER:
        raise LoginError(
            f"that token has {info.level.value} access; the web UI needs an owner token "
            "(the one `zerostore init` saved in owner-token)"
        )
    return info


def saved_owner_token(zs: ZeroStore, config_path: Path) -> str:
    """The owner token `zerostore init` saved next to the config."""
    for path in owner_token_paths(config_path):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            check_owner(zs, raw)
        except LoginError:
            continue
        return raw
    raise LoginError(
        "no working owner token was found in "
        + " or ".join(str(p) for p in owner_token_paths(config_path))
        + "; paste one below, or run `zerostore init`"
    )


@dataclass
class Session:
    token: str
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    expires: float = 0.0
    flash: list[dict[str, Any]] = field(default_factory=list)  # messages for the next page shown


class Sessions:
    """Signed-in browsers, kept in memory: a restart signs everyone out."""

    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def start(self, token: str) -> tuple[str, Session]:
        sid = secrets.token_urlsafe(32)
        session = Session(token, expires=time.time() + self.ttl)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].expires)
                del self._sessions[oldest]
            self._sessions[sid] = session
        return sid, session

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if session.expires < now:
                del self._sessions[sid]
                return None
            session.expires = now + self.ttl
            return session

    def end(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)


def csrf_ok(session: Session, sent: str | None) -> bool:
    return bool(sent) and hmac.compare_digest(session.csrf.encode(), sent.encode())


TOKEN_PROBLEMS = ("token was revoked", "token is not recognized", "no ZeroStore token")


def is_token_problem(message: str) -> bool:
    return any(p in message for p in TOKEN_PROBLEMS)


class FailedSignIns:
    """At most `limit` refused sign-ins per address per minute."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._tries: dict[str, list[float]] = {}

    def _recent(self, who: str, now: float) -> list[float]:
        tries = [t for t in self._tries.get(who, []) if now - t < self.window]
        self._tries[who] = tries
        return tries

    def blocked(self, who: str) -> bool:
        with self._lock:
            return len(self._recent(who, time.time())) >= self.limit

    def add(self, who: str) -> None:
        with self._lock:
            now = time.time()
            self._recent(who, now).append(now)
            if len(self._tries) > 10_000:  # forget the oldest addresses rather than grow forever
                self._tries.pop(next(iter(self._tries)))


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def host_name(header: str) -> str:
    """The name part of a Host header: `localhost:8765` -> `localhost`, `[::1]:80` -> `::1`."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header
