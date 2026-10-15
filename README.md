# ZeroStore

**Tell your sources once. Every agent knows.**

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about your company. It reads the places truth already lives (Google Drive, GitHub, Linear), notices what changed, and serves small, current, cited answers to any agent over MCP.

> Status: early development. Working so far: settings, the fact store, tokens with access levels, the MCP server, and syncing from Google Drive, GitHub and Linear. The librarian's judgment (confidence, review queue, reranking), the demo and the web setup page come next.

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

ZeroStore only reads. Unchanged files are skipped, edited files replace their old version, and deleted files are retired. If a source suddenly lists nothing, ZeroStore keeps what it has and warns you instead of deleting everything.

## Commands so far

| Command | What it does |
|---|---|
| `zerostore init` | Creates settings, data folders and your owner token |
| `zerostore serve` | Runs the MCP server over stdio (one agent) or `--http` (many), plus timed syncs |
| `zerostore sync` | Checks every source for changes now |
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
