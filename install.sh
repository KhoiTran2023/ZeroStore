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
# On a server, give it the domain name agents will use:
#
#   curl -fsSL https://raw.githubusercontent.com/KhoiTran2023/ZeroStore/main/install.sh | sh -s -- --domain zs.example.com
#
# Point the domain's DNS A record at the server and open ports 80 and 443 first.
# The installer then sets up Caddy, which gets a Let's Encrypt certificate for the
# domain and renews it, and serves ZeroStore at https://<domain> (it needs sudo).
#
# Optional environment variables:
#   ZEROSTORE_PACKAGE     what to install (default: the GitHub repository)
#   ZEROSTORE_HOME        where settings and data live (default: ~/.zerostore)
#   ZEROSTORE_NO_SERVICE  1 to skip the background service
#   ZEROSTORE_NO_OPEN     1 to print the setup link instead of opening the browser
#   ZEROSTORE_DOMAIN      the same as --domain
#   ZEROSTORE_PORT        the local port ZeroStore listens on behind Caddy (default: 8765)

set -eu

PACKAGE="${ZEROSTORE_PACKAGE:-git+https://github.com/KhoiTran2023/ZeroStore}"
DOMAIN="${ZEROSTORE_DOMAIN:-}"
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
        die "setting up HTTPS for $DOMAIN needs root. Install sudo, or run this as root."
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
    PUBLIC=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
    if [ -z "$PUBLIC" ]; then
        return 0
    fi
    case " $ADDRESSES" in
        *" $PUBLIC "*) say "$DOMAIN points at this server ($PUBLIC)." ;;
        *) say "Note: $DOMAIN points at $ADDRESSES and this server's public address is $PUBLIC. That's fine behind a CDN or load balancer; otherwise fix the A record." ;;
    esac
}

# Adds a site for the domain to Caddy's settings (once) and reloads Caddy.
setup_https() {
    step "Setting up HTTPS for $DOMAIN"
    check_dns
    install_caddy
    if [ -f "$CADDYFILE" ] && grep -qxF "$DOMAIN {" "$CADDYFILE"; then
        say "Caddy already serves $DOMAIN ($CADDYFILE)."
    else
        printf '\n# ZeroStore\n%s {\n\treverse_proxy localhost:%s\n}\n' "$DOMAIN" "$PORT" |
            as_root tee -a "$CADDYFILE" >/dev/null
        say "Added $DOMAIN to $CADDYFILE."
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

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) [ $# -ge 2 ] || die "--domain needs a name, like --domain zs.example.com"; DOMAIN="$2"; shift 2 ;;
            --domain=*) DOMAIN="${1#--domain=}"; shift ;;
            *) die "unknown option $1 (the only option is --domain <name>)" ;;
        esac
    done
    case "$DOMAIN" in
        "") ;;
        http://* | https://* | */* | *:*) die "give --domain just the name, like zs.example.com" ;;
    esac
}

main() {
    parse_args "$@"
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
    "$ZS" init --no-rule

    if [ -n "$DOMAIN" ]; then
        setup_https
        set -- --port "$PORT" --behind-https-proxy --public-url "https://$DOMAIN"
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

    if [ -n "$DOMAIN" ] && [ "$RUNNING" = 1 ]; then
        step "Checking https://$DOMAIN the way agents will reach it"
        "$ZS" doctor --url "https://$DOMAIN" || say "Caddy may still be getting the certificate: run \`zerostore doctor --url https://$DOMAIN\` again in a minute."
    fi

    step "ZeroStore is installed"
    if [ "$ON_PATH" = 0 ]; then
        say "Open a new terminal to use the zerostore command, or run this one now:"
        say "  export PATH=\"$BIN:\$PATH\""
    fi
    say "Next: on the setup page, connect your docs, add your AI key and connect your agents."
    if [ -n "$DOMAIN" ]; then
        say "Agents add ZeroStore with https://$DOMAIN/mcp (docs/connect-agents.md)."
    fi
    say "Later: \`zerostore ui\` opens the web UI signed in; \`zerostore service status\` shows the server."
}

main "$@"
