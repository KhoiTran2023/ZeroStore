"""The "check ZeroStore first" line in agent settings files.

`zerostore init` and `zerostore connect` offer to add one line to CLAUDE.md,
AGENTS.md or .cursorrules. It is written once, inside a marked block, so it
can be found again and removed cleanly with `zerostore connect --undo`.
"""

from __future__ import annotations

import re
from pathlib import Path

from zerostore.mcp.descriptions import AGENT_RULE

BEGIN = "<!-- zerostore -->"
END = "<!-- /zerostore -->"

# Settings files each agent reads, in the order we look for them.
AGENT_FILES = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "agents": "AGENTS.md",
    "cursor": ".cursorrules",
}

_BLOCK = re.compile(rf"{re.escape(BEGIN)}.*?{re.escape(END)}\n?", re.DOTALL)


def rule_block(line: str = AGENT_RULE) -> str:
    return f"{BEGIN}\n{line}\n{END}\n"


def has_rule(path: Path) -> bool:
    return path.exists() and BEGIN in path.read_text(encoding="utf-8")


def add_rule(path: Path, line: str = AGENT_RULE) -> bool:
    """Append the marked block to `path` (creating it if needed).

    Returns False, changing nothing, when the block is already there.
    """
    if has_rule(path):
        return False
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    separator = "\n" if existing.strip() else ""
    path.write_text(existing + separator + rule_block(line), encoding="utf-8")
    return True


def remove_rule(path: Path) -> bool:
    """Remove the marked block. Returns False when there was none.

    Deletes the file only if ZeroStore's block was all it contained.
    """
    if not has_rule(path):
        return False
    text = path.read_text(encoding="utf-8")
    match = _BLOCK.search(text)
    if match is None:
        raise ValueError(f"{path} has a {BEGIN} line without its closing {END}; fix it by hand")
    before, after = text[: match.start()], text[match.end() :]
    if before.endswith("\n\n"):
        before = before[:-1]  # the blank line add_rule put in front of the block
    cleaned = before + after
    if cleaned.strip():
        path.write_text(cleaned, encoding="utf-8")
    else:
        path.unlink()
    return True


def detect_agent_files(directory: Path) -> list[Path]:
    """Agent settings files that already exist in `directory`."""
    seen: list[Path] = []
    for name in dict.fromkeys(AGENT_FILES.values()):
        candidate = directory / name
        if candidate.exists():
            seen.append(candidate)
    return seen
