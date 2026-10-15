"""Reads and checks zerostore.yaml.

Only `sources` is required; everything else has a default. Values written as
`${NAME}` are filled in from environment variables, or from `secrets.env` next
to the config file (where the web UI saves keys), so API keys never have to
live in the file itself. A real environment variable wins over secrets.env.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_HOME = Path("~/.zerostore")
CONFIG_FILENAME = "zerostore.yaml"
SECRETS_FILENAME = "secrets.env"
OWNER_TOKEN_FILE = "owner-token"  # written by `zerostore init`, next to the config

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """zerostore.yaml is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoogleDriveSource(_Section):
    folders: list[str] = Field(default_factory=list)
    # Path to a Google credentials file: what "Sign in with Google" saves, or a service-account key.
    credentials: str | None = "~/.zerostore/google-credentials.json"


class GitHubSource(_Section):
    repos: list[str] = Field(default_factory=list)
    token: str | None = None  # needed for private repos, e.g. ${GITHUB_TOKEN}
    include: list[str] = Field(default_factory=lambda: ["*.md", "*.mdx", "*.markdown", "*.txt", "*.rst"])

    @field_validator("repos")
    @classmethod
    def _owner_slash_repo(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                raise ValueError(f"GitHub repos are written as owner/name, got {repo!r}")
        return repos


class LinearSource(_Section):
    teams: list[str] = Field(default_factory=list)
    api_key: str | None = None  # e.g. ${LINEAR_API_KEY}


class Sources(_Section):
    google_drive: GoogleDriveSource | None = None
    github: GitHubSource | None = None
    linear: LinearSource | None = None

    def enabled(self) -> list[str]:
        return [name for name in ("google_drive", "github", "linear") if getattr(self, name) is not None]


class SyncSettings(_Section):
    schedule: str = "0 6 * * *"

    @field_validator("schedule")
    @classmethod
    def _valid_cron(cls, schedule: str) -> str:
        from apscheduler.triggers.cron import CronTrigger

        if len(schedule.split()) != 5:
            raise ValueError(
                f"sync.schedule must be a cron line with 5 fields like '0 6 * * *', got {schedule!r}"
            )
        try:
            CronTrigger.from_crontab(schedule)
        except ValueError as exc:
            raise ValueError(f"sync.schedule {schedule!r} isn't a valid cron line: {exc}") from None
        return schedule


LLM_PROVIDERS = ("anthropic", "openai", "gemini", "openrouter", "ollama")

DEFAULT_TOPICS = [
    "pricing and plans",
    "product and roadmap",
    "customers and sales",
    "engineering",
    "people and hiring",
    "finance",
    "legal and security",
    "marketing",
    "operations",
    "company strategy",
]


class AISettings(_Section):
    decision_engine: Literal["jev", "llm", "demo"] = "jev"  # demo: the scripted judge of `zerostore demo`
    jev_api_key: str | None = None
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai"  # or a Jev-compatible endpoint such as OpenRouter
    llm_provider: str | None = None
    llm_model: str = "mid-tier"  # "mid-tier" picks the provider's balanced model; any model name works
    llm_api_key: str | None = None
    llm_base_url: str | None = None  # only for a self-hosted or proxied endpoint
    # provider, local (the fastembed model), or basic (word matching only, no model; used by the demo)
    embeddings: Literal["provider", "local", "basic"] = "provider"

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, provider: str | None) -> str | None:
        if provider is not None and provider.lower() not in LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {', '.join(LLM_PROVIDERS)}, got {provider!r}")
        return provider.lower() if provider else None

    @property
    def has_jev(self) -> bool:
        return bool(self.jev_api_key)

    @property
    def has_llm(self) -> bool:
        # Ollama runs locally and needs no key.
        return self.llm_provider == "ollama" or bool(self.llm_provider and self.llm_api_key)


class ReviewSettings(_Section):
    auto_apply_at: float = Field(0.80, ge=0.0, le=1.0)


class RetrievalSettings(_Section):
    max_facts: int = Field(5, ge=1, le=50)
    answer_cutoff: float = Field(0.50, ge=0.0, le=1.0)
    rerank_rule: str = "Prefer the current decision over old discussion about it."
    narrow_with: Literal["auto", "jev", "llm", "off"] = "auto"
    rerank_with: Literal["auto", "jev", "llm", "local", "off"] = "auto"
    topics: list[str] = Field(default_factory=lambda: list(DEFAULT_TOPICS))

    @field_validator("narrow_with", "rerank_with", mode="before")
    @classmethod
    def _bare_off(cls, value: Any) -> Any:
        return "off" if value is False else value  # YAML reads a bare `off` as false

    @field_validator("topics")
    @classmethod
    def _some_topics(cls, topics: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(t.strip() for t in topics if t.strip()))
        if not cleaned:
            raise ValueError("retrieval.topics needs at least one topic")
        if len(cleaned) > 100:
            raise ValueError("retrieval.topics allows at most 100 topics")
        return cleaned


class ConfidentialRule(_Section):
    google_drive: str | None = None
    linear_label: str | None = None
    github: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ConfidentialRule:
        set_fields = [k for k in ("google_drive", "linear_label", "github") if getattr(self, k)]
        if len(set_fields) != 1:
            raise ValueError("each confidential rule names exactly one of google_drive, linear_label, github")
        return self


class ServerSettings(_Section):
    host: str = "localhost"
    port: int = Field(8765, ge=1, le=65535)
    vectordb: str = "~/.zerostore/facts.lance"
    statedb: str = "~/.zerostore/state.sqlite"

    @field_validator("statedb")
    @classmethod
    def _sqlite_only_for_now(cls, statedb: str) -> str:
        if "://" in statedb:
            raise ValueError(
                "only a SQLite file path is supported for servers.statedb so far; Postgres comes later"
            )
        return statedb

    def vectordb_uri(self) -> str:
        """Local paths get `~` expanded; object-store URIs (s3://...) pass through."""
        return self.vectordb if "://" in self.vectordb else str(Path(self.vectordb).expanduser())

    def statedb_path(self) -> Path:
        return Path(self.statedb).expanduser()


class Config(_Section):
    sources: Sources
    sync: SyncSettings = Field(default_factory=SyncSettings)
    ai: AISettings = Field(default_factory=AISettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    confidential: list[ConfidentialRule] = Field(default_factory=list)
    agent_proposals: bool = True
    # Approved agent proposals are written here and read back as a source.
    decisions_file: str | None = None  # default: ~/.zerostore/decisions.md
    web_ui: bool = True
    servers: ServerSettings = Field(default_factory=ServerSettings)
    # Set only by `zerostore demo`: every source is read from this folder of made-up files.
    demo: str | None = None

    @model_validator(mode="after")
    def _engines_have_keys(self) -> Config:
        r, ai = self.retrieval, self.ai
        if ai.decision_engine == "demo" and not self.demo:
            raise ValueError("ai.decision_engine: demo is only for `zerostore demo`; use jev or llm")
        if "jev" in (r.narrow_with, r.rerank_with) and not ai.has_jev:
            raise ValueError("retrieval uses jev, but ai.jev_api_key is not set")
        if "llm" in (r.narrow_with, r.rerank_with) and not ai.has_llm:
            raise ValueError("retrieval uses llm, but ai.llm_provider and ai.llm_api_key are not both set")
        return self

    def decisions_path(self) -> Path:
        return Path(self.decisions_file).expanduser() if self.decisions_file else home_dir() / "decisions.md"

    def demo_path(self) -> Path | None:
        return Path(self.demo).expanduser() if self.demo else None


def expand_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace `${NAME}` with the environment variable NAME, recursively.

    A variable that isn't set becomes None when it is the whole value (so an
    optional key like `jev_api_key: ${JEV_API_KEY}` simply counts as unset) and
    an error when it is part of a longer string.
    """
    env = os.environ if env is None else env
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if not isinstance(value, str):
        return value

    whole = _ENV_PATTERN.fullmatch(value)
    if whole:
        return env.get(whole.group(1)) or None

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ConfigError(f"environment variable {name} is used in zerostore.yaml but is not set")
        return env[name]

    return _ENV_PATTERN.sub(_sub, value)


def read_secrets(path: Path) -> dict[str, str]:
    """NAME=value lines from a secrets.env file; blank lines and # comments are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"can't read {path}: {exc}") from None
    secrets = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            secrets[name] = value
    return secrets


def config_env(config_path: Path) -> dict[str, str]:
    """secrets.env next to the config, overridden by the real environment."""
    return {**read_secrets(config_path.parent / SECRETS_FILENAME), **os.environ}


def parse_config(data: Any, env: Mapping[str, str] | None = None) -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("zerostore.yaml must be a mapping of settings")
    if "sources" not in data:
        raise ConfigError("zerostore.yaml needs a `sources` section (it may be empty: `sources: {}`)")
    if data["sources"] is None:
        data = {**data, "sources": {}}
    try:
        return Config.model_validate(expand_env(data, env))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"zerostore.yaml has problems: {problems}") from None


def default_config_path() -> Path:
    """ZEROSTORE_CONFIG wins, then ./zerostore.yaml, then ~/.zerostore/zerostore.yaml."""
    env = os.environ.get("ZEROSTORE_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local
    return home_dir() / CONFIG_FILENAME


def home_dir() -> Path:
    # An empty ZEROSTORE_HOME counts as unset, never as "the current folder".
    return Path(os.environ.get("ZEROSTORE_HOME") or str(DEFAULT_HOME)).expanduser()


def resolve_config_path(path: Path | str | None = None) -> Path:
    return Path(path).expanduser() if path else default_config_path()


def load_config(path: Path | str | None = None) -> Config:
    path = resolve_config_path(path)
    if not path.exists():
        raise ConfigError(f"no config found at {path}. Run `zerostore init` to create one.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    return parse_config(data, config_env(path))
