"""End to end: an agent launches `zerostore serve` over stdio with its token."""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import Client, StdioServerParameters

from zerostore.agents import commands
from zerostore.app import ZeroStore
from zerostore.config import load_config
from zerostore.store.facts import Chunk


def test_stdio_server_answers_with_the_agents_level(tmp_path):
    home = tmp_path / "home"
    env = {**os.environ, "ZEROSTORE_HOME": str(home)}
    env.pop("ZEROSTORE_CONFIG", None)
    from typer.testing import CliRunner

    from zerostore.cli import app

    result = CliRunner().invoke(app, ["init", "--no-rule"], env={"ZEROSTORE_HOME": str(home)})
    assert result.exit_code == 0, result.output

    # Seed one fact with the same embedder the server process will pick.
    store = ZeroStore.open(load_config(home / "zerostore.yaml"), quiet=True)
    store.facts.replace_document(
        "gdrive:pricing",
        [
            Chunk(
                "gdrive:pricing",
                "google_drive",
                "Pricing 2026",
                "The Pro plan costs $29 per month.",
                "https://docs.google.com/document/d/pricing",
                "2026-10-12",
            )
        ],
    )
    raw, _ = store.tokens.create("claude-code", "default")
    store.close()

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "zerostore.cli", "serve"],
        env={**env, "ZEROSTORE_TOKEN": raw},
    )

    async def go():
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("search", {"query": "What does the Pro plan cost?"})
            return client.instructions, names, result

    instructions, names, result = anyio.run(go)
    assert "Check it before stating company facts" in instructions
    assert "propose_fact" in names and "token_create" not in names
    assert not result.is_error, result.content
    assert result.structured_content["facts"][0]["text"] == "The Pro plan costs $29 per month."


def test_the_desktop_settings_start_the_server_without_the_shells_path(tmp_path):
    """Claude Desktop starts MCP servers with a short PATH; the pasted settings must still work."""
    home = tmp_path / "home"
    from typer.testing import CliRunner

    from zerostore.cli import app

    result = CliRunner().invoke(app, ["init", "--no-rule"], env={"ZEROSTORE_HOME": str(home)})
    assert result.exit_code == 0, result.output
    store = ZeroStore.open(load_config(home / "zerostore.yaml"), quiet=True)
    raw, _ = store.tokens.create("claude-desktop", "default")
    store.close()

    entry = json.loads(commands.stdio_json(raw, home / "zerostore.yaml"))["mcpServers"]["zerostore"]
    assert os.path.isabs(entry["command"]), entry["command"]
    assert entry["args"] == ["serve", "-c", str((home / "zerostore.yaml").absolute())]
    params = StdioServerParameters(
        command=entry["command"],
        args=entry["args"],
        env={**entry["env"], "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    async def go():
        async with Client(params) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert "search" in anyio.run(go)
