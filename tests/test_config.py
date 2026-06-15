"""Config cascade + schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config


def _w(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def test_repo_overrides_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(
        config.global_config_path(),
        "version: 1\ndefaults: {on_conflict: skip}\nskills: {enabled: false}\n",
    )
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "defaults: {on_conflict: backup}\nskills: {enabled: true}\n")

    loaded = config.load(repo)
    # per-repo wins for overlapping keys
    assert loaded.data["defaults"]["on_conflict"] == "backup"
    assert loaded.data["skills"]["enabled"] is True
    # both layers recorded
    assert any(layer.startswith("global:") for layer in loaded.layers)
    assert any(layer.startswith("repo:") for layer in loaded.layers)


def test_deep_merge_keeps_nonoverlapping_global_keys(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    _w(config.global_config_path(), "version: 1\ndefaults: {skills_target: ~/g, on_conflict: backup}\n")
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "defaults: {ci_target: .github/workflows}\n")
    loaded = config.load(repo)
    # global key survives because the repo dict merges, not replaces
    assert loaded.data["defaults"]["skills_target"] == "~/g"
    assert loaded.data["defaults"]["ci_target"] == ".github/workflows"


def test_explicit_config_replaces_repo_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "skills: {enabled: false}\n")
    explicit = tmp_path / "other.yaml"
    _w(explicit, "skills: {enabled: true}\n")
    loaded = config.load(repo, explicit_config=explicit)
    assert loaded.data["skills"]["enabled"] is True


def test_validate_rejects_unknown_top_key():
    with pytest.raises(config.ConfigError, match="unknown top-level key"):
        config.validate({"version": 1, "bogus": 1})


def test_validate_ignores_legacy_scope():
    # `scope` was removed (location-based cascade); a legacy key is tolerated, not rejected.
    config.validate({"version": 1, "scope": "everywhere"})


def test_load_drops_legacy_scope(tmp_path, monkeypatch):
    # a committed rig.yaml that still carries the removed `scope` key loads fine, and the key
    # is dropped from the result so it never lingers/re-serializes or reads as a live setting.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nscope: both\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert "scope" not in loaded.data


def test_validate_rejects_bad_on_conflict():
    with pytest.raises(config.ConfigError, match="on_conflict"):
        config.validate({"version": 1, "defaults": {"on_conflict": "nuke"}})


def test_validate_rejects_bad_ci_tier():
    with pytest.raises(config.ConfigError, match="tier"):
        config.validate({"version": 1, "ci": {"items": {"x": {"tier": "loud"}}}})


def test_validate_rejects_unsupported_version():
    with pytest.raises(config.ConfigError, match="version"):
        config.validate({"version": 2})


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path, explicit_config=tmp_path / "nope.yaml")


def test_invalid_yaml_wrapped_as_configerror(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\n  bad: : indent\n:::\n")
    with pytest.raises(config.ConfigError, match="invalid YAML"):
        config.load(repo)


# ── harness block ────────────────────────────────────────────────────────────────
def test_validate_accepts_harness_block():
    # a well-formed harness block passes (claude-code is the supported kind)
    config.validate({"version": 1, "harness": {"kind": "claude-code", "auto_mode": True}})


def test_validate_rejects_unknown_harness_kind():
    with pytest.raises(config.ConfigError, match="harness.kind"):
        config.validate({"version": 1, "harness": {"kind": "bogus-harness"}})


def test_validate_rejects_reserved_harness_kind_with_clear_message():
    # opencode is documented but not implemented → fail closed with a helpful message
    with pytest.raises(config.ConfigError, match="not implemented"):
        config.validate({"version": 1, "harness": {"kind": "opencode", "auto_mode": True}})


def test_validate_rejects_non_bool_auto_mode():
    with pytest.raises(config.ConfigError, match="auto_mode"):
        config.validate({"version": 1, "harness": {"auto_mode": "yes"}})


def test_validate_accepts_hook_bridge_block():
    config.validate({"version": 1, "harness": {
        "kind": "claude-code", "hook_bridge": {"enabled": True, "python": "python3.12"}}})
    config.validate({"version": 1, "harness": {"hook_bridge": {"enabled": False}}})


def test_validate_rejects_non_bool_hook_bridge_enabled():
    with pytest.raises(config.ConfigError, match="hook_bridge.enabled"):
        config.validate({"version": 1, "harness": {"hook_bridge": {"enabled": "yes"}}})


def test_validate_rejects_non_string_hook_bridge_python():
    with pytest.raises(config.ConfigError, match="hook_bridge.python"):
        config.validate({"version": 1, "harness": {"hook_bridge": {"python": 3}}})


def test_validate_rejects_non_mapping_hook_bridge():
    with pytest.raises(config.ConfigError, match="hook_bridge must be a mapping"):
        config.validate({"version": 1, "harness": {"hook_bridge": "on"}})


# ── skill harness-link knobs ───────────────────────────────────────────────────────
def test_validate_accepts_skill_harness_link_knobs():
    config.validate(
        {"version": 1, "skills": {"harness_link": True, "harness_skill_dir": "~/.claude/skills"}}
    )
    config.validate({"version": 1, "skills": {"harness_link": False}})


def test_validate_rejects_non_bool_harness_link():
    with pytest.raises(config.ConfigError, match="harness_link"):
        config.validate({"version": 1, "skills": {"harness_link": "yes"}})


def test_validate_rejects_non_string_harness_skill_dir():
    with pytest.raises(config.ConfigError, match="harness_skill_dir"):
        config.validate({"version": 1, "skills": {"harness_skill_dir": 42}})
