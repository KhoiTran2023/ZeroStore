# Set up ZeroStore, step by step

This guide takes you from nothing to Claude, ChatGPT and Grok answering questions from your company's documents. You don't need to know how to program. Every click and every command is written out, and you only copy and paste.

You'll end up with:

- a small server on Amazon Web Services (AWS) that runs ZeroStore all the time,
- a web address such as `https://zs.yourcompany.com` with a free, trusted certificate,
- your Google Drive folders, GitHub repos or Linear teams connected,
- ZeroStore added to Claude, ChatGPT and Grok, with you deciding who sees what.

It takes about an hour, most of it waiting for things to start.

> **Only want it on your own Mac or Linux computer**, for Claude Code or Claude Desktop on that computer? You don't need a server or a domain. Open Terminal (on a Mac, press **Cmd+Space**, type `Terminal` and press **Enter**), paste this line and press **Enter**:
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh
> ```
>
> If a Mac asks to install the "command line developer tools", click **Install**, wait for it to finish, and paste the line again. ZeroStore makes a folder called **ZeroStore** in your Documents folder and reads every Markdown (`.md`) and text (`.txt`) file you put in it, checking for changes every five minutes. To use a folder you already have, add ` -s -- --folder ~/Notes` after `sh` (with your folder's path), or add it later on the setup page under **Folders on this computer**. When the installer is done, the setup page opens in your browser by itself, already past the documents step: add your AI key ([Part 9](#part-9-add-your-ai-key)), then connect your agents. While the repository is private, use the three commands in the box in [Part 6](#part-6-install-zerostore), leaving out ` --domain zs.example.com`. Claude, ChatGPT and Grok on the web and phone need the server.

## Contents

- [Before you start](#before-you-start)
- [Using a terminal in two minutes](#using-a-terminal-in-two-minutes)
- [Part 1. Choose your web address](#part-1-choose-your-web-address)
- [Part 2. Create the server on AWS](#part-2-create-the-server-on-aws)
- [Part 3. Give the server a fixed IP address](#part-3-give-the-server-a-fixed-ip-address)
- [Part 4. Point your web address at the server](#part-4-point-your-web-address-at-the-server)
- [Part 5. Open a terminal on the server](#part-5-open-a-terminal-on-the-server)
- [Part 6. Install ZeroStore](#part-6-install-zerostore)
- [Part 7. Sign in to the web console](#part-7-sign-in-to-the-web-console)
- [Part 8. Connect your documents](#part-8-connect-your-documents)
- [Part 9. Add your AI key](#part-9-add-your-ai-key)
- [Part 10. Run the first sync](#part-10-run-the-first-sync)
- [Part 11. Add ZeroStore to Claude, ChatGPT and Grok](#part-11-add-zerostore-to-claude-chatgpt-and-grok)
- [Part 12. Decide who sees what](#part-12-decide-who-sees-what)
- [Everyday tasks](#everyday-tasks)
- [When something goes wrong](#when-something-goes-wrong)

## Before you start

**What you need**

- A computer with a web browser (Chrome, Edge, Safari or Firefox).
- An AWS account. Sign up at [aws.amazon.com](https://aws.amazon.com) with "Create an AWS Account". It asks for a credit card.
- A domain name you control, such as `yourcompany.com`, or the willingness to buy one in Part 1 (about $10 to $16 a year).
- The places your knowledge lives: a Google account with the Drive folders, a GitHub account with the repos, or a Linear workspace.
- An AI account for the librarian: Anthropic (Claude) or OpenAI. You can add this later.
- A Claude, ChatGPT or Grok account for the people who will ask questions. See [Part 11](#part-11-add-zerostore-to-claude-chatgpt-and-grok) for which plans can add ZeroStore.

**What it costs each month (roughly)**

| Item | Cost |
|---|---|
| AWS server (t3.small, 2 GB of memory) | about $15 a month; a new account's free-tier credits cover it for up to 6 months |
| Fixed IP address on AWS (Elastic IP) | about $3.60 a month ($0.005 an hour) |
| Disk (20 GB) | about $1.60 a month |
| Domain name | $10 to $16 a year |
| Certificate (Let's Encrypt) | free |
| Your AI provider | pay as you go; small for a few hundred documents |

**How to read this guide**

- **Bold** words are buttons, menus and fields, written exactly as they appear on screen.
- Grey boxes are things to copy and paste. Every grey box has a copy button in its top right corner on GitHub.
- `zs.example.com` stands for your own web address. Replace it with yours everywhere.
- Screens change from time to time. If a button has a slightly different name, pick the closest one.

## Using a terminal in two minutes

A terminal is a window where you type commands instead of clicking. In this guide you never type a command yourself: you copy it from here and paste it in.

- **Paste:** in the AWS browser terminal, right-click and choose **Paste**, or press **Ctrl+Shift+V** (on a Mac, **Cmd+V**). If the browser asks to let the page see your clipboard, click **Allow**.
- **Run:** press **Enter** after pasting.
- **Wait for the prompt:** a command is finished when the line ending in `$` comes back, such as `ubuntu@ip-172-31-5-10:~$`. Don't paste the next command until then.
- **Secrets don't show:** when you paste a password or token, nothing appears on screen. That's normal. Press **Enter**.
- **Stop something:** press **Ctrl+C**.
- **Copy from the terminal:** select the text with your mouse, then right-click and choose **Copy**, or press **Ctrl+Shift+C** (on a Mac, **Cmd+C**).
- **Lost?** Close the terminal tab and open it again (Part 5). Nothing is lost: ZeroStore keeps running.

## Part 1. Choose your web address

ZeroStore needs a secure `https://` web address, because Claude, ChatGPT and Grok only connect to those. A domain name is the standard way, and it's what this guide uses.

> **No domain?** You can use the server's fixed IP address itself, such as `https://203.0.113.7`. Do Parts 2 and 3, skip Part 4, and in Part 6 replace `--domain zs.example.com` with `--ip`. Everywhere else, use `https://` and your Elastic IP in place of `https://zs.example.com`. The free certificate for an IP address lasts about six days and renews by itself. A domain is easier to remember and survives a move to a new server, so it's the better long-term choice.

Use a **subdomain** of a domain you already have, such as `zs.yourcompany.com`. It doesn't affect your website or email.

### Find out where your domain's DNS lives

DNS is the internet's address book. You'll add one entry to it in Part 4, at the company that manages your domain's DNS. To find out which company that is:

1. Go to [lookup.icann.org](https://lookup.icann.org), type your domain (for example `yourcompany.com`) and click **Lookup**.
2. Look at **Name Servers**:

   | Name servers contain | Your DNS is at |
   |---|---|
   | `cloudflare.com` | Cloudflare |
   | `awsdns` | Amazon Route 53 |
   | `domaincontrol.com` | GoDaddy |
   | `registrar-servers.com` | Namecheap |
   | `squarespacedns.com` or `googledomains.com` | Squarespace Domains |

   Anything else: it's the company named there. Look for "DNS records" or "Manage DNS" in its dashboard.

### No domain yet? Buy one

Any of these works. Pick one and stay with it for Part 4.

**Cloudflare** (sells at cost, about $10.50 a year for a `.com`):

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com) (create a free account and verify your email first).
2. In the left menu, open **Domain Registration**, then **Register domains**.
3. Type the name you want and click **Search**. Next to an available name, click **Purchase**.
4. Choose how many years under **Payment option**, fill in your contact details and payment, and click **Complete purchase**.

**Namecheap** (about $11 the first year for a `.com`, then about $18.50):

1. Go to [namecheap.com](https://www.namecheap.com), type the name in the search box and press Enter.
2. Click **Add to cart** next to an available name, then **Checkout**, create an account and pay.

**Amazon Route 53** (about $16 a year for a `.com`, plus $0.50 a month for its DNS):

1. In the AWS console, type `Route 53` in the search bar at the top and open it.
2. In the left menu, open **Domains**, then **Registered domains**, then click **Register domains**.
3. Type the name, click **Search**, click **Select** next to an available name, then **Proceed to checkout**.
4. Choose the years and **Auto-renew**, click **Next**, fill in **Contact information**, click **Next**, tick the terms and click **Submit**.
5. Open the email AWS sends you and confirm your address, or the domain is suspended after 15 days.

## Part 2. Create the server on AWS

1. Sign in at [console.aws.amazon.com](https://console.aws.amazon.com).
2. In the top right corner, next to your account name, pick the **Region** closest to you, such as **US East (N. Virginia)** or **Europe (Frankfurt)**. Everything you create lives in this Region, so keep it the same throughout.
3. Type `EC2` in the search bar at the top and click **EC2**.
4. Click the orange **Launch instance** button.
5. Fill in the page from top to bottom:
   - **Name and tags**, **Name:** `zerostore`.
   - **Application and OS Images (Amazon Machine Image):** under **Quick Start**, click **Ubuntu**. In the **Amazon Machine Image (AMI)** list, pick the newest **Ubuntu Server LTS** (24.04 LTS or 26.04 LTS), the one marked **Free tier eligible**. Leave **Architecture** on **64-bit (x86)**.
   - **Instance type:** pick **t3.small** (2 GB of memory). The 1 GB ones can run short of memory.
   - **Key pair (login):** in **Key pair name**, choose **Proceed without a key pair (not recommended)**. You'll open the server's terminal from the browser instead, so you don't need a key file.
   - **Network settings:** leave **Create security group** selected, then:
     - tick **Allow SSH traffic from** and set it to **Anywhere (0.0.0.0/0)**. The browser terminal in Part 5 connects this way, and it can't with **My IP**.
     - tick **Allow HTTPS traffic from the internet**.
     - tick **Allow HTTP traffic from the internet**. The free certificate is checked over HTTP, then visitors are moved to HTTPS.
   - **Configure storage:** change the number to **20** GiB, with **gp3**.
6. Check the **Summary** panel on the right, then click **Launch instance**.
7. Click the instance ID in the green box (it starts with `i-`), or open **Instances** in the left menu. Wait until **Instance state** says **Running** and **Status check** says **3/3 checks passed**. Click the refresh button (the circular arrow) now and then. It takes two or three minutes.

> The SSH rule set to **Anywhere** is safe enough here: with no key pair, nobody can log in with a password, and the browser terminal hands AWS a fresh key for each session. To narrow it later, see [the AWS guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-connect-prerequisites.html) for the EC2 Instance Connect address list.

## Part 3. Give the server a fixed IP address

A server's public IP address changes every time it's stopped and started. An **Elastic IP** keeps it the same, so your web address keeps working.

1. In the EC2 left menu, under **Network & Security**, click **Elastic IPs**.
2. Click **Allocate Elastic IP address**. Leave **Amazon's pool of IPv4 addresses** selected and click **Allocate**.
3. With the new address selected (tick its box), click **Actions**, then **Associate Elastic IP address**.
4. **Resource type:** **Instance**. In **Instance**, pick `zerostore`. Click **Associate**.
5. Write down the **Allocated IPv4 address**, for example `203.0.113.7`. This is your server's IP address for Part 4.

## Part 4. Point your web address at the server

You'll add one **A record**: an address-book entry that says "`zs.yourcompany.com` is at `203.0.113.7`". Use the section for the company you found in Part 1.

In every one of them:

- **Type:** `A`
- **Name** (or **Host**): just the first part, `zs`. Most screens add your domain to it for you.
- **Value** (or **IPv4 address**, **Points to**, **Data**): the Elastic IP from Part 3.
- **TTL:** leave the default, or pick 5 minutes if offered.

### Cloudflare

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com) and click your domain.
2. In the left menu, click **DNS**, then **Records**.
3. Click **Add record**.
4. **Type:** `A`. **Name:** `zs`. **IPv4 address:** the Elastic IP.
5. **Proxy status:** click the orange cloud so it turns **grey** and reads **DNS only**. This matters: with the orange cloud on, the free certificate can't be issued.
6. Click **Save**.

### Amazon Route 53

1. In the AWS console, type `Route 53` in the search bar and open it.
2. In the left menu, click **Hosted zones**, then click your domain's name.
3. Click **Create record**. (If you see a wizard asking for a routing policy, click **Switch to quick create**.)
4. **Record name:** `zs`. **Record type:** **A**. **Value:** the Elastic IP. Leave **TTL (seconds)** at 300.
5. Click **Create records**.

### Namecheap

1. Sign in at [namecheap.com](https://www.namecheap.com) and open **Domain List** in the left menu.
2. Click **Manage** next to your domain, then the **Advanced DNS** tab.
3. Under **Host Records**, click **Add New Record** and choose **A Record**.
4. **Host:** `zs`. **Value:** the Elastic IP. **TTL:** **Automatic**.
5. Click the green check mark at the end of the row to save.

### GoDaddy

1. Sign in at [godaddy.com](https://www.godaddy.com) and open your **Domain Portfolio**.
2. Click your domain, then the **DNS** tab.
3. Click **Add New Record**.
4. **Type:** **A**. **Name:** `zs`. **Value:** the Elastic IP. Leave **TTL** at the default.
5. Click **Save**. If GoDaddy asks you to verify it's you, follow its steps.

### Squarespace Domains (including former Google Domains)

1. Sign in at [account.squarespace.com/domains](https://account.squarespace.com/domains) and click your domain.
2. Click **DNS** in the side panel. Enter your password again if asked.
3. Under **Custom records**, click **Add record**.
4. **Type:** **A**. **Host** (or **Name**): `zs`. **Data:** the Elastic IP. Leave **TTL** at the default.
5. Click **Save**.

### Check that it worked

Go to [dnschecker.org](https://dnschecker.org), type `zs.example.com` (your address), leave **A** selected and click **Search**. When most places on the map show your Elastic IP with a green tick, you're ready. That usually takes a few minutes, and rarely up to an hour.

## Part 5. Open a terminal on the server

1. In the EC2 console, click **Instances** in the left menu and tick the box next to `zerostore`.
2. Click **Connect** at the top.
3. Open the **EC2 Instance Connect** tab. Leave **Connect using a Public IP** selected and **Username** as `ubuntu`.
4. Click **Connect**. A black terminal opens in a new browser tab, ending with a line such as `ubuntu@ip-172-31-5-10:~$`.

If it says it failed to connect, see [When something goes wrong](#when-something-goes-wrong).

## Part 6. Install ZeroStore

Paste this into the server's terminal, with **your** web address in place of `zs.example.com`, and press **Enter**:

```bash
curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --domain zs.example.com
```

> **While the ZeroStore repository is private**, that link answers "404: Not Found". Use these three commands instead, one at a time. You need a GitHub token that can read the ZeroStore repository: make one as in [GitHub](#github) below, choosing **KhoiTran2023/ZeroStore** as the repository. When `git clone` asks for **Username**, type your GitHub username. When it asks for **Password**, paste the token (it won't show) and press **Enter**.
>
> ```bash
> git clone https://github.com/KhoiTran2023/ZeroStore.git
> ```
> ```bash
> cd ZeroStore
> ```
> ```bash
> ZEROSTORE_PACKAGE=. sh install.sh --domain zs.example.com
> ```

The installer works for a few minutes. Here's what it does and what you'll see:

1. **Installing the zerostore command**: downloads ZeroStore and the Python it needs. Lots of "Downloaded" lines are normal.
2. **Creating your settings and owner token**: your settings go in a folder called `.zerostore` in your home folder on the server. The **owner token** is the master key to your ZeroStore. Don't share it.
3. **Setting up HTTPS for zs.example.com**:
   - it checks the address from Part 4 and says `zs.example.com points at this server (203.0.113.7)`.
   - it installs **Caddy**, a well-known web server that gets the free Let's Encrypt certificate and renews it by itself.
4. **Starting ZeroStore in the background**: ZeroStore now runs all the time and starts again by itself if the server restarts.
5. **Opening the setup page**: prints a line like this. You'll need it in Part 7.

   ```
   Sign in to the web UI: https://zs.example.com/login?code=AbC123...
   It works once, for ten minutes, while `zerostore serve --http` is running.
   ```

6. **Checking https://zs.example.com the way agents will reach it**: `zerostore doctor` checks everything. Lines start with ✓ (good), ! (worth knowing) or ✗ (must fix), and a → line under one says what to do. The last line says **All set** or how many problems are left. Right now it's normal to see `! no sources yet`: you'll add them in Part 8. If you see ✗ about the certificate, Caddy may still be getting it: wait a minute and paste `zerostore doctor` to check again.
7. **ZeroStore is installed**.

If the installer stops with "ZeroStore install failed", the message says why. The most common ones are in [When something goes wrong](#when-something-goes-wrong). Fix that and paste the same command again: running it twice is safe.

## Part 7. Sign in to the web console

The web console is ZeroStore's control panel, at your web address.

1. In the terminal, select the whole `https://zs.example.com/login?code=...` link from Part 6 and copy it.
2. Paste it into your browser's address bar on your own computer and press **Enter**.
3. Click **Sign in**.

The link works once, for ten minutes. If it has expired, paste this in the server's terminal for a new one:

```bash
zerostore ui
```

> If the terminal says `zerostore: command not found`, close the terminal tab and connect again (Part 5). New terminals know where ZeroStore is.

You land on the setup page, which says **Connect three things, then run the first sync.** It has three boxes: **1 · your docs**, **2 · librarian** and **3 · your agents**. Each says **○ not set** until it's done, then **✓ ok**. You can always come back to it at `https://zs.example.com/setup`.

The other pages are in the top bar: **status**, **review**, **log** and **settings**. On the setup page the top bar shows only **status**: click it, and the rest appear.

## Part 8. Connect your documents

Click **1 · your docs**. Connect any of Google Drive, GitHub and Linear. One is enough to start.

### Google Drive

There are two ways to let ZeroStore read Drive. Both are read-only, and both use a Google Cloud project you create yourself (ZeroStore doesn't share one between installs).

- **Your own sign-in app** (steps A to F below): ZeroStore reads what your Google account can see, and you pick folders by browsing.
- **A service account key** (the [next section](#google-drive-with-a-service-account-key)): a key file for a robot Google account, like a personal access token. ZeroStore reads only the folders you share with it, so there's no consent screen, no warning page, and no sign-in to renew. Some company Google accounts don't allow these keys.

Google asks every program that signs in to Drive to have its own "sign-in app". You make one once, in about ten minutes, then sign in with it.

**A. Create a Google Cloud project**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with the Google account that can see the Drive folders. Accept the terms if asked.
2. Click the project picker at the top of the page (it may say **Select a project**), then **New project**.
3. **Project name:** `ZeroStore`. Click **Create**. Wait for the notification, then click **Select project** in it, so the picker at the top shows ZeroStore.

**B. Turn on the Google Drive API**

1. Open [the Google Drive API page](https://console.cloud.google.com/apis/library/drive.googleapis.com) (or menu ☰, **APIs & Services**, **Library**, search `Google Drive API`).
2. Check that the project picker at the top shows **ZeroStore**, then click **Enable**.

**C. Describe your sign-in app**

1. Open menu ☰, **Google Auth platform**, **Branding** (or search `Google Auth platform` in the search bar). Click **Get started**.
2. **App Information:** **App name** `ZeroStore`, **User support email** your email. Click **Next**.
3. **Audience:** choose **External**. Click **Next**. (On a Google Workspace account where only your company will sign in, **Internal** also works and skips steps D and the warning in step F.)
4. **Contact Information:** your email. Click **Next**.
5. **Finish:** tick **I agree to the Google API Services: User Data Policy**. Click **Continue**, then **Create**.

**D. Let the app keep working past seven days**

While a sign-in app is "in testing", Google ends its sign-ins after seven days, and ZeroStore would stop reading Drive. Publishing it fixes that. Your app stays private: only people you send to it can use it, and Google shows them a warning first.

1. In the left menu of **Google Auth platform**, click **Audience**.
2. Under **Publishing status**, click **Publish app**, then **Confirm**.

**E. Create the sign-in app's keys**

1. In the left menu of **Google Auth platform**, click **Clients**, then **Create client**.
2. **Application type:** **Desktop app**. **Name:** `ZeroStore`. Click **Create**.
3. A box shows the **Client ID** and **Client secret**. Copy both now, into a note: Google won't show the secret again. (Or click **Download JSON** to keep a copy.)

**F. Sign in from ZeroStore**

1. Back in ZeroStore, on **1 · your docs**, find **Signing in with Google needs a Google sign-in app first** (click it if its boxes are hidden).
2. Paste the **Client ID** and **Client secret** into their boxes and click **Save**. ZeroStore says **Saved your Google sign-in app. Now sign in with Google.**
3. Click **Sign in with Google Drive**. Google opens in a new tab.
4. Choose your Google account.
5. Google says **Google hasn't verified this app**. That's your own app from step C. Click **Advanced**, then **Go to ZeroStore (unsafe)**.
6. Google lists what ZeroStore may do: see and download your Google Drive files (read-only). Click **Continue**.
7. The tab ends on an address starting with `http://localhost/zerostore-google-signin` that says the site can't be reached. **That's expected.** Click the address bar, select the whole address and copy it.
8. Go back to the ZeroStore tab, paste the address into **Address** under step 2, and click **Finish signing in**.
9. ZeroStore now says **Google Drive · signed in as** your email. Browse **My Drive**, **Shared with me** or **Shared drives**. Click a folder name to look inside it, and click **Add** next to each folder ZeroStore should read. It reads every folder inside the ones you add.

ZeroStore only reads. It never changes, moves or deletes your files.

### Google Drive with a service account key

Do A and B from the section above (create the project and turn on the Google Drive API), then:

**Create the robot account and its key**

1. In the Google Cloud console, open menu ☰, **IAM & Admin**, **Service Accounts**. Check that the project picker at the top shows **ZeroStore**.
2. Click **+ Create service account**. **Service account name:** `zerostore-reader`. Click **Create and continue**.
3. Skip **Permissions** and **Principals with access**: click **Continue**, then **Done**. The robot needs no project roles, only access to your folders.
4. In the list, copy the new account's **Email**, which looks like `zerostore-reader@your-project.iam.gserviceaccount.com`. You'll share folders with it.
5. Click the email, open the **Keys** tab, click **Add key**, then **Create new key**. Choose **JSON** and click **Create**. A file ending in `.json` downloads to your computer. Keep it safe: anyone with it can read the folders you share.

If Google says **Service account key creation is disabled**, your organization blocks these keys (the `iam.disableServiceAccountKeyCreation` policy). Use your own sign-in app instead, or ask your Google Workspace admin.

**Share your folders with it**

1. In [Google Drive](https://drive.google.com), right-click a folder ZeroStore should read and choose **Share**, then **Share** again.
2. Paste the robot's email, set it to **Viewer**, untick **Notify people**, and click **Share** (or **Send**).
3. Do the same for each folder. For a shared drive, add the email as a member with the **Viewer** role under **Manage members**.

**Put the key on the server**

1. On your computer, open the downloaded `.json` file in a plain text editor (on Windows, right-click it, **Open with**, **Notepad**; on a Mac, right-click it, **Open With**, **TextEdit**). Select everything (**Ctrl+A**, or **Cmd+A** on a Mac) and copy it.
2. In the server's terminal, paste this and press **Enter**. The terminal then waits for the key, with no prompt:

   ```bash
   cat > ~/.zerostore/google-service-account.json
   ```

3. Paste the key you copied, press **Enter**, then press **Ctrl+D** to save it. The `$` prompt comes back.
4. Make the file readable only by you:

   ```bash
   chmod 600 ~/.zerostore/google-service-account.json
   ```

5. Delete the downloaded `.json` file from your computer and empty the trash.

**Add it in ZeroStore**

1. On **1 · your docs**, open **GitHub, Linear, or a Google service account**.
2. Tick **Google Drive**.
3. **Folders:** the names of the folders you shared, separated by commas, exactly as Drive shows them, for example `Finance, Handbook`. If two shared folders have the same name, use `id:` and the code at the end of the folder's web address instead, for example `id:1AbCdEfGh...`.
4. **Google credentials file:** `~/.zerostore/google-service-account.json`
5. Click **Save**.

### GitHub

ZeroStore reads the written docs in a repository (such as `README.md` and the `docs` folder), not the code. Public repositories need no token: skip to **Add it in ZeroStore** and leave **Token** empty.

**Make a GitHub token**

1. Sign in at [github.com](https://github.com). Click your profile picture (top right), then **Settings**.
2. At the bottom of the left menu, click **Developer settings**, then **Personal access tokens**, then **Fine-grained tokens**.
3. Click **Generate new token**.
4. **Token name:** `ZeroStore`. **Expiration:** pick **90 days** or **Custom** for longer. (You'll paste a new token when it expires.)
5. **Resource owner:** you, or your company's organization if the repos belong to it.
6. **Repository access:** **Only select repositories**, then pick each repository ZeroStore should read.
7. **Permissions:** click **Add permissions**, choose **Contents**, and set it to **Read-only**. (**Metadata: Read-only** is added by itself.)
8. Click **Generate token**. Copy the token (it starts with `github_pat_`). GitHub shows it only once.

Organizations may need to approve the token. If so, GitHub says so on the token page, and an organization owner approves it under the organization's **Settings**, **Personal access tokens**, **Pending requests**.

**Add it in ZeroStore**

1. On **1 · your docs**, open **GitHub, Linear, or a Google service account**.
2. Tick **GitHub**.
3. **Repos:** the repositories as `owner/name`, separated by commas, for example `yourcompany/handbook, yourcompany/app`. That's the part after `github.com/` in the repo's address.
4. **Token:** paste the token.
5. Click **Save**.

### Linear

**Make a Linear API key**

1. In Linear, click your workspace name (top left), then **Settings**.
2. Under **Account**, click **Security & access**.
3. Under **Personal API keys**, click **New key**. Give it a name, `ZeroStore`.
4. For permissions, choose **Read** only. Optionally limit it to the teams ZeroStore should read.
5. Click **Create** and copy the key (it starts with `lin_api_`).

If you can't make keys, your Linear admin has turned that off for members (under **Settings**, **Administration**, **API**). Ask them.

**Add it in ZeroStore**

1. On **1 · your docs**, open **GitHub, Linear, or a Google service account**.
2. Tick **Linear**.
3. **Teams:** the team names as Linear shows them, separated by commas, for example `Product, Engineering`.
4. **API key:** paste the key.
5. Click **Save**.

## Part 9. Add your AI key

The librarian is the AI that reads each change and decides whether it matters, so small talk stays out and newer documents replace older answers. Without a key, every change is used as it is.

**Anthropic (Claude):**

1. Sign in at [platform.claude.com](https://platform.claude.com). Add a payment method and some credit under **Billing** if you haven't.
2. Open [API keys](https://platform.claude.com/settings/keys) and click **Create key**.
3. Name it `ZeroStore` and click **Create** (or **Add**). Copy the key (it starts with `sk-ant-`). It's shown only once.

**OpenAI:**

1. Sign in at [platform.openai.com](https://platform.openai.com). Add credit under **Billing** if you haven't.
2. Open [API keys](https://platform.openai.com/api-keys) and click **Create new secret key**.
3. Name it `ZeroStore`, leave **Permissions** on **All**, and click **Create secret key**. Copy the key (it starts with `sk-`). It's shown only once.

**Add it in ZeroStore:**

1. On the setup page, click **2 · librarian**.
2. **Your AI:** pick `anthropic` or `openai`.
3. **Model:** leave `mid-tier`, which picks the provider's balanced model.
4. **API key:** paste the key. Leave **Jev key** empty unless you have one.
5. Click **Save**.

Keys are saved on the server in a separate file that only your server account can read, never in the settings you see.

## Part 10. Run the first sync

1. At the bottom right of the setup page, click **Run first sync**.
2. ZeroStore reads everything you connected. A few hundred documents take a few minutes.
3. Click **status** at the top to see what it found: the documents it read, the facts it kept, and anything waiting for your review.

From now on, **Sync now** on the **status** page does the same whenever you like. ZeroStore also checks your sources again every day at 06:00 (the server's clock, which is UTC on AWS) by itself. To change when, open **settings** in the top bar, pick another choice from the list in **Check for changes**, and click **Save**.

## Part 11. Add ZeroStore to Claude, ChatGPT and Grok

The address every app needs is your web address followed by `/mcp`:

```
https://zs.example.com/mcp
```

The setup page shows it under **3 · your agents**, with a **Copy** button.

When you add it, each app sends you to a ZeroStore page that asks **Let <app> use your ZeroStore?**. If you're signed in to the web console in the same browser, you see it straight away. If ZeroStore shows **Sign in to ZeroStore** first, it wants your owner token: paste `cat ~/.zerostore/owner-token` in the server's terminal, copy the long line it prints, paste it into **Owner token** and click **Sign in**.

On that page:

- **Access:** **default** never sees anything marked confidential. **confidential** sees it.
- **Role** (once you've made roles in Part 12): which tagged information this app sees.
- Click **Allow**.

### Claude (claude.ai, the Claude desktop app and phone app)

Works on every plan. The Free plan can add one custom connector.

**Pro, Max or Free:**

1. Open [claude.ai](https://claude.ai) and click **Customize** in the sidebar, then **Connectors**.
2. Click **+ Add**, then **Add custom connector**.
3. **Name:** `ZeroStore`. **Remote MCP server URL:** `https://zs.example.com/mcp`. Click **Continue**.
4. If asked how to sign in, keep **Use Claude's published identity (Recommended)** and click **Continue**, then **Add**.
5. Click **Connect** next to ZeroStore. Claude sends you to the ZeroStore page: click **Allow**.

**Team or Enterprise:** an Owner adds it once for everyone. Open **Organization settings**, **Connectors**, click **Add**, point at **Custom**, choose **Web**, fill in the name and address, and click **Add**. Each member then opens **Customize**, **Connectors**, finds ZeroStore (marked **Custom**) and clicks **Connect**.

Claude then looks things up in ZeroStore by itself whenever a question is about your company. Try: "What does our handbook say about time off?"

### ChatGPT

Custom connectors need ChatGPT **Business**, **Enterprise** or **Edu**, where they can do everything, or **Pro**, where they can only look things up. On Business, only an admin or owner can add one. On Enterprise and Edu, admins and owners can, plus members an admin has given permission.

1. Open [chatgpt.com/plugins](https://chatgpt.com/plugins) on a computer (not the phone app).
2. Click the **+** button, then **Add custom MCP server**.
3. **Name:** `ZeroStore`. **Description:** "Our company's knowledge". **Connection:** `https://zs.example.com/mcp`. **Authentication:** **OAuth**.
4. Tick **I understand and want to continue**, then click **Create as a plugin**.
5. ChatGPT sends you to the ZeroStore page: click **Allow**.
6. ChatGPT lists ZeroStore's tools (such as **search** and **fetch**).

In a conversation, turn ZeroStore on from the tools menu next to the message box, then ask your question.

### Grok

1. Open [grok.com/connectors](https://grok.com/connectors). On the phone app: **Settings**, **Connectors**.
2. Click **New Connector**, then **Custom**.
3. Paste `https://zs.example.com/mcp` and continue.
4. Grok sends you to the ZeroStore page: click **Allow**.

On **Grok Business** or **Enterprise**, an admin adds it first: at [console.x.ai](https://console.x.ai), open your team, **Grok Business**, **Connectors**, click **+ Add Connector**, choose **Other**, and paste the address.

### Disconnecting an app

Open **settings** in the ZeroStore web console's top bar. Each app shows under **tokens** as "<app> (connector)". Click **revoke** next to it. The app can't reach ZeroStore again until someone adds it and you click **Allow** again.

### Claude Code, Claude Desktop and Cursor on someone's own computer

These connect without a server address. See [docs/connect-agents.md](connect-agents.md#on-your-own-computer): the setup page's **Connect to Claude** button gives the line to paste.

## Part 12. Decide who sees what

By default every connected app sees all your information, except things you mark **confidential**, which only apps with **confidential** access see. To give teams different views, use tags and roles.

- A **tag** labels information by where it lives: a Drive folder, a GitHub repo or a Linear label. For example, tag the `Company/Finance` folder `finance`.
- A **role** lists the tags it may see, for example a `leadership` role sees `finance, sales`.
- Each app you **Allow** gets a role. It sees everything untagged, plus the tagged information its role lists.

In the web console:

1. Click **settings** in the top bar and scroll down to **who sees what · tags and roles**.
2. In the first table, fill one row per place:
   - **tag:** a short word, such as `finance`. Use `confidential` for things only confidential access should see.
   - **where:** **Drive folder**, **GitHub repo** or **Linear label**.
   - **folder, repo or label:** the folder path as it appears in Drive (`Company/Finance`), the repo (`yourcompany/finance-docs`), or the label name.
3. In the second table, fill one row per role:
   - **role:** a name, such as `sales`.
   - **sees these tags:** the tags it may see, separated by commas, such as `sales, marketing`.
4. Click **Save**.
5. Click **status** in the top bar, then **Sync now** (or wait for tonight's sync), so the tags reach your documents.
6. Give each app its role: on the **settings** page, in the **tokens** list, pick the role next to each app and click **set**. New apps get their role on the **Allow** page.

Rules worth knowing:

- Once a tag exists, an app whose role lacks that tag can't see the tagged information, including apps with no role.
- Untagged information is for every app.
- If you remove a tag, its documents stay hidden until the next sync, so a typo never makes anything public.

### Let apps change information (optional)

Normally apps only read. You can let an app add or fix information too, but only the information its role is given. For example, a `sales` app may change sales information and nothing else.

1. Make the tags and roles first (the steps above).
2. Click **settings** in the top bar, then **yaml** at the top of the page.
3. At the end of the text, add these lines. Use your own role and tag names. `untagged` means information with no tag.

   ```yaml
   agent_writes:
     enabled: true
     review: false
     roles:
       sales: [sales]
   ```

4. Click **Save**.

From then on, apps with the `sales` role can add, change and remove sales information. Anything else they try is refused, and the refusal shows on the **log** page. Apps with no role can't change anything.

Rules worth knowing:

- Nothing waits for you. When an app adds or fixes something, ZeroStore's AI works out which older information it replaces and stops telling apps the old version.
- ZeroStore never changes your Google Docs, GitHub files or Linear issues. When an app fixes or removes a fact that came from one of them, ZeroStore saves that and hides the old fact. If someone later edits that document, the document wins again.
- A role can only change tags it can see.
- Want to check every change before agents are told? Change `review: false` to `review: true`. Changes then wait on the **review** page until you approve them.
- To stop it, change `enabled: true` to `enabled: false`. What was already changed stays.

## Everyday tasks

Open the server's terminal (Part 5) and paste the line you need.

| To | Paste |
|---|---|
| Get a new sign-in link for the web console | `zerostore ui` |
| Check everything is working | `zerostore doctor` |
| See what ZeroStore holds and whether its services run | `zerostore status` |
| Read documents again now, without waiting for tonight | `zerostore sync` |
| See what ZeroStore is doing as it happens (Ctrl+C to stop) | `journalctl --user -u zerostore.service -f` |
| Update ZeroStore to the newest version | the same install command as Part 6. Your settings and data stay. |
| Restart ZeroStore | `systemctl --user restart zerostore.service` |

**Removing ZeroStore from a computer or server:** paste this. It stops ZeroStore, removes the `zerostore` command and takes its site out of Caddy. Your settings and data stay unless you add ` --delete-data` at the end. While the repository is private, run `sh install.sh --uninstall` inside the `ZeroStore` folder instead.

```bash
curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --uninstall
```

**Stopping the server for good:** in the EC2 console, select the instance, then **Instance state**, **Terminate (delete) instance**. Then open **Elastic IPs**, select yours, click **Actions**, **Release Elastic IP addresses**. An Elastic IP you keep still costs money. Delete the A record from Part 4 too.

## When something goes wrong

Start with `zerostore doctor` in the server's terminal: every ✗ line comes with a → line saying what to do. Common problems:

| What you see | What it means | What to do |
|---|---|---|
| `zs.example.com has no DNS record yet` (installer) | The A record from Part 4 isn't there yet, or has a typo | Check the A record. Wait until [dnschecker.org](https://dnschecker.org) shows your Elastic IP, then run the install command again. |
| `Note: zs.example.com points at ... and this server's public address is ...` (installer) | The A record points at a different IP address | Change the A record's value to the Elastic IP from Part 3. If you use Cloudflare, make sure the cloud is grey (**DNS only**). |
| doctor: `the certificate isn't from a public authority` or `can't reach https://zs.example.com/mcp` | Caddy couldn't get the certificate yet | Wait a minute and run `zerostore doctor` again. If it stays, check that HTTP and HTTPS are allowed (below) and the Cloudflare cloud is grey. `sudo journalctl -u caddy --no-pager -n 50` shows Caddy's own messages. |
| The browser can't open `https://zs.example.com` at all | Ports 80 and 443 are closed | In EC2, select the instance, open the **Security** tab and click the security group's name. Click **Edit inbound rules**, then **Add rule**: **Type** **HTTP**, **Source** **Anywhere-IPv4**. Click **Add rule** again: **Type** **HTTPS**, **Source** **Anywhere-IPv4**. Click **Save rules**. |
| EC2 Instance Connect: "Failed to connect to your instance" | SSH isn't allowed from AWS's browser terminal, or the instance is still starting | Wait for **3/3 checks passed**. Then, as above, edit the inbound rules: add **Type** **SSH**, **Source** **Anywhere-IPv4**, and **Save rules**. |
| `zerostore: command not found` | This terminal started before ZeroStore was installed | Close the terminal tab and connect again (Part 5). |
| The sign-in link says it expired or was used | Each link works once, for ten minutes | Run `zerostore ui` for a new one. |
| Google: "Access blocked: ZeroStore has not completed the Google verification process" | The sign-in app is still in testing and your account isn't a test user | Publish the app (Part 8, Google Drive, step D), then sign in again. |
| Google Drive stops syncing after about a week | The sign-in app was left in testing | Publish it (step D), then on the setup page click **sign out of Google** and sign in again. |
| `404: Not Found` when installing | The ZeroStore repository is private | Use the three commands in the box in Part 6. |
| ChatGPT has no **Add custom MCP server** | The plan doesn't allow custom connectors, or you're not an admin | See the plans under [ChatGPT](#chatgpt). |
| An app says it couldn't connect or sign in | Usually the address or the certificate | Run `zerostore doctor --url https://zs.example.com` and do what the ✗ lines say. Make sure the address ends in `/mcp`. |

For more technical detail, see [docs/connect-agents.md](connect-agents.md) and the [README](../README.md).
