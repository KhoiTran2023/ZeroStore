# Changes

What changed for people running ZeroStore and for the agents that use it, newest first.
Each entry says what behaves differently and what, if anything, you need to do.
Setup steps live in the [README](README.md) and [docs/connect-agents.md](docs/connect-agents.md).

## Unreleased

### Agents can change information, within their role

- New, off by default: `agent_writes` in zerostore.yaml. With `enabled: true`, `agent_writes.roles` lists per role the
  tags its agents may change (`untagged` for information with no tag). Those agents get `add_fact`, `change_fact`
  and `remove_fact`; every other write is refused and logged. Owner tokens may change everything. A role may only
  change tags it can see, which the settings check.
- Changes are saved as files in `~/.zerostore/written/` and read back as a source, so timed syncs keep them. A change
  to a fact from Google Drive, GitHub, Linear or a folder is a correction that hides the old fact until the original
  document is edited. Facts from documents can be corrected, not removed.
- `agent_writes.review: true` sends every agent change to the review queue (web UI, `zerostore review`) first.
- Nothing changes for you unless you turn it on.

### A clearer note when search matches words

- With an Anthropic (or Gemini, or Jev) key and no local model, `zerostore serve` and `zerostore sync` no longer say
  "add an AI key". They now say search finds facts by matching words and your AI ranks them, because that provider
  has no embeddings, and how to add search by meaning (`zerostore[local]`, then `zerostore reindex`).

### ZeroStore on your own computer reads a folder of yours

- A localhost install (`install.sh` with no `--domain` or `--ip`) is now a working ZeroStore for your personal use:
  it reads `~/Documents/ZeroStore` (or `~/ZeroStore` when there's no Documents folder), made with a welcome note if
  it's new, and checks it every five minutes. What's left is adding your AI key on the setup page, which now opens
  on that step.
- `--folder <path>` (or `ZEROSTORE_FOLDER`) picks another folder. `zerostore init --folder <path>` does the same on
  an existing install, adding the folder and leaving everything else as it was.
- New source, "Folders on this computer" (`sources.folders.paths` in zerostore.yaml): Markdown, text and
  reStructuredText files in the folders you list and every folder inside them, skipping hidden files and folders.
  The setup page adds and removes folders; the settings page lists them with the other sources. A file may start
  with the demo's `---` header for its title, date and labels.
- Confidential places, tags and roles can name a folder (`- folder: ~/Notes/Private`) and cover everything inside it.
- A new install runs its first sync as soon as it has a source, instead of waiting for the first scheduled time.
- Server installs (`--domain`, `--ip`) are unchanged: they start with no sources, as before.

### Fixes from a localhost end-to-end test

- Without an AI key or the local model, search no longer pads every answer with unrelated facts: a fact must share a
  word with the question (common words like "what" or "our" don't count). When none does, agents get "not sure"
  instead of the five closest facts.
- Over HTTP, an agent now lists only the tools its token may use, as it already did over stdio. A default or
  confidential token no longer sees the owner tools (`token_create`, `sync_now` and the rest); calling them was
  already refused.
- `zerostore status` lists approved decisions (`decisions.md`) as a source once that file exists, instead of
  "none yet".
- When the local embedding model (`zerostore[local]`) can't be downloaded, for example because the computer can't
  reach huggingface.co, `zerostore reindex` explains that in one line instead of crashing, and leaves the fact store
  as it was. A new install starts with word matching and says to run `zerostore reindex` once the model loads.

### Uninstall in one step

- `install.sh --uninstall` stops the background service, removes the `zerostore` command (uv or pipx), and takes
  ZeroStore's site out of Caddy's settings. It keeps `~/.zerostore` (settings, tokens, keys, the fact store) for a
  reinstall; `--delete-data` deletes it too.
- uv and Caddy stay, since other programs may use them. Agents you connected keep listing ZeroStore until you remove
  it in each one.

### HTTPS on a server's IP address, with no domain

- `install.sh --ip` serves ZeroStore at `https://<the server's public IP>`. Let's Encrypt has issued certificates for
  IP addresses since January 2026, and Caddy 2.11 gets them, so agents trust the address with nothing to install.
- `--ip` finds the public IPv4 address, or `--ip=203.0.113.7` names it. It refuses private addresses and a Caddy
  older than 2.11.
- What you do first: give the server a fixed public IP address (an Elastic IP on AWS) and open ports 80 and 443.
  The certificate lasts about six days, and Caddy renews it by itself.
- This corrects the earlier note that a bare IP address can't work. A domain is still recommended for anything
  long-term. `zerostore doctor`'s fixes now mention `--ip` next to `--domain`.

### A step-by-step setup guide

- New: [docs/setup-guide.md](docs/setup-guide.md) walks someone with no technical background through every click and
  command. It covers creating the AWS server, buying a domain, adding the DNS record at Cloudflare, Route 53,
  Namecheap, GoDaddy or Squarespace, installing, connecting Google Drive, GitHub and Linear, adding an AI key, and
  adding ZeroStore to Claude, ChatGPT and Grok. It ends with everyday tasks and a table of common problems.
- For Google Drive, the guide offers a service account key next to your own sign-in app: a key file, like a personal
  access token, that reads only the folders you share with it, with no consent screen or sign-in to renew. ZeroStore
  doesn't ship a shared Google sign-in app; each install uses its own Google Cloud project.
- Google's sign-in app should now be **published** (Google Auth platform, Audience, Publish app). Left in testing,
  Google ends its sign-ins after seven days and Drive stops syncing. The setup page and README say so.
- Claude adds custom connectors under **Customize, Connectors** now. The setup page, README and
  docs/connect-agents.md use the new path.

### A public HTTPS address the standard way

- `install.sh --domain zs.example.com` sets up a server for agents on the internet. It checks the DNS
  record, installs Caddy from its official packages, adds the domain to Caddy, and starts ZeroStore
  behind it at `https://zs.example.com`. Caddy gets a free Let's Encrypt certificate and renews it, so
  there's nothing for anyone to trust or install. The installer finishes by running `zerostore doctor`.
- What you do first: add a DNS A record for the domain pointing at the server (with an Elastic IP on AWS)
  and open ports 80 and 443. A bare IP address isn't enough, because public certificates are issued for
  names.
- `zerostore doctor` now says when a domain has no DNS record. For plain HTTP or a self-signed
  certificate, its fix points at `--domain`.
- The guide (docs/connect-agents.md) leads with these standard steps for AWS and other servers. It covers
  an existing load balancer (such as an AWS ALB with ACM) and tunnels for a computer with no domain.

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
