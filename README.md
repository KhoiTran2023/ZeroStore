# ZeroStore

**Tell your sources once. Every agent knows.**

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about your company. It reads the places truth already lives (Google Drive, GitHub, Linear), notices what changed, and serves small, current, cited answers to any agent over MCP.

> Status: early development. Working so far: settings, the fact store, tokens with access levels, the MCP server, syncing from Google Drive, GitHub and Linear, the librarian's judgment (noise filtering, confidence and the review queue, reranking and "not sure"), `zerostore demo`, and the web UI. The one-line installer and Sign in with Google come next.

## Try it in 60 seconds

```bash
pip install git+https://github.com/KhoiTran2023/ZeroStore
zerostore demo
```

`zerostore demo` loads Pebble & Co., a made-up startup with Google Docs, a GitHub repo and Linear issues, and plays out the story: two agents say the Pro plan costs $20, the CEO edits the pricing doc to $29, `zerostore sync` reports `1 change · Pricing 2026 · demo judge confidence 0.97`, and both agents now answer $29. Then a "maybe we drop the free tier?" note lands at confidence 0.54 and waits for review instead of changing the answer. It needs no accounts, keys or network, and never touches your real setup.

To try it with your own agents, run `zerostore demo start`. It prints a `claude mcp add` line (and the same for Cursor or Codex) with a token for Pebble & Co. Ask your agent what the Pro plan costs, run `zerostore demo change-price` and the sync command it shows, then ask again. The demo judges with a small set of scripted rules; with `JEV_API_KEY` set, Jev judges instead. `zerostore demo remove` deletes it. To see it in the browser, run the `zerostore serve ... --http --open` line it prints.

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

## The web UI

```bash
zerostore serve --http --open
```

Serves agents at `/mcp` and opens the web UI at `http://localhost:8765`, signed in with a one-time link. Later, `zerostore ui` opens it signed in again. Behind a reverse proxy, start it with `--behind-https-proxy --public-url https://zs.example.com`. Four pages, in the mockup E design:

- **Setup:** connect your docs, add your AI key (Jev optional), and "Connect to Claude": it creates a token and shows the `claude mcp add` line and the settings for Claude Desktop, Cursor and others.
- **Status:** the last sync and what it changed, what waits for you, sources and agents, the recent log, and charts of changes per day, decisions by outcome, how sure the librarian was, questions per day and median search time.
- **Review:** each waiting change shows what agents are told now and what they'd be told instead, with approve or reject.
- **Settings:** a form and a YAML view of `zerostore.yaml`, plus tokens. Saves are checked before anything is written, comments in the file are kept, and keys go to `secrets.env` next to it (readable only by you), referred to as `${ZEROSTORE_LLM_API_KEY}` and so on.

The web UI is a client of the MCP server like any agent: it calls the owner tools with your owner token, so every page is checked and logged. Only an owner token signs in, refused sign-ins are logged and limited to ten a minute, and revoking the token signs that browser out. On `localhost` it refuses other host names (DNS rebinding), every form carries a CSRF token, and the session lives in memory, so restarting `zerostore serve` signs you out. Settings you save apply to syncs (scheduled ones too) right away. Turn the web UI off with `web_ui: false`.

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
| `owner` | Everything | Search, read, manage tokens and the review queue, see status and activity, sign in to the web UI |

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
| `zerostore serve` | Runs the MCP server over stdio (one agent) or `--http` (many, plus the web UI), plus timed syncs |
| `zerostore ui` | Prints and opens a one-time sign-in link for the web UI |
| `zerostore sync` | Checks every source for changes now |
| `zerostore review` | Lists what waits for you; `review approve <id>` / `review reject <id>` decide |
| `zerostore reindex` | Rebuilds the fact store from your sources (for example after switching embeddings) |
| `zerostore token create / list / revoke` | Manages access tokens |
| `zerostore status` | Shows what ZeroStore holds and what waits for review |
| `zerostore connect <agent>` | Adds or removes the "check ZeroStore first" line |
| `zerostore demo` | Plays the Pebble & Co. story; `demo start`, `change-price`, `add-note` and `remove` step through it |

See [`zerostore.example.yaml`](zerostore.example.yaml) for every setting.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

Licensed under the Apache License 2.0.
