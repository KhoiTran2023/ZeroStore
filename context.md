# ZeroStore context

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about a company. It reads sources (Google Drive, GitHub, Linear), keeps a fact store current, and serves small, cited answers to agents over a read-only MCP server with tokens and access levels. Tagline: "Tell your sources once. Every agent knows."

## Key decisions

- Python package, installed with pip/pipx or a one-line installer. No Docker.
- Fact store: LanceDB (in-process; local disk or S3). Holds only the current version of each document; history lives in the audit table.
- State: SQLite (tokens, access log, audit table, review queue, sync cursors). Postgres later for split deployments.
- MCP server is the only door. Every request carries a token; every token has one access level: `default` (non-confidential facts), `confidential` (all facts), `owner` (all facts plus setup tools). No token can change a fact.
- Tokens are random `zs_...` strings shown once; only their SHA-256 hash is stored. Each call re-checks the token, so revoking is immediate.
- Transports: stdio (token from `ZEROSTORE_TOKEN`; only the tools that level may use are listed) and streamable HTTP at `/mcp` (bearer token checked at the door and again per call). Plain HTTP is refused on non-local addresses unless TLS files or `--behind-https-proxy` are given.
- Agents learn to check ZeroStore through three nudges: tool descriptions that say when to call, server instructions, and an optional one-line rule in CLAUDE.md / AGENTS.md / .cursorrules written inside a `<!-- zerostore -->` block.
- Agents propose facts with `propose_fact`; proposals wait in the review queue for a person. Elicitation prompts and writing approved facts to `decisions.md` come with the librarian stage.
- Admin CLI commands (init, token, status) work on the local state file directly, because the first owner token has to be made somewhere and whoever can read `~/.zerostore` already holds it.
- Built on the MCP Python SDK 2.x (`MCPServer`), pinned `mcp>=2.3,<3`.

## Current state

- Stage 1 (core): settings, fact store, state DB, tokens and access levels, MCP server (stdio + HTTP), CLI `init`, `serve`, `token`, `status`, `connect` (rule line only). Search is LanceDB hybrid (vector + keyword) search; reranking, narrowing and the "not sure" cutoff arrive with the retrieval layer.
- Embeddings: local fastembed model when `zerostore[local]` is installed, otherwise a hashing embedder that only matches words. The provider embedder arrives with the LLM decision engine. The fact store records which embedder built it and refuses to mix vectors from another.
- Next stages: connectors (Google Drive, GitHub, Linear) and sync scheduling; the librarian's ingestion, update and retrieval layers with the decision engine (Jev, LLM, local reranker); `zerostore demo`; web UI in the mockup E design; installer and service setup.

## Working on it

- `pip install -e ".[dev]"`, then `pytest` and `ruff check src tests` / `ruff format --check src tests`.
- CI runs lint and tests on Python 3.10 to 3.13 (Ubuntu) and 3.13 (macOS).
- Commits are authored as KhoiTran2023 with no AI attribution lines. Update this file with every change.

## File structure

```
ZeroStore/
├── README.md                 Landing page: what it is, quick start, access levels, commands
├── context.md                Project context; ends with this file structure
├── LICENSE                   Apache License 2.0
├── pyproject.toml            Package info, dependencies, the `zerostore` command, ruff and pytest settings
├── zerostore.example.yaml    Every setting, with comments
├── .gitignore                Files git should not track
├── .github/
│   └── workflows/ci.yml      Lint and tests on every push and pull request
├── src/zerostore/
│   ├── __init__.py           Version
│   ├── app.py                Opens config, state, tokens and fact store together; picks the embedder
│   ├── cli.py                init, serve, token, status, connect
│   ├── config.py             Reads and checks zerostore.yaml (env-var expansion, defaults, clear errors)
│   ├── access/
│   │   └── tokens.py         Access levels; token create, verify, list, revoke, auto-approve
│   ├── agents/
│   │   └── rules.py          Adds and removes the "check ZeroStore first" line in agent settings files
│   ├── mcp/
│   │   ├── descriptions.py   Server instructions, tool descriptions and the agent rule line
│   │   ├── server.py         The MCP server: tools, per-call token and level checks, access logging
│   │   └── http.py           Streamable HTTP app with bearer-token check at the door
│   └── store/
│       ├── facts.py          LanceDB fact store: replace/delete documents, hybrid search, access filters
│       ├── state.py          SQLite state: tokens, access log, audit table, review queue, sync cursors
│       └── embeddings.py     Embedder interface, local fastembed model, hashing fallback
└── tests/                    Tests for config, tokens, fact store, MCP server (in-process, HTTP, stdio), CLI, rules
```

## What each folder contains

- `.github/workflows/`: continuous integration.
- `src/zerostore/`: the Python package behind the `zerostore` command.
- `src/zerostore/access/`: tokens and access levels.
- `src/zerostore/agents/`: edits to agent settings files (the "check ZeroStore first" line).
- `src/zerostore/mcp/`: the one door, the MCP server, plus everything agents read from it.
- `src/zerostore/store/`: where data lives: the LanceDB fact store, the SQLite state database and embeddings.
- `tests/`: the pytest suite, using temporary folders and made-up company documents.
