"""The HTTP transport: bearer tokens checked at the door, then per tool."""

from __future__ import annotations

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from servers import running

from zerostore.mcp.http import create_http_app, is_loopback


@pytest.fixture
def http_url(seeded):
    with running(create_http_app(seeded)) as url:
        yield url


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


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}


def initialize(url, token, host):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Host": host,
    }
    return httpx2.post(url, json=INITIALIZE, headers=headers, timeout=30).status_code


def test_behind_a_tunnel_the_public_name_is_answered(seeded):
    """Tailscale, Cloudflare or Caddy on this machine forward requests addressed to the public name."""
    raw, _ = seeded.tokens.create("d", "default")
    app = create_http_app(
        seeded, host="127.0.0.1", mcp_url="https://zs.tail1234.ts.net/mcp", behind_proxy=True
    )
    with running(app) as url:
        port = url.split(":")[2].split("/")[0]
        assert initialize(url, raw, "zs.tail1234.ts.net") == 200
        assert initialize(url, raw, f"localhost:{port}") == 200
        assert initialize(url, raw, "attacker.example") == 421  # still guarded against DNS rebinding


def test_without_a_proxy_only_localhost_is_answered(seeded):
    raw, _ = seeded.tokens.create("d", "default")
    with running(create_http_app(seeded, host="127.0.0.1")) as url:
        assert initialize(url, raw, "zs.tail1234.ts.net") == 421


def test_behind_a_proxy_without_a_public_url_any_name_is_answered(seeded):
    raw, _ = seeded.tokens.create("d", "default")
    app = create_http_app(seeded, host="127.0.0.1", mcp_url="http://127.0.0.1:8765/mcp", behind_proxy=True)
    with running(app) as url:
        assert initialize(url, raw, "zs.example.com") == 200
        assert initialize(url, "zs_nope", "zs.example.com") == 401  # the token still decides


def test_each_token_lists_only_the_tools_its_level_may_use(seeded, http_url):
    default_raw, _ = seeded.tokens.create("d", "default")
    owner_raw, _ = seeded.tokens.create("o", "owner")

    async def names(token):
        async with client_for(http_url, token) as client:
            return {t.name for t in (await client.list_tools()).tools}

    default_tools = anyio.run(names, default_raw)
    assert "search" in default_tools and "propose_fact" in default_tools
    assert not default_tools & {"token_create", "token_list", "sync_now", "status", "review_decide"}
    assert {"token_create", "sync_now", "status"} <= anyio.run(names, owner_raw)
