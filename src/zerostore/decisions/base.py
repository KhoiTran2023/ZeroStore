"""The decision engine: every judgment call the librarian makes, as typed questions.

Each question comes back as an answer with a probability or score from 0 to 1,
whichever backend answers it, so the thresholds in zerostore.yaml mean the
same thing everywhere:

- `keep`: is this chunk lasting company knowledge, or noise? (yes/no)
- `route`: which topics is this text or question about? (choice)
- `compare`: does a new chunk add to, update, or contradict an existing fact? (choice)
- `on_delete`: a source file disappeared: was it moved, should it be retired or kept? (choice)
- `score`: how well does each fact answer a question, given a steering rule? (score per fact)

Backends: `jev` (TypeSafe's decision model), `llm` (your own AI provider) and
`local` (a small reranker on your CPU, `score` only).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class EngineError(Exception):
    """The engine couldn't answer (unreachable, refused the key, or gave an unreadable answer)."""


class Unsupported(EngineError):
    """This backend doesn't answer that kind of question."""


@dataclass
class YesNo:
    """A yes/no answer. `probability` is the chance the answer is yes."""

    probability: float

    @property
    def yes(self) -> bool:
        return self.probability >= 0.5

    @property
    def confidence(self) -> float:
        return max(self.probability, 1.0 - self.probability)


@dataclass
class Pick:
    """One option out of several, with the full distribution and a 0-1 confidence."""

    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0


# The questions, worded once so every backend asks the same thing ----------

KEEP_QUESTION = (
    "Is this text lasting company knowledge that someone could need later, such as a decision, "
    "policy, price, plan, process, fact about a customer or product, or a definition? Answer no "
    "for noise: small talk, scheduling chatter, greetings, placeholders and boilerplate."
)

ROUTE_QUESTION = "Which of these company topics is this text mainly about?"
NO_TOPIC = "none"

COMPARE_QUESTION = (
    "A document added or changed the NEW text. EXISTING is the closest fact from another document. "
    "How does NEW relate to EXISTING?"
)
COMPARE_OPTIONS = {
    "new": "NEW adds information EXISTING doesn't cover, or they are about different things.",
    "changed": "NEW is an update to the same thing EXISTING describes (for example a new price, "
    "date, owner or decision), so EXISTING is out of date.",
    "conflict": "NEW and EXISTING contradict each other about the same thing, and it isn't clear "
    "that NEW is an intentional update.",
}

ON_DELETE_QUESTION = (
    "A file was deleted from a company source. Decide what should happen to the facts it held. "
    "Consider whether one of the candidate documents holds the same content (the file was moved "
    "or renamed), whether a newer document replaced it, and whether it was a draft or an "
    "established policy."
)
ON_DELETE_OPTIONS = {
    "retire": "Stop serving its facts: it was removed on purpose, replaced, or was only a draft.",
    "keep": "Keep serving its facts: they are still true and nothing else holds them.",
    "unsure": "It isn't clear; a person should decide.",
}
MOVED_PREFIX = "moved_to_"  # option "moved_to_<n>" means the file now lives in candidate n

SCORE_QUESTION = "How well does this fact answer the question?"
SCORE_LEVELS = [
    "Unrelated to the question.",
    "Related, but doesn't answer the question.",
    "Partly answers the question.",
    "Answers the question.",
    "Answers the question directly and states the current decision.",
]


@dataclass
class DeletedFile:
    title: str
    source: str
    location: str
    excerpt: str


@dataclass
class Candidate:
    """A document that might hold a deleted file's content now."""

    title: str
    source: str
    excerpt: str
    added_this_sync: bool


class DecisionEngine(ABC):
    """One backend. Methods take lists so a backend can batch or run calls in parallel."""

    name: str = "engine"
    label: str = "Engine"  # shown to people, e.g. "Jev" or "your LLM (claude-sonnet-5-5)"

    @property
    def cache_id(self) -> str:
        """Names this engine and model, so a remembered answer is reused only by the one that gave it."""
        return f"{type(self).__name__}:{self.label}"

    @abstractmethod
    def keep(self, texts: list[str]) -> list[YesNo]: ...

    @abstractmethod
    def route(self, texts: list[str], topics: list[str]) -> list[Pick]:
        """Pick a topic for each text; the choice is NO_TOPIC when none fits."""

    @abstractmethod
    def compare(self, pairs: list[tuple[str, str]]) -> list[Pick]:
        """For each (new, existing) pair, pick one of COMPARE_OPTIONS."""

    @abstractmethod
    def on_delete(self, deleted: DeletedFile, candidates: list[Candidate]) -> Pick:
        """Pick one of ON_DELETE_OPTIONS, or f"{MOVED_PREFIX}{n}" for candidate n."""

    @abstractmethod
    def score(self, question: str, facts: list[str], rule: str) -> list[float]:
        """A 0-1 score per fact for how well it answers `question`, following `rule`."""

    def classify(self, texts: list[str], topics: list[str]) -> list[tuple[YesNo, Pick]]:
        """`keep` and `route` together; backends that can ask both at once override this."""
        return list(zip(self.keep(texts), self.route(texts, topics), strict=True))

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


def topics_from(pick: Pick, floor: float = 0.25) -> list[str]:
    """Topic tags from a route answer: the top topic plus any other likely ones."""
    tags = [t for t, p in sorted(pick.probabilities.items(), key=lambda kv: -kv[1]) if p >= floor]
    if pick.choice not in tags:
        tags.insert(0, pick.choice)
    return [t for t in tags if t != NO_TOPIC]


def clamp(value: object) -> float:
    """A 0-1 number from an engine's answer. Anything unreadable is the engine's error, not a crash."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise EngineError(f"the engine sent {value!r} where a number was expected") from None
    if number != number:  # NaN
        raise EngineError("the engine sent NaN where a number was expected")
    return min(1.0, max(0.0, number))


def on_delete_options(candidates: list[Candidate]) -> dict[str, str]:
    options = {
        f"{MOVED_PREFIX}{i}": f"Moved or renamed: candidate {i} ({c.title}) holds the same content now."
        for i, c in enumerate(candidates)
    }
    return {**options, **ON_DELETE_OPTIONS}
