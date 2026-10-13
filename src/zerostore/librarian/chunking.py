"""Split documents into chunks small enough to hand an agent as one fact.

Chunks follow the document's own structure: Markdown headings and blank
lines first, then sentences, and words only as a last resort. Each chunk
remembers the heading it sits under, so a chunk like "It costs $29." still
says it is about the Pro plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_CHARS = 1000
MAX_CHARS = 1600

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class TextChunk:
    text: str
    heading: str = ""

    def with_heading(self) -> str:
        return f"{self.heading}\n{self.text}" if self.heading else self.text


def chunk_text(text: str, target: int = TARGET_CHARS, maximum: int = MAX_CHARS) -> list[TextChunk]:
    sections = _sections(text.replace("\r\n", "\n"))
    chunks: list[TextChunk] = []
    for heading, body in sections:
        for piece in _pack(_paragraphs(body), target, maximum):
            chunks.append(TextChunk(piece, heading))
    return chunks


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading trail, body) pairs. The trail is like 'Pricing > Pro plan'."""
    trail: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2).strip()
            trail = [(lvl, t) for lvl, t in trail if lvl < level] + [(level, title)]
            sections.append((" > ".join(t for _, t in trail), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(lines).strip()) for h, lines in sections if "\n".join(lines).strip()]


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _split_long(paragraph: str, maximum: int) -> list[str]:
    if len(paragraph) <= maximum:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > maximum:  # one enormous "sentence": cut at a word boundary
            cut = sentence.rfind(" ", 0, maximum)
            cut = cut if cut > 0 else maximum
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > maximum:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _pack(paragraphs: list[str], target: int, maximum: int) -> list[str]:
    """Join paragraphs until a chunk reaches `target`, never exceeding `maximum`."""
    out: list[str] = []
    current = ""
    for para in paragraphs:
        for piece in _split_long(para, maximum):
            if current and len(current) + 2 + len(piece) > maximum:
                out.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
            if len(current) >= target:
                out.append(current)
                current = ""
    if current:
        out.append(current)
    return out
