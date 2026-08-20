"""Repo-list resolution: --repo flag > config file > built-in default."""

from __future__ import annotations

from riglib.daily.config import DEFAULT_REPOS, load_repos


def test_defaults_to_the_two_real_hyperide_repos_when_nothing_else_set(tmp_path):
    assert load_repos(config_path=tmp_path / "missing.yaml") == list(DEFAULT_REPOS)


def test_cli_repos_win_over_everything(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos:\n  - a/b\n", encoding="utf-8")
    assert load_repos(config_path=config, cli_repos=["x/y"]) == ["x/y"]


def test_config_file_repos_used_when_no_cli_override(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos:\n  - a/b\n  - c/d\n", encoding="utf-8")
    assert load_repos(config_path=config) == ["a/b", "c/d"]


def test_malformed_config_falls_back_to_default(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("not: valid: yaml: [", encoding="utf-8")
    assert load_repos(config_path=config) == list(DEFAULT_REPOS)


def test_empty_repos_list_falls_back_to_default(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos: []\n", encoding="utf-8")
    assert load_repos(config_path=config) == list(DEFAULT_REPOS)


def test_duplicate_cli_repos_are_deduplicated_order_preserving(tmp_path):
    got = load_repos(config_path=tmp_path / "missing.yaml", cli_repos=["a/b", "c/d", "a/b"])
    assert got == ["a/b", "c/d"]


def test_duplicate_config_repos_are_deduplicated_order_preserving(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos:\n  - a/b\n  - c/d\n  - a/b\n", encoding="utf-8")
    assert load_repos(config_path=config) == ["a/b", "c/d"]
