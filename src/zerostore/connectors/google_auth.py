"""Google access tokens from a credentials file, read-only Drive scope.

Two kinds of file work:
- an authorized-user file (`"type": "authorized_user"`), which is what
  "Sign in with Google" saves: client id, client secret and refresh token;
- a service-account key (`"type": "service_account"`). Share the folders
  with the service account's email address so it can read them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import jwt

from zerostore.connectors.base import ConnectorError

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleAuth:
    def __init__(self, credentials_path: str | Path, client: httpx.Client):
        path = Path(credentials_path).expanduser()
        try:
            self.info = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConnectorError(f"Google credentials file not found: {path}") from None
        except (OSError, ValueError) as exc:
            raise ConnectorError(f"couldn't read Google credentials at {path}: {exc}") from None
        self.kind = self.info.get("type")
        if self.kind not in ("authorized_user", "service_account"):
            raise ConnectorError(
                f"{path} isn't a Google credentials file ZeroStore understands "
                "(expected an authorized_user or service_account file)"
            )
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        if self.kind == "authorized_user":
            data = {
                "grant_type": "refresh_token",
                "client_id": self.info["client_id"],
                "client_secret": self.info["client_secret"],
                "refresh_token": self.info["refresh_token"],
            }
            uri = TOKEN_URI
        else:
            now = int(time.time())
            uri = self.info.get("token_uri", TOKEN_URI)
            assertion = jwt.encode(
                {"iss": self.info["client_email"], "scope": SCOPE, "aud": uri, "iat": now, "exp": now + 3600},
                self.info["private_key"],
                algorithm="RS256",
                headers={"kid": self.info.get("private_key_id")} if self.info.get("private_key_id") else None,
            )
            data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        try:
            response = self.client.post(uri, data=data)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Google sign-in could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise ConnectorError(
                f"Google refused the credentials ({response.status_code}): {response.text[:200]}. "
                "Sign in again or check the service account key."
            )
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._token
