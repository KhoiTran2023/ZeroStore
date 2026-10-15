"""The web UI's way in: owner tools called through the MCP server in-process.

Each page asks the MCP server exactly as an agent would, with the signed-in
owner token, so access checks and the access log apply to the web UI too.
"""

from __future__ import annotations

import threading
from typing import Any

import anyio

from zerostore.app import ZeroStore


class OwnerError(Exception):
    """An owner tool refused or failed; the message is the tool's own."""


class OwnerClient:
    def __init__(self, zs: ZeroStore):
        self.zs = zs
        self._servers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _server(self, token: str):
        from zerostore.mcp.server import build_server

        with self._lock:
            server = self._servers.get(token)
            if server is None:
                server = build_server(self.zs, lambda ctx, token=token: token)
                self._servers = {token: server}  # one signed-in owner at a time is the norm
            return server

    def call(self, token: str, tool: str, **args: Any) -> dict[str, Any]:
        """Call one tool and return its structured result. Blocking: run it off the event loop."""
        from mcp import Client

        server = self._server(token)

        async def go():
            async with Client(server) as client:
                return await client.call_tool(tool, args)

        result = anyio.run(go)
        if result.is_error:
            text = result.content[0].text if result.content else f"{tool} failed"
            raise OwnerError(text.removeprefix(f"Error executing tool {tool}: "))
        return result.structured_content or {}
