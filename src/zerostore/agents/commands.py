"""What to paste into Claude Code, Claude Desktop, Cursor and other agents to connect them.

On the same computer an agent starts `zerostore serve` itself and talks to it over
stdio: no network, no port and no certificate to trust. The command is the full
path to the zerostore program, because apps opened from the Dock or Start menu
(Claude Desktop among them) don't get your shell's PATH, so a bare `zerostore`
isn't found there.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from zerostore.config import CONFIG_FILENAME, home_dir

SERVER_NAME = "zerostore"


def program() -> str:
    """The zerostore program's full path, or plain `zerostore` when it can't be found."""
    name = "zerostore.exe" if os.name == "nt" else "zerostore"
    # Next to this Python: the uv, pipx or virtualenv install that is running now.
    # Not resolved, so it keeps working when that environment is upgraded in place.
    beside = Path(sys.executable).parent / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside.absolute())
    found = shutil.which("zerostore")
    return str(Path(found).absolute()) if found else "zerostore"


def serve_args(config_path: Path | None) -> list[str]:
    """`zerostore serve`, plus `-c <config>` unless it is the default one."""
    args = ["serve"]
    if config_path is not None:
        default = (home_dir() / CONFIG_FILENAME).expanduser().resolve()
        if config_path.expanduser().resolve() != default:
            args += ["-c", str(config_path.expanduser().absolute())]
    return args


def claude_code_stdio(token: str, config_path: Path | None, name: str = SERVER_NAME) -> str:
    env = f"ZEROSTORE_TOKEN={token}"
    return shlex.join(["claude", "mcp", "add", name, "-e", env, "--", program(), *serve_args(config_path)])


def stdio_json(token: str, config_path: Path | None, name: str = SERVER_NAME) -> str:
    entry = {"command": program(), "args": serve_args(config_path), "env": {"ZEROSTORE_TOKEN": token}}
    return json.dumps({"mcpServers": {name: entry}}, indent=2)


def claude_code_http(token: str, url: str, name: str = SERVER_NAME) -> str:
    header = f"Authorization: Bearer {token}"
    return shlex.join(["claude", "mcp", "add", "--transport", "http", name, url, "--header", header])


def http_json(token: str, url: str, name: str = SERVER_NAME) -> str:
    entry = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
    return json.dumps({"mcpServers": {name: entry}}, indent=2)
