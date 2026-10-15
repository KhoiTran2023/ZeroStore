"""`zerostore demo`: Pebble & Co., the scripted judge and the simulated sources."""

from __future__ import annotations

import json
import os
import sys
import time

import anyio
import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from zerostore.cli import app
from zerostore.config import load_config, parse_config
from zerostore.connectors.demo import DemoConnector, split_header
from zerostore.decisions.base import NO_TOPIC, Candidate, DeletedFile
from zerostore.decisions.demo import DemoEngine
from zerostore.decisions.select import build_engines, build_judge
from zerostore.demo import NOTE, PRICING, Demo, DemoError
from zerostore.demo.story import ask
from zerostore.librarian.ingestion import Ingestor
from zerostore.librarian.review import decide

runner = CliRunner()


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    return Demo(tmp_path / "demo")


@pytest.fixture
def started(demo):
    demo.install()
    zs = demo.open()
    tokens = demo.create_tokens(zs)
    Ingestor(zs).sync()
    yield demo, zs, tokens
    zs.close()


def answer_text(reply):
    return reply["facts"][0]["text"] if reply.get("facts") else "not sure"


# The story ----------------------------------------------------------------


def test_the_whole_story_plays_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    result = runner.invoke(app, ["demo", "--pause", "0", "--home", str(tmp_path / "demo")])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("$20 per user per month") < out.index(
        "1 change · Pricing 2026 · demo judge confidence 0.97"
    )
    assert "replaces facts in Sales FAQ" in out
    assert out.count("\n    The Pro plan costs $29 per user per month") == 2  # both agents follow
    assert "Sales FAQ still says the old price" in out
    assert "PEB-48 Drop the free tier?  (confidence 0.54, waiting for review)" in out
    assert "No changes · 1 waiting for review" in out
    assert "The free tier covers up to 3 users" in out  # agents keep the current answer
    assert "claude mcp add pebble-demo -e ZEROSTORE_TOKEN=zs_" in out


def test_agents_follow_the_ceo(started):
    demo, zs, tokens = started
    for token in tokens.values():
        assert "$20 per user" in answer_text(ask(zs, token, "What does the Pro plan cost?"))

    assert demo.change_price(today="2026-10-02")
    report = Ingestor(zs).sync()
    [change] = report.changes
    assert (change.action, change.title, change.confidence) == ("updated", "Pricing 2026", 0.97)
    assert change.replaces == ["Sales FAQ"]
    assert report.summary() == "1 change · Pricing 2026 · demo judge confidence 0.97"

    for token in tokens.values():
        reply = ask(zs, token, "What does the Pro plan cost?")
        assert "$29 per user" in answer_text(reply)
        assert reply["facts"][0]["updated_at"] == "2026-10-02"
        assert all("$20" not in f["text"] for f in reply["facts"])  # the stale FAQ copy is hidden
    assert not demo.change_price()  # already changed


def test_an_unsure_note_waits_and_agents_keep_the_answer(started):
    demo, zs, tokens = started
    demo.add_note()
    report = Ingestor(zs).sync()
    assert [(c.action, c.confidence) for c in report.changes] == [("review", 0.54)]
    token = tokens["cursor"]
    assert "free tier covers up to 3 users" in answer_text(ask(zs, token, "Is there a free tier?"))

    # Once a person approves it, the note is the newer word on the free tier.
    [item] = zs.state.review_items("pending")
    assert decide(zs, item["id"], approve=True, decided_by="owner").status == "approved"
    assert "Maybe we drop the free tier" in answer_text(ask(zs, token, "Is there a free tier?"))


def test_default_tokens_never_see_the_finance_folder(started):
    demo, zs, tokens = started
    reply = ask(zs, tokens["claude-code"], "What is our runway and burn?")
    assert all(f["document"] != "Runway and salaries" for f in reply.get("facts", []))
    rows = zs.facts.search("runway burn", include_confidential=True)
    assert rows[0]["title"] == "Runway and salaries" and rows[0]["confidential"]


def test_small_talk_is_left_out(started):
    demo, zs, tokens = started
    notes = " ".join(
        r["text"] for r in zs.facts.chunks_of("google_drive:demo/Pebble/Team/Weekly sync notes.md")
    )
    assert "live chat" in notes and "Lunch at noon" not in notes


def test_a_question_nobody_wrote_down_is_not_sure(started):
    demo, zs, tokens = started
    assert ask(zs, tokens["cursor"], "Who runs the Berlin office?")["answer"] == "not sure"


def test_deleting_a_file_retires_its_facts(started):
    demo, zs, tokens = started
    (demo.company / "linear/Pebble/PEB-27.md").unlink()
    report = Ingestor(zs).sync()
    assert [(c.action, c.title) for c in report.changes] == [("retired", "PEB-27 Calendar sync")]


def test_moving_a_file_is_recognised(started):
    demo, zs, tokens = started
    old = demo.company / "google_drive/Pebble/Decisions/Refund policy.md"
    new = demo.company / "google_drive/Pebble/Team/Refund policy.md"
    new.write_text(old.read_text())
    old.unlink()
    report = Ingestor(zs).sync()
    assert sorted(c.action for c in report.changes) == ["added", "moved"]


# A running agent sees syncs from other processes ---------------------------


def test_a_connected_agent_sees_the_change_without_restarting(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    home = tmp_path / "demo"
    result = runner.invoke(app, ["demo", "start", "--home", str(home)])
    assert result.exit_code == 0, result.output
    config = str(home / "zerostore.yaml")
    token = json.loads((home / "agent-tokens.json").read_text())["claude-code"]
    env = {**os.environ, "ZEROSTORE_TOKEN": token}
    env.pop("ZEROSTORE_CONFIG", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "zerostore.cli", "serve", "-c", config], env=env
    )

    async def go():
        async with Client(params) as client:
            before = await client.call_tool("search", {"query": "What does the Pro plan cost?"})
            # The CEO edits the doc and a sync runs in another process, as in the launch video.
            assert runner.invoke(app, ["demo", "change-price", "--home", str(home)]).exit_code == 0
            synced = runner.invoke(app, ["sync", "-c", config])
            assert "1 change · Pricing 2026" in synced.output, synced.output
            time.sleep(1.2)
            after = await client.call_tool("search", {"query": "What does the Pro plan cost?"})
            return before.structured_content, after.structured_content

    before, after = anyio.run(go)
    assert "$20 per user" in answer_text(before)
    assert "$29 per user" in answer_text(after)


# Commands -------------------------------------------------------------------


def test_step_commands(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    home = str(tmp_path / "demo")
    out = runner.invoke(app, ["demo", "change-price", "--home", home])
    assert out.exit_code == 1 and f'run `zerostore demo start --home "{home}"` first' in out.output

    out = runner.invoke(app, ["demo", "start", "--home", home])
    assert out.exit_code == 0 and 'Ask them: "What does the Pro plan cost?"' in out.output
    out = runner.invoke(app, ["demo", "change-price", "--home", home])
    assert "+ The Pro plan costs $29" in out.output
    assert "already changed" in runner.invoke(app, ["demo", "change-price", "--home", home]).output
    out = runner.invoke(app, ["demo", "add-note", "--home", home])
    assert "Maya added PEB-48" in out.output
    assert "already there" in runner.invoke(app, ["demo", "add-note", "--home", home]).output

    status = runner.invoke(app, ["status", "-c", f"{home}/zerostore.yaml"])
    assert "the demo judge" in status.output and "hashing-256" in status.output

    # Starting again resets the company to before the price change.
    runner.invoke(app, ["demo", "start", "--home", home])
    assert "$20 per user" in (tmp_path / "demo" / "company" / PRICING).read_text()
    assert not (tmp_path / "demo" / "company" / NOTE).exists()

    assert "Removed the demo" in runner.invoke(app, ["demo", "remove", "--home", home]).output
    assert not (tmp_path / "demo").exists()
    assert "no demo to remove" in runner.invoke(app, ["demo", "remove", "--home", home]).output


def test_the_demo_never_deletes_a_folder_it_didnt_make(tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "zerostore.yaml").write_text("sources: {}\n")
    demo = Demo(precious)
    with pytest.raises(DemoError):
        demo.install()
    with pytest.raises(DemoError):
        demo.remove()
    assert (precious / "zerostore.yaml").exists()
    out = runner.invoke(app, ["demo", "start", "--home", str(precious)])
    assert out.exit_code == 1 and "left alone" in out.output


def test_the_demo_uses_its_own_home(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setenv("ZEROSTORE_HOME", str(tmp_path / "real"))
    demo = Demo()
    assert demo.home == tmp_path / "real" / "demo"
    demo.install()
    config = load_config(demo.config_path)
    assert config.servers.statedb_path().parent == demo.home
    assert config.decisions_path().parent == demo.home
    assert config.servers.port != 8765  # never clashes with the real server


def test_with_a_jev_key_the_demo_is_judged_by_jev(demo, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "jev-test")
    demo.install(jev=True)
    config = load_config(demo.config_path)
    assert config.ai.decision_engine == "jev" and config.ai.jev_api_key == "jev-test"
    assert "${JEV_API_KEY}" in demo.config_path.read_text()  # the key itself is never written down
    assert build_judge(config).label == "Jev"


# The demo judge ---------------------------------------------------------------


@pytest.fixture
def judge():
    return DemoEngine()


def test_judge_keeps_decisions_and_drops_small_talk(judge):
    keep, noise = judge.keep(["The Pro plan costs $20.", "Lunch at noon on Friday? See you there!"])
    assert keep.yes and keep.confidence == 0.98
    assert not noise.yes


def test_judge_compares_values_per_subject(judge):
    old = "Pro costs $20 per user per month."
    changed, same, other, unsure = judge.compare(
        [
            ("The Pro plan costs $29 per user per month, effective November 1.", old),
            ("The Pro plan costs $20 per user per month.", old),
            ("Pro and Team customers get live chat, starting October 13.", old),
            ("Maybe we drop the free tier?", "The free tier covers up to 3 users."),
        ]
    )
    assert (changed.choice, changed.confidence) == ("changed", 0.97)
    assert same.choice == "new" and other.choice == "new"
    assert (unsure.choice, unsure.confidence) == ("conflict", 0.54)


def test_judge_routes_and_scores(judge):
    topics = ["pricing and plans", "product and roadmap"]
    pricing, roadmap, nothing = judge.route(
        ["What does the Pro plan cost?", "When does the mobile app ship?", "Hello there"], topics
    )
    assert pricing.choice == "pricing and plans" and pricing.confidence >= 0.6
    assert roadmap.choice == "product and roadmap"
    assert nothing.choice == NO_TOPIC
    assert judge.score("What does the Pro plan cost?", ["The Pro plan costs $20.", "Support hours"], "") == [
        1.0,
        0.0,
    ]


def test_judge_recognises_moves(judge):
    deleted = DeletedFile("Refund policy", "google_drive", "Pebble/Decisions", "Refunds within 30 days")
    candidates = [
        Candidate("Roadmap", "google_drive", "", True),
        Candidate("Refund policy", "google_drive", "", True),
    ]
    assert judge.on_delete(deleted, candidates).choice == "moved_to_1"
    assert judge.on_delete(deleted, candidates[:1]).choice == "retire"


def test_demo_settings(tmp_path):
    config = parse_config(
        {
            "sources": {},
            "demo": str(tmp_path / "company"),
            "ai": {"decision_engine": "demo", "embeddings": "basic"},
            "servers": {"vectordb": str(tmp_path / "f"), "statedb": str(tmp_path / "s")},
        }
    )
    engines = build_engines(config)
    assert engines.judge.label == engines.narrow.label == engines.rerank.label == "demo judge"
    from zerostore.app import planned_embedder

    assert planned_embedder(config) == "hashing-256"


# Simulated sources --------------------------------------------------------------


def test_split_header():
    meta, body = split_header("---\ntitle: Pricing 2026\nupdated: 2026-09-12\n---\n# Pricing\n")
    assert meta == {"title": "Pricing 2026", "updated": __import__("datetime").date(2026, 9, 12)}
    assert body == "# Pricing\n"
    assert split_header("no header") == ({}, "no header")
    assert split_header("---\nnot: [closed\n---\ntext")[0] == {}


def test_connectors_read_like_the_real_sources(demo):
    demo.install()
    config = load_config(demo.config_path)
    drive = DemoConnector.from_config("google_drive", config)
    docs = {d.title: d for d in (drive.fetch(r) for r in drive.list_items())}
    pricing = docs["Pricing 2026"]
    assert pricing.source == "google_drive" and pricing.location == "Pebble/Decisions"
    assert pricing.updated_at == "2026-09-12" and pricing.url.startswith("file://")
    assert docs["Runway and salaries"].location == "Pebble/Finance"

    linear = DemoConnector.from_config("linear", config)
    issue = linear.fetch(next(r for r in linear.list_items() if r.title == "PEB-12"))
    assert issue.title == "PEB-12 Ship the mobile app" and issue.labels == ["mobile"]
    assert issue.location == "Pebble"

    github = DemoConnector.from_config("github", config)
    assert {r.title for r in github.list_items()} == {"README", "sales-faq", "support"}

    # Only the folders named in the settings are read, like the real Drive connector.
    narrow = DemoConnector("google_drive", demo.company.parent / "company", ["Pebble/Team"])
    assert {r.title for r in narrow.list_items()} == {"Company handbook", "Weekly sync notes"}


# Fixes from review ------------------------------------------------------------------


def test_home_works_before_or_after_the_subcommand(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    out = runner.invoke(app, ["demo", "--home", "rel demo", "start"])
    assert out.exit_code == 0, out.output
    home = tmp_path / "rel demo"
    config = load_config(home / "zerostore.yaml")
    assert config.servers.statedb_path() == home / "state.sqlite"  # absolute, not relative
    assert f'zerostore demo remove --home "{home}"' in out.output  # hints keep --home
    assert runner.invoke(app, ["demo", "--home", str(home), "remove"]).exit_code == 0
    assert not home.exists()


def test_a_reset_keeps_the_agents_tokens(demo):
    demo.install()
    zs = demo.open()
    first = demo.create_tokens(zs)
    zs.close()
    demo.install()
    assert demo.reset
    zs = demo.open()
    assert demo.create_tokens(zs) == first
    assert zs.tokens.verify(first["cursor"]).name == "cursor"
    zs.close()


def test_a_reset_says_to_restart_connected_agents(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    home = str(tmp_path / "demo")
    assert "Started over" not in runner.invoke(app, ["demo", "start", "--home", home]).output
    assert "Started over" in runner.invoke(app, ["demo", "start", "--home", home]).output


def test_a_half_finished_removal_can_be_retried(demo, monkeypatch):
    demo.install()
    real_unlink = type(demo.home).unlink

    def locked(path, *args, **kwargs):
        if path.name == "zerostore.yaml":
            raise PermissionError("in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(demo.home), "unlink", locked)
    with pytest.raises(PermissionError):
        demo.remove()
    monkeypatch.undo()
    assert demo.remove() and not demo.home.exists()


def test_a_missing_folder_keeps_its_facts(started):
    demo, zs, tokens = started
    (demo.company / "google_drive/Pebble/Finance").rename(demo.company / "google_drive/Pebble/Old")
    report = Ingestor(zs).sync()
    assert report.changes == []
    [failed] = [r for r in report.sources if r.error]
    assert failed.source == "google_drive" and "Pebble/Finance" in failed.error


def test_odd_files_dont_break_a_sync(started):
    demo, zs, tokens = started
    team = demo.company / "linear/Pebble"
    (team / "PEB-50.md").write_bytes(
        "---\r\ntitle: PEB-50 Caf\xe9 budget\r\n---\r\nBudget for the caf\xe9.".encode("latin-1")
    )
    (team / ".#PEB-12.md").symlink_to(team / "missing.md")
    report = Ingestor(zs).sync()
    assert report.ok
    assert [(c.action, c.title) for c in report.changes] == [("added", "PEB-50 Caf\ufffd budget")]


def test_the_demo_judge_is_only_for_the_demo(tmp_path):
    from zerostore.config import ConfigError

    with pytest.raises(ConfigError, match="only for `zerostore demo`"):
        parse_config({"sources": {}, "ai": {"decision_engine": "demo"}})


def test_reused_tokens_must_look_like_tokens(zs):
    with pytest.raises(ValueError):
        zs.tokens.create("x", "default", raw="short")
