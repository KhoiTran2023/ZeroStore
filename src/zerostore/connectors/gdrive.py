"""Google Drive: the folders you pick, including every folder inside them.

Google Docs, Sheets (first sheet) and Slides are exported as text; plain
text and Markdown files are read as-is. Other files (PDFs, images, Office
files) are skipped for now. The file's modified time is the version.

Folders are written as paths ("Company/Decisions") or as folder ids
("id:1AbC..."), which avoids any ambiguity when two folders share a name.
"""

from __future__ import annotations

from typing import Any

import httpx

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument
from zerostore.connectors.google_auth import GoogleAuth

API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DOWNLOADS = {"text/plain", "text/markdown", "text/x-markdown", "text/csv"}
FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)"
SHARED = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveConnector(Connector):
    name = "google_drive"

    def __init__(self, folders: list[str], credentials: str | None, client: httpx.Client | None = None):
        if not credentials:
            raise ConnectorError(
                "Google Drive needs a credentials file: set sources.google_drive.credentials "
                "(the setup page's Sign in with Google writes one, or use a service account key)"
            )
        self.folders = folders
        self.client = client or httpx.Client(timeout=60)
        self.auth = GoogleAuth(credentials, self.client)

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self.client.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self.auth.token()}"}
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Google Drive could not be reached: {exc}") from exc
        if response.status_code in (401, 403):
            raise ConnectorError(
                f"Google Drive refused access ({response.status_code}): {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise ConnectorError(f"Google Drive returned {response.status_code} for {path}")
        return response

    def _files(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = None
        while True:
            params = {"q": query, "fields": FIELDS, "pageSize": "1000", **SHARED}
            if page:
                params["pageToken"] = page
            body = self._get("/files", params).json()
            files.extend(body.get("files", []))
            page = body.get("nextPageToken")
            if not page:
                return files

    def _resolve(self, folder: str) -> str:
        """Folder path or 'id:...' to a folder id."""
        if folder.startswith("id:"):
            return folder[3:]
        parent: str | None = None
        for part in [p for p in folder.split("/") if p]:
            query = f"name = '{_q(part)}' and mimeType = '{FOLDER}' and trashed = false"
            if parent:
                query += f" and '{parent}' in parents"
            matches = self._files(query)
            if not matches:
                raise ConnectorError(f"no Google Drive folder {folder!r} is visible (stuck at {part!r})")
            if len(matches) > 1:
                ids = ", ".join(f"id:{m['id']}" for m in matches[:5])
                raise ConnectorError(f"more than one folder matches {folder!r}; use its id instead: {ids}")
            parent = matches[0]["id"]
        if parent is None:
            raise ConnectorError("an empty Google Drive folder path was given")
        return parent

    def list_items(self) -> list[ItemRef]:
        refs: dict[str, ItemRef] = {}
        for folder in self.folders:
            stack = [(self._resolve(folder), folder.rstrip("/"))]
            seen: set[str] = set()
            while stack:
                folder_id, path = stack.pop()
                if folder_id in seen:  # a folder can live in two places
                    continue
                seen.add(folder_id)
                for f in self._files(f"'{folder_id}' in parents and trashed = false"):
                    if f["mimeType"] == FOLDER:
                        stack.append((f["id"], f"{path}/{f['name']}"))
                    elif f["mimeType"] in EXPORTS or f["mimeType"] in DOWNLOADS:
                        refs[f["id"]] = ItemRef(
                            doc_id=f"gdrive:{f['id']}",
                            version=f["modifiedTime"],
                            title=f["name"],
                            extra={"file": f, "folder": path},
                        )
        return list(refs.values())

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        f = ref.extra["file"]
        if f["mimeType"] in EXPORTS:
            response = self._get(f"/files/{f['id']}/export", {"mimeType": EXPORTS[f["mimeType"]]})
        else:
            response = self._get(f"/files/{f['id']}", {"alt": "media", "supportsAllDrives": "true"})
        text = response.content.decode("utf-8", errors="replace").lstrip("﻿")
        if not text.strip():
            return None
        return SourceDocument(
            doc_id=ref.doc_id,
            source=self.name,
            title=f["name"],
            text=text,
            url=f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view",
            updated_at=f["modifiedTime"],
            version=f["modifiedTime"],
            location=ref.extra["folder"],
        )

    def close(self) -> None:
        self.client.close()
