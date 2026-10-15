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
# Optional environment variables:
#   ZEROSTORE_PACKAGE     what to install (default: the GitHub repository)
#   ZEROSTORE_HOME        where settings and data live (default: ~/.zerostore)
#   ZEROSTORE_NO_SERVICE  1 to skip the background service
#   ZEROSTORE_NO_OPEN     1 to print the setup link instead of opening the browser

set -eu

PACKAGE="${ZEROSTORE_PACKAGE:-git+https://github.com/KhoiTran2023/ZeroStore}"

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

main() {
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

    RUNNING=0
    if [ "${ZEROSTORE_NO_SERVICE:-}" = 1 ]; then
        say "Skipping the background service (ZEROSTORE_NO_SERVICE=1)."
    else
        step "Starting ZeroStore in the background"
        if "$ZS" service install; then
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

    step "ZeroStore is installed"
    if [ "$ON_PATH" = 0 ]; then
        say "Open a new terminal to use the zerostore command, or run this one now:"
        say "  export PATH=\"$BIN:\$PATH\""
    fi
    say "Next: on the setup page, connect your docs, add your AI key and connect your agents."
    say "Later: \`zerostore ui\` opens the web UI signed in; \`zerostore service status\` shows the server."
}

main "$@"
