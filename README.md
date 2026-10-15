# ZeroStore

**Tell your sources once. Every agent knows.**

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about your company. It reads the places truth already lives (Google Drive, GitHub, Linear), notices what changed, and serves small, current, cited answers to any agent over MCP.

> Status: early development. Working so far: settings, the fact store, tokens with access levels, the MCP server, syncing from Google Drive, GitHub and Linear, and the librarian's judgment (noise filtering, confidence and the review queue, reranking and "not sure"). The demo and the web setup page come next.

## Try the core

```bash
pip install git+https://github.com/KhoiTran2023/ZeroStore   # pipx works too
zerostore init                                              # settings, data folder, your owner token
zerostore token create --name claude-code --level confidential
claude mcp add zerostore -e ZEROSTORE_TOKEN=<that token> -- zerostore serve
```

Then list your sources in `~/.zerostore/zerostore.yaml` and run `zerostore sync`:

```yaml
sources:
  google_drive: { folders: ["Company/Decisions"], credentials: ~/.zerostore/google-credentials.json }
  github:       { repos: ["acme/app"], token: ${GITHUB_TOKEN} }
  linear:       { teams: ["Product"], api_key: ${LINEAR_API_KEY} }
```

After that, `zerostore serve` also re-syncs on the schedule in your settings (daily at 06:00 by default), so agents always get the current version.

`zerostore init` also offers to add one line to your agent's settings file (`CLAUDE.md`, `AGENTS.md` or `.cursorrules`) so agents check ZeroStore before assuming anything about the company. `zerostore connect --undo` removes it.

## The librarian's judgment

Add an AI key and ZeroStore judges every change before agents see it. [Jev](https://typesafe.ai) answers each judgment call in about a tenth of a second; without a Jev key, your own AI provider answers the same questions (Anthropic, OpenAI, Gemini, OpenRouter or a local Ollama).

```yaml
ai:
  jev_api_key: ${JEV_API_KEY}     # optional, the fast option
  llm_provider: anthropic         # or openai, gemini, openrouter, ollama
  llm_api_key: ${LLM_API_KEY}
```

- **Noise is left out.** "Lunch at noon?" never becomes a company fact. What's kept is tagged with topics.
- **Newer wins.** When a new document updates or contradicts a fact from another one, the older fact stops being served. It comes back if the newer document is deleted.
- **Unsure changes wait for you.** Changes at or above `review.auto_apply_at` (0.80) go live; anything less certain waits in the review queue while agents keep getting the current version. Run `zerostore review`, then `zerostore review approve <id>` or `reject <id>`.
- **Deleted files are judged too.** A file that was moved is recognised; one that is unclear waits for you, and its facts stay live meanwhile.
- **Search answers or says "not sure".** Each question is narrowed to likely topics, candidates are reranked, and if nothing answers well enough the agent is told "not sure" instead of being handed weak facts.

With no AI key, every change is applied directly, and search still works (reranked by a small local model if you install `zerostore[local]`).

Agents can also propose facts (`propose_fact`). The person using the agent is asked "Save this to company facts?" in their app; approved facts go to `~/.zerostore/decisions.md`, which ZeroStore reads back like any other source.

## How access works

The MCP server is the only way in, and every request carries a token. Each token has one access level:

| Level | Sees | Can do |
|---|---|---|
| `default` | Non-confidential facts | Search, read, propose facts for you to approve |
| `confidential` | Everything | Search, read, propose facts for you to approve |
| `owner` | Everything | Search, read, manage tokens and the review queue |

No token can edit a fact. Facts change only when your sources change. Tokens are stored hashed, every request is logged, and the server binds to `localhost` unless you give it HTTPS.

## What gets read

- **Google Drive:** Google Docs, Sheets (first sheet) and Slides as text, plus plain text and Markdown files, in the folders you list and every folder inside them. Sign in with your Google account or use a service account the folders are shared with.
- **GitHub:** written docs on each repo's default branch (`*.md`, `*.txt`, `*.rst` and similar by default), not code.
- **Linear:** issues and documents of the teams you list.

ZeroStore only reads. Unchanged files are skipped, edited files replace their old version, and deleted files are retired (or, with an AI key, judged first). If a source suddenly lists nothing, ZeroStore keeps what it has and warns you instead of deleting everything.

## Commands so far

| Command | What it does |
|---|---|
| `zerostore init` | Creates settings, data folders and your owner token |
| `zerostore serve` | Runs the MCP server over stdio (one agent) or `--http` (many), plus timed syncs |
| `zerostore sync` | Checks every source for changes now |
| `zerostore review` | Lists what waits for you; `review approve <id>` / `review reject <id>` decide |
| `zerostore reindex` | Rebuilds the fact store from your sources (for example after switching embeddings) |
| `zerostore token create / list / revoke` | Manages access tokens |
| `zerostore status` | Shows what ZeroStore holds and what waits for review |
| `zerostore connect <agent>` | Adds or removes the "check ZeroStore first" line |

See [`zerostore.example.yaml`](zerostore.example.yaml) for every setting.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

Licensed under the Apache License 2.0.
