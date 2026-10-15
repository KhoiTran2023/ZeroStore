"""The HTTP transport: bearer tokens checked at the door, then per tool."""

from __future__ import annotations

import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from zerostore.mcp.http import create_http_app, is_loopback


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_url(seeded):
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_http_app(seeded), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def client_for(url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


def test_requests_without_a_token_get_401(http_url):
    response = httpx2.post(http_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_wrong_token_gets_401(http_url):
    response = httpx2.post(
        http_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer zs_nope"},
    )
    assert response.status_code == 401


def test_search_over_http_respects_levels(seeded, http_url):
    default_raw, _ = seeded.tokens.create("d", "default")
    conf_raw, _ = seeded.tokens.create("c", "confidential")

    async def search(token):
        async with client_for(http_url, token) as client:
            result = await client.call_tool("search", {"query": "pro salary band"})
            assert not result.is_error, result.content
            return {f["document"] for f in result.structured_content["facts"]}

    assert "Salary bands" not in anyio.run(search, default_raw)
    assert "Salary bands" in anyio.run(search, conf_raw)


def test_owner_tools_checked_per_call_over_http(seeded, http_url):
    raw, _ = seeded.tokens.create("d", "default")

    async def go():
        async with client_for(http_url, raw) as client:
            return await client.call_tool("token_list", {})

    result = anyio.run(go)
    assert result.is_error and "default access level" in result.content[0].text


@pytest.mark.parametrize(
    "host, expected",
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("example.com", False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_proposals_ask_the_person_over_http(http_url, seeded):
    import mcp_types as types

    raw, _ = seeded.tokens.create("agent", "default")
    asked = []

    async def person(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"fact": "Refunds are allowed within 30 days."})

    async def go():
        headers = {"Authorization": f"Bearer {raw}"}
        transport = streamable_http_client(http_url, http_client=httpx2.AsyncClient(headers=headers))
        async with Client(transport, elicitation_callback=person) as client:
            result = await client.call_tool("propose_fact", {"fact": "Refunds: 30 days.", "quote": "q"})
            return result.structured_content

    out = anyio.run(go)
    assert out["status"] == "saved" and asked and "Refunds: 30 days." in asked[0]
    assert "within 30 days" in seeded.config.decisions_path().read_text()
