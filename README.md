# ZeroStore

**Tell your sources once. Every agent knows.**

ZeroStore is a free, self-hosted, open-source knowledge layer that keeps every AI agent on the latest truth about your company. It reads the places truth already lives (Google Drive, GitHub, Linear), notices what changed, and serves small, current, cited answers to any agent over MCP.

> Status: early development. Working so far: settings, the fact store, tokens with access levels, the MCP server, syncing from Google Drive, GitHub and Linear, the librarian's judgment (noise filtering, confidence and the review queue, reranking and "not sure"), `zerostore demo`, the web UI, the one-line installer, the background service, Sign in with Google, and remembered keep answers so edits to long documents cost less.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh
```

On Linux or macOS, this installs the `zerostore` command (with uv, or with pipx if you already have it; uv brings Python 3.10 or newer when the machine has none), creates your settings and owner token in `~/.zerostore`, starts ZeroStore in the background and opens the setup page. Running it again updates ZeroStore and keeps your settings and data. While the repository is private, run it from a clone instead: `ZEROSTORE_PACKAGE=. sh install.sh`. `ZEROSTORE_NO_SERVICE=1` skips the background service and `ZEROSTORE_NO_OPEN=1` prints the setup link instead of opening it.

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
claude mcp add zerostore -e ZEROSTORE_TOKEN=<that token> -- "$(command -v zerostore)" serve
```

Then list your sources in `~/.zerostore/zerostore.yaml` and run `zerostore sync`:

```yaml
sources:
  google_drive: { folders: ["Company/Decisions"], credentials: ~/.zerostore/google-credentials.json }
  github:       { repos: ["acme/app"], token: "${GITHUB_TOKEN}" }
  linear:       { teams: ["Product"], api_key: "${LINEAR_API_KEY}" }
```

After that, `zerostore serve` also re-syncs on the schedule in your settings (daily at 06:00 by default), so agents always get the current version.

`zerostore init` also offers to add one line to your agent's settings file (`CLAUDE.md`, `AGENTS.md` or `.cursorrules`) so agents check ZeroStore before assuming anything about the company. `zerostore connect --undo` removes it.

## The web UI

```bash
zerostore serve --http --open
```

Serves agents at `/mcp` and opens the web UI at `http://localhost:8765`, signed in with a one-time link. Later, `zerostore ui` opens it signed in again. Behind a reverse proxy, start it with `--behind-https-proxy --public-url https://zs.example.com`. Four pages, in the mockup E design:

- **Setup:** Sign in with Google and pick your Drive folders, add your AI key (Jev optional), and "Connect to Claude": it creates a token and shows the `claude mcp add` line and the settings for Claude Desktop, Cursor and others.
- **Status:** the last sync and what it changed, what waits for you, sources and agents, the recent log, and charts of changes per day, decisions by outcome, how sure the librarian was, questions per day and median search time.
- **Review:** each waiting change shows what agents are told now and what they'd be told instead, with approve or reject.
- **Settings:** a form and a YAML view of `zerostore.yaml`, plus tokens. Saves are checked before anything is written, comments in the file are kept, and keys go to `secrets.env` next to it (readable only by you), referred to as `${ZEROSTORE_LLM_API_KEY}` and so on.

The web UI is a client of the MCP server like any agent: it calls the owner tools with your owner token, so every page is checked and logged. Only an owner token signs in, refused sign-ins are logged and limited to ten a minute, and revoking the token signs that browser out. On `localhost` it refuses other host names (DNS rebinding), every form carries a CSRF token, and the session lives in memory, so restarting `zerostore serve` signs you out. Settings you save apply to syncs (scheduled ones too) right away. Turn the web UI off with `web_ui: false`.

### Sign in with Google

On the setup page, "Sign in with Google Drive" sends you to Google, asks for read-only Drive access and brings you back, then lets you browse My Drive, folders shared with you and shared drives and add the folders ZeroStore should read. The sign-in is saved as `google-credentials.json` next to `zerostore.yaml`, readable only by you; "sign out of Google" revokes it and deletes the file. When ZeroStore runs on another machine, Google ends on a `http://localhost/...` address that doesn't load: paste it into the setup page to finish.

Signing in needs a Google sign-in app (an OAuth client). Until ZeroStore ships with its own, make one in the Google Cloud console: turn on the Google Drive API, set up the OAuth consent screen with yourself as a test user, create an OAuth client ID of type **Desktop app**, and paste its ID and secret on the setup page (saved in `secrets.env` as `ZEROSTORE_GOOGLE_CLIENT_ID` and `ZEROSTORE_GOOGLE_CLIENT_SECRET`). A service-account key shared on your folders still works too: point `credentials:` at it.

## Keep it running

```bash
zerostore service install     # start on boot (Linux) or at login (macOS), and restart after a crash
zerostore service status      # is it running, and where are its logs
zerostore service uninstall   # stop and remove it; settings and data stay
```

On Linux it's a systemd user unit, and `install` turns on lingering so it starts on boot before anyone logs in (when it can't, it prints the one `sudo loginctl enable-linger` line); run as root, it's a system unit instead. On macOS it's a launchd agent that starts when you log in, logging to `~/.zerostore/logs/serve.log`. The service runs `zerostore serve --http` with the options you give `install` (`--host`, `--port`, `--behind-https-proxy`, `--public-url`, `--tls-cert`/`--tls-key`); everything else comes from `zerostore.yaml`. Running `install` again updates and restarts it.

The service doesn't see your shell's environment variables, so keep keys in `secrets.env` next to `zerostore.yaml` (Settings in the web UI saves them there); `install` warns about any `${NAME}` in your settings that only your shell has.

On an always-on server such as AWS EC2, put HTTPS with a real certificate in front (see [Agents on another computer](#agents-on-another-computer)) and run:

```bash
zerostore service install --behind-https-proxy --public-url https://zs.example.com
```

Add `--host 0.0.0.0` only when the HTTPS proxy runs on a different machine, such as a cloud load balancer.

## The librarian's judgment

Add an AI key and ZeroStore judges every change before agents see it. [Jev](https://typesafe.ai) answers each judgment call in about a tenth of a second; without a Jev key, your own AI provider answers the same questions (Anthropic, OpenAI, Gemini, OpenRouter or a local Ollama).

```yaml
ai:
  jev_api_key: ${JEV_API_KEY}     # optional, the fast option
  llm_provider: anthropic         # or openai, gemini, openrouter, ollama
  llm_api_key: ${LLM_API_KEY}
```

- **Noise is left out.** "Lunch at noon?" never becomes a company fact. What's kept is tagged with topics. When you edit a long document, only the sections you changed are asked about again: the answers for unchanged text are remembered, separately for each AI and model, and dropped after six months without use.
- **Newer wins.** When a new document updates or contradicts a fact from another one, the older fact stops being served. It comes back if the newer document is deleted.
- **Unsure changes wait for you.** Changes at or above `review.auto_apply_at` (0.80) go live; anything less certain waits in the review queue while agents keep getting the current version. Run `zerostore review`, then `zerostore review approve <id>` or `reject <id>`.
- **Deleted files are judged too.** A file that was moved is recognised; one that is unclear waits for you, and its facts stay live meanwhile.
- **Search answers or says "not sure".** Each question is narrowed to likely topics, candidates are reranked, and if nothing answers well enough the agent is told "not sure" instead of being handed weak facts.

With no AI key, every change is applied directly, and search still works (reranked by a small local model if you install `zerostore[local]`).

Agents can also propose facts (`propose_fact`). The person using the agent is asked "Save this to company facts?" in their app; approved facts go to `~/.zerostore/decisions.md`, which ZeroStore reads back like any other source.

## Connect your agents

**On the same computer, nothing needs a certificate.** The agent starts `zerostore serve` itself and talks to it over stdio, with no network and no port. "Connect to Claude" on the setup page (or `zerostore token create`) gives you the lines to paste:

- Claude Code: `claude mcp add zerostore -e ZEROSTORE_TOKEN=<token> -- /full/path/to/zerostore serve`
- Claude Desktop (Settings, Developer, Edit Config), Cursor and most others: the same command as `mcpServers` JSON.

The lines use the full path to `zerostore`, because Claude Desktop and other apps opened from the Dock or Start menu don't get your terminal's PATH and wouldn't find a bare `zerostore`. When `zerostore serve --http` is running, Claude Code can also connect to `http://localhost:8765/mcp` with `claude mcp add --transport http`; plain HTTP is only allowed on `localhost`.

### Agents on another computer

Give the server an HTTPS address with a certificate that every computer already trusts. Then agents connect with nothing to install or trust:

- **Tailscale** (private to your tailnet): on the server, `tailscale serve --bg 8765` publishes `https://<machine>.<tailnet>.ts.net` with a Let's Encrypt certificate.
- **Cloudflare Tunnel** (your own domain, no open ports): `cloudflared tunnel` pointed at `http://localhost:8765`.
- **Caddy** (your own domain, port 443 open): `zs.example.com { reverse_proxy localhost:8765 }` gets a certificate automatically.

Run ZeroStore behind it with `zerostore service install --behind-https-proxy --public-url https://<that address>` (it keeps listening on `localhost` only). The setup page then shows the `claude mcp add --transport http` line with that address.

Avoid a self-signed certificate or one issued for an IP address with `--tls-cert`: every computer that connects would have to trust your `root.crt` first (Node apps such as Claude Code also need `NODE_EXTRA_CA_CERTS`), which breaks handing ZeroStore to someone else.

### Add it like Gmail (Claude, ChatGPT, Grok)

Claude, ChatGPT and Grok on the web and phone add ZeroStore the way they add Gmail, with no token to copy:

1. Paste `https://<your address>/mcp` in the app:
   - Claude: Settings, Connectors, Add custom connector.
   - ChatGPT: chatgpt.com/plugins, then "+", then "Add custom MCP server".
   - Grok: grok.com/connectors, then New Connector, then Custom.
2. The app sends you to ZeroStore's web UI. Sign in if asked, pick the access level and role, and click Allow.

Step-by-step guides for every agent are in [docs/connect-agents.md](docs/connect-agents.md).

From then on Claude looks things up in ZeroStore by itself whenever a question is about your company. ChatGPT works the same once you turn the connector on for a conversation. The connection shows in your token list as "<app> (connector)"; revoke it there to disconnect. The apps' servers make the connection, so the address must be public HTTPS that the internet can reach: Cloudflare Tunnel or Caddy with your own domain work, a private Tailscale address doesn't (use Tailscale Funnel instead). The setup page shows the address to paste when ZeroStore runs with a public HTTPS `--public-url`. Claude Code can sign in the same way with `claude mcp add --transport http zerostore https://<your address>/mcp`, then `/mcp` inside Claude Code.

## How access works

The MCP server is the only way in, and every request carries a token. Each token has one access level:

| Level | Sees | Can do |
|---|---|---|
| `default` | Non-confidential facts | Search, read, propose facts for you to approve |
| `confidential` | Everything | Search, read, propose facts for you to approve |
| `owner` | Everything | Search, read, manage tokens and the review queue, see status and activity, sign in to the web UI |

A connector added in Claude's settings gets `default` or `confidential`, never `owner`.

### Roles and tags

Levels decide confidential versus not. Roles decide the rest: which teams' information each agent sees.

1. **Tag information by where it lives.** On the web UI's settings page, under "who sees what", give a Drive folder (and everything in it), a GitHub repo or a Linear label a tag, for example `sales` for `Company/Sales`. The same table holds your `confidential` places.
2. **Make roles.** A role lists the tags it may see, for example `leadership: sales, engineering`.
3. **Give each token a role.** Pick it when you create the token, in the token list, or on the page where you allow a Claude connector. From the command line: `zerostore token create --name sales-bot --role sales`, or `zerostore token role <id> sales` to change it later.

The rules:
- Untagged information is for every agent.
- A tagged document reaches only tokens whose role has all of its tags.
- A default token still never sees confidential information, whatever its role.
- Owner tokens see everything.
- Tags reach search, reading a fact, the document list and "what changed". Facts with different tags never hide each other when one updates the other.

The page writes `confidential:`, `tags:` and `roles:` in `zerostore.yaml`, the same settings the CLI and the YAML view use (see `zerostore.example.yaml`). The next sync applies new tags to your documents. A tag you remove stays hidden until that sync, so a typo never turns tagged information public.

No token can edit a fact. Facts change only when your sources change. Tokens are stored hashed, every request is logged, and the server binds to `localhost` unless you give it HTTPS.

## What gets read

- **Google Drive:** Google Docs, Sheets (first sheet) and Slides as text, plus plain text and Markdown files, in the folders you list and every folder inside them. Sign in with your Google account on the setup page, or use a service account the folders are shared with.
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
| `zerostore token create / list / role / revoke` | Manages access tokens and their roles |
| `zerostore status` | Shows what ZeroStore holds and what waits for review |
| `zerostore connect <agent>` | Adds or removes the "check ZeroStore first" line |
| `zerostore service install / status / uninstall` | Keeps `zerostore serve --http` running in the background (systemd or launchd) |
| `zerostore demo` | Plays the Pebble & Co. story; `demo start`, `change-price`, `add-note` and `remove` step through it |

See [`zerostore.example.yaml`](zerostore.example.yaml) for every setting.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

Licensed under the Apache License 2.0.
