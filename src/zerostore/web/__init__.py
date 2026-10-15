"""The web UI (mockup E): setup, status, review, log and settings pages.

Served by `zerostore serve --http` next to the MCP server at /mcp. Like any
other client it talks to the MCP server with your owner token: every page
calls the owner tools in-process, so each request is checked and logged the
same way an agent's is. Settings pages edit zerostore.yaml (and keys go to
secrets.env beside it), the same files the CLI reads.
"""
