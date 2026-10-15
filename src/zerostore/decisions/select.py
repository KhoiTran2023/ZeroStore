"""Which backend answers which step, from zerostore.yaml.

- The librarian's judgment (keep, topics, compare, deletes): Jev when
  `ai.decision_engine` is jev and a Jev key is set, otherwise your LLM when one
  is set up, otherwise none (every change is applied directly, as before).
  `decision_engine: demo` is the scripted judge `zerostore demo` uses.
- Narrowing (`retrieval.narrow_with`) and reranking (`retrieval.rerank_with`):
  `auto` follows the judgment engine; reranking falls back to the local
  reranker when no AI is set up and `zerostore[local]` is installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from zerostore.config import Config
from zerostore.decisions.base import DecisionEngine

log = logging.getLogger("zerostore.decisions")


@dataclass
class Engines:
    judge: DecisionEngine | None = None
    narrow: DecisionEngine | None = None
    rerank: DecisionEngine | None = None

    def close(self) -> None:
        for engine in {id(e): e for e in (self.judge, self.narrow, self.rerank) if e}.values():
            engine.close()


def make_jev(config: Config) -> DecisionEngine:
    from zerostore.decisions.jev import JevEngine

    ai = config.ai
    return JevEngine(ai.jev_api_key or "", model=ai.jev_model, base_url=ai.jev_base_url)


def make_llm(config: Config) -> DecisionEngine:
    from zerostore.decisions.llm import LLMEngine

    ai = config.ai
    return LLMEngine(ai.llm_provider or "", ai.llm_model, ai.llm_api_key, base_url=ai.llm_base_url)


def make_demo(config: Config) -> DecisionEngine:
    from zerostore.decisions.demo import DemoEngine

    return DemoEngine(config.retrieval.topics)


def make_local() -> DecisionEngine | None:
    from zerostore.decisions.local import LocalReranker

    try:
        return LocalReranker()
    except RuntimeError:
        return None
    except Exception as exc:  # model download failed, unsupported platform
        log.warning("the local reranker couldn't start: %s", exc)
        return None


def build_engines(config: Config) -> Engines:
    ai, r = config.ai, config.retrieval
    cache: dict[str, DecisionEngine | None] = {}

    def get(kind: str) -> DecisionEngine | None:
        if kind not in cache:
            cache[kind] = (
                {"jev": make_jev, "llm": make_llm, "demo": make_demo}[kind](config)
                if kind != "local"
                else make_local()
            )
        return cache[kind]

    if ai.decision_engine == "demo":
        default = "demo"
    elif ai.decision_engine == "jev" and ai.has_jev:
        default = "jev"
    elif ai.has_llm:
        default = "llm"
    elif ai.has_jev:
        default = "jev"
    else:
        default = None

    def step(setting: str) -> DecisionEngine | None:
        if setting == "off":
            return None
        if setting == "auto":
            return get(default) if default else None
        return get(setting)

    judge = get(default) if default else None
    narrow = step(r.narrow_with)
    rerank = step(r.rerank_with) if (r.rerank_with != "auto" or default) else get("local")
    return Engines(judge=judge, narrow=narrow, rerank=rerank)


def build_judge(config: Config) -> DecisionEngine | None:
    """Only the judgment engine (for syncs), without starting any reranker."""
    ai = config.ai
    if ai.decision_engine == "demo":
        return make_demo(config)
    if ai.decision_engine == "jev" and ai.has_jev:
        return make_jev(config)
    if ai.has_llm:
        return make_llm(config)
    if ai.has_jev:
        return make_jev(config)
    return None
