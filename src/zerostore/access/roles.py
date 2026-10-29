"""Roles and tags: which agents see which information.

A tag marks information by where it lives (a Drive folder and everything in it,
a GitHub repo, a Linear label). A role lists the tags its agents may see, and each
token (or connector) can have one role. Information with no tag is for every
agent. A document with several tags is shown only to roles that have all of them.

The `confidential` places and access level work as before, next to tags: a
default token never sees confidential information, whatever its role. Owner
tokens see everything.

Each document's tags are kept with its facts as an access key such as
`,finance,sales,` (empty for none), so a search can leave out every fact that
carries a tag the caller's role lacks.
"""

from __future__ import annotations

from collections.abc import Iterable

from zerostore.config import Config, LocationRule
from zerostore.connectors.base import SourceDocument


def access_key(tags: Iterable[str]) -> str:
    tags = sorted(set(tags))
    return f",{','.join(tags)}," if tags else ""


def key_tags(key: str | None) -> list[str]:
    return [t for t in (key or "").split(",") if t]


def matches(doc: SourceDocument, rule: LocationRule) -> bool:
    """Whether a document lives in the place a rule names."""
    if rule.google_drive and doc.source == "google_drive":
        folder = rule.google_drive.strip("/")
        return doc.location == folder or doc.location.startswith(folder + "/")
    if rule.github and doc.source == "github":
        return doc.location.lower() == rule.github.lower()
    if rule.linear_label and doc.source == "linear":
        return rule.linear_label.lower() in (label.lower() for label in doc.labels)
    if rule.folder and doc.source == "folders":
        folder = rule.folder.rstrip("/") or "/"
        return doc.location == folder or doc.location.startswith(folder.rstrip("/") + "/")
    return False


def tags_for(doc: SourceDocument, config: Config) -> list[str]:
    return sorted(name for name, rules in config.tags.items() if any(matches(doc, r) for r in rules))


def hidden_tags(config: Config, role: str | None, *, owner: bool, known: Iterable[str] = ()) -> list[str]:
    """The tags whose facts this caller must not see.

    `known` adds tags still on stored facts that the settings no longer define (until the
    next sync re-checks them), so a removed tag stays hidden rather than turning public.
    """
    if owner:
        return []
    allowed = set(config.roles.get(role or "", []))
    return sorted((set(config.tags) | set(known)) - allowed)


def role_problem(config: Config, role: str | None) -> str | None:
    """Why a role can't be given to a token, or None when it can."""
    if role and role not in config.roles:
        names = ", ".join(config.roles) or "none yet"
        return f"there is no role called {role!r} in zerostore.yaml (roles: {names})"
    return None
