"""Sign in with Google and the Drive folder picker, against a fake Google."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from zerostore.config import load_config, read_secrets
from zerostore.connectors import google_signin as google
from zerostore.connectors.base import ConnectorError
from zerostore.connectors.google_auth import SCOPE, GoogleAuth
from zerostore.demo import Demo
from zerostore.web.app import create_web_app
from zerostore.web.settings import update_drive

BASE = "http://localhost"
CLIENT_ID = "123-abc.apps.googleusercontent.com"

FOLDERS = {
    "root": [{"id": "f-company", "name": "Company"}, {"id": "f-personal", "name": "Personal"}],
    "f-company": [
        {"id": "f-dec", "name": "Decisions"},
        {"id": "f-dup1", "name": "Notes"},
        {"id": "f-dup2", "name": "Notes"},
    ],
}
SHARED = [{"id": "f-shared", "name": "Board"}]
DRIVES = [{"id": "d-eng", "name": "Engineering"}]


class FakeGoogle:
    """Google's token, Drive and revoke endpoints, enough for the sign-in flow."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.send_refresh_token = True
        self.scope = SCOPE
        self.drive_ok = True
        self.revoked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            if grant == "authorization_code":
                if form["code"][0] != "good-code":
                    return httpx.Response(
                        400, json={"error": "invalid_grant", "error_description": "Bad code"}
                    )
                assert form["code_verifier"][0] and form["client_id"][0] == CLIENT_ID
                body = {"access_token": "at-1", "expires_in": 3600, "scope": self.scope}
                if self.send_refresh_token:
                    body["refresh_token"] = "rt-1"
                return httpx.Response(200, json=body)
            if form["refresh_token"][0] != "rt-1":
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Token revoked"}
                )
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        if url.path == "/drive/v3/about":
            if not self.drive_ok:
                return httpx.Response(403, json={"error": {"message": "Drive API has not been used"}})
            return httpx.Response(200, json={"user": {"emailAddress": "khoi@example.com"}})
        if url.path == "/drive/v3/drives":
            return httpx.Response(200, json={"drives": DRIVES})
        if url.path == "/drive/v3/files":
            q = url.params["q"]
            if q.startswith("sharedWithMe"):
                return httpx.Response(200, json={"files": SHARED})
            parent = re.match(r"'([^']+)' in parents", q).group(1)
            return httpx.Response(200, json={"files": FOLDERS.get(parent, [])})
        return httpx.Response(404)


@pytest.fixture
def fake():
    return FakeGoogle()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    for name in (google.CLIENT_ID_NAME, google.CLIENT_SECRET_NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(google, "BUILTIN_CLIENT", None)
    demo = Demo(tmp_path / "demo")
    demo.install()
    zs = demo.open()
    demo.create_tokens(zs)
    yield demo, zs
    zs.close()


def make_web(demo, fake, **kw) -> TestClient:
    d, zs = demo
    app = create_web_app(
        zs, d.config_path, google_http=lambda: httpx.Client(transport=httpx.MockTransport(fake)), **kw
    )
    client = TestClient(app, base_url=BASE)
    response = client.post("/login", data={"token": d.owner_token()}, follow_redirects=False)
    assert response.status_code == 303
    return client


def csrf(client: TestClient) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/setup?step=1").text).group(1)


def save_client(client: TestClient) -> None:
    response = client.post(
        "/setup/google/client",
        data={"csrf": csrf(client), "client_id": CLIENT_ID, "client_secret": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def start(client: TestClient) -> dict[str, str]:
    response = client.get("/setup/google/start", follow_redirects=False)
    assert response.status_code == 303
    url = urlparse(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == google.AUTH_URI
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def sign_in(client: TestClient) -> None:
    save_client(client)
    query = start(client)
    page = client.get(f"/setup/google/callback?state={query['state']}&code=good-code")
    assert "Signed in to Google" in page.text


# The setup page before signing in -----------------------------------------


def test_without_a_sign_in_app_the_page_says_how_to_make_one(demo, fake):
    web = make_web(demo, fake)
    page = web.get("/setup?step=1").text
    assert "needs a Google sign-in app first" in page
    assert "Desktop app" in page and "Sign in with Google Drive" not in page

    response = web.get("/setup/google/start", follow_redirects=False)
    assert response.headers["location"] == "/setup?step=1"
    assert "Add your Google sign-in app below first" in web.get("/setup?step=1").text


def test_saving_your_own_sign_in_app(demo, fake):
    d, _ = demo
    web = make_web(demo, fake)
    bad = web.post(
        "/setup/google/client", data={"csrf": csrf(web), "client_id": "nope", "client_secret": "x"}
    )
    assert "ends in .apps.googleusercontent.com" in bad.text
    save_client(web)
    secrets_file = d.config_path.parent / "secrets.env"
    saved = read_secrets(secrets_file)
    assert saved[google.CLIENT_ID_NAME] == CLIENT_ID and saved[google.CLIENT_SECRET_NAME] == "s3cret"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    page = web.get("/setup?step=1").text
    assert "Saved your Google sign-in app" in page
    assert 'href="/setup/google/start" hx-boost="false"' in page
    assert "Change your Google sign-in app" in page


def test_zerostores_own_sign_in_app_needs_no_setup(demo, fake, monkeypatch):
    monkeypatch.setattr(google, "BUILTIN_CLIENT", ("builtin.apps.googleusercontent.com", "pub"))
    web = make_web(demo, fake)
    page = web.get("/setup?step=1").text
    assert "Sign in with Google Drive" in page and "sign-in app" not in page
    assert start(web)["client_id"] == "builtin.apps.googleusercontent.com"


# Signing in -----------------------------------------------------------------


def test_sign_in_sends_you_to_google_with_pkce(demo, fake):
    web = make_web(demo, fake)
    save_client(web)
    query = start(web)
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "http://localhost/setup/google/callback"
    assert query["scope"] == SCOPE  # read-only Drive, nothing else
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256" and len(query["state"]) >= 40


def test_signing_in_saves_the_credentials_and_turns_on_drive(demo, fake):
    d, zs = demo
    web = make_web(demo, fake)
    save_client(web)
    query = start(web)
    # Google's redirect is cross-site, so the browser leaves the session cookie behind.
    cookieless = TestClient(web.app, base_url=BASE)
    page = cookieless.get(f"/setup/google/callback?state={query['state']}&code=good-code")
    assert page.status_code == 200
    assert '<meta http-equiv="refresh" content="0;url=/setup?step=1">' in page.text

    path = d.config_path.parent / "google-credentials.json"
    saved = json.loads(path.read_text())
    assert saved == {
        "type": "authorized_user",
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
        "refresh_token": "rt-1",
        "account": "khoi@example.com",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(d.config_path).sources.google_drive.credentials == str(path)
    assert zs.config.sources.google_drive.credentials == str(path)

    # The PKCE verifier sent to Google matches the challenge in the sign-in link.
    exchange = next(r for r in fake.calls if r.url.path == "/token")
    verifier = parse_qs(exchange.content.decode())["code_verifier"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert expected == query["code_challenge"]

    setup = web.get("/setup?step=1").text
    assert "Signed in to Google as khoi@example.com" in setup
    assert "signed in as <b>khoi@example.com</b>" in setup


def test_signing_in_never_overwrites_a_service_account_key(demo, fake):
    d, _ = demo
    key = d.config_path.parent / "google-credentials.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "sa@x"}))
    web = make_web(demo, fake)
    sign_in(web)
    assert json.loads(key.read_text())["type"] == "service_account"
    saved = d.config_path.parent / "google-signin.json"
    assert json.loads(saved.read_text())["refresh_token"] == "rt-1"
    assert load_config(d.config_path).sources.google_drive.credentials == str(saved)


def test_a_sign_in_link_works_once(demo, fake):
    web = make_web(demo, fake)
    save_client(web)
    state = start(web)["state"]
    assert "Signed in" in web.get(f"/setup/google/callback?state={state}&code=good-code").text
    again = web.get(f"/setup/google/callback?state={state}&code=good-code")
    assert "didn't finish" in again.text and "expired or was already used" in again.text
    forged = web.get("/setup/google/callback?state=made-up&code=good-code")
    assert "expired or was already used" in forged.text


def test_declining_on_googles_page(demo, fake):
    d, _ = demo
    web = make_web(demo, fake)
    save_client(web)
    state = start(web)["state"]
    page = web.get(f"/setup/google/callback?state={state}&error=access_denied")
    assert "you didn't allow access on Google's page" in html.unescape(page.text)
    assert "refresh" not in page.text.split("</head>")[0]
    assert not (d.config_path.parent / "google-credentials.json").exists()


def test_google_problems_are_explained(demo, fake):
    web = make_web(demo, fake)
    save_client(web)
    fake.send_refresh_token = False
    page = web.get(f"/setup/google/callback?state={start(web)['state']}&code=good-code")
    assert "didn't send a refresh token" in html.unescape(page.text)

    fake.send_refresh_token, fake.scope = True, "openid"
    page = web.get(f"/setup/google/callback?state={start(web)['state']}&code=good-code")
    assert "needs read access to Drive" in html.unescape(page.text)

    fake.scope, fake.drive_ok = SCOPE, False
    page = web.get(f"/setup/google/callback?state={start(web)['state']}&code=good-code")
    assert "turn on the Google Drive API" in html.unescape(page.text)

    page = web.get(f"/setup/google/callback?state={start(web)['state']}&code=bad-code")
    assert "Google refused the sign-in (400): Bad code" in html.unescape(page.text)


def test_on_another_machine_you_paste_the_address(demo, fake):
    web = make_web(demo, fake, bound_host="0.0.0.0", behind_proxy=True)
    save_client(web)
    page = web.get("/setup?step=1").text
    assert 'target="_blank"' in page and google.PASTE_REDIRECT in page
    query = start(web)
    assert query["redirect_uri"] == google.PASTE_REDIRECT

    other = TestClient(web.app, base_url=BASE)  # a second browser, signed in too
    other.post("/login", data={"token": demo[0].owner_token()})
    address = f"{google.PASTE_REDIRECT}?state={query['state']}&code=good-code&scope={SCOPE}"
    refused = other.post("/setup/google/paste", data={"csrf": csrf(other), "address": address})
    assert "started in another browser" in refused.text

    query = start(web)
    address = f"{google.PASTE_REDIRECT}?state={query['state']}&code=good-code&scope={SCOPE}"
    response = web.post(
        "/setup/google/paste", data={"csrf": csrf(web), "address": address}, follow_redirects=False
    )
    assert response.headers["location"] == "/setup?step=1"
    assert "Signed in to Google as khoi@example.com" in web.get("/setup?step=1").text


def test_signing_out_revokes_and_deletes(demo, fake):
    d, _ = demo
    web = make_web(demo, fake)
    sign_in(web)
    page = web.post("/setup/google/signout", data={"csrf": csrf(web)}).text
    assert fake.revoked == ["rt-1"]
    assert not (d.config_path.parent / "google-credentials.json").exists()
    assert "Signed out of Google" in page and "Sign in with Google Drive" in page


# Picking folders -----------------------------------------------------------


def test_picking_folders(demo, fake):
    d, zs = demo
    web = make_web(demo, fake)
    sign_in(web)
    page = web.get("/setup?step=1").text
    assert "▸ Company" in page and "▸ Personal" in page
    token = csrf(web)

    web.post("/setup/google/folders", data={"csrf": token, "value": "Company", "back": "/setup?step=1"})
    inside = web.get("/setup?step=1&folder=f-company&path=Company").text
    assert "Decisions" in inside and "<b>Company</b>" in inside
    # Two folders with one name are saved by id, so the path can't pick the wrong one.
    assert 'name="value" value="id:f-dup1"' in inside
    assert 'name="value" value="Company/Decisions"' in inside
    web.post(
        "/setup/google/folders",
        data={"csrf": token, "value": "Company/Decisions", "back": "https://evil.example"},
    )

    shared = web.get("/setup?step=1&folder=shared").text
    assert 'name="value" value="id:f-shared"' in shared
    web.post("/setup/google/folders", data={"csrf": token, "value": "id:f-shared", "label": "Board"})
    drives = web.get("/setup?step=1&folder=drives").text
    assert 'name="value" value="id:d-eng"' in drives

    folders = load_config(d.config_path).sources.google_drive.folders
    assert folders[-3:] == ["Company", "Company/Decisions", "id:f-shared"]
    assert zs.config.sources.google_drive.folders == folders
    page = web.get("/setup?step=1").text
    assert '<span class="m" style="flex:1;min-width:0;overflow-wrap:anywhere">Board</span>' in page

    removed = web.post(
        "/setup/google/folders",
        data={"csrf": token, "value": "Company", "action": "remove", "back": "/setup?step=1&folder=root"},
        follow_redirects=False,
    )
    assert removed.headers["location"] == "/setup?step=1&folder=root"
    assert "Company" not in load_config(d.config_path).sources.google_drive.folders


def test_a_broken_sign_in_shows_on_the_page(demo, fake):
    d, _ = demo
    web = make_web(demo, fake)
    sign_in(web)
    path = d.config_path.parent / "google-credentials.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "refresh_token": "revoked"}))
    page = web.get("/setup?step=1").text
    assert "Google refused the credentials" in page


def test_folder_forms_need_csrf(demo, fake):
    web = make_web(demo, fake)
    assert web.post("/setup/google/folders", data={"value": "Company"}).status_code == 403
    assert (
        web.post("/setup/google/client", data={"client_id": CLIENT_ID, "client_secret": "x"}).status_code
        == 403
    )


# The pieces ------------------------------------------------------------------


def test_read_pasted():
    assert google.read_pasted("http://localhost/x?state=s&code=c&scope=a") == ("c", "s", None)
    assert google.read_pasted("  http://localhost/x?error=access_denied&state=s ") == (
        None,
        "s",
        "access_denied",
    )
    assert google.read_pasted("not a url") == (None, None, None)


def test_own_client_wins_over_zerostores(monkeypatch):
    monkeypatch.setattr(google, "BUILTIN_CLIENT", ("b.apps.googleusercontent.com", "pub"))
    assert google.oauth_client({}).builtin
    own = google.oauth_client({google.CLIENT_ID_NAME: CLIENT_ID, google.CLIENT_SECRET_NAME: "s"})
    assert own == google.OAuthClient(CLIENT_ID, "s")
    monkeypatch.setattr(google, "BUILTIN_CLIENT", None)
    assert google.oauth_client({google.CLIENT_ID_NAME: CLIENT_ID}) is None


def test_flows_expire():
    flows = google.Flows()
    flow = flows.start(google.OAuthClient(CLIENT_ID, "s"), "http://localhost/cb", owner=None)
    with pytest.raises(google.SignInError):
        flows.take(flow.state, now=flow.created + google.FLOW_TTL + 1)
    with pytest.raises(google.SignInError):
        flows.take(None)


def test_saved_sign_in_works_for_syncing(tmp_path, fake):
    path = tmp_path / "google-credentials.json"
    google.save_credentials(
        path,
        {"type": "authorized_user", "client_id": CLIENT_ID, "client_secret": "s", "refresh_token": "rt-1"},
        "k@example.com",
    )
    assert google.signed_in_as(path) == "k@example.com"
    auth = GoogleAuth(path, httpx.Client(transport=httpx.MockTransport(fake)))
    assert auth.token() == "at-2"
    (tmp_path / "sa.json").write_text(json.dumps({"type": "service_account"}))
    assert google.signed_in_as(tmp_path / "sa.json") is None
    assert google.signed_in_as(tmp_path / "missing.json") is None


def test_update_drive_keeps_comments_and_checks(demo):
    d, _ = demo
    update_drive(d.config_path, add="Company/Pricing")
    text = d.config_path.read_text()
    assert "# The ZeroStore demo" in text
    assert load_config(d.config_path).sources.google_drive.folders[-1] == "Company/Pricing"
    update_drive(d.config_path, add="Company/Pricing")  # no duplicates
    assert load_config(d.config_path).sources.google_drive.folders.count("Company/Pricing") == 1


def test_missing_credentials_still_explained():
    with pytest.raises(ConnectorError, match="not found"):
        GoogleAuth("/nonexistent/google-credentials.json", httpx.Client())
