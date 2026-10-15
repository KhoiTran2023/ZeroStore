"""The demo, told in the terminal: two agents, the CEO's price change, and a note that waits for review.

Agents ask through the same MCP server real agents use, each with its own
token, so every answer here is one a connected agent would get.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import anyio
import typer

from zerostore.app import ZeroStore
from zerostore.demo import NEW_PRICE, NOTE_TEXT, OLD_PRICE, Demo
from zerostore.librarian.ingestion import Ingestor, SyncReport

PRICE_QUESTION = "What does the Pro plan cost?"
FREE_QUESTION = "Is there a free tier?"

SOURCE_NAMES = {
    "google_drive": "Google Drive",
    "github": "GitHub",
    "linear": "Linear",
    "decisions": "decisions.md",
}


def wants_jev() -> bool:
    return bool(os.environ.get("JEV_API_KEY"))


def ask(zs: ZeroStore, token: str, question: str) -> dict[str, Any]:
    """Ask through the MCP server, exactly as a connected agent would."""
    from mcp import Client

    from zerostore.mcp.server import build_server

    server = build_server(zs, lambda ctx: token)

    async def go() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool("search", {"query": question})
            if result.is_error:
                raise RuntimeError(result.content[0].text)
            return result.structured_content or {}

    return anyio.run(go)


def fact_line(fact: dict[str, Any]) -> str:
    """The fact's own words, without the heading trail the store keeps for context."""
    lines = [line.strip("- ").strip() for line in fact["text"].split("\n") if line.strip()]
    if len(lines) > 1 and not lines[0].endswith((".", "?", "!", ":")):
        lines = lines[1:]
    text = " ".join(lines)
    return text if len(text) <= 180 else text[:177].rstrip() + "..."


def citation(fact: dict[str, Any]) -> str:
    source = SOURCE_NAMES.get(fact["source"], fact["source"])
    return f"{fact['document']} · {source} · updated {str(fact['updated_at'])[:10]}"


class Teller:
    def __init__(self, pause: float = 1.0, echo: Callable[..., None] = typer.secho):
        self.pause = pause
        self.echo = echo

    def wait(self, factor: float = 1.0) -> None:
        if self.pause > 0:
            time.sleep(self.pause * factor)

    def say(self, text: str = "", **style: Any) -> None:
        self.echo(text, **style)

    def step(self, text: str) -> None:
        self.say("")
        self.say(text, bold=True)
        self.wait(0.5)

    def command(self, text: str) -> None:
        self.say(f"$ {text}", fg=typer.colors.BRIGHT_BLACK)
        self.wait(0.5)

    def answer(self, agent: str, question: str, reply: dict[str, Any]) -> None:
        self.say(f"  [{agent}] {question}", fg=typer.colors.CYAN)
        facts = reply.get("facts") or []
        if not facts:
            self.say("    not sure: nothing in ZeroStore answers this")
        else:
            self.say(f"    {fact_line(facts[0])}")
            self.say(f"    {citation(facts[0])}", fg=typer.colors.BRIGHT_BLACK)
        self.wait(0.6)

    def changes(self, report: SyncReport) -> None:
        marks = {"added": "+", "updated": "~", "retired": "-", "moved": ">", "kept": "=", "review": "?"}
        for change in report.changes:
            extra = []
            if change.confidence is not None:
                extra.append(f"confidence {change.confidence:.2f}")
            if change.action == "review":
                extra.append("waiting for review")
            if change.note:
                extra.append(change.note)
            detail = f"  ({', '.join(extra)})" if extra else ""
            self.say(f"  {marks.get(change.action, '?')} {change.title}{detail}")
        self.say(report.summary(), bold=True)


def first_sync(zs: ZeroStore, teller: Teller) -> SyncReport:
    teller.command("zerostore sync")
    report = Ingestor(zs).sync()
    by_source: dict[str, int] = {}
    for change in report.changes:
        by_source[change.source] = by_source.get(change.source, 0) + 1
    read = ", ".join(f"{n} from {SOURCE_NAMES[s]}" for s, n in by_source.items())
    teller.say(f"  Read {len(report.changes)} documents: {read}.")
    noise = sum(1 for c in report.changes if "noise" in c.note)
    secret = [c.title for c in report.changes if c.confidential]
    if noise:
        teller.say(f"  Left out small talk in {noise} document{'s' if noise != 1 else ''}.")
    if secret:
        teller.say(f"  Marked {', '.join(secret)} confidential: agents with default tokens never see it.")
    return report


def setup(demo: Demo, teller: Teller) -> tuple[ZeroStore, dict[str, str]]:
    jev = wants_jev()
    demo.install(jev=jev)
    zs = demo.open()
    tokens = demo.create_tokens(zs)
    teller.say("ZeroStore demo: Pebble & Co.", bold=True)
    if demo.reset:
        teller.say(
            "Started over from the beginning. Agents connected earlier keep their tokens; restart their "
            "ZeroStore connection (or the agent) so they read the fresh demo.",
            fg=typer.colors.YELLOW,
        )
    teller.say(
        "A made-up 12-person startup with Google Docs, a GitHub repo and Linear issues, simulated from "
        f"files in {demo.company}. Your real ZeroStore and accounts aren't touched."
    )
    if jev:
        teller.say("Judging with Jev (JEV_API_KEY is set).")
    else:
        teller.say("Judging with the demo's scripted judge, offline. Set JEV_API_KEY to judge with Jev.")
    teller.step("1. ZeroStore reads the company.")
    first_sync(zs, teller)
    return zs, tokens


def play(demo: Demo, teller: Teller) -> None:
    """The whole story, start to finish."""
    zs, tokens = setup(demo, teller)
    try:
        teller.step("2. Two agents ask the same question, each with its own token.")
        for agent, token in tokens.items():
            teller.answer(agent, PRICE_QUESTION, ask(zs, token, PRICE_QUESTION))

        teller.step("3. The CEO edits Pricing 2026 in Google Drive.")
        demo.change_price()
        teller.say(f"  - {OLD_PRICE}", fg=typer.colors.RED)
        teller.say(f"  + {NEW_PRICE}", fg=typer.colors.GREEN)
        teller.wait()

        teller.step("4. The next sync notices.")
        teller.command("zerostore sync")
        report = Ingestor(zs).sync()
        teller.changes(report)

        teller.step("5. Both agents ask again.")
        for agent, token in tokens.items():
            teller.answer(agent, PRICE_QUESTION, ask(zs, token, PRICE_QUESTION))
        stale = sorted({t for c in report.changes for t in c.replaces})
        if stale:
            teller.say(
                f"  {', '.join(stale)} still says the old price, so ZeroStore stopped serving that copy."
            )

        teller.step("6. Maya wonders in Linear whether to drop the free tier.")
        demo.add_note()
        teller.say(f'  "{NOTE_TEXT}"', fg=typer.colors.YELLOW)
        teller.wait()
        teller.command("zerostore sync")
        report = Ingestor(zs).sync()
        teller.changes(report)
        if report.waiting:
            teller.say(
                "  It's a question, not a decision, so it waits for a person. Agents keep the current answer:"
            )
        teller.wait(0.5)
        agent, token = next(iter(tokens.items()))
        teller.answer(agent, FREE_QUESTION, ask(zs, token, FREE_QUESTION))

        teller.say("")
        teller.say("Tell your sources once. Every agent knows.", bold=True)
        connect_help(demo, tokens, teller, after_story=True)
    finally:
        zs.close()


def start(demo: Demo, teller: Teller) -> None:
    """Pebble & Co. as it was before the price change, for stepping through with real agents."""
    zs, tokens = setup(demo, teller)
    zs.close()
    connect_help(demo, tokens, teller, after_story=False)


def connect_help(demo: Demo, tokens: dict[str, str], teller: Teller, *, after_story: bool) -> None:
    config = demo.config_path
    serve = f'zerostore serve -c "{config}"'
    claude = tokens.get("claude-code", "<token>")
    other = tokens.get("cursor", "<token>")
    teller.say("")
    teller.say("Ask Pebble & Co. from your own agents:", bold=True)
    teller.say(f"  Claude Code:  claude mcp add pebble-demo -e ZEROSTORE_TOKEN={claude} -- {serve}")
    teller.say(f"  Cursor, Codex and others: run `{serve}` with ZEROSTORE_TOKEN={other}")
    teller.say("  Both tokens have default access, so the confidential Finance folder stays hidden.")
    teller.say("")
    if after_story:
        teller.say(f"Step through it yourself: `{demo.command('start')}`, ask your agents about the Pro")
        teller.say(f"plan, then `{demo.command('change-price')}` and the sync below.")
    else:
        teller.say(f'Ask them: "{PRICE_QUESTION}" Then run `{demo.command("change-price")}` (the CEO')
        teller.say(
            f"edits Pricing 2026), sync, and ask again. `{demo.command('add-note')}` adds the free-tier note."
        )
    teller.say(f'  Sync:    zerostore sync -c "{config}"')
    teller.say(f'  Review:  zerostore review -c "{config}"')
    teller.say(f"  Remove:  {demo.command('remove')}")
