"""The demo's judge: plain word rules instead of a model, so `zerostore demo` runs
offline, needs no account and plays out the same way every time.

It answers the same questions as Jev or your LLM, tuned for the made-up
company in `zerostore/demo/`:

- keep: small talk (lunch, coffee, greetings) is noise; everything else is kept.
- route: the topic whose words appear most often.
- compare: two texts about the same subject (the Pro plan, the free tier, ...)
  that state a different value for it (a price, a user limit, a date) are an
  update; a tentative note ("maybe we ...")
  about a subject is an unclear conflict, so it waits for review.
- on_delete: a candidate with the same title means the file moved; else retire.
- score: the share of the question's words found in the fact.

It is labelled "demo judge" everywhere, so nobody mistakes it for a real model.
"""

from __future__ import annotations

import re

from zerostore.decisions.base import (
    MOVED_PREFIX,
    NO_TOPIC,
    Candidate,
    DecisionEngine,
    DeletedFile,
    Pick,
    YesNo,
)

_TOKEN = re.compile(r"\$?\d+(?:[.,]\d+)?|[a-z]+")

STOPWORDS = frozenset(
    """a about after all also am an and any are as at be been but by can could did do does
    doing for from get got had has have how i if in into is it its just me more most much my
    no not now of on or our out over per should so some still than that the their them then
    there these they this those to too up us was we were what when where which while who why
    will with would you your""".split()
)

NOISE = [
    re.compile(p)
    for p in (
        r"\blunch\b",
        r"\bcoffee\b",
        r"\bhappy (monday|friday|birthday)\b",
        r"\bsee you\b",
        r"\bthanks,? (all|everyone)\b",
    )
]

TENTATIVE = re.compile(r"\b(maybe|perhaps|should we|what if|thinking about|not sure)\b")

_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december)"

# Subjects two texts can disagree about: how to spot each one, and the value that matters for it.
SUBJECTS = {
    "the Pro plan": (re.compile(r"\bpro\b"), re.compile(r"\$\d+")),
    "the free tier": (re.compile(r"\bfree (tier|plan)\b"), re.compile(r"\b\d+ users?\b")),
    "the Team plan": (re.compile(r"\bteam plan\b"), re.compile(r"\$\d+")),
    "refunds": (re.compile(r"\brefund"), re.compile(r"\b\d+ days\b")),
    "the mobile app": (re.compile(r"\bmobile app\b"), re.compile(_MONTH + r" \d+")),
    "support hours": (re.compile(r"\bsupport hours\b"), re.compile(r"\b\d+:\d\d\b")),
}

TOPIC_WORDS = {
    "pricing and plans": "price pricing plan pro free tier cost billing discount annual seat subscription",
    "product and roadmap": "roadmap ship mobile app feature launch release calendar beta android ios",
    "customers and sales": "customer sales deal trial churn refund",
    "engineering": "api deploy bug code repo server database stack",
    "people and hiring": "hire hiring team onboarding handbook role holiday vacation ceo cto who",
    "finance": "runway revenue budget salary burn cash invoice",
    "legal and security": "legal privacy security gdpr contract terms",
    "marketing": "marketing campaign newsletter brand blog",
    "operations": "support hours office process tool",
    "company strategy": "strategy mission goal vision priority",
}


def stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def words(text: str) -> set[str]:
    """Content words, lowercased and roughly stemmed: 'Pricing' and 'prices' both become 'pric'."""
    return {stem(w) for w in _TOKEN.findall(text.lower()) if w not in STOPWORDS}


def _values(text: str, subject: re.Pattern[str], value: re.Pattern[str]) -> set[str]:
    """The values stated in the sentences that mention a subject, e.g. {'$20'} for the Pro plan."""
    found: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if subject.search(sentence):
            found.update(m.group(0) for m in value.finditer(sentence))
    return found


class DemoEngine(DecisionEngine):
    name = "demo"
    label = "demo judge"

    def __init__(self, topics: list[str] | None = None):
        self.topics = topics
        self._topic_words = {topic: words(w) for topic, w in TOPIC_WORDS.items()}

    def keep(self, texts: list[str]) -> list[YesNo]:
        return [YesNo(0.04 if any(p.search(t.lower()) for p in NOISE) else 0.98) for t in texts]

    def route(self, texts: list[str], topics: list[str]) -> list[Pick]:
        out = []
        for text in texts:
            found = words(text)
            hits = {t: len(found & self._topic_words.get(t, set())) for t in topics}
            total = sum(hits.values())
            if not total:
                out.append(Pick(NO_TOPIC, {NO_TOPIC: 0.5}, 0.5))
                continue
            shares = {t: n / total for t, n in hits.items() if n}
            best = max(shares, key=lambda t: shares[t])
            out.append(Pick(best, shares, min(0.95, shares[best])))
        return out

    def compare(self, pairs: list[tuple[str, str]]) -> list[Pick]:
        out = []
        for new, old in pairs:
            low_new, low_old = new.lower(), old.lower()
            shared = [
                (subj, val)
                for subj, val in SUBJECTS.values()
                if subj.search(low_new) and subj.search(low_old)
            ]
            if shared and TENTATIVE.search(low_new):
                out.append(Pick("conflict", {"conflict": 0.54, "new": 0.46}, 0.54))
                continue
            changed = False
            for subj, val in shared:
                ours, theirs = _values(low_new, subj, val), _values(low_old, subj, val)
                changed = changed or bool(ours and theirs and ours != theirs)
            if changed:
                out.append(Pick("changed", {"changed": 0.97, "new": 0.03}, 0.97))
            else:
                out.append(Pick("new", {"new": 0.98, "changed": 0.02}, 0.98))
        return out

    def on_delete(self, deleted: DeletedFile, candidates: list[Candidate]) -> Pick:
        for i, candidate in enumerate(candidates):
            if candidate.title.strip().lower() == deleted.title.strip().lower():
                choice = f"{MOVED_PREFIX}{i}"
                return Pick(choice, {choice: 0.95}, 0.95)
        return Pick("retire", {"retire": 0.9}, 0.9)

    def score(self, question: str, facts: list[str], rule: str) -> list[float]:
        asked = words(question)
        if not asked:
            return [0.0 for _ in facts]
        return [round(len(asked & words(fact)) / len(asked), 3) for fact in facts]
