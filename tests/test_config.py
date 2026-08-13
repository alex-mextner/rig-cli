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


def test_lint_rule_policy_deep_merges_global_and_repo_layers(tmp_path, monkeypatch):
    """The central lint-policy contract: repo Rig refines, rather than replaces, global policy."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _w(
        config.global_config_path(),
        "version: 1\n"
        "linters:\n"
        "  rules:\n"
        "    all: true\n"
        "    disable: [anti-slop/no-module-mocking]\n"
        "    severity:\n"
        "      anti-slop/no-reflect-get: warn\n",
    )
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "linters:\n"
        "  rules:\n"
        "    disable: [anti-slop/no-runtime-typeof]\n"
        "    severity:\n"
        "      anti-slop/no-reflect-get: error\n",
    )

    loaded = config.load(repo)
    rules = loaded.data["linters"]["rules"]
    assert rules["all"] is True
    # Lists are repository decisions and therefore replace the global list.
    assert rules["disable"] == ["anti-slop/no-runtime-typeof"]
    # Nested maps deep-merge, with the repository's value winning on collision.
    assert rules["severity"]["anti-slop/no-reflect-get"] == "error"


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


def test_load_round_trips_valid_scripts_and_dev_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "scripts:\n"
        "  test: uv run pytest\n"
        "  custom:\n"
        "    cmd: ./scripts/custom.sh\n"
        "dev:\n"
        "  server:\n"
        "    script: test\n"
        "    ports: [5173]\n",
    )

    loaded = config.load(repo)

    assert loaded.data["scripts"]["custom"]["cmd"] == "./scripts/custom.sh"
    assert loaded.data["dev"]["server"]["ports"] == [5173]


def test_load_rejects_unknown_scripts_key(tmp_path, monkeypatch):
    # scripts was a loose accept-and-preserve pass-through before the rich dev-server schema;
    # it is now strict like every other block — an unknown key is REJECTED, not silently
    # preserved.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "scripts:\n"
        "  test: uv run pytest\n"
        "  custom:\n"
        "    cmd: ./scripts/custom.sh\n"
        "    unexpected: [still, preserved]\n",
    )

    with pytest.raises(config.ConfigError, match=r"unknown scripts\.custom key: unexpected"):
        config.load(repo)


def test_load_rejects_unknown_dev_key(tmp_path, monkeypatch):
    # dev was a loose accept-and-preserve pass-through before the rich dev-server schema; it
    # is now strict like every other block — an unknown key is REJECTED, not silently
    # preserved.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    _w(
        repo / "rig.yaml",
        "version: 1\n"
        "dev:\n"
        "  servver:\n"
        "    typo_owned_by_dev_helper: true\n",
    )

    with pytest.raises(config.ConfigError, match=r"unknown dev key: servver"):
        config.load(repo)


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
        ({"version": 1, "scripts": {"test": {"command": "pytest"}}}, "scripts.test.command", "unknown scripts.test key"),
        ({"version": 1, "dev": {"serve": {"script": "server"}}}, "dev.serve", "unknown dev key"),
        ({"version": 1, "dev": {"server": {"command": "vite"}}}, "dev.server.command", "unknown dev.server key"),
        ({"version": 1, "dev": {"e2e": {"command": "playwright"}}}, "dev.e2e.command", "unknown dev.e2e key"),
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
    assert "harness.aut_mode" in block
    assert "/properties/harness/properties/aut_mode" in block
