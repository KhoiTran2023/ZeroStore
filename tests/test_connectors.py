"""Connectors against recorded-shape fake APIs (no network)."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from zerostore.connectors.base import ConnectorError
from zerostore.connectors.gdrive import FOLDER, GoogleDriveConnector
from zerostore.connectors.github import GitHubConnector
from zerostore.connectors.linear import LinearConnector

# GitHub ----------------------------------------------------------------------


def github_api(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/acme/app":
        return httpx.Response(200, json={"default_branch": "main"})
    if path == "/repos/acme/app/git/trees/main":
        return httpx.Response(
            200,
            json={
                "truncated": False,
                "tree": [
                    {"path": "README.md", "type": "blob", "sha": "s1", "size": 20},
                    {"path": "docs/decisions/0001-pricing.md", "type": "blob", "sha": "s2", "size": 30},
                    {"path": "src/app.py", "type": "blob", "sha": "s3", "size": 10},
                    {"path": "docs", "type": "tree", "sha": "t1"},
                    {"path": "docs/huge.md", "type": "blob", "sha": "s4", "size": 10_000_000},
                ],
            },
        )
    if path.startswith("/repos/acme/app/git/blobs/"):
        sha = path.rsplit("/", 1)[-1]
        text = {"s1": "# App\n\nHello.", "s2": "We chose $29 for Pro."}[sha]
        return httpx.Response(
            200, json={"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
        )
    if path == "/repos/acme/app/commits":
        assert request.url.params["sha"] == "main"
        return httpx.Response(200, json=[{"commit": {"committer": {"date": "2026-10-12T10:00:00Z"}}}])
    if path == "/repos/acme/private":
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.Response(500)


def github(repos=("acme/app",), token="ghp_test"):
    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(github_api))
    return GitHubConnector(list(repos), token, client=client)


def test_github_lists_only_docs():
    refs = github().list_items()
    assert [r.doc_id for r in refs] == [
        "github:acme/app:README.md",
        "github:acme/app:docs/decisions/0001-pricing.md",
    ]
    assert refs[1].version == "s2"


def test_github_fetches_text_link_and_date():
    connector = github()
    doc = connector.fetch(connector.list_items()[1])
    assert doc.text == "We chose $29 for Pro."
    assert doc.url == "https://github.com/acme/app/blob/main/docs/decisions/0001-pricing.md"
    assert doc.updated_at == "2026-10-12T10:00:00Z" and doc.location == "acme/app"


def test_github_sends_the_token():
    connector = github()
    assert connector.client.headers["authorization"] == "Bearer ghp_test"


def test_github_missing_repo_is_a_clear_error():
    with pytest.raises(ConnectorError, match="can't see it"):
        github(repos=("acme/private",)).list_items()


def test_github_include_patterns():
    connector = github()
    connector.include = ["docs/decisions/*"]
    assert [r.title for r in connector.list_items()] == ["acme/app/docs/decisions/0001-pricing.md"]


# Linear ----------------------------------------------------------------------


def linear_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "lin_api_test"
    body = json.loads(request.content)
    query, variables = body["query"], body["variables"]
    if "teams(" in query:
        return httpx.Response(
            200, json={"data": {"teams": {"nodes": [{"id": "team-1", "key": "PRD", "name": "Product"}]}}}
        )
    if "issues(" in query:
        assert variables["team"] == "team-1"
        page_two = variables["after"] == "c1"
        node = {
            "id": "i2" if page_two else "i1",
            "identifier": "PRD-2" if page_two else "PRD-1",
            "title": "Raise Pro price" if page_two else "Empty one",
            "description": "Pro goes to $29 in November." if page_two else "",
            "url": "https://linear.app/acme/issue/PRD-2",
            "updatedAt": "2026-10-10T00:00:00Z",
            "state": {"name": "Done"},
            "labels": {"nodes": [{"name": "pricing"}]},
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "issues": {
                        "nodes": [node],
                        "pageInfo": {"hasNextPage": not page_two, "endCursor": None if page_two else "c1"},
                    }
                }
            },
        )
    if "documents(" in query:
        return httpx.Response(
            200,
            json={
                "data": {
                    "documents": {
                        "nodes": [
                            {
                                "id": "d1",
                                "title": "Pricing spec",
                                "content": "# Pricing\n\nPro is $29.",
                                "url": "https://linear.app/acme/document/d1",
                                "updatedAt": "2026-10-09T00:00:00Z",
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )
    return httpx.Response(400)


def linear(teams=("product",)):
    client = httpx.Client(transport=httpx.MockTransport(linear_api))
    return LinearConnector(list(teams), "lin_api_test", client=client)


def test_linear_lists_issues_across_pages_and_documents():
    refs = linear().list_items()
    assert [r.doc_id for r in refs] == ["linear:issue:i1", "linear:issue:i2", "linear:doc:d1"]
    assert refs[1].title == "PRD-2 Raise Pro price"


def test_linear_issue_text_has_status_and_labels():
    connector = linear()
    doc = connector.fetch(connector.list_items()[1])
    assert doc.text.startswith("Status: Done · Labels: pricing")
    assert "Pro goes to $29" in doc.text
    assert doc.labels == ["pricing"] and doc.location == "Product"


def test_linear_document_text():
    connector = linear()
    doc = connector.fetch(connector.list_items()[2])
    assert doc.text == "# Pricing\n\nPro is $29." and doc.url.endswith("/d1")


def test_linear_unknown_team():
    with pytest.raises(ConnectorError, match="Design"):
        linear(teams=("Design",)).list_items()


def test_linear_needs_a_key():
    with pytest.raises(ConnectorError, match="api_key"):
        LinearConnector(["Product"], None)


def test_linear_graphql_errors_surface():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"errors": [{"message": "boom"}]}))
    )
    with pytest.raises(ConnectorError, match="boom"):
        LinearConnector(["Product"], "k", client=client).list_items()


# Google Drive ----------------------------------------------------------------


class DriveAPI:
    """Company/ holds Pricing (a Google Doc), notes.md and a Finance/ folder with a Sheet."""

    def __init__(self):
        self.token_requests: list[dict] = []
        self.files = {
            "root-company": {"id": "root-company", "name": "Company", "mimeType": FOLDER, "parent": None},
            "doc1": {
                "id": "doc1",
                "name": "Pricing 2026",
                "mimeType": "application/vnd.google-apps.document",
                "parent": "root-company",
                "modifiedTime": "2026-10-12T09:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc1",
            },
            "md1": {
                "id": "md1",
                "name": "notes.md",
                "mimeType": "text/markdown",
                "parent": "root-company",
                "modifiedTime": "2026-10-01T09:00:00Z",
            },
            "img": {
                "id": "img",
                "name": "logo.png",
                "mimeType": "image/png",
                "parent": "root-company",
                "modifiedTime": "2026-10-01T09:00:00Z",
            },
            "fin": {"id": "fin", "name": "Finance", "mimeType": FOLDER, "parent": "root-company"},
            "sheet1": {
                "id": "sheet1",
                "name": "Budget",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parent": "fin",
                "modifiedTime": "2026-10-05T09:00:00Z",
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer ya29.test"
        path = request.url.path.removeprefix("/drive/v3")
        if path == "/files":
            q = request.url.params["q"]
            if q.startswith("name = 'Company'"):
                found = [self.files["root-company"]]
            elif "' in parents" in q:
                parent = q.split("'")[1]
                found = [f for f in self.files.values() if f["parent"] == parent]
            else:
                found = []
            return httpx.Response(
                200, json={"files": [{k: v for k, v in f.items() if k != "parent"} for f in found]}
            )
        if path == "/files/doc1/export":
            assert request.url.params["mimeType"] == "text/plain"
            return httpx.Response(200, content="﻿The Pro plan costs $29.".encode())
        if path == "/files/sheet1/export":
            return httpx.Response(200, content=b"item,amount\nads,100\n")
        if path == "/files/md1":
            assert request.url.params["alt"] == "media"
            return httpx.Response(200, content=b"# Notes\n\nShip Friday.")
        return httpx.Response(404)


@pytest.fixture
def authorized_user(tmp_path):
    path = tmp_path / "google.json"
    path.write_text(
        json.dumps(
            {"type": "authorized_user", "client_id": "cid", "client_secret": "sec", "refresh_token": "rt"}
        )
    )
    return path


def drive(credentials, api=None, folders=("Company",)):
    api = api or DriveAPI()
    return GoogleDriveConnector(
        list(folders), str(credentials), client=httpx.Client(transport=httpx.MockTransport(api))
    ), api


def test_drive_lists_readable_files_recursively(authorized_user):
    connector, api = drive(authorized_user)
    refs = {r.title: r for r in connector.list_items()}
    assert set(refs) == {"Pricing 2026", "notes.md", "Budget"}
    assert refs["Budget"].extra["folder"] == "Company/Finance"
    assert api.token_requests[0]["grant_type"] == "refresh_token"
    assert len(api.token_requests) == 1  # the access token is reused


def test_drive_exports_docs_and_downloads_text(authorized_user):
    connector, _ = drive(authorized_user)
    refs = {r.title: r for r in connector.list_items()}
    doc = connector.fetch(refs["Pricing 2026"])
    assert doc.text == "The Pro plan costs $29."
    assert doc.url == "https://docs.google.com/document/d/doc1" and doc.location == "Company"
    assert connector.fetch(refs["notes.md"]).text == "# Notes\n\nShip Friday."
    assert "ads,100" in connector.fetch(refs["Budget"]).text


def test_drive_folder_by_id(authorized_user):
    connector, _ = drive(authorized_user, folders=("id:fin",))
    assert [r.title for r in connector.list_items()] == ["Budget"]


def test_drive_missing_folder(authorized_user):
    connector, _ = drive(authorized_user, folders=("Nope/Here",))
    with pytest.raises(ConnectorError, match="Nope/Here"):
        connector.list_items()


def test_drive_service_account_signs_a_jwt(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    path = tmp_path / "sa.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "zs@acme.iam.gserviceaccount.com",
                "private_key": pem,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    connector, api = drive(path)
    connector.list_items()
    request = api.token_requests[0]
    assert request["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(
        request["assertion"],
        key.public_key(),
        algorithms=["RS256"],
        audience="https://oauth2.googleapis.com/token",
    )
    assert claims["iss"] == "zs@acme.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/drive.readonly"


def test_drive_credentials_problems(tmp_path):
    with pytest.raises(ConnectorError, match="credentials file"):
        GoogleDriveConnector(["Company"], None)
    with pytest.raises(ConnectorError, match="not found"):
        GoogleDriveConnector(["Company"], str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"type": "something_else"}))
    with pytest.raises(ConnectorError, match="understands"):
        GoogleDriveConnector(["Company"], str(bad))


def test_drive_refused_token(authorized_user):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="invalid_grant")))
    connector = GoogleDriveConnector(["Company"], str(authorized_user), client=client)
    with pytest.raises(ConnectorError, match="invalid_grant"):
        connector.list_items()
