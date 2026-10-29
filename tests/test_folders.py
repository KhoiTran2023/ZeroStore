"""Folders on this computer: the source a ZeroStore on your own machine starts with."""

from __future__ import annotations

import re

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from zerostore.access.roles import matches
from zerostore.cli import WELCOME_NOTE, app
from zerostore.config import LocationRule, load_config, parse_config
from zerostore.connectors.base import ConnectorError
from zerostore.connectors.folder import FolderConnector
from zerostore.librarian.ingestion import Ingestor, make_connector
from zerostore.web.app import create_web_app
from zerostore.web.settings import SettingsError, update_folders

INCLUDE = ["*.md", "*.markdown", "*.txt", "*.rst"]


@pytest.fixture
def notes(tmp_path):
    root = tmp_path / "Notes"
    (root / "Decisions").mkdir(parents=True)
    (root / "Decisions" / "Pricing.md").write_text(
        "---\ntitle: Pricing 2026\nupdated: 2026-09-12\nlabels: [pricing]\n---\nThe Pro plan costs $29.\n"
    )
    (root / "todo.txt").write_text("Call the bank about the loan.\n")
    (root / "diagram.png").write_bytes(b"\x89PNG not text")
    (root / "binary.txt").write_bytes(b"\xff\xfe\x00 not utf-8")
    (root / ".hidden.md").write_text("secret\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD.md").write_text("ref\n")
    (root / "empty.md").write_text("   \n")
    return root


def test_reads_text_files_in_the_folder_and_inside_it(notes):
    connector = FolderConnector([str(notes)], INCLUDE)
    refs = {r.title: r for r in connector.list_items()}
    assert set(refs) == {"Pricing", "todo", "binary", "empty"}  # hidden, .git and .png are left out
    pricing = connector.fetch(refs["Pricing"])
    assert pricing.title == "Pricing 2026" and pricing.text.startswith("The Pro plan costs $29.")
    assert pricing.source == "folders" and pricing.labels == ["pricing"]
    assert pricing.updated_at == "2026-09-12" and pricing.location == str(notes / "Decisions")
    assert pricing.url == (notes / "Decisions" / "Pricing.md").resolve().as_uri()
    todo = connector.fetch(refs["todo"])
    assert todo.title == "todo" and todo.updated_at[:4].isdigit()  # the modified time
    assert connector.fetch(refs["binary"]) is None and connector.fetch(refs["empty"]) is None


def test_an_edit_changes_the_version(notes):
    connector = FolderConnector([str(notes)], INCLUDE)
    before = {r.doc_id: r.version for r in connector.list_items()}
    (notes / "todo.txt").write_text("Call the bank about the loan, and the accountant.\n")
    after = {r.doc_id: r.version for r in connector.list_items()}
    changed = [d for d in before if before[d] != after[d]]
    assert changed == [f"folders:{notes / 'todo.txt'}"]


def test_a_missing_folder_is_an_error_not_an_empty_folder(tmp_path):
    with pytest.raises(ConnectorError, match="isn't there"):
        FolderConnector([str(tmp_path / "gone")], INCLUDE).list_items()


def test_folders_must_be_full_paths(tmp_path):
    with pytest.raises(Exception, match="full paths"):
        parse_config({"sources": {"folders": {"paths": ["Notes"]}}})
    cfg = parse_config({"sources": {"folders": {"paths": ["~/Notes", "~/Notes"]}}})
    assert len(cfg.sources.folders.paths) == 1 and not cfg.sources.folders.paths[0].startswith("~")
    assert cfg.sources.enabled() == ["folders"]


def test_rules_match_a_folder_and_everything_inside(notes):
    doc = FolderConnector([str(notes)], INCLUDE)
    pricing = next(doc.fetch(r) for r in doc.list_items() if r.title == "Pricing")
    assert matches(pricing, LocationRule(folder=str(notes)))
    assert matches(pricing, LocationRule(folder=str(notes / "Decisions")))
    assert not matches(pricing, LocationRule(folder=str(notes / "Dec")))
    assert not matches(pricing, LocationRule(google_drive=str(notes)))


def write_config(path, notes, **extra):
    cfg = {
        "sources": {"folders": {"paths": [str(notes)]}},
        "ai": {"embeddings": "basic"},
        "decisions_file": str(path.parent / "decisions.md"),
        "servers": {"vectordb": str(path.parent / "f.lance"), "statedb": str(path.parent / "s.sqlite")},
        **extra,
    }
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_a_sync_reads_the_folder_and_keeps_confidential_places_apart(tmp_path, notes):
    from zerostore.app import ZeroStore

    config = load_config(
        write_config(tmp_path / "zerostore.yaml", notes, confidential=[{"folder": str(notes / "Decisions")}])
    )
    zs = ZeroStore.open(config, quiet=True)
    try:
        report = Ingestor(zs).sync()
        assert report.ok, report.summary()
        public = {d["title"] for d in zs.facts.sources(include_confidential=False)}
        everything = {d["title"] for d in zs.facts.sources(include_confidential=True)}
        assert public == {"todo"} and everything == {"todo", "Pricing 2026"}
        (notes / "todo.txt").unlink()  # deleting the file retires its facts on the next sync
        Ingestor(zs).sync()
        assert {d["title"] for d in zs.facts.sources(include_confidential=True)} == {"Pricing 2026"}
    finally:
        zs.close()


def test_the_demo_reads_real_folders_too(demo, notes):
    d, _ = demo
    cfg = load_config(d.config_path).model_copy(
        update={"sources": parse_config({"sources": {"folders": {"paths": [str(notes)]}}}).sources}
    )
    assert isinstance(make_connector("folders", cfg), FolderConnector)


def test_settings_add_and_remove_a_folder(tmp_path, notes):
    path = write_config(tmp_path / "zerostore.yaml", notes)
    other = tmp_path / "More"
    other.mkdir()
    assert update_folders(path, add=str(other)).sources.folders.paths == [str(notes), str(other)]
    with pytest.raises(SettingsError, match="no folder at"):
        update_folders(path, add=str(tmp_path / "nope"))
    with pytest.raises(SettingsError, match="full path"):
        update_folders(path, add="Notes")
    update_folders(path, remove=str(notes))
    assert update_folders(path, remove=str(other)).sources.folders is None  # the last one turns it off


def test_init_with_a_folder_is_ready_for_an_ai_key(tmp_path, monkeypatch):
    home = tmp_path / "zs-home"
    monkeypatch.setenv("ZEROSTORE_HOME", str(home))
    monkeypatch.delenv("ZEROSTORE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "Documents" / "ZeroStore"
    result = CliRunner().invoke(app, ["init", "--no-rule", "--folder", str(folder)])
    assert result.exit_code == 0, result.output
    assert (folder / WELCOME_NOTE).exists()  # a new, empty folder gets a welcome note
    cfg = load_config(home / "zerostore.yaml")
    assert cfg.sources.folders.paths == [str(folder)] and cfg.sync.schedule == "*/5 * * * *"
    # Running it again with another folder adds it and leaves your files alone.
    (folder / WELCOME_NOTE).unlink()
    other = tmp_path / "Other"
    other.mkdir()
    (other / "mine.md").write_text("mine\n")
    assert CliRunner().invoke(app, ["init", "--no-rule", "--folder", str(other)]).exit_code == 0
    assert load_config(home / "zerostore.yaml").sources.folders.paths == [str(folder), str(other)]
    assert not (other / WELCOME_NOTE).exists() and not (folder / WELCOME_NOTE).exists()


def test_without_a_folder_init_starts_with_no_sources(tmp_path, monkeypatch):
    home = tmp_path / "zs-home"
    monkeypatch.setenv("ZEROSTORE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert CliRunner().invoke(app, ["init", "--no-rule"]).exit_code == 0
    cfg = load_config(home / "zerostore.yaml")
    assert cfg.sources.enabled() == [] and cfg.sync.schedule == "0 6 * * *"


def test_the_setup_page_adds_and_removes_folders(demo, tmp_path):
    d, zs = demo
    client = TestClient(create_web_app(zs, d.config_path), base_url="http://localhost")
    client.post("/login", data={"token": d.owner_token()})
    page = client.get("/setup?step=1").text
    assert "Folders on this computer" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    folder = tmp_path / "Mine"
    folder.mkdir()
    client.post("/setup/folders", data={"csrf": token, "value": str(folder)})
    assert load_config(d.config_path).sources.folders.paths == [str(folder)]
    assert str(folder) in client.get("/setup?step=1").text
    refused = client.post("/setup/folders", data={"csrf": token, "value": str(tmp_path / "nope")})
    assert "no folder at" in refused.text
    client.post("/setup/folders", data={"csrf": token, "value": str(folder), "action": "remove"})
    assert load_config(d.config_path).sources.folders is None
