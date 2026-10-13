from __future__ import annotations

import pytest
from fakes import FakeConnector

from zerostore.config import ConfidentialRule, GitHubSource, GoogleDriveSource, LinearSource
from zerostore.connectors.base import SourceDocument
from zerostore.librarian.ingestion import Ingestor, SyncBusy, SyncReport, is_confidential


@pytest.fixture
def drive():
    return FakeConnector("google_drive")


@pytest.fixture
def ingest(zs, drive):
    zs.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    connectors = {"google_drive": drive}
    return Ingestor(zs, lambda name, config: connectors[name])


def search(zs, query, confidential=True):
    return [r["text"] for r in zs.facts.search(query, include_confidential=confidential)]


def test_first_sync_adds_documents(zs, drive, ingest):
    drive.put("pricing", "Pricing 2026", "# Pro plan\n\nThe Pro plan costs $20 per month.")
    report = ingest.sync()
    assert report.ok and report.summary() == "1 change · Pricing 2026"
    assert [c.action for c in report.changes] == ["added"]
    assert any("$20" in t for t in search(zs, "pro plan price"))
    assert zs.state.documents()["google_drive:pricing"]["chunks"] == 1
    assert drive.closed


def test_the_ceo_changes_the_price(zs, drive, ingest):
    drive.put("pricing", "Pricing 2026", "The Pro plan costs $20 per month.")
    ingest.sync()
    drive.put("pricing", "Pricing 2026", "The Pro plan costs $29 per month.", version="v2")
    report = ingest.sync()
    assert [(c.action, c.title) for c in report.changes] == [("updated", "Pricing 2026")]
    texts = " ".join(search(zs, "pro plan price"))
    assert "$29" in texts and "$20" not in texts
    actions = [r["action"] for r in zs.state.audit_since("2000-01-01", actions=("document_updated",))]
    assert actions == ["document_updated"]


def test_unchanged_versions_are_not_fetched_again(drive, ingest):
    drive.put("a", "A", "alpha")
    ingest.sync()
    drive.fetched.clear()
    report = ingest.sync()
    assert drive.fetched == [] and report.changes == []
    assert report.summary() == "No changes"


def test_resave_without_edits_is_skipped(zs, drive, ingest):
    drive.put("a", "A", "alpha")
    ingest.sync()
    drive.put("a", "A", "alpha", version="v2")
    report = ingest.sync()
    assert report.changes == [] and report.sources[0].unchanged == 1
    assert zs.state.documents()["google_drive:a"]["version"] == "v2"


def test_deleted_documents_are_retired(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    drive.put("b", "B", "beta text")
    ingest.sync()
    del drive.docs["b"]
    report = ingest.sync()
    assert [(c.action, c.title) for c in report.changes] == [("retired", "B")]
    assert set(zs.state.documents()) == {"google_drive:a"}
    assert all("beta" not in t for t in search(zs, "beta text"))


def test_empty_listing_keeps_everything(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    ingest.sync()
    drive.docs.clear()
    report = ingest.sync()
    assert report.changes == [] and "kept them" in report.sources[0].warning
    assert set(zs.state.documents()) == {"google_drive:a"}


def test_unreachable_source_changes_nothing(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    ingest.sync()
    drive.fail_listing = True
    report = ingest.sync()
    assert not report.ok and report.sources[0].error == "source unreachable"
    assert "1 source failed" in report.summary()
    assert set(zs.state.documents()) == {"google_drive:a"}
    assert [r["action"] for r in zs.state.audit_since("2000-01-01", actions=("sync_failed",))] == [
        "sync_failed"
    ]


def test_one_unreadable_file_doesnt_stop_the_rest(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    drive.put("b", "B", "beta text")
    drive.fail_fetch = {"a"}
    report = ingest.sync()
    assert [c.title for c in report.changes] == ["B"]
    assert report.sources[0].failed == ["A: timeout"]
    drive.fail_fetch = set()
    assert [c.title for c in ingest.sync().changes] == ["A"]  # retried next time


def test_file_that_becomes_empty_is_retired(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    ingest.sync()
    drive.put("a", "A", "   ", version="v2")
    assert [c.action for c in ingest.sync().changes] == ["retired"]


def test_confidential_folder_is_hidden_from_default_tokens(zs, drive, ingest):
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    drive.put("pay", "Salaries", "Engineer salary band", location="Company/Finance/2026")
    drive.put("price", "Pricing", "Pro plan price", location="Company/Sales")
    ingest.sync()
    assert all("salary" not in t.lower() for t in search(zs, "salary band", confidential=False))
    assert any("salary" in t.lower() for t in search(zs, "salary band", confidential=True))
    changes = zs.state.audit_since("2000-01-01", actions=("document_added",), include_confidential=False)
    assert [c["detail"] for c in changes] == ["Pricing"]


def test_changing_rules_rechecks_unchanged_documents(zs, drive, ingest):
    drive.put("pay", "Salaries", "Engineer salary band", location="Company/Finance")
    ingest.sync()
    assert any("salary" in t.lower() for t in search(zs, "salary band", confidential=False))
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["updated"]
    assert all("salary" not in t.lower() for t in search(zs, "salary band", confidential=False))
    drive.fetched.clear()
    ingest.sync()
    assert drive.fetched == []  # rules recorded; no more rechecks


def test_removed_source_is_retired(zs, drive, ingest):
    drive.put("a", "A", "alpha text")
    ingest.sync()
    zs.config.sources.google_drive = None
    zs.config.sources.github = GitHubSource(repos=["acme/app"])
    github = FakeConnector("github")
    report = Ingestor(zs, lambda name, config: github).sync()
    assert [(c.action, c.source) for c in report.changes] == [("retired", "google_drive")]


def test_only_one_sync_at_a_time(zs, ingest):
    assert zs.state.acquire_lock("sync", "someone-else", 60)
    with pytest.raises(SyncBusy):
        ingest.sync()
    zs.state.release_lock("sync", "someone-else")
    ingest.sync()


def test_lock_from_a_dead_process_expires(zs, ingest):
    assert zs.state.acquire_lock("sync", "crashed", -1)  # already expired
    ingest.sync()


def test_unknown_source_filter(ingest):
    with pytest.raises(ValueError, match="linear"):
        ingest.sync(only=["linear"])


def test_report_round_trips(drive, ingest):
    drive.put("a", "A", "alpha")
    report = ingest.sync()
    again = SyncReport.from_json(report.to_json())
    assert again.summary() == report.summary() and again.ok


def doc(source, location="", labels=()):
    return SourceDocument("x", source, "T", "t", "u", "2026", "v", location, list(labels))


def test_confidential_rules():
    rules = [
        ConfidentialRule(google_drive="Company/Finance"),
        ConfidentialRule(linear_label="HR"),
        ConfidentialRule(github="acme/secret"),
    ]
    assert is_confidential(doc("google_drive", "Company/Finance"), rules)
    assert is_confidential(doc("google_drive", "Company/Finance/Payroll"), rules)
    assert not is_confidential(doc("google_drive", "Company/FinanceTeam"), rules)
    assert is_confidential(doc("linear", "Product", ["hr"]), rules)
    assert not is_confidential(doc("linear", "Product", ["bug"]), rules)
    assert is_confidential(doc("github", "Acme/Secret"), rules)
    assert not is_confidential(doc("github", "acme/app"), rules)


def test_config_takes_credentials_from_env(monkeypatch):
    from zerostore.config import parse_config

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_x")
    cfg = parse_config(
        {
            "sources": {
                "github": {"repos": ["acme/app"], "token": "${GITHUB_TOKEN}"},
                "linear": {"teams": ["Product"], "api_key": "${LINEAR_API_KEY}"},
            }
        }
    )
    assert cfg.sources.github.token == "ghp_x" and cfg.sources.linear.api_key == "lin_x"
    assert isinstance(cfg.sources.linear, LinearSource)
