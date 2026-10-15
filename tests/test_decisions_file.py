"""decisions.md: approved proposals, read back as a source."""

from __future__ import annotations

from zerostore.connectors.decisions import DOC_ID, DecisionsConnector, append_decision


def test_append_and_read_back(tmp_path):
    path = tmp_path / "sub" / "decisions.md"
    connector = DecisionsConnector(path)
    assert connector.list_items() == []
    append_decision(path, "Refunds are allowed within 30 days.", "khoi", "user: make it\n30 days")
    append_decision(path, "The Pro plan costs $29.", "the person using cursor")
    text = path.read_text()
    assert text.startswith("# Approved decisions")
    assert text.count("## ") == 2 and "> Context: user: make it 30 days" in text
    [ref] = connector.list_items()
    doc = connector.fetch(ref)
    assert doc.doc_id == DOC_ID and doc.source == "decisions" and "$29" in doc.text
    assert doc.url.startswith("file://")
    before = ref.version
    append_decision(path, "Support hours are 9 to 5.", "khoi")
    assert connector.list_items()[0].version != before


def test_concurrent_approvals_keep_every_fact(tmp_path):
    import threading

    path = tmp_path / "decisions.md"
    threads = [
        threading.Thread(target=append_decision, args=(path, f"Fact number {i}.", "khoi")) for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = path.read_text()
    assert text.count("# Approved decisions") == 1
    assert all(f"Fact number {i}." in text for i in range(20))
