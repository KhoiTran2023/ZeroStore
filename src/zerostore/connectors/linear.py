"""Linear: issues and documents from the teams you choose.

One GraphQL listing per team returns every issue with its text and
`updatedAt`, which is the version. Linear documents (specs, notes) attached
to the team's projects come along too.
"""

from __future__ import annotations

from typing import Any

import httpx

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument

API = "https://api.linear.app/graphql"
PAGE = 100

_TEAMS = "query { teams(first: 250) { nodes { id key name } } }"

_ISSUES = """
query($team: ID!, $after: String) {
  issues(first: PAGE_SIZE, after: $after, filter: { team: { id: { eq: $team } } }) {
    nodes {
      id identifier title description url updatedAt
      state { name }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".replace("PAGE_SIZE", str(PAGE))

_DOCUMENTS = """
query($team: ID!, $after: String) {
  documents(first: PAGE_SIZE, after: $after,
            filter: { project: { accessibleTeams: { some: { id: { eq: $team } } } } }) {
    nodes { id title content url updatedAt }
    pageInfo { hasNextPage endCursor }
  }
}
""".replace("PAGE_SIZE", str(PAGE))


class LinearConnector(Connector):
    name = "linear"

    def __init__(self, teams: list[str], api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise ConnectorError(
                "Linear needs an API key: set sources.linear.api_key (e.g. ${LINEAR_API_KEY}). "
                "Create one in Linear under Settings > Security & access > Personal API keys."
            )
        self.teams = teams
        self.client = client or httpx.Client(timeout=30)
        self.client.headers.update({"Authorization": api_key, "Content-Type": "application/json"})

    def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.client.post(API, json={"query": query, "variables": variables or {}})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Linear could not be reached: {exc}") from exc
        if response.status_code in (400, 401, 403) and "authentic" in response.text.lower():
            raise ConnectorError("Linear refused the API key in sources.linear.api_key")
        if response.status_code >= 400:
            raise ConnectorError(f"Linear returned {response.status_code}: {response.text[:200]}")
        body = response.json()
        if body.get("errors"):
            raise ConnectorError(f"Linear query failed: {body['errors'][0].get('message')}")
        return body["data"]

    def _paged(self, query: str, field: str, team_id: str) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        after = None
        while True:
            data = self._query(query, {"team": team_id, "after": after})[field]
            nodes.extend(data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                return nodes
            after = data["pageInfo"]["endCursor"]

    def _team_ids(self) -> dict[str, str]:
        teams = self._query(_TEAMS)["teams"]["nodes"]
        found: dict[str, str] = {}
        for wanted in self.teams:
            match = next((t for t in teams if wanted.lower() in (t["name"].lower(), t["key"].lower())), None)
            if match is None:
                raise ConnectorError(f"no Linear team named {wanted!r} is visible to this API key")
            found[match["id"]] = match["name"]
        return found

    def list_items(self) -> list[ItemRef]:
        refs: list[ItemRef] = []
        for team_id, team_name in self._team_ids().items():
            for issue in self._paged(_ISSUES, "issues", team_id):
                refs.append(
                    ItemRef(
                        doc_id=f"linear:issue:{issue['id']}",
                        version=issue["updatedAt"],
                        title=f"{issue['identifier']} {issue['title']}",
                        extra={"kind": "issue", "team": team_name, "node": issue},
                    )
                )
            for doc in self._paged(_DOCUMENTS, "documents", team_id):
                refs.append(
                    ItemRef(
                        doc_id=f"linear:doc:{doc['id']}",
                        version=doc["updatedAt"],
                        title=doc["title"],
                        extra={"kind": "document", "team": team_name, "node": doc},
                    )
                )
        # A project shared by two teams would list the same document twice.
        return list({r.doc_id: r for r in refs}.values())

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        node = ref.extra["node"]
        if ref.extra["kind"] == "issue":
            labels = [lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])]
            state = (node.get("state") or {}).get("name", "")
            header = f"Status: {state}" + (f" · Labels: {', '.join(labels)}" if labels else "")
            text = f"{header}\n\n{node.get('description') or ''}".strip()
        else:
            labels = []
            text = node.get("content") or ""
        if not text.strip():
            return None
        return SourceDocument(
            doc_id=ref.doc_id,
            source=self.name,
            title=ref.title,
            text=text,
            url=node["url"],
            updated_at=node["updatedAt"],
            version=ref.version,
            location=ref.extra["team"],
            labels=labels,
        )

    def close(self) -> None:
        self.client.close()
