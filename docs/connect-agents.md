# Connecting agents to ZeroStore

Every agent reaches ZeroStore through its MCP server. Pick the way that fits where the agent runs. No way below asks you to install or trust a certificate.

| Agent | Runs where | How it connects | What you paste |
|---|---|---|---|
| Claude Code | your computer | starts ZeroStore itself (stdio) | one `claude mcp add` line |
| Claude Desktop, Cursor and most other apps | your computer | starts ZeroStore itself (stdio) | an `mcpServers` block |
| Claude (web, desktop, phone) | Anthropic's servers | connector sign-in | `https://<your address>/mcp` |
| ChatGPT | OpenAI's servers | connector sign-in | `https://<your address>/mcp` |
| Grok (grok.com, phone) | xAI's servers | connector sign-in | `https://<your address>/mcp` |
| xAI API, scripts, other computers | anywhere | a token in the request | address and token |

The setup page's "Connect to Claude" step shows the exact lines for your install.

## On your own computer

Create a token on the setup page (or run `zerostore token create --name claude-code`), then:

- **Claude Code:** `claude mcp add zerostore -e ZEROSTORE_TOKEN=<token> -- /full/path/to/zerostore serve`
- **Claude Desktop** (Settings, Developer, Edit Config), **Cursor** and others: paste the `mcpServers` block the setup page shows, then restart the app.

The agent starts ZeroStore when it needs it and talks to it directly, with no network and no certificate.

## Claude, ChatGPT and Grok on the web and phone

These apps connect from their makers' servers, so ZeroStore needs a public HTTPS address first.

### 1. Give ZeroStore a public HTTPS address

This is the same setup as any website: a domain name, a DNS record, and a free certificate from Let's Encrypt. There's no certificate to install on anyone's computer, and a bare IP address won't work, because public certificates are issued for names.

**On a server (AWS EC2 or any cloud VM, Linux):**

1. Point a domain name at the server. Add an **A record** such as `zs.example.com` with the server's public IP address. That's in Route 53 on AWS, or wherever your domain's DNS is. On AWS, attach an **Elastic IP** first so the address doesn't change.
2. Open ports **80 and 443** to everyone, in the instance's security group on AWS. Let's Encrypt checks the domain on port 80.
3. Install with the domain:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --domain zs.example.com
   ```

The installer checks the DNS record and installs Caddy from its official packages (apt or dnf, which needs sudo). It adds the domain to `/etc/caddy/Caddyfile`, and starts ZeroStore at `https://zs.example.com` on `localhost` behind Caddy. Caddy gets the certificate and renews it. The installer then runs `zerostore doctor` to check the address the way agents reach it.

**Behind a load balancer or proxy you already run** (an AWS Application Load Balancer with an ACM certificate, nginx, Cloudflare): send its traffic to port 8765, then start ZeroStore with the address people use:

```bash
zerostore service install --behind-https-proxy --public-url https://zs.example.com
```

Add `--host 0.0.0.0` only when the proxy runs on another machine, such as a load balancer. Then let only the load balancer reach port 8765.

**On your own computer, with no domain:** a tunnel gives it a public HTTPS name. Use `tailscale funnel 8765` for a fixed `*.ts.net` address, or a Cloudflare Tunnel with your domain. Then run the `service install` line above with that address.

Before adding it to an app, check it the way the app will reach it:

```bash
zerostore doctor --url https://zs.example.com
```

It calls the public address with only the public certificate authorities, as ChatGPT, Grok and Claude do. It catches a self-signed certificate or private `root.crt`, a server started without `--public-url`, and connector sign-in being off. Each problem comes with the one thing to change. It also checks your sources' keys, the four services, and whether every tag has a role that can see it. It exits with 1 while anything needs fixing, so scripts can run it too.

### 2. Add it in the app

Each app sends you to a ZeroStore page that asks "Let <app> use your ZeroStore?". Sign in if asked, pick the access level and role, and click Allow. The app gets its own token, shown in Settings as "<app> (connector)". Revoke that token to disconnect it.

- **Claude:** Settings, Connectors, Add custom connector, then paste the address.
- **ChatGPT:** open chatgpt.com/plugins, choose "+", then "Add custom MCP server". Paste the address and choose OAuth sign-in.
  - ChatGPT Business, Enterprise and Edu can use every ZeroStore tool, including proposing facts. On Business, only an admin or owner can add it.
  - Pro allows read-only use.
- **Grok:** open grok.com/connectors (on the phone: Settings, Connectors), choose New Connector, then Custom, and paste the address. On Grok Business or Enterprise, an admin adds the connector in the xAI console first.

### What each app supports

- **Sign-in:** ChatGPT and Grok identify themselves with a published client document, which ZeroStore reads (it only reads such documents from public internet addresses). Apps that register themselves instead also work.
- **Tools:** ChatGPT's search and deep research use the `search` results and the `fetch` tool. The other apps use `search` and `get_fact`.
- **Using ZeroStore without being asked:**
  - Claude decides from ZeroStore's own instructions when to look things up, so you don't need a CLAUDE.md line.
  - ChatGPT has you turn the connector on for a conversation, then decides from the same instructions.
  - In Grok, a connector you've added is available in your conversations.

## Scripts and the xAI API

Anything that can send a header can use a token directly. For example, the xAI API's remote MCP tool:

```json
{
  "type": "mcp",
  "server_url": "https://zs.example.com/mcp",
  "server_label": "zerostore",
  "authorization": "<a ZeroStore token>",
  "allowed_tools": ["search", "fetch", "get_fact", "whats_changed", "list_sources"]
}
```

Claude Code on another computer works the same way: `claude mcp add --transport http zerostore https://zs.example.com/mcp --header "Authorization: Bearer <token>"`. You can also leave out the header and sign in with `/mcp`.

## Who sees what

Every connection is a token with a level and, optionally, a role. Untagged information is for every agent. Tags and roles (Settings, "who sees what") decide who sees the rest, and default tokens never see confidential information. The same rules apply however the agent connected.
