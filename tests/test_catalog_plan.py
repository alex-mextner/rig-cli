"""Catalog scan + plan resolution against the fake agent-tools checkout."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.catalog import Catalog, CatalogError
from riglib.config import LoadedConfig
from riglib.errors import UnknownItemError
from riglib.plan import build


def test_catalog_scan_finds_all_categories(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    assert "shell-timeouts" in cat.names("skills")
    assert "by-type/cli/lazy-imports" in cat.names("skills")
    assert "block-no-verify" in cat.names("agent_hooks")
    assert "codeql" in cat.names("ci")
    assert "secret-scan" in cat.names("ci")
    assert "dispatcher" in cat.names("git_hooks")
    assert "review" in cat.names("mcp")


def test_catalog_situational_default_off(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    push = cat.get("skills", "push-regularly")
    assert push is not None and push.default_enabled is False


def test_resolve_source_rejects_non_checkout(tmp_path):
    with pytest.raises(CatalogError):
        Catalog.scan(str(tmp_path))


def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data=data, repo_root=repo_root)


def test_plan_universal_optout(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"universal": {"all": True, "disable": ["naming"]}, "by_type": {"enable": []}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "shell-timeouts" in skills
    assert "naming" not in skills  # disabled
    # situational stays off under all:true? all:true forces on — but push-regularly is universal
    assert "push-regularly" in skills


def test_plan_by_type_pulled_by_project_type(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-type/cli/lazy-imports" in skills
    assert "by-type/backend/atomic-tx" not in skills  # different type


def test_plan_by_type_explicit_enable_overrides_type(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {"enable": ["backend"]}}}, tmp_path)
    plan = build(cfg, cat, project_type="frontend")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-type/backend/atomic-tx" in skills
    assert "by-type/frontend/tokens" not in skills


def test_plan_ci_export_only_writes_nothing(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"target": "export-only", "items": {"codeql": {"enabled": True}}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.category == "ci"]
    assert any("export-only" in n for n in plan.notes)


def test_plan_codeql_variant_selected(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"codeql": {"enabled": True, "variant": "selfgate"}}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    codeql = next(a for a in plan.actions if a.item == "codeql")
    assert codeql.options["variant"] == "selfgate"


def test_plan_unknown_universal_skill_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"universal": {"disable": ["shell-timeout"]}}}, tmp_path)  # typo
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    # error-system v2: did-you-mean suggests the nearest valid skill
    assert "shell-timeouts" in exc.value.fix


def test_plan_unknown_agent_hook_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "agent_hooks": {"items": {"block-no-verifyy": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "agent_hooks" in exc.value.what
    assert "block-no-verify" in exc.value.fix  # nearest valid


def test_plan_unknown_by_type_bundle_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {"enable": ["backendd"]}}}, tmp_path)  # typo
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "backend" in exc.value.fix  # nearest valid bundle


def test_plan_unknown_ci_item_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"secret_scan": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "secret-scan" in exc.value.fix  # nearest valid


def test_plan_ci_all_true_enables_catalog(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}, "ci": {"all": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    ci_items = {a.item for a in plan.actions if a.category == "ci"}
    # codeql + secret-scan are real workflow slots in the fake catalog; both should be on
    assert "codeql" in ci_items
    assert "secret-scan" in ci_items


def test_plan_unknown_ci_enable_name_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"all": False, "enable": ["secret_scan"]}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError, match="unknown ci item"):
        build(cfg, cat, project_type="unknown")


def test_plan_ci_all_with_disable(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}, "ci": {"all": True, "disable": ["codeql"]}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    ci_items = {a.item for a in plan.actions if a.category == "ci"}
    assert "codeql" not in ci_items
    assert "secret-scan" in ci_items


def test_plan_unknown_git_hooks_key_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
         "git_hooks": {"dispatcherr": {"enabled": True}}},  # typo
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "git_hooks" in exc.value.what
    assert "dispatcher" in exc.value.fix


def test_plan_ship_absent_from_catalog_fails_closed(fake_agent_tools, tmp_path):
    # remove ship from the catalog, then enabling it must fail closed (not silently drop)
    cat = Catalog.scan(str(fake_agent_tools))
    cat.items = [i for i in cat.items if not (i.category == "ci" and i.name == "ship")]
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"ship": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError, match="unknown ci item"):
        build(cfg, cat, project_type="unknown")


def test_xdg_config_home_maps_dispatcher_dir(fake_agent_tools, tmp_path, monkeypatch):
    """A portable ~/.config dir resolves under $XDG_CONFIG_HOME at apply time when set."""
    from riglib.plan import _expand

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    resolved = _expand("~/.config/git/global-hooks.d", tmp_path)
    assert resolved == xdg / "git" / "global-hooks.d"
    # without XDG it falls back to ~/.config
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    import os

    home = Path(os.path.expanduser("~"))
    assert _expand("~/.config/git", tmp_path) == home / ".config" / "git"


def test_plan_disabled_category(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    # agents_md and the github ruleset are default-ON, so turn them off too to assert a truly
    # empty plan.
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "agents_md": {"enabled": False},
            "github": {"ruleset": {"enabled": False}},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="cli")
    assert len(plan) == 0


# ── harness (auto-mode / permission provisioning) ─────────────────────────────────
def _harness_action(plan):
    return next((a for a in plan.actions if a.category == "harness"), None)


def test_plan_no_harness_block_no_action(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}}, tmp_path)  # no harness key at all
    plan = build(cfg, cat, project_type="unknown")
    assert _harness_action(plan) is None


def test_plan_harness_auto_mode_on_maps_to_user_auto(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"kind": "claude-code", "auto_mode": True}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["mode_value"] == "auto"
    assert a.options["auto_mode"] is True
    # `auto` is honored only from the user's machine settings (CC strips it from project
    # scope), so the default target is ~/.claude/settings.json, NOT the repo.
    import os

    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))


def test_plan_harness_nonauto_mode_writes_project(fake_agent_tools, tmp_path):
    # a non-auto mode IS committable at project scope → target stays the repo settings file
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False},
         "harness": {"kind": "claude-code", "mode": "acceptEdits"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.options["mode_value"] == "acceptEdits"
    assert a.target == (tmp_path / ".claude" / "settings.json")


def test_plan_harness_auto_mode_off_maps_to_default(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"auto_mode": False}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None and a.options["mode_value"] == "default"


def test_plan_harness_explicit_mode_overrides_auto_mapping(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"auto_mode": True, "mode": "acceptEdits"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    assert a is not None and a.options["mode_value"] == "acceptEdits"


def test_plan_harness_disabled_no_action(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "harness": {"enabled": False, "auto_mode": True}},
        tmp_path,
    )
    assert _harness_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_harness_custom_settings_path(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False},
         "harness": {"auto_mode": True, "settings_path": "~/.claude/settings.json"}},
        tmp_path,
    )
    a = _harness_action(build(cfg, cat, project_type="unknown"))
    import os

    assert a is not None
    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))


# ── hook bridge (agents-hooks/v1 → CC settings.json) ──────────────────────────────
def _bridge_action(plan):
    return next((a for a in plan.actions if a.kind == "register_hook_bridge"), None)


def test_plan_hook_bridge_emitted_with_harness(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"kind": "claude-code", "auto_mode": True}}, tmp_path)
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    # anchored on the resolved agent-tools checkout's lib/ for PYTHONPATH
    assert a.options["lib_dir"] == str(fake_agent_tools / "lib")
    assert a.target == (tmp_path / ".claude" / "settings.json")


def test_plan_hook_bridge_skipped_without_harness(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}}, tmp_path)  # no harness block
    assert _bridge_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_hook_bridge_disabled_explicitly(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"auto_mode": True, "hook_bridge": {"enabled": False}}},
        tmp_path,
    )
    assert _bridge_action(build(cfg, cat, project_type="unknown")) is None


def test_plan_hook_bridge_skipped_when_agent_hooks_disabled(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"auto_mode": True}, "agent_hooks": {"enabled": False}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("agent_hooks disabled" in n for n in plan.notes)


def test_plan_hook_bridge_honors_settings_path_and_python(fake_agent_tools, tmp_path):
    import os

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"harness": {"auto_mode": True, "settings_path": "~/.claude/settings.json",
                     "hook_bridge": {"python": "/opt/py/bin/python3"}}},
        tmp_path,
    )
    a = _bridge_action(build(cfg, cat, project_type="unknown"))
    assert a is not None
    assert a.target == Path(os.path.expanduser("~/.claude/settings.json"))
    assert a.options["python"] == "/opt/py/bin/python3"


def test_plan_hook_bridge_skipped_when_dispatcher_module_absent(fake_agent_tools, tmp_path):
    """Fail-closed: an agent-tools checkout without lib/cc_hook_bridge must NOT wire a
    settings.json command that would error at runtime — skip with an actionable note."""
    # remove the dispatcher the fixture ships
    (fake_agent_tools / "lib" / "cc_hook_bridge" / "dispatch.py").unlink()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"harness": {"auto_mode": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert _bridge_action(plan) is None
    assert any("cc_hook_bridge not found" in n for n in plan.notes), plan.notes
