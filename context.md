# ZeroStore context

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about a company. It reads sources (Google Drive, GitHub, Linear), keeps a fact store current, and serves small, cited answers to agents over a read-only MCP server with tokens and access levels. Tagline: "Tell your sources once. Every agent knows."

## Key decisions

- Python package, installed with pip/pipx or a one-line installer. No Docker.
- Fact store: LanceDB (in-process; local disk or S3). Holds only the current version of each document; history lives in the audit table.
- State: SQLite (tokens, access log, audit table, review queue, sync cursors, the per-document index, locks). Postgres later for split deployments.
- MCP server is the only door. Every request carries a token; every token has one access level: `default` (non-confidential facts), `confidential` (all facts), `owner` (all facts plus setup tools). No token can change a fact.
- Tokens are random `zs_...` strings shown once; only their SHA-256 hash is stored. Each call re-checks the token, so revoking is immediate.
- Transports: stdio (token from `ZEROSTORE_TOKEN`; only the tools that level may use are listed) and streamable HTTP at `/mcp` (bearer token checked at the door and again per call). Plain HTTP is refused on non-local addresses unless TLS files or `--behind-https-proxy` are given.
- Agents learn to check ZeroStore through three nudges: tool descriptions that say when to call, server instructions, and an optional one-line rule in CLAUDE.md / AGENTS.md / .cursorrules written inside a `<!-- zerostore -->` block.
- Agents propose facts with `propose_fact`. The person in the agent app is asked "Save this to company facts?" through MCP elicitation (an SDK resolver, so it works on both the 2026-07-28 multi-round-trip protocol and older ones); declining drops it, cancelling or an app that can't ask leaves it in the review queue, and tokens with auto-approve skip the question. Approved facts are appended (append-only, so concurrent approvals never lose one) to `decisions.md` (default `~/.zerostore/decisions.md`), or to `decisions-confidential.md` next to it when the proposing token is confidential, and read back as the `decisions` source, never judged again. Removing an entry from those files retires it directly.
- Admin CLI commands (init, token, status) work on the local state file directly, because the first owner token has to be made somewhere and whoever can read `~/.zerostore` already holds it.
- Built on the MCP Python SDK 2.x (`MCPServer`), pinned `mcp>=2.3,<3`.
- Connectors only read. Each sync lists ids plus a version marker (Drive modified time, git blob sha, Linear `updatedAt`) and fetches text only for changed items; a content fingerprint skips re-saves with no edits. Items no longer listed are retired, except when a source lists nothing at all (treated as lost access, everything kept, warning shown). A failed listing changes nothing for that source; one unreadable file doesn't stop the rest.
- Credentials live in `zerostore.yaml` as `${ENV}` references (GitHub token, Linear API key) or a file path (Google credentials: an authorized-user file from Sign in with Google, or a service-account key). Google tokens are minted with httpx + PyJWT, no Google SDK.
- Confidentiality rules (Drive folder, GitHub repo, Linear label) are applied at sync time; changing them makes the next sync re-check every document.
- Timed syncs run inside `zerostore serve` (cron line from `sync.schedule`, local time, APScheduler's cron parser). Several processes may share one state file, so a SQLite lock lets only one sync run at a time, and a stored anchor makes each scheduled slot run once; a slot missed while the computer slept runs once on waking.
- Removing a source from `zerostore.yaml` retires its documents on the next full sync.
- Decision engine: one interface (`keep`, `route`, `compare`, `on_delete`, `score`), every answer 0 to 1. Backends: Jev (TypeSafe, `POST /v1/systemone`, one request per chunk with keep and topic asked together, run in parallel), your LLM over plain HTTP (Anthropic Messages with a forced tool; OpenAI, Gemini, OpenRouter and Ollama through the OpenAI chat format with a JSON schema; items batched 15 per call; confidence is self-reported), and a local fastembed cross-encoder for `score` only. LiteLLM from the spec was replaced by direct HTTP calls to keep dependencies small. `ai.llm_model: mid-tier` maps to claude-sonnet-5-5, gpt-6-sol or gemini-3.8-flash; Ollama and OpenRouter need a model name.
- Which engine answers: the judge is Jev when `decision_engine: jev` and its key is set, else the LLM, else none (every change applied directly, as in stage 2). `narrow_with`/`rerank_with: auto` follow the judge; reranking falls back to the local model when no AI is set up and `zerostore[local]` is installed.
- A change's confidence is the lowest confidence among its judgments (keep for every chunk, compare for every kept chunk against the closest fact from another document). At or above `review.auto_apply_at` it is applied; below, it waits in the review queue as a `change` item holding the full planned chunks, and the live version stays. The same version is never queued twice; a newer version, or the source going back to the live version, closes the older pending item; a rejected version is remembered (`documents.rejected_fingerprint`) and not proposed again. A change queued under different confidentiality rules can't be approved; the next sync re-checks it. Approving a change or deletion takes the sync lock, so it never races a running sync. Engine or embedding failures skip that one document and are reported; the sync carries on.
- Newer wins across documents: when `compare` says a kept chunk updates (`changed`) or contradicts (`conflict`) the closest fact from another document, the older one (by `updated_at`) gets `superseded_by = <newer doc>` and drops out of search. Retiring or changing the newer document restores what it hid. Only facts with the same access level are compared, so a confidential document never hides or is named next to a fact default tokens can see (deletion candidates follow the same rule). A queued change remembers the text of each fact it would hide and skips any that changed before approval.
- Deleted files go through `on_delete` after every source is read, with candidates from documents added in the same sync plus the nearest facts: moved (retired, audited as `document_moved`), retire, keep (facts stay, `source_deleted_at` set, not asked again) or unsure/low confidence (a `delete` review item; facts stay live). An engine failure keeps everything as it was and reports it.
- Retrieval: `route` narrows to topics only when its confidence is at least 0.60 (untagged chunks are always included), hybrid search fetches 4 candidates per fact returned, `score` reranks them with the steering rule and each document's date, and below `retrieval.answer_cutoff` the answer is "not sure". If a narrowed search scores below the cutoff, everything is searched before giving up.
- Embeddings: your provider's service when it has one (OpenAI `text-embedding-3-small`, Ollama `nomic-embed-text`), else the local fastembed model, else the hashing embedder. A store keeps the embedder it was built with until `zerostore reindex`, so adding a key later never breaks it. Reindex takes the sync lock before emptying the store, keeps rejected versions, re-judges every document, and warns first about documents it can't rebuild (deleted from their source, or kept at a version the source no longer has).
- State schema v3 adds `review_queue.doc_id`, `documents.rejected_fingerprint` and `documents.source_deleted_at`; older files are upgraded on open. Older fact stores get the `superseded_by` column on open.

## Current state

- Stage 1 (core, merged): settings, fact store, state DB, tokens and access levels, MCP server (stdio + HTTP), CLI `init`, `serve`, `token`, `status`, `connect` (rule line only). Search is LanceDB hybrid (vector + keyword) search; reranking, narrowing and the "not sure" cutoff arrive with the retrieval layer.
- Stage 2 (sources and sync, merged): Google Drive, GitHub and Linear connectors; Markdown-aware chunking; ingestion that adds, updates and retires documents with audit rows; `zerostore sync`; timed syncs in `serve`; owner-only `sync_now` MCP tool; last sync shown in `zerostore status`.
- Stage 3 (the librarian's judgment): decision engine with Jev, LLM and local backends; noise filtering and topic tags at ingestion; confidence, the review queue and superseding in the update layer; judged deletions; retrieval with narrowing, reranking and "not sure"; proposals with in-app approval and `decisions.md`; provider embeddings; `zerostore review` and `zerostore reindex`. The Jev and LLM backends are tested against fake APIs only, not live accounts.
- Next stages: `zerostore demo`; web UI in the mockup E design; installer and service setup; a Sign in with Google flow that writes the credentials file; `zerostore report`; caching keep decisions for unchanged chunks so edits to long documents cost less.

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
│   ├── cli.py                init, serve, sync, review, reindex, token, status, connect
│   ├── config.py             Reads and checks zerostore.yaml (env-var expansion, defaults, clear errors)
│   ├── access/
│   │   └── tokens.py         Access levels; token create, verify, list, revoke, auto-approve
│   ├── agents/
│   │   └── rules.py          Adds and removes the "check ZeroStore first" line in agent settings files
│   ├── connectors/
│   │   ├── base.py           Connector interface: list items with versions, fetch one document
│   │   ├── decisions.py      decisions.md: approved agent proposals, appended and read back as a source
│   │   ├── gdrive.py         Google Drive folders (Docs, Sheets, Slides, text and Markdown files)
│   │   ├── google_auth.py    Google access tokens from an authorized-user file or service-account key
│   │   ├── github.py         Written docs on each repo's default branch
│   │   └── linear.py         Linear issues and documents for chosen teams (GraphQL)
│   ├── decisions/
│   │   ├── base.py           DecisionEngine interface, answer types and the questions every backend asks
│   │   ├── jev.py            Jev (TypeSafe) backend: typed questions over HTTP, run in parallel
│   │   ├── llm.py            Your AI provider as the engine: Anthropic or OpenAI-style structured output
│   │   ├── local.py          Local cross-encoder reranker (score only)
│   │   └── select.py         Which backend answers judging, narrowing and reranking, from the settings
│   ├── librarian/
│   │   ├── chunking.py       Splits documents into heading-aware chunks
│   │   ├── ingestion.py      Sync: list and fetch what changed, hand it to the update layer, handle deletions
│   │   ├── update.py         Judgment on each change: noise, topics, newer-wins, confidence, deletions
│   │   ├── review.py         What approving or rejecting each kind of review item does
│   │   └── retrieval.py      Questions to answers: narrow, search, rerank, "not sure"
│   ├── scheduler/
│   │   └── runner.py         Timed syncs inside `serve`, once per scheduled slot across processes
│   ├── mcp/
│   │   ├── descriptions.py   Server instructions, tool descriptions and the agent rule line
│   │   ├── server.py         The MCP server: tools, per-call token and level checks, access logging
│   │   └── http.py           Streamable HTTP app with bearer-token check at the door
│   └── store/
│       ├── facts.py          LanceDB fact store: replace/delete documents, superseding, hybrid search, access filters
│       ├── state.py          SQLite state: tokens, access log, audit, review queue, documents, locks, meta
│       └── embeddings.py     Embedder interface, provider embeddings, local fastembed model, hashing fallback
└── tests/                    Tests: config, tokens, fact store, state, MCP server (in-process, HTTP, stdio), CLI,
                              rules, connectors and engines (fake APIs), chunking, ingestion, the librarian's
                              judgment, retrieval, proposals, embeddings, scheduler, end-to-end sync
```

## What each folder contains

- `.github/workflows/`: continuous integration.
- `src/zerostore/`: the Python package behind the `zerostore` command.
- `src/zerostore/access/`: tokens and access levels.
- `src/zerostore/agents/`: edits to agent settings files (the "check ZeroStore first" line).
- `src/zerostore/connectors/`: one read-only module per source, plus the shared interface, Google sign-in tokens and decisions.md.
- `src/zerostore/decisions/`: the decision engine: every judgment call as a typed question, answered by Jev, your LLM or a local reranker.
- `src/zerostore/librarian/`: the librarian's three layers: ingestion (with chunking), update (judgment and the review queue) and retrieval, plus review decisions.
- `src/zerostore/scheduler/`: timed syncs.
- `src/zerostore/mcp/`: the one door, the MCP server, plus everything agents read from it.
- `src/zerostore/store/`: where data lives: the LanceDB fact store, the SQLite state database and embeddings.
- `tests/`: the pytest suite, using temporary folders, made-up company documents, fake source and AI APIs, and a scripted decision engine (`fakes.py`).
