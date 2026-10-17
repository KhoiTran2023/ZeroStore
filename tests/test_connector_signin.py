"""Adding ZeroStore under Claude's Settings, Connectors: the OAuth sign-in, allowed by the owner."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from servers import running

from zerostore.access import oauth
from zerostore.access.oauth import ApprovalError, ConnectorAuth
from zerostore.mcp.http import connector_sign_in, create_http_app
from zerostore.store.state import StateDB
from zerostore.web.app import create_web_app

BASE = "http://localhost"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def server(demo):
    d, zs = demo
    app = create_http_app(zs, config_path=d.config_path, mcp_url=f"{BASE}/mcp")
    with TestClient(app, base_url=BASE) as client:  # runs the MCP server's lifespan too
        yield client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def register(client, name: str = "Claude") -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def start(client, app: dict, challenge: str, state: str = "st-1") -> str:
    """Claude sends the browser to /authorize, which hands it to ZeroStore's approval page."""
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": app["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": f"{BASE}/mcp",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    where = response.headers["location"]
    assert where.startswith(f"{BASE}/connect/approve?request=")
    return where[len(BASE) :]


def sign_in(client, demo) -> None:
    d, _ = demo
    assert client.post("/login", data={"token": d.owner_token()}, follow_redirects=False).status_code == 303


def answer(client, where: str, choice: str = "allow", level: str = "default"):
    page = client.get(where)
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    request = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/connect/approve",
        data={"csrf": csrf, "request": request, "answer": choice, "level": level},
        follow_redirects=False,
    )


def swap(client, app: dict, code: str, verifier: str):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "code_verifier": verifier,
            "resource": f"{BASE}/mcp",
        },
    )


# Finding the sign-in --------------------------------------------------------------


def test_a_401_says_where_to_sign_in(server):
    response = server.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)
    assert metadata_url == f"{BASE}/.well-known/oauth-protected-resource/mcp"

    resource = server.get(metadata_url[len(BASE) :]).json()
    assert resource["resource"] == f"{BASE}/mcp"
    [issuer] = resource["authorization_servers"]
    found = server.get("/.well-known/oauth-authorization-server").json()
    assert found["issuer"] == issuer  # compared exactly by clients
    assert found["registration_endpoint"] == f"{BASE}/register"
    assert found["code_challenge_methods_supported"] == ["S256"]


def test_plain_http_addresses_get_no_sign_in(demo):
    _, zs = demo
    assert connector_sign_in(zs, None) is None
    assert connector_sign_in(zs, "http://10.0.0.5:8765/mcp") is None  # tokens would cross the network bare
    assert connector_sign_in(zs, "https://zs.example.com/mcp") is not None
    assert connector_sign_in(zs, "http://127.0.0.1:8765/mcp") is not None  # this computer only


def test_without_a_sign_in_the_approval_page_is_not_there(demo):
    d, zs = demo
    app = create_http_app(zs, config_path=d.config_path, mcp_url="http://10.0.0.5:8765/mcp")
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/connect/approve?request=x").status_code == 404
        assert "resource_metadata" not in client.post("/mcp", json={}).headers["www-authenticate"]


# The owner's answer -------------------------------------------------------------


def test_allowing_gives_the_app_its_own_token(server, demo):
    _, zs = demo
    app = register(server)
    verifier, challenge = pkce()
    where = start(server, app, challenge)
    sign_in(server, demo)
    page = server.get(where)
    assert "Let Claude use your ZeroStore?" in page.text and "claude.ai" in page.text
    # The answer redirects back to Claude, so this page's form-action must allow it.
    assert "form-action 'self' https://claude.ai;" in page.headers["content-security-policy"]

    response = answer(server, where, level="default")
    assert response.status_code == 303
    back = urlparse(response.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    query = parse_qs(back.query)
    assert query["state"] == ["st-1"]

    made = swap(server, app, query["code"][0], verifier)
    assert made.status_code == 200, made.text
    token = made.json()
    assert token["token_type"] == "Bearer" and "refresh_token" not in token
    [connector] = [t for t in zs.tokens.list() if t.name == "Claude (connector)"]
    assert connector.level.value == "default"

    assert zs.tokens.verify(token["access_token"]).name == "Claude (connector)"


def test_a_code_works_once_and_only_with_its_verifier(server, demo):
    app = register(server)
    verifier, challenge = pkce()
    where = start(server, app, challenge)
    sign_in(server, demo)
    code = parse_qs(urlparse(answer(server, where).headers["location"]).query)["code"][0]
    assert swap(server, app, code, "not-the-verifier-" + "x" * 40).status_code == 400
    assert swap(server, app, code, verifier).status_code == 200
    again = swap(server, app, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_denying_sends_the_app_away_without_a_token(server, demo):
    _, zs = demo
    app = register(server)
    _, challenge = pkce()
    where = start(server, app, challenge, state="st-2")
    sign_in(server, demo)
    before = len(zs.tokens.list())
    response = answer(server, where, choice="deny")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["st-2"] and "code" not in query
    assert len(zs.tokens.list()) == before
    # The request is used up: answering again explains instead of connecting.
    assert "already answered" in server.get(where).text


def test_a_connector_never_gets_owner_access(server, demo):
    app = register(server)
    _, challenge = pkce()
    where = start(server, app, challenge)
    sign_in(server, demo)
    response = answer(server, where, level="owner")
    assert response.status_code == 400
    assert answer(server, where, level="confidential").status_code == 303  # the request survived


def test_the_answer_needs_the_form_token(server, demo):
    app = register(server)
    _, challenge = pkce()
    where = start(server, app, challenge)
    sign_in(server, demo)
    request = where.split("=", 1)[1]
    response = server.post("/connect/approve", data={"request": request, "answer": "allow"})
    assert response.status_code == 403


# Arriving from Claude's site ---------------------------------------------------------


def test_arriving_from_claude_without_the_cookie_takes_one_step_then_signs_in(server, demo):
    d, _ = demo
    app = register(server)
    _, challenge = pkce()
    where = start(server, app, challenge)

    # The cross-site redirect from claude.ai leaves the SameSite=Strict cookie behind, so the
    # page first moves on from ZeroStore itself, which brings the cookie along.
    hop = server.get(where)
    assert hop.status_code == 200
    assert f'content="0;url={where}&amp;hop=1"' in hop.text

    # Still no session after that step: sign in, then come straight back.
    to_login = server.get(f"{where}&hop=1", follow_redirects=False)
    assert to_login.status_code == 303
    login_page = server.get(to_login.headers["location"])
    assert f'name="next" value="{where}"' in login_page.text
    signed_in = server.post("/login", data={"token": d.owner_token(), "next": where}, follow_redirects=False)
    assert signed_in.headers["location"] == where
    assert "Let Claude use your ZeroStore?" in server.get(where).text


@pytest.mark.parametrize(
    "next_page",
    ["https://evil.example/", "//evil.example", "/settings", "/connect/approve?request=a&b=c", ""],
)
def test_signing_in_only_returns_to_an_approval_page(server, demo, next_page):
    d, _ = demo
    form = {"token": d.owner_token(), "next": next_page}
    response = server.post("/login", data=form, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_setup_shows_the_connector_address_on_a_public_https_address(demo):
    d, zs = demo
    url = "https://zs.example.com/mcp"
    provider = ConnectorAuth(zs.state, zs.tokens, "https://zs.example.com")
    web = create_web_app(zs, d.config_path, mcp_url=url, behind_proxy=True, connectors=provider)
    client = TestClient(web, base_url="https://zs.example.com")
    client.post("/login", data={"token": d.owner_token()})
    page = client.get("/setup?step=3").text
    assert "Add custom connector" in page and url in page

    local = TestClient(create_web_app(zs, d.config_path), base_url=BASE)
    local.post("/login", data={"token": d.owner_token()})
    assert "Add custom connector" not in local.get("/setup?step=3").text


# A real MCP client ------------------------------------------------------------------


class Memory:
    """Where an MCP client keeps its registration and token."""

    def __init__(self):
        self.tokens = None
        self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


def test_an_mcp_client_signs_in_the_way_claude_does(demo):
    """The MCP SDK's own OAuth client: discovery from the 401, registration, PKCE, the owner's OK."""
    d, zs = demo

    def build(url):
        return create_http_app(zs, config_path=d.config_path, mcp_url=url)

    async def scenario(url):
        base = url[: -len("/mcp")]
        browser = httpx2.AsyncClient(base_url=base)  # the owner's browser, signed in to the web UI
        await browser.post("/login", data={"token": d.owner_token()})
        returned = {}

        async def open_in_browser(authorize_url):
            approve = (await browser.get(authorize_url)).headers["location"]
            page = (await browser.get(approve)).text
            csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
            request = re.search(r'name="request" value="([^"]+)"', page).group(1)
            form = {"csrf": csrf, "request": request, "answer": "allow", "level": "default"}
            back = await browser.post("/connect/approve", data=form)
            returned.update({k: v[0] for k, v in parse_qs(urlparse(back.headers["location"]).query).items()})

        async def wait_for_callback():
            return AuthorizationCodeResult(code=returned["code"], state=returned.get("state"))

        auth = OAuthClientProvider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name="Claude",
                redirect_uris=["http://localhost:53682/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=Memory(),
            redirect_handler=open_in_browser,
            callback_handler=wait_for_callback,
        )
        async with Client(streamable_http_client(url, http_client=httpx2.AsyncClient(auth=auth))) as client:
            result = await client.call_tool("search", {"query": "pro plan price"})
            assert not result.is_error, result.content
        await browser.aclose()

    with running(build=build) as url:
        anyio.run(scenario, url)
    assert [t.level.value for t in zs.tokens.list() if t.name == "Claude (connector)"] == ["default"]


# The provider itself ------------------------------------------------------------------


def client_info(redirect: str = CALLBACK, name: str = "Claude") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=secrets.token_hex(8), client_name=name, redirect_uris=[redirect], client_secret="s"
    )


def params(state: str = "s") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=None,
        code_challenge="c" * 43,
        redirect_uri=CALLBACK,
        redirect_uri_provided_explicitly=True,
    )


def test_requests_and_codes_expire(seeded, monkeypatch):
    provider = ConnectorAuth(seeded.state, seeded.tokens, BASE)
    app = client_info()
    now = [1000.0]
    monkeypatch.setattr(oauth.time, "time", lambda: now[0])

    where = anyio.run(provider.authorize, app, params())
    request = where.split("=", 1)[1]
    now[0] += oauth.REQUEST_TTL + 1
    with pytest.raises(ApprovalError, match="expired"):
        provider.pending(request)

    request = anyio.run(provider.authorize, app, params()).split("=", 1)[1]
    code = parse_qs(urlparse(provider.answer(request, allow=True)).query)["code"][0]
    assert anyio.run(provider.load_authorization_code, app, code) is not None
    assert anyio.run(provider.load_authorization_code, client_info(), code) is None  # another app's code
    now[0] += oauth.CODE_TTL + 1
    assert anyio.run(provider.load_authorization_code, app, code) is None


def test_registered_apps_survive_a_restart_and_are_capped(seeded, monkeypatch):
    provider = ConnectorAuth(seeded.state, seeded.tokens, BASE)
    app = client_info()
    anyio.run(provider.register_client, app)
    again = ConnectorAuth(StateDB(seeded.config.servers.statedb), seeded.tokens, BASE)
    assert anyio.run(again.get_client, app.client_id).client_name == "Claude"

    monkeypatch.setattr(oauth, "MAX_CLIENTS", 2)
    anyio.run(provider.register_client, client_info())
    with pytest.raises(RegistrationError):
        anyio.run(provider.register_client, client_info())


def test_an_app_can_disconnect_itself_but_never_the_owner(seeded):
    provider = ConnectorAuth(seeded.state, seeded.tokens, BASE)
    raw, made = seeded.tokens.create("Claude (connector)", "default")
    owner_raw, owner = seeded.tokens.create("me", "owner")
    anyio.run(provider.revoke_token, anyio.run(provider.load_access_token, owner_raw))
    anyio.run(provider.revoke_token, anyio.run(provider.load_access_token, raw))
    assert anyio.run(provider.load_access_token, raw) is None
    assert anyio.run(provider.load_access_token, owner_raw) is not None
