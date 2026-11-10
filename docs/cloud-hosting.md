# Host ZeroStore in the cloud for your agents

This guide is for people who run ZeroStore on a cloud server so AI agents elsewhere can reach it: Claude, ChatGPT and Grok in their apps, Claude Code or Cursor on teammates' laptops, and your own scripts and agents calling the Claude, OpenAI or xAI APIs.

- Want every click on AWS, the domain and DNS spelled out? Use [the step-by-step setup guide](setup-guide.md).
- Only want the exact lines to paste into each app? See [connecting agents](connect-agents.md).
- This page explains how the hosted setup fits together, how each kind of agent gets in, how to keep it safe, how to rehearse it on your own computer first, and what to do when an agent can't connect.

## Contents

1. [How it fits together](#how-it-fits-together)
2. [What the server needs](#what-the-server-needs)
3. [Put ZeroStore online](#put-zerostore-online)
4. [Check it from outside](#check-it-from-outside)
5. [Give each agent its own way in](#give-each-agent-its-own-way-in)
6. [Decide what each agent may see and change](#decide-what-each-agent-may-see-and-change)
7. [Keep it safe](#keep-it-safe)
8. [Keep it running](#keep-it-running)
9. [Rehearse the cloud setup on your own computer](#rehearse-the-cloud-setup-on-your-own-computer)
10. [When an agent can't connect](#when-an-agent-cant-connect)

## How it fits together

```
 Claude / ChatGPT / Grok apps ─┐
 Claude Code, Cursor (laptops) ─┼─ HTTPS :443 ─▶ Caddy (or your load balancer) ─▶ localhost:8765 ─▶ ZeroStore
 Your scripts and API agents ──┘                 holds the Let's Encrypt certificate          one program, four services
```

ZeroStore is one program (`zerostore serve --http`) on one always-on Linux server. Inside it:

| Part | Where agents and people reach it | What it does |
|---|---|---|
| MCP server | `https://<your address>/mcp` | The only way in for agents. Every request needs a ZeroStore token. |
| Connector sign-in | `/authorize`, `/token`, `/register`, `/.well-known/oauth-*` | Lets Claude, ChatGPT and Grok sign in and get their own token, after you click Allow. |
| Web console | `https://<your address>/` | Setup, settings, who sees what, the review queue and the activity log. Owner only. |
| Ingestion agent | runs on a timer inside the program | Reads Google Drive, GitHub, Linear and other sources and keeps the facts current. |
| Vector DB | files on the server's disk | The fact store (LanceDB) agents search. |

Its data lives in `~/.zerostore` on the server's disk: the fact store, a SQLite file (tokens, audit log, review queue, connector sign-ins), `zerostore.yaml`, `secrets.env`, `decisions.md` and the folder that holds agents' changes.

ZeroStore never listens on the internet directly. It listens on `localhost:8765`, and Caddy (or a load balancer you already run) takes HTTPS on port 443 and passes requests to it. ZeroStore is started with `--behind-https-proxy --public-url https://<your address>`, so it knows the name agents use and accepts requests addressed to it.

## What the server needs

- **One Linux machine that stays on, with a disk that keeps its files.** For example AWS EC2 or Lightsail, Google Compute Engine, Azure, DigitalOcean or Hetzner. A small machine (2 GB of memory, such as an AWS t3.small) is enough to start.
- **A fixed public IP address.** On AWS, attach an Elastic IP.
- **A domain name** pointing at that address, such as `zs.example.com` (an A record). Without a domain, the IP address itself can work; see `--ip` below.
- **Inbound ports 80 and 443 open to everyone.** Claude, ChatGPT and Grok connect from their makers' servers, so you can't limit these to your office. Let's Encrypt also checks your domain on port 80.
- **Outbound internet** to your sources (Google, GitHub, Linear) and your AI provider.

**What doesn't fit:** serverless and "functions" hosts such as Vercel, Netlify Functions or AWS Lambda. They start a fresh copy for each request and throw away files, so ZeroStore's tokens, facts and timed syncs would be lost. Container hosts with a persistent disk (Railway, Render, Fly.io) should work in principle, but ZeroStore doesn't ship a template for them yet and they haven't been tested.

## Put ZeroStore online

Pick one way. Each ends with ZeroStore at an `https://` address with a certificate every agent already trusts. Nobody has to install a certificate.

**A. A server with a domain (recommended).** Point the A record at the server, open ports 80 and 443, then on the server:

```bash
curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --domain zs.example.com
```

The installer checks the DNS record, installs Caddy, adds your domain to `/etc/caddy/Caddyfile`, and installs ZeroStore as a background service with `--behind-https-proxy --public-url https://zs.example.com`. Caddy gets and renews the certificate. While the repository is private, install from a clone instead (see [Part 6 of the setup guide](setup-guide.md#part-6-install-zerostore)).

**B. A server with no domain.** On a server with a fixed public IP address, use `--ip` instead of `--domain`. Agents then use `https://203.0.113.7/mcp`. These certificates last about six days and Caddy renews them, so a domain is still better for anything long-term.

**C. Behind a load balancer or proxy you already run** (an AWS Application Load Balancer with an ACM certificate, nginx, Cloudflare). Send its traffic to port 8765 and start ZeroStore with the public address:

```bash
zerostore service install --behind-https-proxy --public-url https://zs.example.com
```

Add `--host 0.0.0.0` only when the proxy runs on another machine. Then let only that proxy reach port 8765.

**D. From a computer with no public address.** A tunnel gives it one: Cloudflare Tunnel with your domain, or `tailscale funnel 8765` for a `*.ts.net` name. Then run the `service install` line above with that address. Use Tailscale Funnel, not plain Tailscale: Claude, ChatGPT and Grok connect from the internet and can't reach a private tailnet.

ZeroStore refuses plain HTTP on a network address. If it says `refusing to serve plain HTTP`, it's missing `--behind-https-proxy` or a certificate.

## Check it from outside

On the server, run:

```bash
zerostore doctor --url https://zs.example.com
```

It reaches your address the way the apps do, trusting only public certificate authorities. Every line should show ✓; each ✗ comes with the one thing to change. It checks that `/mcp` answers and asks for a token, that the certificate is publicly trusted, that the sign-in names the right address, that the four services are running, and that every tag has a role that can see it.

From any other computer you can check the door is locked:

```bash
curl -i -X POST https://zs.example.com/mcp
```

You should get `401 Unauthorized` and a `www-authenticate: Bearer ... resource_metadata="https://zs.example.com/.well-known/oauth-protected-resource/mcp"` line. That line is how Claude, ChatGPT and Grok find the sign-in.

## Give each agent its own way in

Every agent connects to the same address, `https://zs.example.com/mcp`, and every agent ends up with its own ZeroStore token. There are two ways to get one.

| Agent | How it gets in |
|---|---|
| Claude (claude.ai, desktop and phone apps), ChatGPT, Grok | **Sign-in.** Paste the address; the app sends you to ZeroStore, you pick the access level and role and click Allow. |
| Claude Code on a laptop | A token in a header, or sign-in with `/mcp` |
| Cursor, Claude Desktop config files, other MCP apps | A token in a header |
| Your own agents on the Claude, OpenAI or xAI APIs | A token in a header |
| Scripts using an MCP library | A token in a header |

### Sign-in (Claude, ChatGPT, Grok)

The click paths for each app are in [connecting agents](connect-agents.md#2-add-it-in-the-app). Each app shows up in the web console's token list as "<app> (connector)". Revoke that token to disconnect the app. You can't give these apps the owner level; pick `default` or `confidential` and, if you use roles, a role.

### A token in a header

Make one token per agent, so you can see each agent in the log and revoke it alone. In the web console use **settings**, **tokens**, or on the server:

```bash
zerostore token create --name sales-bot --role sales
zerostore token create --name finance-assistant --level confidential
```

The token (`zs_...`) is shown once. Then:

**Claude Code** on someone's laptop:

```bash
claude mcp add --transport http zerostore https://zs.example.com/mcp --header "Authorization: Bearer zs_..."
```

**Cursor and other apps with an `mcpServers` file:**

```json
{
  "mcpServers": {
    "zerostore": {
      "type": "http",
      "url": "https://zs.example.com/mcp",
      "headers": { "Authorization": "Bearer zs_..." }
    }
  }
}
```

**An agent on the Claude API** (MCP connector, Python):

```python
import anthropic

client = anthropic.Anthropic()
response = client.beta.messages.create(
    model="claude-opus-5-5",
    max_tokens=16000,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{
        "type": "url",
        "url": "https://zs.example.com/mcp",
        "name": "zerostore",
        "authorization_token": "zs_...",
    }],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "zerostore"}],
    messages=[{"role": "user", "content": "What's our refund policy?"}],
)
```

**An agent on the OpenAI Responses API:**

```json
{
  "type": "mcp",
  "server_label": "zerostore",
  "server_url": "https://zs.example.com/mcp",
  "headers": { "Authorization": "Bearer zs_..." },
  "require_approval": "never"
}
```

**An agent on the xAI API:**

```json
{
  "type": "mcp",
  "server_url": "https://zs.example.com/mcp",
  "server_label": "zerostore",
  "authorization": "zs_...",
  "allowed_tools": ["search", "fetch", "get_fact", "whats_changed", "list_sources"]
}
```

**A script with the MCP Python SDK** (version 2):

```python
import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer zs_..."})
    async with Client(streamable_http_client("https://zs.example.com/mcp", http_client=http)) as zs:
        result = await zs.call_tool("search", {"query": "refund policy"})
        print(result.structured_content)

anyio.run(main)
```

The API providers call ZeroStore from their own servers, so the address must be public (ways A to D above), the same as for the apps.

### What agents can do once they're in

Each token sees only the tools its level and role allow. `tools/list` hides the rest.

| Tools | Who gets them |
|---|---|
| `search`, `get_fact`, `fetch`, `whats_changed`, `list_sources`, `propose_fact` | Every token |
| `add_fact`, `change_fact`, `remove_fact` | Tokens whose role may change information (below), and owner tokens, when agent changes are turned on |
| `status`, `activity`, `sync_now`, `review_list`, `review_decide`, `token_*` | Owner tokens only |

Agents don't need to be told to use ZeroStore: its tool descriptions and server instructions tell them when to look things up. For agents that need a nudge, `zerostore connect` adds a one-line rule to CLAUDE.md, AGENTS.md or .cursorrules.

## Decide what each agent may see and change

The same rules apply however an agent connected.

- **Level.** `default` tokens never see confidential information. `confidential` tokens see everything their role allows. `owner` tokens see everything and can change setup. Never give an owner token to an agent.
- **Tags and roles.** Information is tagged by where it lives (a Drive folder, a GitHub repo, a Linear label). A role lists the tags it may see. Untagged information is for every agent. Set these on the web console's settings page ("who sees what") or in `zerostore.yaml`.
- **Changing information (optional).** With `agent_writes` turned on, an agent may add, fix and remove only the information its role is given:

  ```yaml
  agent_writes:
    enabled: true
    review: false     # true: every change waits for you on the review page
    roles:
      sales: [sales]  # agents with the sales role may change sales information
  ```

  A `sales` agent asked to change untagged or finance information is refused, and the refusal shows in the log. Agents never edit your Google Docs, GitHub files or Linear issues; their changes are saved as ZeroStore's own files on the server, so the next sync keeps them. See [Part 12 of the setup guide](setup-guide.md#part-12-decide-who-sees-what).

## Keep it safe

- Open only ports 80 and 443 to everyone. Keep port 8765 closed to the internet, and limit SSH (port 22) to your own address if you can.
- Don't start ZeroStore with `--host 0.0.0.0` unless a load balancer on another machine needs it, and then let only that load balancer reach port 8765.
- One token per agent or person. Revoke a token the moment a laptop is lost or a person leaves (`zerostore token revoke <id>` or the token list). Revoking takes effect on the agent's next call.
- Keep the owner token for yourself. It is in `~/.zerostore/owner-token` on the server, readable only by you.
- Use `default` tokens unless an agent truly needs confidential information, and a role for every agent once you use tags.
- Use `zerostore ui` to get a one-time link into the web console instead of pasting the owner token into browsers.
- Leave `agent_writes` off until you need it, and start with `review: true` if you want to see each change before agents are told.
- Keep your AI and source keys in `secrets.env` (the setup page saves them there), never in `zerostore.yaml` or in a repository.

## Keep it running

| To | Run on the server |
|---|---|
| Check everything | `zerostore doctor` |
| See facts, sources and the four services | `zerostore status` |
| Watch what it's doing (Ctrl+C to stop) | `journalctl --user -u zerostore.service -f` (`journalctl -u zerostore.service -f` if installed as root) |
| Restart it | `systemctl --user restart zerostore.service` |
| Read sources now | `zerostore sync` |
| Update it | run the same install command again; settings and data stay |

**Back up `~/.zerostore`.** It holds everything: tokens, facts, settings, keys and agents' changes. The simplest backup is a disk snapshot (an EBS snapshot on AWS) on a schedule. If you lose the fact store but keep `zerostore.yaml`, `secrets.env` and the `written` folder, a `zerostore sync` reads your sources again; tokens live in `state.sqlite`, so keep that too.

ZeroStore runs as one program on one server. Running several copies behind a load balancer isn't supported yet: each copy would have its own data, and connector sign-ins are kept in the program's memory.

## Rehearse the cloud setup on your own computer

You can run the same layout on your own computer before you rent a server: Caddy on HTTPS in front, ZeroStore on `localhost:8765` behind it, started with `--behind-https-proxy --public-url`. Agents on your computer then connect exactly as they would to the cloud, through the same address checks, tokens, roles and sign-in. The only difference is the certificate: Caddy makes a private one, which only your computer trusts.

1. Install Caddy (`brew install caddy` on a Mac, or your Linux package manager) and ZeroStore (`sh install.sh` with no `--domain`).
2. Make sure `zs.localhost` points at your own computer. Browsers and curl already do this; for other programs add a line to `/etc/hosts`:

   ```bash
   echo "127.0.0.1 zs.localhost" | sudo tee -a /etc/hosts
   ```

3. Save this as `Caddyfile` in an empty folder:

   ```
   zs.localhost {
       tls internal
       reverse_proxy localhost:8765
   }
   ```

4. In that folder, start Caddy and let it add its private certificate authority to your computer (asks for your password):

   ```bash
   sudo caddy start
   sudo caddy trust
   ```

5. Start ZeroStore the way the cloud install does (stop the background service first with `zerostore service uninstall` if you installed one):

   ```bash
   zerostore serve --http --behind-https-proxy --public-url https://zs.localhost
   ```

6. Open `https://zs.localhost`, run `zerostore ui` for a sign-in link, and add a token. Connect Claude Code with `claude mcp add --transport http zerostore https://zs.localhost/mcp --header "Authorization: Bearer zs_..."`. Node-based tools such as Claude Code may also need `NODE_EXTRA_CA_CERTS` set to Caddy's root certificate: `~/.local/share/caddy/pki/authorities/local/root.crt` on Linux, `~/Library/Application Support/Caddy/pki/authorities/local/root.crt` on a Mac (when Caddy runs with sudo, look under root's home folder instead).

Things to try: a token with no role can't see tagged information; a role's agent can change its own tag but is refused elsewhere; a revoked token gets 401 at once; `curl -i -X POST https://zs.localhost/mcp` shows the sign-in hint.

`zerostore doctor --url https://zs.localhost` will report one ✗, `the certificate isn't from a public authority`. That's expected in a rehearsal and is exactly what it would catch on a real server. Claude, ChatGPT and Grok in their apps can't reach `zs.localhost`, because they connect from the internet. To try them, use a tunnel (way D above).

When you're done: `sudo caddy stop`, and `sudo caddy untrust` to remove the private certificate authority.

## When an agent can't connect

Start with `zerostore doctor --url https://<your address>` on the server.

| What you see | What it means | What to do |
|---|---|---|
| `401 Unauthorized` | No token, a wrong token, or a revoked one | Check the header is exactly `Authorization: Bearer zs_...`, or make a new token. |
| `421 Misdirected Request` / `Invalid Host header` | ZeroStore doesn't know the address the agent used | Start it with `--public-url https://<the exact address agents use>`, or use that exact address. |
| Certificate errors (`CERTIFICATE_VERIFY_FAILED`, "self-signed") | The certificate isn't from a public authority | Use way A or B so Caddy gets a Let's Encrypt certificate. Never ask agents to trust a private certificate. |
| The app can't reach the address at all | Ports 80 and 443 are closed, the DNS record is wrong, or the address is private (plain Tailscale, a home network) | Open the ports, check the A record, or use a public tunnel. |
| The app's sign-in says ZeroStore doesn't support sign-in | ZeroStore is on plain HTTP or was started without `--public-url` | Use an `https://` address and start with `--public-url`. |
| The "Let <app> use your ZeroStore?" page asks you to sign in | You're not signed in to the web console in that browser | Sign in there with the owner token, or open a `zerostore ui` link in that browser first. The Allow page comes back after you sign in. |
| The agent connects but finds nothing | Nothing synced yet, or its role or level hides the information | Check `zerostore status`, then the token's level and role, and the log page. |
| `add_fact` or `change_fact` is refused | Agent changes are off, the token has no role, or the role may not change that tag | Check `agent_writes` in `zerostore.yaml`; the refusal message names the reason. |
| The agent doesn't see `add_fact` at all | Its token may not change anything | Give it a role listed under `agent_writes.roles`. |

More detail on each app: [connecting agents](connect-agents.md). Every setup step, click by click: [the setup guide](setup-guide.md).
