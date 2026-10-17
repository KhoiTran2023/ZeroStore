# Changes

What changed for people running ZeroStore and for the agents that use it, newest first.
Each entry says what behaves differently and what, if anything, you need to do.
Setup steps live in the [README](README.md) and [docs/connect-agents.md](docs/connect-agents.md).

## Unreleased

### `zerostore doctor`

- New command that checks everything an agent needs before you add ZeroStore to it: source keys and
  credentials, the four services, the address agents use, its certificate, connector sign-in, and
  whether every tag has a role that can see it.
- It calls the address the way ChatGPT, Grok and Claude do, trusting only public certificate
  authorities. It catches a self-signed certificate or private `root.crt`, plain HTTP on a network
  address, and a server started without `--public-url` (the sign-in would name the wrong address).
- Each problem comes with the one thing to change. It exits with 1 while any problem is left, and
  `--json` prints the checks for scripts. `--url` checks an address other than where the server runs.

### Service health for the four parts

- `zerostore serve --http` runs the four parts in one program: the web console, the MCP server, the
  vector DB (LanceDB) and the ingestion agent (timed syncs).
- The settings page has a **services** table showing each part as running, stopped, off or error.
  Each row says why: the fact count and read time for the vector DB, or the last and next sync for
  the ingestion agent. The status page shows the same four as dots under the sync summary.
- `zerostore status` lists the services too, even from another terminal. The server writes a heartbeat
  to the state file every 15 seconds. A server that misses three heartbeats, or shuts down, shows as
  stopped. `zerostore status --json` includes them under `services`.
- Nothing to do: it works as soon as you run the new version.

### ChatGPT and Grok sign in like Claude

- ChatGPT (Settings, Plugins, Add custom MCP server) and Grok (Connectors, New Connector, Custom) can
  add ZeroStore by its `https://<address>/mcp`. They sign in the same way Claude's connectors do: the
  owner allows them on a web UI page, with no token or certificate to paste.
- Agents that publish a client document (Claude, ChatGPT, Grok) no longer need to register first.
  ZeroStore reads their document over HTTPS. It refuses documents on private addresses, redirects,
  and documents over 64 KB.
- Every sign-in answer now carries `iss`, which ChatGPT requires.
- New `fetch` tool, and `search` also returns `results` (`id`, `title`, `url`). These are the shapes
  ChatGPT's connectors expect. The existing `facts` field is unchanged.
- New guide: [docs/connect-agents.md](docs/connect-agents.md) covers every agent.

### Roles and tags

- A tag marks information by where it lives: a Drive folder, a GitHub repo or a Linear label.
  A role lists the tags its agents may see.
- Every token or connector can have one role, set in the web UI, with `zerostore token create --role`,
  or with `zerostore token role`.
- **Behavior change:** once a tag exists, information carrying it is hidden from every agent whose role
  lacks that tag, including agents with no role. Untagged information stays visible to all. Owner tokens
  see everything.
- The `confidential` rule still applies on top: a default-level token never sees confidential
  information, whatever its role.
- Tags apply from the next sync. A tag you remove keeps hiding its documents until that sync, so a typo
  never makes information public.
- Edit tags and roles on the settings page ("who sees what") or in `zerostore.yaml`. Both write the
  same settings.
- The `whats_changed` tool leaves out changes to documents the caller can't see.
- The state file's format moves to version 7 on first start. It is upgraded in place.

### Add it like Gmail (Claude's connectors)

- ZeroStore can be added under Claude's Settings, Connectors with its `https://<address>/mcp`.
  The owner allows it on the web UI's approval page, and it gets its own token.
- The setup page shows the address to paste.
- Agents call ZeroStore when a question needs company knowledge, with no CLAUDE.md line needed. The
  server's instructions and tool descriptions tell them when to call it.

### Connecting Claude without a certificate

- The pasted Claude Code and Claude Desktop settings use the full path to `zerostore`, so they work
  without changing PATH.
- `/mcp` accepts a tunnel's or proxy's public name when you start the server with `--public-url`.
  Before, it answered 421.
- Claude connects over plain local HTTP or over a public HTTPS name. No `root.crt` to trust.

### Remembered keep answers

- When a long document is edited, only the changed chunks go to your AI for keep and topic answers.
  Earlier answers are remembered, so edits cost less.

### Sign in with Google

- The setup page signs in to Google, saves the credentials file, and lets you browse Drive folders to
  add or remove. No service account is needed.

### Installer and background service

- `curl ... | sh` installs ZeroStore, sets it up, starts it, and opens the setup page.
- `zerostore service install / status / uninstall` keeps `zerostore serve --http` running with systemd
  or launchd. Keys belong in `secrets.env`, because the service doesn't see your shell.

### Web UI

- `zerostore serve --http` serves the web console next to `/mcp`: setup, status (with charts), review,
  log and settings.
- Sign in with a one-time link from `zerostore ui`, or with the owner token.

### The demo

- `zerostore demo` plays the Pebble & Co. story with no accounts or network. `demo start`,
  `change-price`, `add-note` and `remove` step through it with real agents.

### The librarian's judgment

- Each change is judged before it is applied. Noise is left out. Low-confidence changes and deletions
  wait in the review queue (`zerostore review`). Newer facts supersede older ones.
- Search narrows, reranks, and says "not sure" instead of guessing.
- `zerostore reindex` rebuilds the fact store, for example after switching embeddings.

### Sources and sync

- Google Drive, GitHub and Linear are read on a schedule (default daily at 06:00) and with
  `zerostore sync`.

### The core

- Settings in `zerostore.yaml`, the LanceDB fact store, the SQLite state file, tokens with access levels,
  and the MCP server over stdio or HTTP.
