#!/bin/sh
# The ZeroStore installer, for Linux and macOS:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh
#
# It installs the `zerostore` command with uv (or pipx, when that is here and uv
# isn't; with neither, it installs uv first, which also brings Python if needed),
# creates your settings and owner token in ~/.zerostore, starts ZeroStore in the
# background (`zerostore service install`), and opens the setup page.
# Running it again updates ZeroStore and keeps your settings and data.
#
# On your own computer, ZeroStore reads a folder of your notes and docs: ~/Documents/ZeroStore
# (or ~/ZeroStore when there's no Documents folder), created with a welcome note if it's new.
# Name another one with --folder; after the install, add your AI key on the setup page:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --folder ~/Notes
#
# On a server, give it the domain name agents will use:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --domain zs.example.com
#
# Point the domain's DNS A record at the server and open ports 80 and 443 first.
# The installer then sets up Caddy, which gets a Let's Encrypt certificate for the
# domain and renews it, and serves ZeroStore at https://<domain> (it needs sudo).
#
# With no domain, a cloud server with a fixed public IP address (an Elastic IP on
# AWS) can use the IP itself; Let's Encrypt issues certificates for IP addresses:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --ip
#
# --ip finds the server's public IPv4 address; --ip=203.0.113.7 names it. Open ports
# 80 and 443 first. Caddy renews the certificate every few days by itself.
#
# To remove ZeroStore (the service, the command, and its site in Caddy), keeping your
# settings and data in ~/.zerostore; add --delete-data to delete those too:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --uninstall
#
# Optional environment variables:
#   ZEROSTORE_PACKAGE     what to install (default: the GitHub repository)
#   ZEROSTORE_HOME        where settings and data live (default: ~/.zerostore)
#   ZEROSTORE_NO_SERVICE  1 to skip the background service
#   ZEROSTORE_NO_OPEN     1 to print the setup link instead of opening the browser
#   ZEROSTORE_FOLDER      the same as --folder
#   ZEROSTORE_DOMAIN      the same as --domain
#   ZEROSTORE_IP          the same as --ip=<address>
#   ZEROSTORE_PORT        the local port ZeroStore listens on behind Caddy (default: 8765)

set -eu

PACKAGE="${ZEROSTORE_PACKAGE:-git+https://github.com/KhoiTran2023/ZeroStore}"
DOMAIN="${ZEROSTORE_DOMAIN:-}"
FOLDER="${ZEROSTORE_FOLDER:-}"
IP="${ZEROSTORE_IP:-}"
SITE=""
UNINSTALL=0
DELETE_DATA=0
PORT="${ZEROSTORE_PORT:-8765}"
CADDYFILE="${ZEROSTORE_CADDYFILE:-/etc/caddy/Caddyfile}"

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() {
    printf 'ZeroStore install failed: %s\n' "$*" >&2
    exit 1
}
has() { command -v "$1" >/dev/null 2>&1; }

# A Python 3.10 or newer, for pipx. Sets PYTHON.
find_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if has "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PYTHON=$(command -v "$candidate")
            return 0
        fi
    done
    return 1
}

# uv's own installer (https://docs.astral.sh/uv/). Sets UV.
install_uv() {
    say "Installing uv, which installs ZeroStore and the Python it needs."
    if has curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif has wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        die "curl or wget is needed to download uv."
    fi
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            return 0
        fi
    done
    die "uv was installed but can't be found. Open a new terminal and run this installer again."
}

# Runs a command as root: directly when we are root, else with sudo.
as_root() {
    if [ "$(id -u)" = 0 ]; then
        "$@"
    elif has sudo; then
        sudo "$@"
    else
        die "changing Caddy's settings needs root. Install sudo, or run this as root."
    fi
}

# Caddy from its official packages (https://caddyserver.com/docs/install).
install_caddy() {
    if has caddy; then
        return 0
    fi
    say "Installing Caddy, which gets and renews the HTTPS certificate."
    if has apt-get; then
        as_root apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
            as_root gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
            as_root tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        as_root apt-get update
        as_root apt-get install -y caddy
    elif has dnf; then
        as_root dnf install -y 'dnf-command(copr)'
        as_root dnf copr enable -y @caddy/caddy
        as_root dnf install -y caddy
    elif has brew; then
        brew install caddy
    else
        die "install Caddy (https://caddyserver.com/docs/install), then run this again."
    fi
    has caddy || die "Caddy was installed but the caddy command can't be found."
}

# Let's Encrypt certificates for IP addresses need Caddy 2.11 or newer.
check_caddy_for_ip() {
    VERSION=$(caddy version 2>/dev/null | awk '{print $1}')
    case "$VERSION" in
        v2.[0-9] | v2.[0-9].* | v2.10 | v2.10.* | v1.*)
            die "Caddy $VERSION can't get a certificate for an IP address; 2.11 or newer can. Update Caddy (https://caddyserver.com/docs/install), then run this again." ;;
    esac
}

# The public IPv4 address this server is reached at, or nothing.
public_ip() {
    curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true
}

# Fills in IP when --ip came without an address, and notes an address that isn't this server's.
check_ip() {
    PUBLIC=$(public_ip)
    if [ "$IP" = detect ]; then
        [ -n "$PUBLIC" ] || die "couldn't find this server's public IP address. Give it: --ip=203.0.113.7"
        IP="$PUBLIC"
        say "This server's public IP address is $IP."
    elif [ -n "$PUBLIC" ] && [ "$PUBLIC" != "$IP" ]; then
        say "Note: this server's public address is $PUBLIC, not $IP. That's fine behind a load balancer; otherwise use --ip=$PUBLIC."
    fi
    case "$IP" in
        10.* | 127.* | 169.254.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].* | 100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*)
            die "$IP is a private address, so no certificate authority can check it and agents on the internet can't reach it. Use the server's public IP address." ;;
    esac
}

# Warns when the domain doesn't point at this server yet: the certificate can't be issued until it does.
check_dns() {
    if has getent; then
        ADDRESSES=$(getent ahosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')
    elif has host; then
        ADDRESSES=$(host "$DOMAIN" 2>/dev/null | awk '/has (IPv6 )?address/ {print $NF}' | tr '\n' ' ')
    else
        return 0
    fi
    if [ -z "$ADDRESSES" ]; then
        die "$DOMAIN has no DNS record yet. Add an A record pointing at this server's public IP address, wait a minute, then run this again."
    fi
    PUBLIC=$(public_ip)
    if [ -z "$PUBLIC" ]; then
        return 0
    fi
    case " $ADDRESSES" in
        *" $PUBLIC "*) say "$DOMAIN points at this server ($PUBLIC)." ;;
        *) say "Note: $DOMAIN points at $ADDRESSES and this server's public address is $PUBLIC. That's fine behind a CDN or load balancer; otherwise fix the A record." ;;
    esac
}

# Adds a site for the domain or IP address to Caddy's settings (once) and reloads Caddy.
setup_https() {
    if [ -n "$DOMAIN" ]; then
        SITE="$DOMAIN"
        step "Setting up HTTPS for $SITE"
        check_dns
        install_caddy
        BLOCK=$(printf '%s {\n\treverse_proxy localhost:%s\n}' "$SITE" "$PORT")
    else
        step "Setting up HTTPS for this server's IP address"
        check_ip
        SITE="$IP"
        install_caddy
        check_caddy_for_ip
        # Let's Encrypt issues IP certificates only under its short-lived profile (about six days).
        BLOCK=$(printf 'https://%s {\n\ttls {\n\t\tissuer acme {\n\t\t\tprofile shortlived\n\t\t}\n\t}\n\treverse_proxy localhost:%s\n}' "$SITE" "$PORT")
    fi
    FIRST=$(printf '%s\n' "$BLOCK" | head -n 1)
    if [ -f "$CADDYFILE" ] && grep -qxF "$FIRST" "$CADDYFILE"; then
        say "Caddy already serves $SITE ($CADDYFILE)."
    else
        printf '\n# ZeroStore\n%s\n' "$BLOCK" | as_root tee -a "$CADDYFILE" >/dev/null
        say "Added $SITE to $CADDYFILE."
    fi
    if has systemctl; then
        as_root systemctl enable caddy >/dev/null 2>&1 || true
        as_root systemctl reload-or-restart caddy
    elif has brew; then
        brew services restart caddy
    else
        as_root caddy reload --config "$CADDYFILE"
    fi
}

# Takes this install's site out of Caddy's settings: the "# ZeroStore" line and the block after it.
remove_caddy_site() {
    if [ ! -f "$CADDYFILE" ] || ! grep -qxF "# ZeroStore" "$CADDYFILE"; then
        return 0
    fi
    KEPT=$(mktemp)
    awk '$0 == "# ZeroStore" { skip = 1; next } skip && $0 == "}" { skip = 0; next } !skip' "$CADDYFILE" > "$KEPT"
    as_root cp "$KEPT" "$CADDYFILE"
    rm -f "$KEPT"
    say "Removed ZeroStore's site from $CADDYFILE. Caddy itself stays; remove it with your package manager if nothing else uses it."
    if has systemctl; then
        as_root systemctl reload caddy >/dev/null 2>&1 || true
    elif has brew; then
        brew services restart caddy >/dev/null 2>&1 || true
    fi
}

uninstall() {
    # A new terminal may not have uv's folder on PATH yet, so look where the installer puts it too.
    UV=""
    for candidate in "$(command -v uv 2>/dev/null || true)" "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            UV="$candidate"
            break
        fi
    done
    UV_BIN=""
    if [ -n "$UV" ]; then
        UV_BIN=$("$UV" tool dir --bin 2>/dev/null || true)
    fi
    ZS=""
    for candidate in "$UV_BIN/zerostore" "$(command -v zerostore 2>/dev/null || true)" "$HOME/.local/bin/zerostore"; do
        if [ -x "$candidate" ] && [ ! -d "$candidate" ]; then
            ZS="$candidate"
            break
        fi
    done

    step "Stopping the background service"
    if [ -n "$ZS" ]; then
        "$ZS" service uninstall || say "The service couldn't be removed (see above); carrying on."
    else
        say "The zerostore command isn't here, so there's no service to stop."
    fi

    step "Removing the zerostore command"
    if [ -n "$UV" ] && "$UV" tool list 2>/dev/null | grep -q '^zerostore '; then
        "$UV" tool uninstall zerostore
    elif has pipx && pipx list --short 2>/dev/null | grep -q '^zerostore '; then
        pipx uninstall zerostore
    else
        say "It wasn't installed with uv or pipx; nothing to remove."
    fi

    remove_caddy_site

    HOME_DIR="${ZEROSTORE_HOME:-$HOME/.zerostore}"
    step "Your settings and data"
    if [ ! -d "$HOME_DIR" ]; then
        say "$HOME_DIR isn't here."
    elif [ "$DELETE_DATA" = 1 ]; then
        rm -rf "$HOME_DIR"
        say "Deleted $HOME_DIR (settings, tokens, keys and the fact store)."
    else
        say "Kept $HOME_DIR (settings, tokens, keys and the fact store), so a reinstall picks up where you left off."
        say "To delete it too: rm -rf \"$HOME_DIR\""
    fi

    step "ZeroStore is uninstalled"
    say "Agents you connected still list it: remove it there too (Claude Code: claude mcp remove zerostore; Claude, ChatGPT and Grok: their connector settings)."
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) [ $# -ge 2 ] || die "--domain needs a name, like --domain zs.example.com"; DOMAIN="$2"; shift 2 ;;
            --domain=*) DOMAIN="${1#--domain=}"; shift ;;
            --folder) [ $# -ge 2 ] || die "--folder needs a path, like --folder ~/Notes"; FOLDER="$2"; shift 2 ;;
            --folder=*) FOLDER="${1#--folder=}"; shift ;;
            --ip) IP=detect; shift ;;
            --ip=*) IP="${1#--ip=}"; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            --delete-data) DELETE_DATA=1; shift ;;
            *) die "unknown option $1 (the options are --folder <path>, --domain <name>, --ip, --uninstall and --delete-data)" ;;
        esac
    done
    case "$DOMAIN" in
        "") ;;
        http://* | https://* | */* | *:*) die "give --domain just the name, like zs.example.com" ;;
    esac
    if [ -n "$DOMAIN" ] && [ -n "$IP" ]; then
        die "use --domain or --ip, not both"
    fi
    if [ "$UNINSTALL" = 1 ] && { [ -n "$DOMAIN" ] || [ -n "$IP" ] || [ -n "$FOLDER" ]; }; then
        die "--uninstall takes no --domain, --ip or --folder"
    fi
    if [ "$DELETE_DATA" = 1 ] && [ "$UNINSTALL" = 0 ]; then
        die "--delete-data goes with --uninstall"
    fi
    case "$IP" in
        "" | detect) ;;
        *[!0-9.]* | *..* | .* | *.) die "give --ip an IPv4 address, like --ip=203.0.113.7, or just --ip to find it" ;;
        *.*.*.*.*) die "give --ip an IPv4 address, like --ip=203.0.113.7" ;;
        *.*.*.*) ;;
        *) die "give --ip an IPv4 address, like --ip=203.0.113.7" ;;
    esac
}

main() {
    parse_args "$@"
    if [ "$UNINSTALL" = 1 ]; then
        uninstall
        return 0
    fi
    case "$(uname -s)" in
        Linux | Darwin) ;;
        *) die "this installer is for Linux and macOS. Elsewhere, run: pipx install $PACKAGE" ;;
    esac
    case "$PACKAGE" in
        git+*) has git || die "git is needed to install from GitHub. Install git, then run this again." ;;
    esac

    step "Installing the zerostore command"
    if has uv; then
        UV=$(command -v uv)
        INSTALLER=uv
    elif has pipx && find_python; then
        INSTALLER=pipx
    else
        install_uv
        INSTALLER=uv
    fi
    if [ "$INSTALLER" = uv ]; then
        "$UV" tool install --force --reinstall-package zerostore --python '>=3.10' "$PACKAGE"
        BIN=$("$UV" tool dir --bin)
    else
        pipx install --force --python "$PYTHON" "$PACKAGE"
        BIN=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || printf '%s' "$HOME/.local/bin")
    fi
    ZS="$BIN/zerostore"
    [ -x "$ZS" ] || die "the install finished but $ZS is missing."
    "$ZS" --version

    ON_PATH=1
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *)
            ON_PATH=0
            # So new terminals find `zerostore`; it's fine if this can't edit the shell profile.
            if [ "$INSTALLER" = uv ]; then "$UV" tool update-shell >/dev/null 2>&1 || true; else pipx ensurepath >/dev/null 2>&1 || true; fi
            ;;
    esac

    step "Creating your settings and owner token"
    if [ -n "$FOLDER" ] || { [ -z "$DOMAIN" ] && [ -z "$IP" ]; }; then
        # On your own computer ZeroStore starts out reading a folder of yours.
        if [ -z "$FOLDER" ]; then
            if [ -d "$HOME/Documents" ]; then FOLDER="$HOME/Documents/ZeroStore"; else FOLDER="$HOME/ZeroStore"; fi
        fi
        case "$FOLDER" in
            \~) FOLDER="$HOME" ;;
            \~/*) FOLDER="$HOME/${FOLDER#\~/}" ;;
        esac
        "$ZS" init --no-rule --folder "$FOLDER"
    else
        "$ZS" init --no-rule
    fi

    if [ -n "$DOMAIN" ] || [ -n "$IP" ]; then
        setup_https
        set -- --port "$PORT" --behind-https-proxy --public-url "https://$SITE"
    else
        set --
    fi

    RUNNING=0
    if [ "${ZEROSTORE_NO_SERVICE:-}" = 1 ]; then
        say "Skipping the background service (ZEROSTORE_NO_SERVICE=1)."
    else
        step "Starting ZeroStore in the background"
        if "$ZS" service install "$@"; then
            RUNNING=1
        else
            say "ZeroStore couldn't run in the background here (see above). Start it in a terminal instead:"
            say "  zerostore serve --http --open"
        fi
    fi

    if [ "$RUNNING" = 1 ]; then
        step "Opening the setup page"
        if [ "${ZEROSTORE_NO_OPEN:-}" = 1 ]; then "$ZS" ui --no-open; else "$ZS" ui; fi
    fi

    if [ -n "$SITE" ] && [ "$RUNNING" = 1 ]; then
        step "Checking https://$SITE the way agents will reach it"
        "$ZS" doctor --url "https://$SITE" || say "Caddy may still be getting the certificate: run \`zerostore doctor --url https://$SITE\` again in a minute."
    fi

    step "ZeroStore is installed"
    if [ "$ON_PATH" = 0 ]; then
        say "Open a new terminal to use the zerostore command, or run this one now:"
        say "  export PATH=\"$BIN:\$PATH\""
    fi
    if [ -n "$FOLDER" ]; then
        say "Next: put your notes and docs in $FOLDER, add your AI key on the setup page, and connect your agents."
    else
        say "Next: on the setup page, connect your docs, add your AI key and connect your agents."
    fi
    if [ -n "$SITE" ]; then
        say "Agents add ZeroStore with https://$SITE/mcp (docs/connect-agents.md)."
    fi
    say "Later: \`zerostore ui\` opens the web UI signed in; \`zerostore service status\` shows the server."
}

main "$@"
