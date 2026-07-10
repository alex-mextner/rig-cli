"""Global autonomous mode config: schema, routing, plan notes, and permission flows."""

from __future__ import annotations

from riglib import config, config_schema, schema
from riglib.catalog import Catalog
from riglib.cli import main
from riglib.config import LoadedConfig
from riglib.plan import build

import pytest


def _mode_doc(**overrides):
    autonomous = {
        "review_fix": {"enabled": True, "max_iterations": 5, "until": "clean"},
        "decisions": {
            "review_quorum": {"enabled": True, "min_iterations": 2, "min_models": 3},
        },
        "escalation": {
            "framework_skill": "decision-request-discipline",
            "require_parallel_worktree_comparison": True,
        },
        "parallel_worktree_comparison": {"enabled": True, "candidates": 2},
        "development_tools": {
            "allow": ["Bash(dev:*)", "Bash(review:*)", "Bash(task:*)"],
        },
        "parallelism": {
            "max_agents": 4,
            "max_worktrees": 4,
            "reserve_slots": 1,
            "limit_aware": True,
        },
    }
    autonomous.update(overrides)
    return {"version": 1, "mode": {"name": "autonomous", "autonomous": autonomous}}


def test_validate_accepts_autonomous_mode_schema():
    config.validate(_mode_doc())


@pytest.mark.parametrize(
    "doc, schema_path",
    [
        ({"version": 1, "mode": {"name": "manual"}}, "mode.name"),
        ({"version": 1, "mode": {"name": []}}, "mode.name"),
        ({"version": 1, "mode": {"name": {}}}, "mode.name"),
        (_mode_doc(review_fix={"max_iterations": 0}), "mode.autonomous.review_fix.max_iterations"),
        (
            _mode_doc(decisions={"review_quorum": {"min_models": 1}}),
            "mode.autonomous.decisions.review_quorum.min_models",
        ),
        (
            _mode_doc(parallelism={"max_agents": 0}),
            "mode.autonomous.parallelism.max_agents",
        ),
        (
            _mode_doc(development_tools={"allow": ["Bash(dev:*)", 42]}),
            "mode.autonomous.development_tools.allow",
        ),
        (_mode_doc(review_fix={"max_iterations": None}), "mode.autonomous.review_fix.max_iterations"),
        (_mode_doc(review_fix={"enabled": None}), "mode.autonomous.review_fix.enabled"),
        (_mode_doc(review_fix={"until": None}), "mode.autonomous.review_fix.until"),
        (_mode_doc(review_fix={"until": []}), "mode.autonomous.review_fix.until"),
        (_mode_doc(review_fix={"until": {}}), "mode.autonomous.review_fix.until"),
        (_mode_doc(escalation={"framework_skill": None}), "mode.autonomous.escalation.framework_skill"),
        (_mode_doc(parallelism=None), "mode.autonomous.parallelism"),
        (_mode_doc(parallelism={"surprise": True}), "mode.autonomous.parallelism.surprise"),
    ],
)
def test_validate_rejects_bad_autonomous_values_with_schema_path(doc, schema_path):
    with pytest.raises(config.ConfigError) as ei:
        config.validate(doc)
    assert ei.value.schema_path == schema_path


def test_mode_is_global_only_and_exposed_to_setup_registry():
    assert schema.writable_layer_for_category("mode") == schema.GLOBAL
    assert schema.option_for_key("mode.name").default == "standard"
    assert schema.option_for_key("mode.autonomous.review_fix.max_iterations").default == 5
    assert config_schema.block_child_keys("mode.autonomous.parallelism") == {
        "max_agents",
        "max_worktrees",
        "reserve_slots",
        "limit_aware",
    }
    schema_doc = config_schema.json_schema()
    allow_items = schema_doc["properties"]["mode"]["properties"]["autonomous"]["properties"]["development_tools"]["properties"]["allow"]["items"]
    assert allow_items["pattern"] == config_schema.PERMISSION_RULE_JSON_PATTERN


def test_autonomous_mode_adds_plan_notes_and_permission_allow_rules(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / "settings.json"
    loaded = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {
                "settings_path": str(settings),
                "tools": ["git"],
            },
            "mode": _mode_doc()["mode"],
        },
        repo_root=repo,
    )
    plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    notes = "\n".join(plan.notes)
    assert [a.kind for a in plan.actions].count("record_mode") == 1
    assert "autonomous mode: review/fix until clean (max 5 iterations)" in notes
    assert "decisions require review quorum (2 iterations across 3 models)" in notes
    assert "parallel worktree comparison required before escalation (2 candidates)" in notes
    assert "limit-aware parallelism max_agents=4 max_worktrees=4 reserve_slots=1" in notes

    perm = [a for a in plan.actions if a.kind == "provision_permissions"][0]
    assert perm.options["tools"] == ["git"]
    assert perm.options["allow_rules"] == ["Bash(dev:*)", "Bash(review:*)", "Bash(task:*)"]


@pytest.mark.parametrize("mode", [{}, {"name": "standard"}])
def test_standard_mode_does_not_add_autonomous_plan_surface(fake_agent_tools, tmp_path, mode):
    repo = tmp_path / "repo"
    repo.mkdir()
    loaded = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {"settings_path": str(repo / "settings.json"), "tools": ["git"]},
            "mode": mode,
        },
        repo_root=repo,
    )

    plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert not [a for a in plan.actions if a.kind == "record_mode"]
    assert not any("autonomous mode:" in note for note in plan.notes)
    perm = [a for a in plan.actions if a.kind == "provision_permissions"][0]
    assert perm.options["allow_rules"] == []


def test_autonomous_mode_minimal_global_config_uses_default_allow_rules(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    loaded = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {"settings_path": str(repo / "settings.json"), "tools": ["git"]},
            "mode": {"name": "autonomous"},
        },
        repo_root=repo,
    )

    plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    perm = [a for a in plan.actions if a.kind == "provision_permissions"][0]
    assert perm.options["allow_rules"] == ["Bash(dev:*)", "Bash(review:*)", "Bash(task:*)"]


def test_autonomous_mode_notes_when_permissions_disabled(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    loaded = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {"enabled": False},
            "mode": {"name": "autonomous"},
        },
        repo_root=repo,
    )

    plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert any("permissions.enabled=false" in note for note in plan.notes)
    assert not [a for a in plan.actions if a.kind == "provision_permissions"]


def test_autonomous_mode_drops_allow_rules_for_unverified_raw_rule_dialect(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    loaded = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {"kind": "opencode", "settings_path": str(repo / "opencode.json"), "tools": ["git"]},
            "mode": {"name": "autonomous"},
        },
        repo_root=repo,
    )

    plan = build(loaded, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert any("development tool allow rules dropped" in note and "opencode" in note for note in plan.notes)
    perm = [a for a in plan.actions if a.kind == "provision_permissions"][0]
    assert perm.options["allow_rules"] == []


def test_config_set_global_can_provision_autonomous_mode(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nci: {enabled: false}\n"
        "mcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n"
        "permissions: {enabled: false}\nharness: {enabled: false}\n",
        encoding="utf-8",
    )

    rc = main(["config", "set", "mode.name", "autonomous", "--global", "--no-apply", "-C", str(repo)])

    assert rc == 0
    assert config.load(repo).data["mode"]["name"] == "autonomous"


def test_repo_local_mode_block_is_rejected(tmp_path, fake_agent_tools, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nmode: {{name: autonomous}}\n",
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigError) as ei:
        config.load(repo)

    assert ei.value.schema_path == "mode"
    assert "global-only" in ei.value.what

    with pytest.raises(config.ConfigError) as explicit_ei:
        config.load(repo, explicit_config=repo / "rig.yaml")
    assert explicit_ei.value.schema_path == "mode"


def test_apply_config_repo_rigyaml_with_mode_is_rejected(tmp_path, fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nmode: {{name: autonomous}}\n",
        encoding="utf-8",
    )

    rc = main(["apply", "-C", str(repo), "--config", "rig.yaml"])

    assert rc == 2
    assert "mode is a global-only config block" in capsys.readouterr().out


def test_init_config_template_with_mode_is_rejected(tmp_path, fake_agent_tools, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "template.yaml"
    template.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\nmode: {{name: autonomous}}\n",
        encoding="utf-8",
    )

    rc = main(["init", "-C", str(repo), "--config", str(template), "--yes"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "mode is a global-only config block" in out
    assert not (repo / "rig.yaml").exists()
