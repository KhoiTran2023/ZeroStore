"""Sign in with Google: the browser flow that saves a Drive credentials file.

It is Google's flow for installed apps (OAuth 2.0 with PKCE). The setup page
sends you to Google, you allow read-only Drive access, and Google sends you back
with a one-time code. ZeroStore swaps the code for a refresh token and saves it
as an authorized-user file, `google-credentials.json` next to zerostore.yaml
(readable only by you). `GoogleAuth` reads that file on every sync.

Signing in needs a Google sign-in app (an OAuth client). Use your own, saved as
ZEROSTORE_GOOGLE_CLIENT_ID and ZEROSTORE_GOOGLE_CLIENT_SECRET in secrets.env, or
ZeroStore's own once it ships with one (`BUILTIN_CLIENT`). Make a "Desktop app"
client: Google lets those send you back to any address on this computer.

When ZeroStore runs on another machine, Google can only send you back to an
address on your own computer, which doesn't answer, so you paste that address
into the setup page instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from zerostore.connectors.google_auth import SCOPE, TOKEN_URI

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
DRIVE_API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"

CREDENTIALS_FILENAME = "google-credentials.json"
CLIENT_ID_NAME = "ZEROSTORE_GOOGLE_CLIENT_ID"
CLIENT_SECRET_NAME = "ZEROSTORE_GOOGLE_CLIENT_SECRET"
# Where Google sends you back when ZeroStore isn't on this computer: nothing answers
# there, and you paste the address from the browser into the setup page.
PASTE_REDIRECT = "http://localhost/zerostore-google-signin"
FLOW_TTL = 600  # seconds a sign-in may take

# ZeroStore's own sign-in app, (client id, client secret), once it is registered.
# An installed app's secret isn't secret (Google says so); PKCE protects the code.
BUILTIN_CLIENT: tuple[str, str] | None = None


class SignInError(Exception):
    """Signing in with Google didn't work; the message says why."""


@dataclass(frozen=True)
class OAuthClient:
    id: str
    secret: str
    builtin: bool = False


def oauth_client(env: Mapping[str, str]) -> OAuthClient | None:
    """Your own sign-in app when one is saved, else ZeroStore's, else None."""
    own_id, own_secret = env.get(CLIENT_ID_NAME, "").strip(), env.get(CLIENT_SECRET_NAME, "").strip()
    if own_id and own_secret:
        return OAuthClient(own_id, own_secret)
    if BUILTIN_CLIENT:
        return OAuthClient(*BUILTIN_CLIENT, builtin=True)
    return None


def check_client_id(client_id: str) -> str:
    client_id = client_id.strip()
    if not client_id.endswith(".apps.googleusercontent.com") or any(c.isspace() for c in client_id):
        raise SignInError("a Google client ID ends in .apps.googleusercontent.com")
    return client_id


@dataclass
class Flow:
    """One sign-in in progress, from leaving for Google until the code comes back."""

    state: str
    verifier: str
    redirect_uri: str
    client: OAuthClient
    owner: Any  # the web session that started it
    created: float = field(default_factory=time.time)

    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=")
        return f"{AUTH_URI}?" + urlencode(
            {
                "client_id": self.client.id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": self.state,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "access_type": "offline",  # a refresh token, so syncs keep working
                "prompt": "consent",  # Google only hands out a refresh token on consent
            }
        )


class Flows:
    """Sign-ins in progress, in memory. Each `state` works once, for ten minutes."""

    def __init__(self) -> None:
        self._flows: dict[str, Flow] = {}

    def start(self, client: OAuthClient, redirect_uri: str, owner: str) -> Flow:
        self._drop_old()
        flow = Flow(secrets.token_urlsafe(32), secrets.token_urlsafe(48), redirect_uri, client, owner)
        self._flows[flow.state] = flow
        return flow

    def take(self, state: str | None, now: float | None = None) -> Flow:
        flow = self._flows.pop(state or "", None)
        if flow is None or (now or time.time()) - flow.created > FLOW_TTL:
            raise SignInError("this sign-in expired or was already used; start again from the setup page")
        return flow

    def _drop_old(self) -> None:
        cutoff = time.time() - FLOW_TTL
        for state in [s for s, f in self._flows.items() if f.created < cutoff]:
            del self._flows[state]


def read_pasted(text: str) -> tuple[str | None, str | None, str | None]:
    """(code, state, error) from the address Google sent the browser to."""
    query = parse_qs(urlparse(text.strip()).query)
    first = {k: v[0] for k, v in query.items() if v}
    return first.get("code"), first.get("state"), first.get("error")


def finish(flow: Flow, code: str, http: httpx.Client) -> tuple[dict[str, str], str]:
    """Swap Google's code for a refresh token. Returns the credentials and the account's email."""
    try:
        response = http.post(
            TOKEN_URI,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": flow.verifier,
                "redirect_uri": flow.redirect_uri,
                "client_id": flow.client.id,
                "client_secret": flow.client.secret,
            },
        )
    except httpx.HTTPError as exc:
        raise SignInError(f"Google couldn't be reached: {exc}") from None
    if response.status_code != 200:
        raise SignInError(f"Google refused the sign-in ({response.status_code}): {_reason(response)}")
    body = response.json()
    if not body.get("refresh_token"):
        raise SignInError(
            "Google didn't send a refresh token; remove ZeroStore's access in your Google account "
            "and sign in again"
        )
    granted = set(str(body.get("scope", SCOPE)).split())
    if SCOPE not in granted:
        raise SignInError(
            "ZeroStore needs read access to Drive; tick that box on Google's page and sign in again"
        )
    credentials = {
        "type": "authorized_user",
        "client_id": flow.client.id,
        "client_secret": flow.client.secret,
        "refresh_token": body["refresh_token"],
    }
    return credentials, account_email(body["access_token"], http)


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
        return body.get("error_description") or body.get("error") or response.text[:200]
    except ValueError:
        return response.text[:200]


def account_email(access_token: str, http: httpx.Client) -> str:
    try:
        response = http.get(
            f"{DRIVE_API}/about",
            params={"fields": "user(emailAddress)"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        raise SignInError(f"Google Drive couldn't be reached: {exc}") from None
    if response.status_code != 200:
        raise SignInError(
            f"signed in, but Google Drive refused access ({response.status_code}): {_reason(response)}. "
            "If you use your own sign-in app, turn on the Google Drive API in its Google Cloud project."
        )
    return response.json().get("user", {}).get("emailAddress", "your Google account")


def save_credentials(path: Path, credentials: Mapping[str, str], email: str) -> None:
    """Write the credentials file in one step, readable only by you."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({**credentials, "account": email}, fh, indent=2)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def signed_in_as(path: Path) -> str | None:
    """The account a credentials file belongs to, or None when it isn't a saved sign-in."""
    try:
        info = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("type") != "authorized_user":
        return None
    return info.get("account") or "your Google account"


def sign_out(path: Path, http: httpx.Client) -> None:
    """Tell Google to forget the refresh token (best effort), then delete the file."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        token = info.get("refresh_token") if info.get("type") == "authorized_user" else None
    except (OSError, ValueError):
        token = None
    if token:
        try:
            http.post(REVOKE_URI, data={"token": token})
        except httpx.HTTPError:
            pass  # deleting the file still signs ZeroStore out
    path.unlink(missing_ok=True)


def list_folders(access_token: str, parent: str, http: httpx.Client) -> list[dict[str, Any]]:
    """Folders directly inside `parent`: "root" (My Drive), "shared" (shared with you),
    "drives" (shared drives) or a folder id. Sorted by name."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict[str, str] = {
        "pageSize": "1000",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if parent == "drives":
        url, key = f"{DRIVE_API}/drives", "drives"
        params = {"pageSize": "100"}
    else:
        url, key = f"{DRIVE_API}/files", "files"
        where = "sharedWithMe" if parent == "shared" else f"'{parent.replace(chr(39), '')}' in parents"
        params.update(
            q=f"{where} and mimeType = '{FOLDER}' and trashed = false",
            fields="nextPageToken, files(id, name)",
            orderBy="name",
        )
    found: list[dict[str, Any]] = []
    while True:
        try:
            response = http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise SignInError(f"Google Drive couldn't be reached: {exc}") from None
        if response.status_code != 200:
            raise SignInError(
                f"Google Drive refused to list folders ({response.status_code}): {_reason(response)}"
            )
        body = response.json()
        found.extend({"id": f["id"], "name": f["name"]} for f in body.get(key, []))
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return sorted(found, key=lambda f: f["name"].lower())
