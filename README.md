# ZeroStore

**Tell your sources once. Every agent knows.**

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about your company. It reads the places truth already lives (Google Drive, GitHub, Linear), notices what changed, and serves small, current, cited answers to any agent over MCP.

> Status: early development. This stage has the core: settings, the fact store, tokens with access levels, and the MCP server. Connecting sources, syncing, the demo and the web setup page come next.

## Try the core

```bash
pip install git+https://github.com/KhoiTran2023/ZeroStore   # pipx works too
zerostore init                                              # settings, data folder, your owner token
zerostore token create --name claude-code --level confidential
claude mcp add zerostore -e ZEROSTORE_TOKEN=<that token> -- zerostore serve
```

`zerostore init` also offers to add one line to your agent's settings file (`CLAUDE.md`, `AGENTS.md` or `.cursorrules`) so agents check ZeroStore before assuming anything about the company. `zerostore connect --undo` removes it.

## How access works

The MCP server is the only way in, and every request carries a token. Each token has one access level:

| Level | Sees | Can do |
|---|---|---|
| `default` | Non-confidential facts | Search, read, propose facts for you to approve |
| `confidential` | Everything | Search, read, propose facts for you to approve |
| `owner` | Everything | Search, read, manage tokens and the review queue |

No token can edit a fact. Facts change only when your sources change. Tokens are stored hashed, every request is logged, and the server binds to `localhost` unless you give it HTTPS.

## Commands so far

| Command | What it does |
|---|---|
| `zerostore init` | Creates settings, data folders and your owner token |
| `zerostore serve` | Runs the MCP server over stdio (one agent) or `--http` (many) |
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
