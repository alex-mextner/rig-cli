"""Repo-list resolution: --repo flag > config file > built-in default.

``explicit=True`` marks a user-supplied ``--config PATH`` (see ``command.py``'s
``explicit=args.config is not None`` — NOT truthiness, so an odd-but-real
``--config ""`` still counts as explicit); every test in this file that does NOT pass
``explicit=True`` is exercising the "just checking whatever's at this path" case (the
implicit default-location lookup), where a missing/malformed/empty file is a silent,
convenience fall back to :data:`DEFAULT_REPOS` — never a real ``--config`` flag."""

from __future__ import annotations

import pytest

from riglib.daily import config as daily_config
from riglib.daily.config import DEFAULT_REPOS, load_repos
from riglib.errors import ConfigError


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


# ── explicit `--config PATH` (fail-closed) — codex review P1 finding ───────────────────


def test_explicit_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_repos(config_path=tmp_path / "missing.yaml", explicit=True)


def test_explicit_config_path_is_a_directory_raises_with_accurate_message(tmp_path):
    """A path that EXISTS but isn't a regular file (e.g. `--config` pointed at a
    directory, or the `Path("").expanduser()` == cwd edge case) must not be reported as
    "does not exist" — that's a different, misleading diagnostic (review finding)."""
    directory = tmp_path / "some-dir"
    directory.mkdir()
    with pytest.raises(ConfigError, match="is not a regular file"):
        load_repos(config_path=directory, explicit=True)


def test_explicit_config_malformed_yaml_raises(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("not: valid: yaml: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be read/parsed"):
        load_repos(config_path=config, explicit=True)


def test_explicit_config_non_mapping_yaml_raises(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a YAML mapping"):
        load_repos(config_path=config, explicit=True)


def test_explicit_config_empty_repos_list_raises(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no valid `repos:` list"):
        load_repos(config_path=config, explicit=True)


def test_explicit_config_missing_repos_key_raises(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("other: stuff\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no valid `repos:` list"):
        load_repos(config_path=config, explicit=True)


def test_explicit_config_valid_repos_still_works(tmp_path):
    config = tmp_path / "daily.yaml"
    config.write_text("repos:\n  - a/b\n  - c/d\n", encoding="utf-8")
    assert load_repos(config_path=config, explicit=True) == ["a/b", "c/d"]


def test_explicit_config_permission_error_raises_config_error_not_raw_crash(monkeypatch, tmp_path):
    """`Path.exists()`/`Path.is_file()` only swallow a narrow errno set (ENOENT, ENOTDIR,
    EBADF, ELOOP) on some Python versions — `EACCES` (an unreadable parent directory) can
    propagate a raw `PermissionError` instead of returning `False`. That must still
    surface as the module's structured `ConfigError` contract, not an unhandled crash
    (review finding, round 7)."""

    def _raises(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(daily_config.Path, "exists", _raises)
    with pytest.raises(ConfigError, match="could not be checked"):
        load_repos(config_path=tmp_path / "daily.yaml", explicit=True)


def test_default_location_permission_error_falls_back_to_default_silently(monkeypatch, tmp_path):
    """Same probe failure as above, but at the DEFAULT (non-explicit) location: must keep
    the "convenience no-op" promise — fall back to :data:`DEFAULT_REPOS`, never crash."""

    def _raises(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(daily_config.Path, "exists", _raises)
    assert load_repos(config_path=tmp_path / "daily.yaml") == list(DEFAULT_REPOS)


def test_explicit_flag_ignored_when_cli_repos_given(tmp_path):
    # --repo wins outright, even over a broken --config — no reason to fail-closed on a
    # config file whose repos are about to be ignored anyway.
    assert load_repos(config_path=tmp_path / "missing.yaml", cli_repos=["x/y"], explicit=True) == ["x/y"]
