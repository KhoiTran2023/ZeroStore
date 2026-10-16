"""Jev, TypeSafe's decision model: typed answers with calibrated probabilities.

One HTTP endpoint (`POST /v1/systemone`) takes a `state` (the text being
judged) and named `questions`, each a `noul` (yes/no), `choice` or `score`
(position on a scale). Each request is about a tenth of a second, so the
librarian sends one request per chunk or per pair and runs them in parallel.
Any Jev-compatible endpoint works by changing `ai.jev_base_url`.
"""

from __future__ import annotations

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

RETRY_STATUS = {429, 500, 502, 503, 504, 529}
MAX_TEXT = 24_000  # characters per text; the state and questions share about 64k tokens
SCORE_BATCH = 20  # facts scored per request


def _trim(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n[...]"


class JevEngine(DecisionEngine):
    name = "jev"
    label = "Jev"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "jev-latest",
        base_url: str = "https://api.typesafe.ai",
        client: httpx.Client | None = None,
        workers: int = 8,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise EngineError("Jev needs an API key: set ai.jev_api_key (e.g. ${JEV_API_KEY})")
        self.model = model
        self.url = base_url.rstrip("/") + "/v1/systemone"
        self.client = client or httpx.Client(timeout=30)
        self.client.headers.update({"Authorization": f"Bearer {api_key}"})
        self.workers = workers
        self.retries = retries
        self.sleep = sleep
        self.calls = 0
        self.seconds = 0.0
        self._stats = threading.Lock()

    @property
    def cache_id(self) -> str:
        return f"jev:{self.model}"

    # Transport ---------------------------------------------------------------

    def _ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        body = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                response = self.client.post(self.url, json=body)
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    self.sleep(0.5 * 2**attempt)
                    continue
                raise EngineError(f"Jev could not be reached: {exc}") from exc
            finally:
                with self._stats:
                    self.calls += 1
                    self.seconds += time.monotonic() - started
            if response.status_code in RETRY_STATUS and attempt < self.retries:
                self.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise EngineError("Jev refused the API key in ai.jev_api_key")
            if response.status_code >= 400:
                raise EngineError(f"Jev returned {response.status_code}: {response.text[:200]}")
            try:
                answers = response.json()["answers"]
                missing = set(questions) - set(answers)
            except (ValueError, KeyError, TypeError) as exc:
                raise EngineError("Jev sent an answer ZeroStore couldn't read") from exc
            if missing:
                raise EngineError(f"Jev left questions unanswered: {', '.join(sorted(missing))}")
            return answers
        raise AssertionError("unreachable")  # pragma: no cover

    def _parallel(self, fn: Callable[[Any], Any], items: list[Any]) -> list[Any]:
        if len(items) <= 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=min(self.workers, len(items))) as pool:
            return list(pool.map(fn, items))

    # Answers -----------------------------------------------------------------

    @staticmethod
    def _yes_no(answer: dict[str, Any]) -> YesNo:
        try:
            return YesNo(clamp(answer["noul"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineError("Jev sent a yes/no answer without a probability") from exc

    @staticmethod
    def _pick(answer: dict[str, Any], options: dict[str, Any]) -> Pick:
        try:
            choice = answer["choice"]
            probabilities = {str(k): clamp(v) for k, v in (answer.get("probabilities") or {}).items()}
            confidence = clamp(answer.get("confidence", probabilities.get(choice, 0.0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineError("Jev sent a choice answer ZeroStore couldn't read") from exc
        if not isinstance(choice, str) or choice not in options:
            raise EngineError(f"Jev picked {choice!r}, which wasn't one of the options")
        return Pick(choice, probabilities, confidence)

    @staticmethod
    def _score(answer: dict[str, Any]) -> float:
        """Jev's score is a level number (the mean over levels); map it onto 0-1."""
        try:
            levels = sorted(int(k) for k in answer.get("legend") or {}) or [0, len(SCORE_LEVELS) - 1]
            low, high = levels[0], levels[-1]
            return clamp((float(answer["score"]) - low) / (high - low))
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise EngineError("Jev sent a score ZeroStore couldn't read") from exc

    @staticmethod
    def _topic_question(topics: list[str]) -> dict[str, Any]:
        criteria: dict[str, str | None] = {t: None for t in topics}
        criteria[NO_TOPIC] = "None of these topics fits."
        return {
            "type": "choice",
            "instructions": f"{ROUTE_QUESTION} The text is `text`.",
            "criteria": criteria,
        }

    # The questions -----------------------------------------------------------

    def keep(self, texts: list[str]) -> list[YesNo]:
        question = {"type": "noul", "instructions": f"{KEEP_QUESTION} The text is `text`."}
        return self._parallel(
            lambda t: self._yes_no(self._ask({"text": _trim(t)}, {"keep": question})["keep"]), texts
        )

    def route(self, texts: list[str], topics: list[str]) -> list[Pick]:
        question = self._topic_question(topics)
        options = question["criteria"]
        return self._parallel(
            lambda t: self._pick(self._ask({"text": _trim(t)}, {"topic": question})["topic"], options), texts
        )

    def classify(self, texts: list[str], topics: list[str]) -> list[tuple[YesNo, Pick]]:
        keep = {"type": "noul", "instructions": f"{KEEP_QUESTION} The text is `text`."}
        topic = self._topic_question(topics)

        def one(text: str) -> tuple[YesNo, Pick]:
            answers = self._ask({"text": _trim(text)}, {"keep": keep, "topic": topic})
            return self._yes_no(answers["keep"]), self._pick(answers["topic"], topic["criteria"])

        return self._parallel(one, texts)

    def compare(self, pairs: list[tuple[str, str]]) -> list[Pick]:
        question = {
            "type": "choice",
            "instructions": f"{COMPARE_QUESTION} NEW is `NEW`; EXISTING is `EXISTING`.",
            "criteria": COMPARE_OPTIONS,
        }

        def one(pair: tuple[str, str]) -> Pick:
            state = {"NEW": _trim(pair[0]), "EXISTING": _trim(pair[1])}
            return self._pick(self._ask(state, {"relation": question})["relation"], COMPARE_OPTIONS)

        return self._parallel(one, pairs)

    def on_delete(self, deleted: DeletedFile, candidates: list[Candidate]) -> Pick:
        options = on_delete_options(candidates)
        state = {
            "deleted": {
                "title": deleted.title,
                "source": deleted.source,
                "folder": deleted.location,
                "content": _trim(deleted.excerpt),
            },
            "candidates": {
                str(i): {
                    "title": c.title,
                    "source": c.source,
                    "added_in_the_same_sync": c.added_this_sync,
                    "content": _trim(c.excerpt),
                }
                for i, c in enumerate(candidates)
            },
        }
        question = {
            "type": "choice",
            "instructions": f"{ON_DELETE_QUESTION} The deleted file is `deleted`; "
            "the candidates are `candidates`.",
            "criteria": options,
        }
        return self._pick(self._ask(state, {"action": question})["action"], options)

    def score(self, question: str, facts: list[str], rule: str) -> list[float]:
        batches = [facts[i : i + SCORE_BATCH] for i in range(0, len(facts), SCORE_BATCH)]

        def one(batch: list[str]) -> list[float]:
            state = {
                "question": question,
                "rule": rule,
                "facts": {f"f{i}": _trim(f) for i, f in enumerate(batch)},
            }
            questions = {
                f"f{i}": {
                    "type": "score",
                    "instructions": f"{SCORE_QUESTION} The question is `question`; the fact is "
                    f"`facts.f{i}`. Follow this rule: `rule`.",
                    "criteria": SCORE_LEVELS,
                }
                for i in range(len(batch))
            }
            answers = self._ask(state, questions)
            return [self._score(answers[f"f{i}"]) for i in range(len(batch))]

        return [s for scores in self._parallel(one, batches) for s in scores]

    def close(self) -> None:
        self.client.close()
