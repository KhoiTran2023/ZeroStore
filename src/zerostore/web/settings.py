"""Reading and changing zerostore.yaml from the web UI.

Edits go into the same file the CLI reads, through ruamel.yaml so your
comments and layout survive. API keys never go into zerostore.yaml: they are
saved in secrets.env next to it (readable only by you) and the config refers
to them as `${NAME}`. Every save is checked by the same rules `zerostore`
uses before anything is written, so a bad value never reaches the file.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from zerostore.config import (
    LLM_PROVIDERS,
    SECRETS_FILENAME,
    Config,
    ConfigError,
    parse_config,
    read_secrets,
)

# Names a typed key is saved under. The ZEROSTORE_ prefix keeps a GITHUB_TOKEN or
# LLM_API_KEY already set in your shell from quietly winning over the key you typed.
SECRET_NAMES = {
    "llm_key": "ZEROSTORE_LLM_API_KEY",
    "jev_key": "ZEROSTORE_JEV_API_KEY",
    "github_token": "ZEROSTORE_GITHUB_TOKEN",
    "linear_key": "ZEROSTORE_LINEAR_API_KEY",
}
SCHEDULES = {
    "0 6 * * *": "daily at 06:00",
    "0 * * * *": "every hour",
    "*/15 * * * *": "every 15 minutes",
    "0 6 * * 1": "Mondays at 06:00",
}
ENGINE_CHOICES = ("auto", "jev", "llm", "off")
RERANK_CHOICES = ("auto", "jev", "llm", "local", "off")


class SettingsError(Exception):
    """The new settings were not saved; the message says what is wrong."""


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"can't read {path}: {exc}") from None


def version_of(text: str) -> str:
    """A fingerprint of the file as a page showed it, to catch edits made elsewhere meanwhile."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _check_version(text: str, expected: str | None) -> None:
    if expected and expected != version_of(text):
        raise SettingsError(
            "zerostore.yaml changed since this page was opened (in another tab or the CLI); "
            "reload the page to see it, then make your change again"
        )


def _secrets_env(path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return {**read_secrets(path.parent / SECRETS_FILENAME), **(extra or {}), **os.environ}


def validate(path: Path, text: str, new_secrets: Mapping[str, str] | None = None) -> Config:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SettingsError(f"that isn't valid YAML: {exc}") from None
    try:
        return parse_config(data, _secrets_env(path, new_secrets))
    except ConfigError as exc:
        raise SettingsError(str(exc)) from None


def _write(path: Path, text: str) -> None:
    """Replace a file in one step.

    A symlinked file is written where the link points (the link stays). An existing
    file keeps its permissions; a new one is readable only by you.
    """
    target = path.resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = stat.S_IRUSR | stat.S_IWUSR
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    except OSError as exc:
        raise SettingsError(f"can't write {target}: {exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise SettingsError(f"can't write {target}: {exc}") from None
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_text(path: Path, text: str, version: str | None = None) -> Config:
    """Save zerostore.yaml as typed in the YAML view, if it is valid."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # browsers send CRLF
    _check_version(read_text(path), version)
    config = validate(path, text)
    _write(path, text if text.endswith("\n") else text + "\n")
    return config


def save_secrets(path: Path, secrets: Mapping[str, str]) -> None:
    """Add or replace NAME=value lines in secrets.env next to the config."""
    if not secrets:
        return
    target = path.parent / SECRETS_FILENAME
    lines = (
        target.read_text(encoding="utf-8").splitlines()
        if target.exists()
        else [
            "# Keys saved by the ZeroStore web UI. zerostore.yaml refers to them as ${NAME}.",
        ]
    )
    left = dict(secrets)
    out = []
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in left:
            out.append(f"{name}={left.pop(name)}")
        else:
            out.append(line)
    out.extend(f"{name}={value}" for name, value in left.items())
    _write(target, "\n".join(out) + "\n")
    os.chmod(target.resolve(), stat.S_IRUSR | stat.S_IWUSR)  # keys: only you, always


# The form ------------------------------------------------------------------


def _split(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]


def _plain(value: str) -> str | DoubleQuotedScalarString:
    """Quote a string that YAML would otherwise read as something else (on, no, 1.0, null...)."""
    try:
        reads_back = yaml.safe_load(value)
    except yaml.YAMLError:
        reads_back = None
    return value if reads_back == value else DoubleQuotedScalarString(value)


def _flow(items: list[str]) -> CommentedSeq:
    seq = CommentedSeq([_plain(i) for i in items])
    seq.fa.set_flow_style()
    return seq


def _map(parent: CommentedMap, key: str) -> CommentedMap:
    if not isinstance(parent.get(key), dict):
        parent[key] = CommentedMap()
    return parent[key]


def _number(form: Mapping[str, str], key: str) -> float:
    try:
        return round(float(form[key]), 2)
    except ValueError:
        raise SettingsError(f"{key.replace('_', ' ')} must be a number") from None


def _secret(
    form: Mapping[str, str], field: str, target: CommentedMap, key: str, found: dict[str, str]
) -> None:
    value = (form.get(field) or "").strip()
    if not value:
        return
    if "\n" in value or "\r" in value:
        raise SettingsError("a key can't contain line breaks")
    name = SECRET_NAMES[field]
    found[name] = value
    target[key] = "${" + name + "}"


def apply_form(path: Path, form: Mapping[str, str]) -> Config:
    """Apply the sections of a settings form to zerostore.yaml and save it.

    Only the sections named in the form's `sections` field change; blank key
    fields keep the key already set.
    """
    y = _yaml()
    current = read_text(path)
    _check_version(current, form.get("version"))
    doc = y.load(current) or CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise SettingsError(f"{path.name} must be a mapping of settings")
    sections = set(_split(form.get("sections", "")))
    found: dict[str, str] = {}

    if "sources" in sections:
        sources = _map(doc, "sources")
        if form.get("drive_on"):
            drive = _map(sources, "google_drive")
            drive["folders"] = _flow(_split(form.get("drive_folders", "")))
            credentials = (form.get("drive_credentials") or "").strip()
            if credentials:
                drive["credentials"] = _plain(credentials)
        else:
            sources.pop("google_drive", None)
        if form.get("github_on"):
            github = _map(sources, "github")
            github["repos"] = _flow(_split(form.get("github_repos", "")))
            _secret(form, "github_token", github, "token", found)
        else:
            sources.pop("github", None)
        if form.get("linear_on"):
            linear = _map(sources, "linear")
            linear["teams"] = _flow(_split(form.get("linear_teams", "")))
            _secret(form, "linear_key", linear, "api_key", found)
        else:
            sources.pop("linear", None)

    if "ai" in sections:
        ai = _map(doc, "ai")
        engine = form.get("decision_engine", "")
        if engine in ("jev", "llm") or (engine == "demo" and doc.get("demo")):
            ai["decision_engine"] = engine
        provider = (form.get("llm_provider") or "").strip().lower()
        if provider and provider not in LLM_PROVIDERS:
            raise SettingsError(f"pick one of {', '.join(LLM_PROVIDERS)} as the AI provider")
        if provider:
            ai["llm_provider"] = provider
        else:
            ai.pop("llm_provider", None)
        model = (form.get("llm_model") or "").strip()
        if model:
            ai["llm_model"] = _plain(model)
        _secret(form, "llm_key", ai, "llm_api_key", found)
        _secret(form, "jev_key", ai, "jev_api_key", found)

    if "judgment" in sections:
        _map(doc, "review")["auto_apply_at"] = _number(form, "auto_apply_at")
        retrieval = _map(doc, "retrieval")
        retrieval["answer_cutoff"] = _number(form, "answer_cutoff")
        rule = (form.get("rerank_rule") or "").strip()
        if rule:
            retrieval["rerank_rule"] = _plain(rule)
        if form.get("narrow_with") in ENGINE_CHOICES:
            retrieval["narrow_with"] = form["narrow_with"]
        if form.get("rerank_with") in RERANK_CHOICES:
            retrieval["rerank_with"] = form["rerank_with"]
        doc["agent_proposals"] = bool(form.get("agent_proposals"))

    if "sync" in sections:
        schedule = " ".join((form.get("schedule") or "").split())
        if schedule:
            _map(doc, "sync")["schedule"] = schedule

    out = io.StringIO()
    y.dump(doc, out)
    text = out.getvalue()
    config = validate(path, text, found)
    save_secrets(path, found)
    _write(path, text)
    return config


def update_drive(
    path: Path, *, credentials: str | None = None, add: str | None = None, remove: str | None = None
) -> Config:
    """Turn on Google Drive with a credentials file, or add or remove one of its folders."""
    y = _yaml()
    doc = y.load(read_text(path)) or CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise SettingsError(f"{path.name} must be a mapping of settings")
    drive = _map(_map(doc, "sources"), "google_drive")
    folders = [str(f) for f in drive.get("folders") or []]
    if credentials:
        drive["credentials"] = _plain(credentials)
    if add and add not in folders:
        folders.append(add)
    if remove:
        folders = [f for f in folders if f != remove]
    drive["folders"] = _flow(folders)
    out = io.StringIO()
    y.dump(doc, out)
    config = validate(path, out.getvalue())
    _write(path, out.getvalue())
    return config


def key_state(raw: Any, value: str | None) -> str:
    """How a key is set, for the form: never the key itself."""
    if not value:
        return "not set"
    if isinstance(raw, str) and raw.startswith("${"):
        return f"set ({raw[2:-1]})"
    return "set in zerostore.yaml"


def form_values(path: Path) -> dict[str, Any]:
    """The current settings, as the form shows them. Keys are reported as set or not, never shown."""
    text = read_text(path)
    config = validate(path, text)
    raw = yaml.safe_load(text) or {}
    raw_ai = raw.get("ai") or {}
    raw_github = (raw.get("sources") or {}).get("github") or {}
    raw_linear = (raw.get("sources") or {}).get("linear") or {}
    s = config.sources
    return {
        "version": version_of(text),
        "config": config,
        "drive": s.google_drive,
        "github": s.github,
        "linear": s.linear,
        "github_token": key_state(raw_github.get("token"), s.github and s.github.token),
        "linear_key": key_state(raw_linear.get("api_key"), s.linear and s.linear.api_key),
        "llm_key": key_state(raw_ai.get("llm_api_key"), config.ai.llm_api_key),
        "jev_key": key_state(raw_ai.get("jev_api_key"), config.ai.jev_api_key),
        "providers": LLM_PROVIDERS,
        "schedules": SCHEDULES,
        "engine_choices": ENGINE_CHOICES,
        "rerank_choices": RERANK_CHOICES,
    }
