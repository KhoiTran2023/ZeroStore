"""Your own AI provider as the decision engine.

The same typed questions Jev answers, asked of a chat model with structured
(JSON) output. The model reports its own confidence, which is less reliable
than Jev's calibrated probabilities, so its answers sit a little further from
the thresholds in practice.

Providers: Anthropic (Messages API) and anything that speaks the OpenAI chat
format: OpenAI, Gemini, OpenRouter and a local Ollama. Items are batched, so
a sync makes a handful of calls rather than one per chunk.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from zerostore.decisions.base import (
    COMPARE_OPTIONS,
    COMPARE_QUESTION,
    KEEP_QUESTION,
    NO_TOPIC,
    ON_DELETE_QUESTION,
    ROUTE_QUESTION,
    SCORE_LEVELS,
    SCORE_QUESTION,
    Candidate,
    DecisionEngine,
    DeletedFile,
    EngineError,
    Pick,
    YesNo,
    clamp,
    on_delete_options,
)

BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}

# "mid-tier" in zerostore.yaml means the provider's balanced model. Any model name works instead.
MID_TIER = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-6-sol",
    "gemini": "gemini-3.8-flash",
}

RETRY_STATUS = {429, 500, 502, 503, 504, 529}
MAX_TEXT = 6_000  # characters per item sent to the model
BATCH = 15  # items per call

SYSTEM = (
    "You are the librarian of a company knowledge base. You make small, careful judgment calls "
    "and answer only in the requested JSON format. Every confidence or probability you give is "
    "your honest chance of being right, from 0 to 1: use values near 0.5 when unsure, and above "
    "0.9 only when the text leaves no real doubt."
)


def resolve_model(provider: str, model: str) -> str:
    if model != "mid-tier":
        return model
    if provider not in MID_TIER:
        raise EngineError(f"name a model for {provider} in ai.llm_model (for example the one you run)")
    return MID_TIER[provider]


def _trim(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[...]"


def _items(texts: list[str]) -> str:
    return "\n\n".join(f"<item id={i}>\n{_trim(t)}\n</item>" for i, t in enumerate(texts))


def _str_in(value: Any, options: Any) -> bool:
    return isinstance(value, str) and value in options


def _number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def _list_of(item: dict[str, Any]) -> dict[str, Any]:
    props = {"id": {"type": "integer"}, **item}
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": list(props),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


class LLMEngine(DecisionEngine):
    name = "llm"

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | None,
        *,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        workers: int = 4,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if provider not in BASE_URLS:
            raise EngineError(f"unknown AI provider {provider!r}")
        if not api_key and provider != "ollama":
            raise EngineError("your AI provider needs a key: set ai.llm_api_key (e.g. ${LLM_API_KEY})")
        self.provider = provider
        self.model = resolve_model(provider, model)
        self.label = f"your LLM ({self.model})"
        self.base_url = (base_url or BASE_URLS[provider]).rstrip("/")
        self.client = client or httpx.Client(timeout=120)
        if provider == "anthropic":
            self.client.headers.update({"x-api-key": api_key or "", "anthropic-version": "2023-06-01"})
        elif api_key:
            self.client.headers.update({"Authorization": f"Bearer {api_key}"})
        self.workers = workers
        self.retries = retries
        self.sleep = sleep
        self.calls = 0
        self.seconds = 0.0
        self._stats = threading.Lock()

    # Transport ---------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                response = self.client.post(self.base_url + path, json=body)
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    self.sleep(1.0 * 2**attempt)
                    continue
                raise EngineError(f"{self.provider} could not be reached: {exc}") from exc
            finally:
                with self._stats:
                    self.calls += 1
                    self.seconds += time.monotonic() - started
            if response.status_code in RETRY_STATUS and attempt < self.retries:
                self.sleep(1.0 * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise EngineError(f"{self.provider} refused the key in ai.llm_api_key")
            if response.status_code >= 400:
                raise EngineError(f"{self.provider} returned {response.status_code}: {response.text[:200]}")
            try:
                return response.json()
            except ValueError as exc:
                raise EngineError(f"{self.provider} sent a reply that isn't JSON") from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def ask(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One structured-output call. Returns the parsed JSON object."""
        if self.provider == "anthropic":
            body = {
                "model": self.model,
                "max_tokens": 4096,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [{"name": "answer", "description": "Record the answer.", "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "answer"},
            }
            reply = self._post("/messages", body)
            for block in reply.get("content", []):
                if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    return block["input"]
            raise EngineError("the model didn't answer in the requested format")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            },
        }
        reply = self._post("/chat/completions", body)
        try:
            content = reply["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EngineError("the model didn't answer in the requested format") from exc
        if not isinstance(parsed, dict):
            raise EngineError("the model didn't answer in the requested format")
        return parsed

    def _batched(self, texts: list[Any], fn: Callable[[list[Any]], list[Any]]) -> list[Any]:
        batches = [texts[i : i + BATCH] for i in range(0, len(texts), BATCH)]
        if len(batches) <= 1:
            return [r for b in batches for r in fn(b)]
        with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as pool:
            return [r for results in pool.map(fn, batches) for r in results]

    @staticmethod
    def _by_id(answer: dict[str, Any], count: int) -> list[dict[str, Any]]:
        rows = {}
        for row in answer.get("answers") or []:
            if isinstance(row, dict) and isinstance(row.get("id"), int):
                rows[row["id"]] = row
        missing = [i for i in range(count) if i not in rows]
        if missing:
            raise EngineError(f"the model skipped {len(missing)} of {count} items")
        return [rows[i] for i in range(count)]

    # The questions -----------------------------------------------------------

    def classify(self, texts: list[str], topics: list[str]) -> list[tuple[YesNo, Pick]]:
        options = [*topics, NO_TOPIC]
        schema = _list_of(
            {
                "keep_probability": _number("chance from 0 to 1 that the item is lasting company knowledge"),
                "topic": {"type": "string", "enum": options},
                "topic_confidence": _number("chance from 0 to 1 that the topic is right"),
            }
        )

        def batch(chunk: list[str]) -> list[tuple[YesNo, Pick]]:
            prompt = (
                f"For each item, answer two questions.\n1. {KEEP_QUESTION}\n2. {ROUTE_QUESTION} "
                f"Topics: {', '.join(topics)}. Use {NO_TOPIC!r} if none fits.\n\n{_items(chunk)}"
            )
            out = []
            for row in self._by_id(self.ask(prompt, schema), len(chunk)):
                topic = row.get("topic") if _str_in(row.get("topic"), options) else NO_TOPIC
                conf = clamp(row.get("topic_confidence", 0.0))
                out.append((YesNo(clamp(row.get("keep_probability", 0.5))), Pick(topic, {topic: conf}, conf)))
            return out

        return self._batched(texts, batch)

    def keep(self, texts: list[str]) -> list[YesNo]:
        return [k for k, _ in self.classify(texts, [NO_TOPIC])]

    def route(self, texts: list[str], topics: list[str]) -> list[Pick]:
        return [p for _, p in self.classify(texts, topics)]

    def compare(self, pairs: list[tuple[str, str]]) -> list[Pick]:
        labels = list(COMPARE_OPTIONS)
        schema = _list_of(
            {
                "relation": {"type": "string", "enum": labels},
                "confidence": _number("chance from 0 to 1 that the relation is right"),
            }
        )
        meaning = "\n".join(f"- {k}: {v}" for k, v in COMPARE_OPTIONS.items())

        def batch(chunk: list[tuple[str, str]]) -> list[Pick]:
            items = "\n\n".join(
                f"<item id={i}>\nNEW:\n{_trim(new)}\n\nEXISTING:\n{_trim(old)}\n</item>"
                for i, (new, old) in enumerate(chunk)
            )
            prompt = f"{COMPARE_QUESTION}\n{meaning}\n\n{items}"
            out = []
            for row in self._by_id(self.ask(prompt, schema), len(chunk)):
                if not _str_in(row.get("relation"), COMPARE_OPTIONS):
                    raise EngineError("the model gave a relation that wasn't one of the options")
                conf = clamp(row.get("confidence", 0.0))
                out.append(Pick(row["relation"], {row["relation"]: conf}, conf))
            return out

        return self._batched(pairs, batch)

    def on_delete(self, deleted: DeletedFile, candidates: list[Candidate]) -> Pick:
        options = on_delete_options(candidates)
        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(options)},
                "confidence": _number("chance from 0 to 1 that the action is right"),
            },
            "required": ["action", "confidence"],
            "additionalProperties": False,
        }
        listed = "\n\n".join(
            f"<candidate id={i} title={c.title!r} source={c.source} "
            f"added_in_the_same_sync={str(c.added_this_sync).lower()}>\n{_trim(c.excerpt)}\n</candidate>"
            for i, c in enumerate(candidates)
        )
        prompt = (
            f"{ON_DELETE_QUESTION}\n\nActions:\n"
            + "\n".join(f"- {k}: {v}" for k, v in options.items())
            + f"\n\n<deleted title={deleted.title!r} source={deleted.source} folder={deleted.location!r}>\n"
            f"{_trim(deleted.excerpt)}\n</deleted>\n\n{listed or 'No candidates.'}"
        )
        answer = self.ask(prompt, schema)
        if not _str_in(answer.get("action"), options):
            raise EngineError("the model gave an action that wasn't one of the options")
        conf = clamp(answer.get("confidence", 0.0))
        return Pick(answer["action"], {answer["action"]: conf}, conf)

    def score(self, question: str, facts: list[str], rule: str) -> list[float]:
        schema = _list_of({"score": _number("0 to 1, following the scale")})
        scale = "\n".join(f"- {i / (len(SCORE_LEVELS) - 1):.2f}: {s}" for i, s in enumerate(SCORE_LEVELS))

        def batch(chunk: list[str]) -> list[float]:
            prompt = (
                f"{SCORE_QUESTION} Score every item from 0 to 1 on this scale:\n{scale}\n"
                f"Follow this rule: {rule}\n\n<question>\n{question}\n</question>\n\n{_items(chunk)}"
            )
            return [clamp(row.get("score", 0.0)) for row in self._by_id(self.ask(prompt, schema), len(chunk))]

        return self._batched(facts, batch)

    def close(self) -> None:
        self.client.close()
