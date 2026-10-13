from zerostore.agents import rules
from zerostore.mcp.descriptions import AGENT_RULE


def test_add_to_new_file_and_remove_deletes_it(tmp_path):
    path = tmp_path / "CLAUDE.md"
    assert rules.add_rule(path)
    assert path.read_text() == f"<!-- zerostore -->\n{AGENT_RULE}\n<!-- /zerostore -->\n"
    assert rules.remove_rule(path)
    assert not path.exists()


def test_add_is_written_once(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Agents\nBe nice.")
    assert rules.add_rule(path)
    assert not rules.add_rule(path)
    assert path.read_text().count(AGENT_RULE) == 1


def test_remove_restores_the_users_text(tmp_path):
    path = tmp_path / ".cursorrules"
    original = "Use tabs.\n\nPrefer small PRs.\n"
    path.write_text(original)
    rules.add_rule(path)
    assert rules.remove_rule(path)
    assert path.read_text() == original
    assert not rules.remove_rule(path)


def test_remove_from_the_middle(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("top\n")
    rules.add_rule(path)
    path.write_text(path.read_text() + "\nbottom\n")
    rules.remove_rule(path)
    assert path.read_text() == "top\n\nbottom\n"


def test_detect_agent_files(tmp_path):
    assert rules.detect_agent_files(tmp_path) == []
    (tmp_path / "AGENTS.md").write_text("x")
    (tmp_path / "CLAUDE.md").write_text("y")
    assert sorted(p.name for p in rules.detect_agent_files(tmp_path)) == ["AGENTS.md", "CLAUDE.md"]


def test_half_a_block_is_reported_not_mangled(tmp_path):
    import pytest

    path = tmp_path / "CLAUDE.md"
    path.write_text("keep me\n<!-- zerostore -->\nCheck ZeroStore.\n")
    with pytest.raises(ValueError, match="closing"):
        rules.remove_rule(path)
    assert path.read_text() == "keep me\n<!-- zerostore -->\nCheck ZeroStore.\n"
