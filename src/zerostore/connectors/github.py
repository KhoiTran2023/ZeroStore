"""GitHub: the written docs in your repositories (README, docs/, decision records).

Reads text files on each repo's default branch. Code is left out: ZeroStore
holds company knowledge, and agents already read code directly. The git
blob hash is the version, so unchanged files are never downloaded again.
"""

from __future__ import annotations

import base64
import fnmatch

import httpx

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument

API = "https://api.github.com"
DEFAULT_INCLUDE = ["*.md", "*.mdx", "*.markdown", "*.txt", "*.rst"]
MAX_FILE_BYTES = 512 * 1024


class GitHubConnector(Connector):
    name = "github"

    def __init__(
        self,
        repos: list[str],
        token: str | None = None,
        include: list[str] | None = None,
        client: httpx.Client | None = None,
    ):
        self.repos = repos
        self.include = include or DEFAULT_INCLUDE
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(base_url=API, timeout=30)
        self.client.headers.update(headers)

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"GitHub could not be reached: {exc}") from exc
        if response.status_code in (401, 403):
            raise ConnectorError(
                f"GitHub refused access to {url} ({response.status_code}). "
                "Check the token in sources.github.token and that it can read this repo."
            )
        if response.status_code == 404:
            raise ConnectorError(f"GitHub has no {url}, or the token can't see it")
        if response.status_code >= 400:
            raise ConnectorError(f"GitHub returned {response.status_code} for {url}")
        return response

    def _wanted(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in self.include)

    def list_items(self) -> list[ItemRef]:
        refs: list[ItemRef] = []
        for repo in self.repos:
            branch = self._get(f"/repos/{repo}").json()["default_branch"]
            tree = self._get(f"/repos/{repo}/git/trees/{branch}", {"recursive": "1"}).json()
            if tree.get("truncated"):
                raise ConnectorError(
                    f"{repo} has too many files for one listing; narrow sources.github.include"
                )
            for entry in tree.get("tree", []):
                if entry.get("type") != "blob" or not self._wanted(entry["path"]):
                    continue
                if entry.get("size", 0) > MAX_FILE_BYTES:
                    continue
                refs.append(
                    ItemRef(
                        doc_id=f"github:{repo}:{entry['path']}",
                        version=entry["sha"],
                        title=f"{repo}/{entry['path']}",
                        extra={"repo": repo, "path": entry["path"], "sha": entry["sha"], "branch": branch},
                    )
                )
        return refs

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        repo, path, sha, branch = (ref.extra[k] for k in ("repo", "path", "sha", "branch"))
        blob = self._get(f"/repos/{repo}/git/blobs/{sha}").json()
        try:
            text = base64.b64decode(blob["content"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError):
            return None  # not text after all
        commits = self._get(f"/repos/{repo}/commits", {"path": path, "sha": branch, "per_page": "1"}).json()
        updated = commits[0]["commit"]["committer"]["date"] if commits else ""
        return SourceDocument(
            doc_id=ref.doc_id,
            source=self.name,
            title=ref.title,
            text=text,
            url=f"https://github.com/{repo}/blob/{branch}/{path}",
            updated_at=updated,
            version=sha,
            location=repo,
        )

    def close(self) -> None:
        self.client.close()
