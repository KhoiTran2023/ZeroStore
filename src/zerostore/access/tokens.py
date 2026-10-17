"""Tokens and access levels.

Every request carries a token, and every token carries one access level:

- `default`: sees non-confidential facts; read tools + propose_fact.
- `confidential`: sees everything; read tools + propose_fact.
- `owner`: sees everything; read tools + setup tools; owns the review queue.

A default or confidential token can also have a role, which decides which
tagged information it sees (see roles.py).

Tokens are long random strings shown once at creation. Only a SHA-256 hash is
stored, which is safe for high-entropy secrets like these (no password
stretching needed) and lets a lookup go straight to the matching row.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from enum import Enum

from zerostore.store.state import StateDB, utcnow

TOKEN_PREFIX = "zs_"


class AccessLevel(str, Enum):
    DEFAULT = "default"
    CONFIDENTIAL = "confidential"
    OWNER = "owner"

    @property
    def sees_confidential(self) -> bool:
        return self in (AccessLevel.CONFIDENTIAL, AccessLevel.OWNER)

    @property
    def is_owner(self) -> bool:
        return self is AccessLevel.OWNER

    @property
    def can_propose(self) -> bool:
        return self in (AccessLevel.DEFAULT, AccessLevel.CONFIDENTIAL)


class TokenError(Exception):
    """A token is missing, unknown or revoked."""


@dataclass(frozen=True)
class TokenInfo:
    id: str
    name: str
    level: AccessLevel
    auto_approve: bool
    created_at: str
    revoked_at: str | None
    last_used_at: str | None
    role: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, state: StateDB):
        self.state = state

    def create(
        self, name: str, level: AccessLevel | str, *, raw: str | None = None, role: str | None = None
    ) -> tuple[str, TokenInfo]:
        """Create a token. Returns (raw token, info); the raw token is never stored.

        `role` names a role from the settings (checked by the caller); owner tokens have none.

        `raw` re-registers a token made earlier (the demo keeps its agents' tokens
        across resets); it must look like one this method generates.
        """
        level = AccessLevel(level)
        name = name.strip()
        if not name:
            raise ValueError("a token needs a name, e.g. cursor-laptop")
        if raw is not None and not (raw.startswith(TOKEN_PREFIX) and len(raw) >= 40):
            raise ValueError("not a ZeroStore token")
        role = (role or "").strip() or None
        if level is AccessLevel.OWNER:
            role = None  # the owner sees everything
        token_id = secrets.token_hex(4)
        raw = raw or f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        now = utcnow()
        with self.state.tx() as cur:
            cur.execute(
                "INSERT INTO tokens (id, name, level, token_hash, created_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (token_id, name, level.value, hash_token(raw), now, role),
            )
        detail = f"{token_id} ({name}, {level.value}{f', role {role}' if role else ''})"
        self.state.audit("token_created", detail=detail)
        return raw, TokenInfo(token_id, name, level, False, now, None, None, role)

    def verify(self, raw: str | None) -> TokenInfo:
        """Return the active token matching `raw`, or raise TokenError."""
        if not raw:
            raise TokenError("no ZeroStore token was provided")
        rows = self.state.query("SELECT * FROM tokens WHERE token_hash = ?", (hash_token(raw.strip()),))
        if not rows:
            raise TokenError("this ZeroStore token is not recognized")
        info = _info(rows[0])
        if not info.active:
            raise TokenError("this ZeroStore token was revoked")
        with self.state.tx() as cur:
            cur.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (utcnow(), info.id))
        return info

    def get(self, token_id: str) -> TokenInfo | None:
        rows = self.state.query("SELECT * FROM tokens WHERE id = ?", (token_id,))
        return _info(rows[0]) if rows else None

    def list(self, include_revoked: bool = False) -> list[TokenInfo]:
        sql = "SELECT * FROM tokens"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        return [_info(r) for r in self.state.query(sql + " ORDER BY created_at, id")]

    def revoke(self, token_id: str) -> bool:
        with self.state.tx() as cur:
            cur.execute(
                "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (utcnow(), token_id)
            )
            changed = cur.rowcount == 1
        if changed:
            self.state.audit("token_revoked", detail=token_id)
        return changed

    def set_role(self, token_id: str, role: str | None) -> bool:
        """Give an agent token another role (None: untagged information only). Owner tokens have none."""
        with self.state.tx() as cur:
            cur.execute(
                "UPDATE tokens SET role = ? WHERE id = ? AND revoked_at IS NULL AND level != 'owner'",
                ((role or "").strip() or None, token_id),
            )
            changed = cur.rowcount == 1
        if changed:
            self.state.audit("token_role_set", detail=f"{token_id} -> {role or 'no role'}")
        return changed

    def set_auto_approve(self, token_id: str, enabled: bool) -> bool:
        """Per-token auto-approve for agent proposals. Off by default; owner-only to change."""
        with self.state.tx() as cur:
            cur.execute(
                "UPDATE tokens SET auto_approve = ? WHERE id = ? AND revoked_at IS NULL",
                (int(enabled), token_id),
            )
            return cur.rowcount == 1


def _info(row) -> TokenInfo:
    return TokenInfo(
        id=row["id"],
        name=row["name"],
        level=AccessLevel(row["level"]),
        auto_approve=bool(row["auto_approve"]),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
        role=row["role"],
    )
