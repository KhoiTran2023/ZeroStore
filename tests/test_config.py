from pathlib import Path

import pytest

from zerostore.config import ConfigError, load_config, parse_config


def test_only_sources_is_required():
    cfg = parse_config({"sources": {}})
    assert cfg.sync.schedule == "0 6 * * *"
    assert cfg.review.auto_apply_at == 0.80
    assert cfg.retrieval.max_facts == 5
    assert cfg.retrieval.answer_cutoff == 0.50
    assert cfg.servers.host == "localhost"
    assert cfg.sources.enabled() == []


def test_missing_sources_is_an_error():
    with pytest.raises(ConfigError, match="sources"):
        parse_config({"sync": {"schedule": "0 6 * * *"}})


def test_empty_sources_key_is_allowed():
    assert parse_config({"sources": None}).sources.enabled() == []


def test_full_example_parses(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    cfg = load_config(Path(__file__).parent.parent / "zerostore.example.yaml")
    assert cfg.sources.enabled() == ["google_drive", "github", "linear"]
    assert cfg.sources.github.repos == ["acme/app"]
    assert cfg.ai.llm_api_key == "sk-test"
    assert cfg.ai.jev_api_key is None  # unset optional key counts as missing
    assert cfg.confidential[1].linear_label == "hr"
    assert cfg.sources.github.token == "ghp_test"
    assert cfg.sources.linear.api_key is None


def test_bad_cron_is_rejected():
    with pytest.raises(ConfigError, match="sync.schedule"):
        parse_config({"sources": {}, "sync": {"schedule": "0 25 * * *"}})


def test_env_inside_longer_string_must_exist(monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    with pytest.raises(ConfigError, match="NOPE_NOT_SET"):
        parse_config({"sources": {}, "servers": {"vectordb": "s3://bucket/${NOPE_NOT_SET}/facts"}})


@pytest.mark.parametrize(
    "bad, where",
    [
        ({"sync": {"schedule": "daily"}}, "sync.schedule"),
        ({"review": {"auto_apply_at": 1.5}}, "review.auto_apply_at"),
        ({"retrieval": {"rerank_with": "magic"}}, "retrieval.rerank_with"),
        ({"surprise": True}, "surprise"),
        ({"confidential": [{"google_drive": "A", "linear_label": "b"}]}, "confidential"),
        ({"servers": {"statedb": "postgresql://x"}}, "servers.statedb"),
    ],
)
def test_invalid_settings_name_the_problem(bad, where):
    with pytest.raises(ConfigError, match=where.replace(".", r"\.")):
        parse_config({"sources": {}, **bad})


def test_bad_github_repo_format():
    with pytest.raises(ConfigError, match="owner/name"):
        parse_config({"sources": {"github": {"repos": ["just-a-name"]}}})


def test_paths_expand_home_but_keep_s3(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = parse_config({"sources": {}, "servers": {"vectordb": "~/x.lance"}})
    assert cfg.servers.vectordb_uri() == str(tmp_path / "x.lance")
    cfg = parse_config({"sources": {}, "servers": {"vectordb": "s3://bucket/facts"}})
    assert cfg.servers.vectordb_uri() == "s3://bucket/facts"


def test_missing_file_points_to_init(tmp_path):
    with pytest.raises(ConfigError, match="zerostore init"):
        load_config(tmp_path / "nope.yaml")
