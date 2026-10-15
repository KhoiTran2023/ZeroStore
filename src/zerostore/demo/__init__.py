"""`zerostore demo`: Pebble & Co., a made-up startup, set up as a separate ZeroStore.

Its Google Docs, GitHub repo and Linear issues are plain files in
`<demo home>/company/`, read by the simulated connectors in
`connectors/demo.py`, and judged by the scripted demo judge (or Jev, when
`JEV_API_KEY` is set). Nothing touches the real install in ~/.zerostore,
the network, or any account.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import date
from pathlib import Path

from zerostore.app import ZeroStore
from zerostore.config import CONFIG_FILENAME, OWNER_TOKEN_FILE, home_dir, load_config

COMPANY = Path(__file__).parent / "company"
MARKER = ".zerostore-demo"  # only a folder holding this file is ever deleted by the demo
TOKENS_FILE = "agent-tokens.json"
AGENTS = ("claude-code", "cursor")

PRICING = "google_drive/Pebble/Decisions/Pricing 2026.md"
OLD_PRICE = "The Pro plan costs $20 per user per month, billed monthly."
NEW_PRICE = "The Pro plan costs $29 per user per month, billed monthly, effective November 1."

NOTE = "linear/Pebble/PEB-48.md"
NOTE_TEXT = (
    "Maybe we drop the free tier? It brings in a lot of support questions and few upgrades. "
    "Thoughts before Friday? (Maya)"
)

CONFIG_TEMPLATE = """\
# The ZeroStore demo: Pebble & Co., a made-up startup. Every source below is
# simulated from the files in `demo:`. Edit them, then run
#   zerostore sync -c {config}
# to watch ZeroStore notice. `zerostore demo remove` deletes this folder.
demo: {company}

sources:
  google_drive:
    folders: ["Pebble/Decisions", "Pebble/Team", "Pebble/Finance"]
    credentials: null
  github:
    repos: ["pebble-co/app"]
  linear:
    teams: ["Pebble"]

ai:
{ai}  embeddings: basic   # word matching, no model to download

confidential:
  - google_drive: "Pebble/Finance"

decisions_file: {decisions}

servers:
  port: 8766
  vectordb: {vectordb}
  statedb: {statedb}
"""


class DemoError(Exception):
    """The demo can't do that (not set up yet, or a folder it must not touch)."""


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _q(path: Path) -> str:
    return json.dumps(str(path))  # a quoted string is valid YAML for any path


class Demo:
    def __init__(self, home: Path | None = None):
        default = (home_dir() / "demo").expanduser().resolve()
        # Absolute, because agents launch `zerostore serve -c <path>` from their own folders.
        self.home = Path(home).expanduser().resolve() if home else default
        self.is_default = self.home == default
        self.reset = False  # set by install() when it replaced an earlier demo
        self._kept_tokens: dict[str, str] = {}
        self._kept_owner: str | None = None

    def command(self, sub: str) -> str:
        """The `zerostore demo` command for this demo, with --home when it isn't the default one."""
        where = "" if self.is_default else f' --home "{self.home}"'
        return f"zerostore demo {sub}{where}"

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_FILENAME

    @property
    def company(self) -> Path:
        return self.home / "company"

    @property
    def installed(self) -> bool:
        return (self.home / MARKER).exists() and self.config_path.exists()

    def install(self, *, jev: bool = False) -> None:
        """A fresh copy of Pebble & Co. as it was before the CEO changed the price.

        The agents' tokens survive a reset, so agents connected earlier keep working
        once their ZeroStore connection restarts.
        """
        self._kept_tokens = self.tokens() if self.installed else {}
        self._kept_owner = self.owner_token() if self.installed else None
        self.reset = self.remove()
        self.home.mkdir(parents=True)
        os.chmod(self.home, stat.S_IRWXU)
        (self.home / MARKER).write_text("Made by `zerostore demo`. Safe to delete.\n")
        shutil.copytree(COMPANY, self.company, copy_function=shutil.copyfile)  # writable copies
        ai = (
            "  decision_engine: jev\n  jev_api_key: ${JEV_API_KEY}\n"
            if jev
            else "  decision_engine: demo   # the demo's scripted judge; set JEV_API_KEY to use Jev\n"
        )
        self.config_path.write_text(
            CONFIG_TEMPLATE.format(
                config=self.config_path,
                company=_q(self.company),
                ai=ai,
                decisions=_q(self.home / "decisions.md"),
                vectordb=_q(self.home / "facts.lance"),
                statedb=_q(self.home / "state.sqlite"),
            )
        )

    def remove(self) -> bool:
        """Delete the demo folder. Refuses any folder the demo didn't make."""
        if not self.home.exists():
            return False
        if not (self.home / MARKER).exists():
            raise DemoError(
                f"{self.home} exists but wasn't made by `zerostore demo`, so it was left alone. "
                "Move it, or pass --home to use another folder."
            )
        # The marker goes last: if a file is locked (an agent still connected on Windows),
        # the folder stays recognisable and the next try can finish the job.
        for child in self.home.iterdir():
            if child.name == MARKER:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        (self.home / MARKER).unlink()
        self.home.rmdir()
        return True

    def _require(self) -> None:
        if not self.installed:
            raise DemoError(f"the demo isn't set up yet; run `{self.command('start')}` first")

    def open(self) -> ZeroStore:
        self._require()
        return ZeroStore.open(load_config(self.config_path), quiet=True)

    def create_tokens(self, zs: ZeroStore) -> dict[str, str]:
        """A default-level token per demo agent, saved so the connect commands can be shown again.

        Also an owner token in the demo's own owner-token file, for signing in to the web UI.
        """
        kept = self._kept_tokens
        tokens = {name: zs.tokens.create(name, "default", raw=kept.get(name))[0] for name in AGENTS}
        _write_private(self.home / TOKENS_FILE, json.dumps(tokens, indent=2))
        owner = zs.tokens.create("owner", "owner", raw=self._kept_owner)[0]
        _write_private(self.home / OWNER_TOKEN_FILE, owner + "\n")
        return tokens

    def owner_token(self) -> str | None:
        try:
            return (self.home / OWNER_TOKEN_FILE).read_text().strip() or None
        except OSError:
            return None

    def tokens(self) -> dict[str, str]:
        try:
            return json.loads((self.home / TOKENS_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def change_price(self, today: str | None = None) -> bool:
        """The CEO edits Pricing 2026: Pro goes from $20 to $29. False if it was already changed."""
        self._require()
        path = self.company / PRICING
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise DemoError(
                f"Pricing 2026 is gone from the demo; `{self.command('start')}` resets it"
            ) from None
        if NEW_PRICE in text:
            return False
        if OLD_PRICE not in text:
            raise DemoError(f"Pricing 2026 was edited by hand; `{self.command('start')}` resets it")
        lines = text.replace(OLD_PRICE, NEW_PRICE).split("\n")
        stamp = today or date.today().isoformat()
        lines = [f"updated: {stamp}" if line.startswith("updated:") else line for line in lines]
        path.write_text("\n".join(lines), encoding="utf-8")
        return True

    def add_note(self, today: str | None = None) -> bool:
        """Maya wonders in Linear whether to drop the free tier. False if the note is already there."""
        self._require()
        path = self.company / NOTE
        if path.exists():
            return False
        stamp = today or date.today().isoformat()
        header = f"---\ntitle: PEB-48 Drop the free tier?\nupdated: {stamp}\nlabels: [pricing]\n---\n"
        path.write_text(header + NOTE_TEXT + "\n", encoding="utf-8")
        return True
