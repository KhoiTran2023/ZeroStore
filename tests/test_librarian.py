"""The update layer: noise, topics, confidence, the review queue, superseding and deletions."""

from __future__ import annotations

import pytest
from fakes import FakeConnector, FakeEngine

from zerostore.config import ConfidentialRule, GitHubSource, GoogleDriveSource
from zerostore.librarian.ingestion import Ingestor
from zerostore.librarian.review import ReviewError, decide, describe

PRICE_2025 = "# Pro plan\n\nThe Pro plan costs $20 per month."
PRICE_2026 = "# Pro plan\n\nThe Pro plan costs $29 per month."


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def drive():
    return FakeConnector("google_drive")


@pytest.fixture
def github():
    return FakeConnector("github")


@pytest.fixture
def ingest(zs, drive, github, engine):
    zs.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    zs.config.sources.github = GitHubSource(repos=["acme/app"])
    connectors = {"google_drive": drive, "github": github}
    return Ingestor(zs, lambda name, config: connectors[name], judge=engine)


def texts(zs, query="pro plan price", confidential=True):
    return " ".join(r["text"] for r in zs.facts.search(query, include_confidential=confidential))


def pending(zs):
    return zs.state.review_items("pending")


# Ingestion: noise and topics ------------------------------------------------


def test_noise_is_left_out_and_the_rest_is_tagged(zs, drive, ingest, engine):
    engine.keep_rules = {"lunch": 0.03}
    drive.put("pricing", "Pricing 2026", PRICE_2026 + "\n\n# Lunch\n\nLunch at noon?")
    report = ingest.sync()
    assert report.summary() == "1 change · Pricing 2026 · Jev confidence 0.97"
    assert report.changes[0].note == "1 noise chunk left out"
    chunks = zs.facts.chunks_of("google_drive:pricing")
    assert [c["text"] for c in chunks] == ["Pro plan\nThe Pro plan costs $29 per month."]
    assert chunks[0]["topics"] == ["pricing and plans"]
    assert zs.state.documents()["google_drive:pricing"]["chunks"] == 1


def test_a_document_that_is_only_noise_is_skipped_and_not_refetched(zs, drive, ingest, engine):
    engine.keep_rules = {"lunch": 0.02}
    drive.put("chat", "Team chat", "Lunch at noon?")
    report = ingest.sync()
    assert report.changes == [] and report.sources[0].skipped == 1
    assert zs.facts.count() == 0
    assert zs.state.documents()["google_drive:chat"]["chunks"] == 0
    drive.fetched.clear()
    ingest.sync()
    assert drive.fetched == []
    # Deleting it later is not news: nothing was ever served from it.
    del drive.docs["chat"]
    drive.put("other", "Other", "The refund window is 30 days.")
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["added"]
    assert "google_drive:chat" not in zs.state.documents()


def test_without_an_engine_nothing_is_judged(zs, drive):
    zs.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    ingest = Ingestor(zs, lambda name, config: drive, judge=None)
    drive.put("chat", "Team chat", "Lunch at noon?")
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["added"]
    assert report.changes[0].confidence is None
    assert report.summary() == "1 change · Team chat"


# Confidence and the review queue --------------------------------------------


def test_a_low_confidence_change_waits_and_the_old_version_stays_live(zs, drive, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    engine.keep_default = 0.62  # the engine isn't sure the new text is lasting knowledge
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["review"]
    assert report.summary() == "No changes · 1 waiting for review"
    assert "$20" in texts(zs) and "$29" not in texts(zs)
    [item] = pending(zs)
    assert item["kind"] == "change" and item["confidence"] == pytest.approx(0.62)
    view = describe(item)
    assert view["document"] == "Pricing 2026" and "$29" in view["preview"]
    # The same version isn't queued twice.
    drive.fetched.clear()
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2b")
    ingest.sync()
    assert len(pending(zs)) == 1

    outcome = decide(zs, item["id"], True, "khoi")
    assert outcome.status == "approved"
    assert "$29" in texts(zs) and "$20" not in texts(zs)
    [row] = zs.state.audit_since("2000-01-01", actions=("document_updated",))
    assert row["confidence"] == pytest.approx(0.62) and row["engine"] == "Jev"
    assert "approved by khoi" in row["detail"]
    with pytest.raises(ReviewError):
        decide(zs, item["id"], True, "khoi")


def test_a_rejected_version_is_not_proposed_again(zs, drive, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    ingest.sync()
    [item] = pending(zs)
    decide(zs, item["id"], False, "khoi")
    assert "$20" in texts(zs)
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v3")  # re-saved, same text
    assert ingest.sync().changes == [] and pending(zs) == []
    engine.keep_default = 0.97
    drive.put("pricing", "Pricing 2026", "# Pro plan\n\nThe Pro plan costs $25 per month.", version="v4")
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["updated"] and "$25" in texts(zs)


def test_rejecting_a_new_document_keeps_it_out(zs, drive, ingest, engine):
    engine.keep_default = 0.55
    drive.put("draft", "Draft memo", "We might move the launch date to March.")
    ingest.sync()
    [item] = pending(zs)
    assert describe(item)["change"] == "added"
    decide(zs, item["id"], False, "khoi")
    assert zs.facts.count() == 0
    drive.fetched.clear()
    drive.put("draft", "Draft memo", "We might move the launch date to March.", version="v2")
    ingest.sync()
    assert pending(zs) == []


def test_a_newer_version_replaces_one_waiting_for_review(zs, drive, ingest, engine):
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    [first] = pending(zs)
    engine.keep_default = 0.97
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["added"]
    assert pending(zs) == []
    closed = zs.state.get_review_item(first["id"])
    assert closed["status"] == "rejected" and "newer version" in closed["decided_by"]


def test_an_engine_failure_changes_nothing(zs, drive, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    engine.fail = "keep"
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    report = ingest.sync()
    assert report.changes == []
    assert "couldn't be judged" in report.sources[0].failed[0]
    assert "$20" in texts(zs)
    engine.fail = None
    assert [c.action for c in ingest.sync().changes] == ["updated"]


# Newer wins across documents ---------------------------------------------------


def test_a_newer_document_hides_the_old_fact_until_it_is_retired(zs, drive, github, ingest):
    drive.put("old", "Pricing 2025", PRICE_2025, updated="2025-01-10")
    ingest.sync()
    github.put("new", "pricing.md", PRICE_2026, updated="2026-10-01", location="acme/app")
    report = ingest.sync()
    assert report.changes[0].note == "replaces facts in Pricing 2025"
    assert "$29" in texts(zs) and "$20" not in texts(zs)
    [hidden] = zs.facts.chunks_of("google_drive:old")
    assert hidden["superseded_by"] == "github:new"
    # The newer document goes away: the older fact is the best answer again.
    del github.docs["new"]
    github.put("other", "README.md", "Install with pipx.", location="acme/app")
    ingest.sync()
    assert "$20" in texts(zs)


def test_an_older_document_does_not_hide_a_newer_one(zs, drive, github, ingest):
    github.put("new", "pricing.md", PRICE_2026, updated="2026-10-01", location="acme/app")
    ingest.sync()
    drive.put("old", "Pricing 2025", PRICE_2025, updated="2025-01-10")
    report = ingest.sync()
    assert report.changes[0].note == "older than pricing.md"
    assert "$29" in texts(zs) and "$20" not in texts(zs)


def test_a_confidential_document_never_hides_a_public_fact(zs, drive, ingest):
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    drive.put("public", "Pricing", PRICE_2025, updated="2025-01-10")
    ingest.sync()
    drive.put("board", "Board notes", PRICE_2026, location="Company/Finance")
    ingest.sync()
    assert "$20" in texts(zs, confidential=False)
    assert zs.facts.chunks_of("google_drive:public")[0]["superseded_by"] == ""


# Deleted files -----------------------------------------------------------------


def test_a_moved_file_is_recognised(zs, drive, github, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2026)
    ingest.sync()
    del drive.docs["pricing"]
    drive.put("keep", "Other", "The refund window is 30 days.")
    github.put(
        "pricing", "pricing.md", PRICE_2026 + "\n\nAnnual billing gets two months free.", location="acme/app"
    )

    def moved(deleted, candidates):
        index = next(i for i, c in enumerate(candidates) if c.title == "pricing.md")
        return (f"moved_to_{index}", 0.93)

    engine.delete_answer = moved
    report = ingest.sync()
    assert {(c.action, c.title) for c in report.changes} >= {
        ("added", "pricing.md"),
        ("moved", "Pricing 2026"),
    }
    deleted, candidates = engine.seen_delete[0]
    assert deleted.title == "Pricing 2026" and "$29" in deleted.excerpt
    assert any(c.added_this_sync for c in candidates)
    [row] = zs.state.audit_since("2000-01-01", actions=("document_moved",))
    assert "moved to pricing.md" in row["detail"]
    assert "google_drive:pricing" not in zs.state.documents()


def test_a_deleted_file_can_be_kept(zs, drive, ingest, engine):
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    drive.put("other", "Other", "Install with pipx.")
    ingest.sync()
    del drive.docs["policy"]
    engine.delete_answer = ("keep", 0.9)
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["kept"]
    assert "30 days" in texts(zs, "refund window")
    engine.calls.clear()
    assert ingest.sync().changes == [] and engine.calls["on_delete"] == 0
    # It comes back: it's an ordinary live document again.
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    ingest.sync()
    assert zs.state.documents()["google_drive:policy"]["source_deleted_at"] is None


def test_an_unclear_deletion_waits_for_a_person(zs, drive, ingest, engine):
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    drive.put("other", "Other", "Install with pipx.")
    ingest.sync()
    del drive.docs["policy"]
    engine.delete_answer = ("retire", 0.55)  # leaning retire, but not sure enough
    report = ingest.sync()
    assert [c.action for c in report.changes] == ["review_delete"]
    assert "30 days" in texts(zs, "refund window")
    [item] = pending(zs)
    assert describe(item)["suggestion"] == "retire"
    engine.calls.clear()
    ingest.sync()
    assert engine.calls["on_delete"] == 0 and len(pending(zs)) == 1
    decide(zs, item["id"], True, "khoi")
    assert "30 days" not in texts(zs, "refund window")


def test_rejecting_a_deletion_keeps_the_facts(zs, drive, ingest, engine):
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    drive.put("other", "Other", "Install with pipx.")
    ingest.sync()
    del drive.docs["policy"]
    engine.delete_answer = ("unsure", 0.9)
    ingest.sync()
    [item] = pending(zs)
    decide(zs, item["id"], False, "khoi")
    assert "30 days" in texts(zs, "refund window")
    assert zs.state.documents()["google_drive:policy"]["source_deleted_at"]


def test_a_deletion_the_engine_cannot_judge_keeps_the_facts(zs, drive, ingest, engine):
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    drive.put("other", "Other", "Install with pipx.")
    ingest.sync()
    del drive.docs["policy"]
    engine.fail = "on_delete"
    report = ingest.sync()
    assert report.changes == [] and "deletion couldn't be judged" in report.sources[0].failed[0]
    assert "30 days" in texts(zs, "refund window")


def test_a_file_that_comes_back_closes_its_deletion_review(zs, drive, ingest, engine):
    drive.put("policy", "Refund policy", "The refund window is 30 days.")
    drive.put("other", "Other", "Install with pipx.")
    ingest.sync()
    saved = drive.docs.pop("policy")
    engine.delete_answer = ("unsure", 0.9)
    ingest.sync()
    [item] = pending(zs)
    drive.docs["policy"] = saved
    ingest.sync()
    assert pending(zs) == []
    assert "back in the source" in zs.state.get_review_item(item["id"])["decided_by"]
    assert "30 days" in texts(zs, "refund window")


# Found in review: access levels, stale approvals, failures ------------------------


def test_a_newer_confidential_document_never_hides_an_older_public_fact(zs, drive, ingest):
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    drive.put("board", "Secret board notes", PRICE_2026, location="Company/Finance")
    ingest.sync()
    drive.put("public", "Pricing", PRICE_2025, updated="2025-01-10")
    report = ingest.sync()
    assert "$20" in texts(zs, confidential=False)
    assert report.changes[0].note == ""
    public_rows = zs.state.audit_since("2000-01-01", include_confidential=False)
    assert all("Secret" not in (row["detail"] or "") for row in public_rows)


def test_a_public_deletion_is_never_explained_with_a_confidential_document(zs, drive, ingest, engine):
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    drive.put("pricing", "Pricing", PRICE_2026)
    ingest.sync()
    del drive.docs["pricing"]
    drive.put("board", "Secret board notes", PRICE_2026, location="Company/Finance")
    ingest.sync()
    _, candidates = engine.seen_delete[0]
    assert all(c.title != "Secret board notes" for c in candidates)


def test_a_waiting_change_follows_new_confidentiality_rules(zs, drive, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2025, location="Company/Finance")
    ingest.sync()
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2", location="Company/Finance")
    ingest.sync()
    [old] = pending(zs)
    zs.config.confidential = [ConfidentialRule(google_drive="Company/Finance")]
    with pytest.raises(ReviewError, match="confidentiality rules changed"):
        decide(zs, old["id"], True, "khoi")
    ingest.sync()  # the rules changed, so every document is checked again
    [new] = pending(zs)
    assert new["id"] != old["id"] and new["payload"]["record"]["confidential"] == 1
    decide(zs, new["id"], True, "khoi")
    assert "$29" in texts(zs, confidential=True) and "$29" not in texts(zs, confidential=False)


def test_an_old_approval_never_hides_a_fact_that_changed_since(zs, drive, github, ingest, engine):
    drive.put("old", "Pricing 2025", PRICE_2025, updated="2025-01-10")
    ingest.sync()
    engine.keep_default = 0.6
    github.put("new", "pricing.md", PRICE_2026, location="acme/app")
    ingest.sync()
    [item] = pending(zs)
    assert item["payload"]["supersedes"][0]["doc_id"] == "google_drive:old"
    engine.keep_default = 0.97
    drive.put(
        "old",
        "Pricing 2025",
        "# Refunds\n\nThe refund window is 30 days.",
        version="v2",
        updated="2025-02-01",
    )
    ingest.sync()
    decide(zs, item["id"], True, "khoi")
    assert "30 days" in texts(zs, "refund window")


def test_a_source_that_goes_back_closes_the_waiting_change(zs, drive, ingest, engine):
    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    ingest.sync()
    [item] = pending(zs)
    drive.fetched.clear()
    ingest.sync()
    assert drive.fetched == []  # waiting versions aren't fetched again every sync
    drive.put("pricing", "Pricing 2026", PRICE_2025, version="v3")
    ingest.sync()
    assert pending(zs) == []
    assert "went back" in zs.state.get_review_item(item["id"])["decided_by"]


def test_approvals_wait_for_a_running_sync(zs, drive, ingest, engine):
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", PRICE_2026)
    ingest.sync()
    [item] = pending(zs)
    assert zs.state.acquire_lock("sync", "someone-else", 60)
    with pytest.raises(ReviewError, match="sync is running"):
        decide(zs, item["id"], True, "khoi")
    zs.state.release_lock("sync", "someone-else")
    decide(zs, item["id"], True, "khoi")


def test_an_embedding_failure_skips_the_document_not_the_sync(zs, drive, ingest, monkeypatch):
    from zerostore.store.embeddings import EmbeddingError

    drive.put("pricing", "Pricing 2026", PRICE_2025)
    ingest.sync()
    original = zs.facts.embedder.embed

    def flaky(texts_):
        if any("$29" in t for t in texts_):
            raise EmbeddingError("openai embeddings returned 500")
        return original(texts_)

    monkeypatch.setattr(zs.facts.embedder, "embed", flaky)
    drive.put("pricing", "Pricing 2026", PRICE_2026, version="v2")
    drive.put("refunds", "Refunds", "The refund window is 30 days.")
    report = ingest.sync()
    assert [c.title for c in report.changes] == ["Refunds"]
    assert "embeddings returned 500" in report.sources[0].failed[0]
    assert "$20" in texts(zs)
