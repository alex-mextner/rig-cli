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


def test_committed_repo_rig_yaml_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = Path(__file__).resolve().parent.parent

    loaded = config.load(repo)

    assert loaded.primary_config_path == repo / "rig.yaml"
    assert any(layer.startswith("repo:") for layer in loaded.layers)


def test_validate_rejects_unknown_top_key():
    with pytest.raises(config.ConfigError, match="unknown top-level key"):
        config.validate({"version": 1, "bogus": 1})


def test_validate_accepts_project_dev_script_blocks():
    config.validate({
        "version": 1,
        "scripts": {"test": "uv run pytest"},
        "dev": {"server": {"script": "server", "ports": [5173]}},
    })


def test_load_preserves_project_dev_script_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "scripts:\n"
        "  test: uv run pytest\n"
        "  custom:\n"
        "    cmd: ./scripts/custom.sh\n"
        "    unexpected: [still, preserved]\n"
        "dev:\n"
        "  servver:\n"
        "    typo_owned_by_dev_helper: true\n",
    )

    loaded = config.load(repo)

    assert loaded.data["scripts"]["custom"]["unexpected"] == ["still", "preserved"]
    assert loaded.data["dev"]["servver"]["typo_owned_by_dev_helper"] is True


@pytest.mark.parametrize("key", ["scripts", "dev"])
def test_validate_rejects_non_mapping_project_dev_blocks(key):
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, key: ["test"]})
    assert ei.value.schema_path == key


# ── roadmap §5: every block REJECTS an unknown key (no silent no-op), with a schema path ──
@pytest.mark.parametrize(
    "doc, schema_path, msg",
    [
        ({"version": 1, "harness": {"aut_mode": True}}, "harness.aut_mode", "unknown harness key"),
        ({"version": 1, "skills": {"enabld": True}}, "skills.enabld", "unknown skills key"),
        ({"version": 1, "defaults": {"on_conflic": "skip"}}, "defaults.on_conflic", "unknown defaults key"),
        ({"version": 1, "git_hooks": {"dispatcher": {"enabld": True}}}, "git_hooks.dispatcher.enabld", "unknown git_hooks.dispatcher key"),
        ({"version": 1, "ci": {"enabld": True}}, "ci.enabld", "unknown ci key"),
        ({"version": 1, "mcp": {"enabld": True}}, "mcp.enabld", "unknown mcp key"),
        ({"version": 1, "agent_hooks": {"enabld": True}}, "agent_hooks.enabld", "unknown agent_hooks key"),
        ({"version": 1, "harness": {"hook_bridge": {"enabld": True}}}, "harness.hook_bridge.enabld", "unknown harness.hook_bridge key"),
        ({"version": 1, "skills": {"universal": {"al": True}}}, "skills.universal.al", "unknown skills.universal key"),
        ({"version": 1, "skills": {"by_type": {"enabl": []}}}, "skills.by_type.enabl", "unknown skills.by_type key"),
        ({"version": 1, "models": {"schedule": {"tim": "12:00"}}}, "models.schedule.tim", "unknown models.schedule key"),
        ({"version": 1, "github": {"ruleset": {"nam": "x"}}}, "github.ruleset.nam", "unknown github.ruleset key"),
        ({"version": 1, "tmux": {"continuum": {"save_intervall": 5}}}, "tmux.continuum.save_intervall", "unknown tmux.continuum key"),
        ({"version": 1, "tmux": {"boot": {"labl": "x"}}}, "tmux.boot.labl", "unknown tmux.boot key"),
        ({"version": 1, "permissions": {"tols": []}}, "permissions.tols", "unknown permissions key"),
        ({"version": 1, "gitignore": {"entres": []}}, "gitignore.entres", "unknown gitignore key"),
        ({"version": 1, "tg_ctl": {"labl": "x"}}, "tg_ctl.labl", "unknown tg_ctl key"),
        ({"version": 1, "agents_md": {"symlnk": True}}, "agents_md.symlnk", "unknown agents_md key"),
    ],
)
def test_validate_rejects_unknown_block_key_with_schema_path(doc, schema_path, msg):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    err = ei.value
    assert msg in err.what
    assert err.schema_path == schema_path
    assert err.fix  # an unknown-key error always offers the accepted keys


def test_validate_rejects_bad_value_with_schema_path():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"auto_mode": "yes"}})
    assert ei.value.schema_path == "harness.auto_mode"
    with pytest.raises(config.ConfigError) as ei2:
        config.validate({"version": 1, "defaults": {"on_conflict": "nuke"}})
    assert ei2.value.schema_path == "defaults.on_conflict"


def test_render_config_error_is_three_part_with_pointer():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"aut_mode": True}})
    block = config.render_config_error(ei.value, color=False)
    assert "error:" in block  # WHAT
    assert "why:" in block and "fix:" in block  # WHY + FIX
    # the schema path is shown both dotted and as a resolvable JSON pointer into the published file
    # (the typo points at the PARENT block node, which exists, not the non-existent typo leaf).
    assert "harness.aut_mode" in block
    assert "schema/rig.schema.json#/properties/harness" in block


def test_git_hooks_dispatcher_now_validated():
    # git_hooks previously had NO validator — a typo'd dispatcher knob was silently ignored.
    # It is now fail-closed, like every other block.
    config.validate({"version": 1, "git_hooks": {"dispatcher": {"enabled": False}}})  # valid
    with pytest.raises(config.ConfigError, match="git_hooks.dispatcher.enabled must be a bool"):
        config.validate({"version": 1, "git_hooks": {"dispatcher": {"enabled": "nope"}}})


def test_open_item_maps_still_accept_arbitrary_names():
    # the strictness must NOT break the open `items`/`fragments` maps (catalog item names).
    config.validate({"version": 1, "ci": {"items": {"secret-scan": {"tier": "block"}, "my-gate": {}}}})
    config.validate({
        "version": 1,
        "mcp": {
            "items": {
                "review": {
                    "command": "review",
                    "args": ["--serve"],
                    "env": {"NODE_ENV": "test"},
                }
            }
        },
    })
    config.validate({"version": 1, "agent_hooks": {"items": {"block-no-verify": {"on_error": "closed"}}}})
    config.validate({"version": 1, "git_hooks": {"dispatcher": {"fragments": {"secret-scan": {"enabled": True}}}}})


def test_fixed_typo_rejected_even_alongside_a_valid_open_map():
    # a real item map must NOT mask a fixed-knob typo in the SAME block — `items` stays valid,
    # `enabld` is rejected (the open map whitelists only its own key, not arbitrary fixed keys).
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "ci": {"items": {"secret-scan": {"tier": "block"}}, "enabld": True}})
    assert ei.value.schema_path == "ci.enabld"


@pytest.mark.parametrize(
    "doc, schema_path",
    [
        ({"version": 1, "skills": {"enabled": "yes"}}, "skills.enabled"),
        ({"version": 1, "ci": {"enabled": "yes"}}, "ci.enabled"),
        ({"version": 1, "ci": {"target": 5}}, "ci.target"),
        ({"version": 1, "agent_hooks": {"all": "no"}}, "agent_hooks.all"),
        ({"version": 1, "mcp": {"enabled": "yes"}}, "mcp.enabled"),
        ({"version": 1, "permissions": {"enabled": "yes"}}, "permissions.enabled"),
    ],
)
def test_open_map_block_fixed_knobs_are_type_checked(doc, schema_path):
    # roadmap §5: the runtime and the JSON schema agree — a bad-typed fixed knob in ci/mcp/skills/
    # agent_hooks/permissions is rejected at runtime, not only by an editor.
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert ei.value.schema_path == schema_path


def test_agent_hooks_target_kind_is_accepted_as_ignored_legacy_key():
    config.validate({"version": 1, "agent_hooks": {"target_kind": "claude-code"}})
    config.validate({"version": 1, "agent_hooks": {"target_kind": "generic"}})


def test_agent_hooks_target_kind_rejects_bad_legacy_value():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "agent_hooks": {"target_kind": "bogus"}})
    assert ei.value.schema_path == "agent_hooks.target_kind"


def test_mcp_non_dict_block_rejected_cleanly():
    # a non-dict mcp must fail with a clean "must be a mapping" (the category check catches it
    # first in validate()), never a nonsensical char-set error from set() over a string.
    with pytest.raises(config.ConfigError, match="mcp.* must be a mapping"):
        config.validate({"version": 1, "mcp": "nope"})
    # and the dedicated validator's own guard is clean when called directly (belt-and-suspenders)
    with pytest.raises(config.ConfigError, match="mcp must be a mapping"):
        config._validate_mcp("nope")


@pytest.mark.parametrize(
    "doc, schema_path",
    [
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"argz": []}}}}, "mcp.items.fake-mcp.argz"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": "nope"}}}, "mcp.items.fake-mcp"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"enabled": "yes"}}}}, "mcp.items.fake-mcp.enabled"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"server": 3}}}}, "mcp.items.fake-mcp.server"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"command": ["node"]}}}}, "mcp.items.fake-mcp.command"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"args": "--serve"}}}}, "mcp.items.fake-mcp.args"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"args": ["ok", 1]}}}}, "mcp.items.fake-mcp.args"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"env": []}}}}, "mcp.items.fake-mcp.env"),
        ({"version": 1, "mcp": {"items": {"fake-mcp": {"env": {"PORT": 3000}}}}}, "mcp.items.fake-mcp.env.PORT"),
    ],
)
def test_mcp_item_specs_are_validated_fail_closed(doc, schema_path):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert ei.value.schema_path == schema_path


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


def test_key_sources_track_layer_provenance(tmp_path, monkeypatch):
    # a key set only in the GLOBAL config maps to the global path; a key the repo sets maps to
    # the repo path. This is what source_for_key() uses to name the right file in an error.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    _w(config.global_config_path(), "version: 1\nmcp: {enabled: true}\nskills: {enabled: true}\n")
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nci: {enabled: false}\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert loaded.key_sources["mcp"] == config.global_config_path()  # global-only key
    assert loaded.key_sources["ci"] == repo / "rig.yaml"  # repo-only key
    assert loaded.key_sources["skills"] == repo / "rig.yaml"  # repo OVERRIDES global
    assert loaded.source_for_key("mcp.items.x") == config.global_config_path()


def test_load_strips_scope_from_key_sources(tmp_path, monkeypatch):
    # the removed legacy `scope` key is dropped from provenance too (not just from data), so a
    # stray `scope:` never lingers as a tracked source. source_for_key falls back when untracked.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(repo / "rig.yaml", "version: 1\nscope: both\nskills: {enabled: false}\n")
    loaded = config.load(repo)
    assert "scope" not in loaded.key_sources
    assert loaded.source_for_key("scope") == loaded.primary_config_path  # fallback, not KeyError


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


def test_validate_rejects_non_string_harness_kind():
    with pytest.raises(config.ConfigError) as ei:
        config.validate({"version": 1, "harness": {"kind": ["codex"]}})
    assert ei.value.schema_path == "harness.kind"


def test_validate_accepts_all_supported_harness_kinds():
    # rig-cli#9: every harness rig knows a skill/instruction discovery convention for is now
    # ACCEPTED in harness.kind (skills-dir harnesses claude-code/opencode + instruction-file
    # harnesses codex/gemini/pi/commandcode). Previously opencode (and the rest) were rejected.
    for kind in ("claude-code", "opencode", "codex", "gemini", "pi", "commandcode"):
        config.validate({"version": 1, "harness": {"kind": kind}})


def test_validate_accepts_additional_harness_kinds():
    config.validate({"version": 1, "harness": {"kind": "claude-code", "kinds": ["codex", "opencode"]}})


def test_validate_rejects_typo_harness_kind_with_supported_list():
    # a typo still fails closed — and the message names the supported kinds so the fix is obvious.
    with pytest.raises(config.ConfigError, match="harness.kind must be one of"):
        config.validate({"version": 1, "harness": {"kind": "claudecode"}})


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
