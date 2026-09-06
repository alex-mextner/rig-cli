"""Provenance-aware drift (rig-cli#357): on-disk items rig can PLACE are "known", not drift.

A freshly applied reference machine printed 48 disk→config drift lines, every one of them
legitimate state (skills the ecosystem CLIs install themselves, hand-installed packs, a hook an
agent-tools ops installer wrote, permission entries rig once wrote by default, the repo's own CI
workflow). These tests pin the classification in ``riglib.provenance`` end to end through
``detect`` / ``compute_drift_report`` / ``rig status`` — hermetic (fake agent-tools + tmp HOME) —
and, just as important, that the REAL signal survives: an unknown-origin skill and an undeclared
catalog gate workflow are still drift.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from riglib import errors
from riglib.actions.runner import run_plan
from riglib.catalog import Catalog
from riglib.cli import main
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import compute_drift_report, detect
from riglib.permissions import CLAUDE_CODE_DENY_RULES, DEFAULT_RULES, RETIRED_DEFAULT_TOOLS, desired_entries
from riglib.plan import build
from riglib.provenance import (
    INSTALLED_BY_MARKER,
    KIND_CATALOG_UNSELECTED,
    KIND_CONFIG_KNOWN,
    KIND_DISABLED_BASELINE,
    KIND_ECOSYSTEM,
    KIND_OPS_INSTALLER,
    KIND_REPO_WORKFLOW,
    KIND_RETIRED_DEFAULT,
    KIND_SKILLS_CLI,
    KIND_TOOL_INSTALLED,
    KIND_USER_EXTRA,
    SKILL_LOCK_FILE,
)


def _cfg(repo: Path, source: Path, **overrides) -> LoadedConfig:
    data = {
        "agent_tools_source": str(source),
        "defaults": {
            "skills_target": str(repo / "skills-out"),
            "hooks_target": str(repo / "hooks-out"),
            "ci_target": str(repo / ".github/workflows"),
            "mcp_target": str(repo / "mcp-out"),
        },
        "skills": {"universal": {"all": True}, "harness_skill_dir": str(repo / "harness-skills")},
        "agent_hooks": {"all": True},
        "ci": {"items": {"secret-scan": {"enabled": True}}},
        "mcp": {"enabled": False},
        "harness": {"enabled": False},
        "permissions": {"enabled": False},
    }
    for key, value in overrides.items():
        data[key] = {**data.get(key, {}), **value}
    return LoadedConfig(data=data, repo_root=repo)


def _applied(fake_agent_tools: Path, tmp_path: Path, **overrides):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, **overrides), cat, project_type="unknown")
    assert not run_plan(plan).errors
    return repo, plan


def _plant_skill(skills_dir: Path, name: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")
    return d


def _skill_extras(report):
    return [i.item for i in report.by_direction("extra") if i.category == "skills"]


def _known(report, category):
    return {k.name: k for k in report.known if k.category == category}


# ── skills ─────────────────────────────────────────────────────────────────────────────────


def test_skill_with_installed_by_marker_is_known_not_drift(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    d = _plant_skill(repo / "skills-out", "tg")
    (d / INSTALLED_BY_MARKER).write_text("tg\n", encoding="utf-8")
    report = detect(plan)
    assert "tg" not in _skill_extras(report)
    k = _known(report, "skills")["tg"]
    assert k.kind == KIND_TOOL_INSTALLED and k.by == "tg"


def test_skill_with_blurb_marker_is_known_not_drift(fake_agent_tools, tmp_path):
    # the `.blurbs/<name>.md` file `<tool> install-skill` already writes today is honored as the marker
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "frobnicate")
    blurbs = repo / "skills-out" / ".blurbs"
    blurbs.mkdir()
    (blurbs / "frobnicate.md").write_text("frobnicate — does things\n", encoding="utf-8")
    report = detect(plan)
    assert "frobnicate" not in _skill_extras(report)
    assert _known(report, "skills")["frobnicate"].kind == KIND_TOOL_INSTALLED


def _write_skill_lock(skills_dir: Path, entries: dict) -> None:
    # the `skills` CLI (`npx skills add`) keeps its lockfile NEXT TO the skills dir it fills
    (skills_dir.parent / SKILL_LOCK_FILE).write_text(
        json.dumps({"version": 3, "skills": entries}), encoding="utf-8"
    )


def test_skill_recorded_in_the_skills_cli_lockfile_is_known_with_its_source(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "vercel-react-best-practices")
    _write_skill_lock(repo / "skills-out", {
        "vercel-react-best-practices": {"source": "vercel-labs/agent-skills", "sourceType": "github"},
    })
    report = detect(plan)
    assert "vercel-react-best-practices" not in _skill_extras(report)
    k = _known(report, "skills")["vercel-react-best-practices"]
    assert k.kind == KIND_SKILLS_CLI and k.by == "vercel-labs/agent-skills"


def test_skill_absent_from_the_skills_cli_lockfile_is_still_drift(fake_agent_tools, tmp_path):
    # the lockfile exists but lists OTHER skills: no provenance for this one
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "mystery-pack")
    _write_skill_lock(repo / "skills-out", {"agent-browser": {"source": "vercel-labs/agent-browser"}})
    assert "mystery-pack" in _skill_extras(detect(plan))


@pytest.mark.parametrize("lock_text", ["{not json", '{"skills": ["mystery-pack"]}', '{"skills": {"mystery-pack": "x"}}'])
def test_malformed_skills_cli_lockfile_is_ignored(fake_agent_tools, tmp_path, lock_text):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "mystery-pack")
    (repo / "skills-out").parent.joinpath(SKILL_LOCK_FILE).write_text(lock_text, encoding="utf-8")
    assert "mystery-pack" in _skill_extras(detect(plan))


def test_skills_cli_lockfile_source_is_never_rendered_raw(fake_agent_tools, tmp_path):
    # user-writable JSON: a `source` that is not a plain token still places the skill (the lockfile
    # entry IS the provenance) but never reaches the terminal as `by`
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "agent-browser")
    _write_skill_lock(repo / "skills-out", {"agent-browser": {"source": "evil \x1b[31m text"}})
    report = detect(plan)
    assert "agent-browser" not in _skill_extras(report)
    k = _known(report, "skills")["agent-browser"]
    assert k.kind == KIND_SKILLS_CLI and k.by == ""


def test_allowlisted_ecosystem_skill_name_is_known_not_drift(fake_agent_tools, tmp_path):
    # no marker, no blurb: the shipped allowlist covers a tool that predates the marker contract
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "review")
    report = detect(plan)
    assert "review" not in _skill_extras(report)
    assert _known(report, "skills")["review"].kind == KIND_ECOSYSTEM


def test_skill_listed_in_skills_known_is_known_not_drift(fake_agent_tools, tmp_path):
    # the config list rides on the plan (`InstallPlan.known_names`, filled by build) — no second channel
    repo, plan = _applied(fake_agent_tools, tmp_path, skills={"known": ["swiftui-pro"]})
    _plant_skill(repo / "skills-out", "swiftui-pro")
    report = detect(plan)
    assert "swiftui-pro" not in _skill_extras(report)
    assert _known(report, "skills")["swiftui-pro"].kind == KIND_CONFIG_KNOWN


def test_catalog_skill_unselected_by_this_repo_is_known_not_drift(fake_agent_tools, tmp_path):
    # the skills dir is machine-wide, the plan per-repo: a by-type skill another repo's apply
    # installed (`lazy-imports` is by-type/cli in the fake catalog; this repo is type unknown) —
    # byte-identical to the catalog source, exactly as rig copies it
    repo, plan = _applied(fake_agent_tools, tmp_path)
    shutil.copytree(fake_agent_tools / "skills/by-type/cli/lazy-imports", repo / "skills-out" / "lazy-imports")
    report = detect(plan)
    assert "lazy-imports" not in _skill_extras(report)
    assert _known(report, "skills")["lazy-imports"].kind == KIND_CATALOG_UNSELECTED


def test_catalog_leaf_name_shared_across_namespaces_matches_any_source(fake_agent_tools, tmp_path):
    # `by-type/cli/x` and `by-stack/…/x` share the on-disk leaf; a copy of EITHER is rig-installed
    repo, plan = _applied(fake_agent_tools, tmp_path)
    src_a = fake_agent_tools / "skills/by-type/cli/lazy-imports"
    src_b = fake_agent_tools / "skills/by-stack/frontend/ts/lazy-imports"
    shutil.copytree(src_a, src_b)
    (src_b / "SKILL.md").write_text("---\nname: lazy-imports\n---\n# the by-stack twin\n", encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert len(plan.catalog_items["skills"]["lazy-imports"]) == 2
    shutil.copytree(src_b, repo / "skills-out" / "lazy-imports")
    report = detect(plan)
    assert "lazy-imports" not in _skill_extras(report)
    assert _known(report, "skills")["lazy-imports"].kind == KIND_CATALOG_UNSELECTED


def test_catalog_named_skill_with_foreign_content_is_still_drift(fake_agent_tools, tmp_path):
    # a name-alike dir (a broken installer, a spoof) must NOT ride on the catalog name alone
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "lazy-imports")  # same name, different SKILL.md
    report = detect(plan)
    assert "lazy-imports" in _skill_extras(report)
    item = next(i for i in report.by_direction("extra") if i.item == "lazy-imports")
    assert "content differs from the catalog source" in item.detail  # named as a stale/foreign copy


def test_marker_cannot_vouch_for_a_catalog_named_skill_with_foreign_content(fake_agent_tools, tmp_path):
    # a catalog leaf name is rig's namespace: `.installed-by` does not bypass the content check
    repo, plan = _applied(fake_agent_tools, tmp_path)
    d = _plant_skill(repo / "skills-out", "lazy-imports")
    (d / INSTALLED_BY_MARKER).write_text("foo\n", encoding="utf-8")
    report = detect(plan)
    assert "lazy-imports" in _skill_extras(report)
    assert "lazy-imports" not in _known(report, "skills")


def test_skills_known_may_claim_a_catalog_name(fake_agent_tools, tmp_path):
    # the user's own pack under a catalog name, declared once — the config outranks the content check
    repo, plan = _applied(fake_agent_tools, tmp_path, skills={"known": ["lazy-imports"]})
    _plant_skill(repo / "skills-out", "lazy-imports")
    report = detect(plan)
    assert "lazy-imports" not in _skill_extras(report)
    assert _known(report, "skills")["lazy-imports"].kind == KIND_CONFIG_KNOWN


def test_marker_with_control_characters_is_not_a_marker(fake_agent_tools, tmp_path):
    # the marker is rendered by `rig status`; only a plain identifier counts as an installer name
    repo, plan = _applied(fake_agent_tools, tmp_path)
    d = _plant_skill(repo / "skills-out", "weird")
    (d / INSTALLED_BY_MARKER).write_text("evil\x1b]52;c;aGk=\x07\n", encoding="utf-8")
    report = detect(plan)
    assert "weird" in _skill_extras(report)
    assert "weird" not in _known(report, "skills")


def test_unknown_origin_skill_is_still_drift(fake_agent_tools, tmp_path):
    # the real signal: no marker, no blurb, not allowlisted, not declared known, not in the catalog
    repo, plan = _applied(fake_agent_tools, tmp_path, skills={"known": ["something-else"]})
    _plant_skill(repo / "skills-out", "rogue-skill")
    report = detect(plan)
    assert "rogue-skill" in _skill_extras(report)
    assert "rogue-skill" not in _known(report, "skills")
    item = next(i for i in report.by_direction("extra") if i.item == "rogue-skill")
    assert "skills.known" in item.detail  # the remedy is named
    assert not report.in_sync


def test_known_items_never_affect_in_sync(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_skill(repo / "skills-out", "tg")
    report = detect(plan)
    assert report.known and report.in_sync


# ── agent hooks ────────────────────────────────────────────────────────────────────────────


def _plant_hook(hooks_dir: Path, hook_id: str, **extra) -> Path:
    p = hooks_dir / f"{hook_id}.pre-bash.json"
    spec = {"id": hook_id, "point": "pre-bash", "cmd": "/x"}
    spec.update(extra)
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


def _hook_extras(report):
    return [i.item for i in report.by_direction("extra") if i.category == "agent_hooks"]


def test_hook_descriptor_with_installed_by_key_is_known(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_hook(repo / "hooks-out", "some-tool-hook", installed_by="some-tool")
    report = detect(plan)
    assert not _hook_extras(report)
    k = _known(report, "agent_hooks")["some-tool-hook.pre-bash"]
    assert k.kind == KIND_TOOL_INSTALLED and k.by == "some-tool"


def test_agent_tools_ops_installer_hook_is_known(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_hook(repo / "hooks-out", "agent-browser-session-claim")
    report = detect(plan)
    assert not _hook_extras(report)
    assert _known(report, "agent_hooks")["agent-browser-session-claim.pre-bash"].kind == KIND_OPS_INSTALLER


def test_hook_listed_in_agent_hooks_known_is_known(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"known": ["house-hook"]})
    _plant_hook(repo / "hooks-out", "house-hook")
    report = detect(plan)
    assert not _hook_extras(report)
    assert _known(report, "agent_hooks")["house-hook.pre-bash"].kind == KIND_CONFIG_KNOWN


def test_hook_listed_in_agent_hooks_known_by_full_descriptor_stem(fake_agent_tools, tmp_path):
    # `<id>.<point>` names ONE descriptor of a hook that ships several points
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"known": ["house-hook.pre-bash"]})
    _plant_hook(repo / "hooks-out", "house-hook")
    report = detect(plan)
    assert not _hook_extras(report)
    assert _known(report, "agent_hooks")["house-hook.pre-bash"].kind == KIND_CONFIG_KNOWN


def test_agent_hooks_known_by_full_stem_leaves_the_sibling_point_as_drift(fake_agent_tools, tmp_path):
    # claiming `<id>.<point>` names ONE descriptor: a sibling point of the same hook id that the
    # config did not claim is still of unknown origin (found in review)
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"known": ["house-hook.pre-bash"]})
    _plant_hook(repo / "hooks-out", "house-hook")
    sibling = repo / "hooks-out" / "house-hook.pre-write.json"
    sibling.write_text(json.dumps({"id": "house-hook", "point": "pre-write", "cmd": "/x"}), encoding="utf-8")
    report = detect(plan)
    assert _hook_extras(report) == ["house-hook.pre-write"]
    assert list(_known(report, "agent_hooks")) == ["house-hook.pre-bash"]


def test_unknown_origin_hook_is_still_drift(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path)
    _plant_hook(repo / "hooks-out", "rogue-hook")
    report = detect(plan)
    assert _hook_extras(report) == ["rogue-hook.pre-bash"]


def test_catalog_hook_unselected_by_this_repo_is_known_when_cmd_is_the_catalog_script(fake_agent_tools, tmp_path):
    # another repo's config enabled a catalog hook this one turned off: the descriptor's cmd runs
    # the catalog's own script → rig-installed, known
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"all": True, "items": {"block-no-verify": {"enabled": False}}})
    assert not (repo / "hooks-out" / "block-no-verify.pre-bash.json").exists()
    _plant_hook(repo / "hooks-out", "block-no-verify",
                cmd=str(fake_agent_tools / "agent-hooks/block-no-verify/block_no_verify.py"))
    report = detect(plan)
    assert not _hook_extras(report)
    assert _known(report, "agent_hooks")["block-no-verify.pre-bash"].kind == KIND_CATALOG_UNSELECTED


def test_catalog_hook_cmd_with_arguments_still_counts_as_the_catalog_script(fake_agent_tools, tmp_path):
    # `cmd` is shell-ish: arguments after the script must not make a rig-installed descriptor look foreign
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"all": True, "items": {"block-no-verify": {"enabled": False}}})
    script = fake_agent_tools / "agent-hooks/block-no-verify/block_no_verify.py"
    _plant_hook(repo / "hooks-out", "block-no-verify", cmd=f"{script} --strict 'a b'")
    report = detect(plan)
    assert not _hook_extras(report)
    assert _known(report, "agent_hooks")["block-no-verify.pre-bash"].kind == KIND_CATALOG_UNSELECTED


def test_catalog_hook_id_with_foreign_cmd_is_still_drift(fake_agent_tools, tmp_path):
    # a descriptor wearing a catalog id but running something else is not trusted by name
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"all": True, "items": {"block-no-verify": {"enabled": False}}})
    _plant_hook(repo / "hooks-out", "block-no-verify", cmd="/tmp/not-the-catalog/evil.py")
    report = detect(plan)
    assert _hook_extras(report) == ["block-no-verify.pre-bash"]
    item = next(i for i in report.by_direction("extra") if i.category == "agent_hooks")
    assert "outside that catalog hook's directory" in item.detail


def test_installed_by_key_cannot_vouch_for_a_catalog_hook_id_with_foreign_cmd(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path, agent_hooks={"all": True, "items": {"block-no-verify": {"enabled": False}}})
    _plant_hook(repo / "hooks-out", "block-no-verify", cmd="/tmp/not-the-catalog/evil.py", installed_by="foo")
    report = detect(plan)
    assert _hook_extras(report) == ["block-no-verify.pre-bash"]


# ── CI workflows ───────────────────────────────────────────────────────────────────────────


def _ci_extras(report):
    return [i.item for i in report.by_direction("extra") if i.category == "ci"]


def test_repo_own_workflow_is_known_not_drift(fake_agent_tools, tmp_path):
    # `ci.yml` is not a catalog slot → the repository's own workflow, rig has no opinion on it
    repo, plan = _applied(fake_agent_tools, tmp_path)
    (repo / ".github/workflows/ci.yml").write_text("name: ci\n", encoding="utf-8")
    report = detect(plan)
    assert not _ci_extras(report)
    assert _known(report, "ci")["ci.yml"].kind == KIND_REPO_WORKFLOW


def test_undeclared_catalog_gate_workflow_is_still_drift(fake_agent_tools, tmp_path):
    # `codeql` IS a catalog slot and this config does not enable it → a genuine rig orphan
    repo, plan = _applied(fake_agent_tools, tmp_path)
    (repo / ".github/workflows/codeql.yml").write_text("name: codeql\n", encoding="utf-8")
    report = detect(plan)
    assert _ci_extras(report) == ["codeql"]


def test_ci_known_covers_a_slot_name_collision(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path, ci={"known": ["codeql"]})
    (repo / ".github/workflows/codeql.yml").write_text("name: our own codeql\n", encoding="utf-8")
    report = detect(plan)
    assert not _ci_extras(report)
    assert _known(report, "ci")["codeql.yml"].kind == KIND_CONFIG_KNOWN


def test_without_catalog_knowledge_every_undeclared_workflow_is_drift(fake_agent_tools, tmp_path):
    # a hand-built plan carries no catalog names → the conservative reading (nothing hidden)
    repo, plan = _applied(fake_agent_tools, tmp_path)
    (repo / ".github/workflows/ci.yml").write_text("name: ci\n", encoding="utf-8")
    plan.catalog_items = None
    assert _ci_extras(detect(plan)) == ["ci"]


# ── MCP ────────────────────────────────────────────────────────────────────────────────────


def test_mcp_known_covers_a_server_registered_by_something_else(fake_agent_tools, tmp_path):
    repo, plan = _applied(fake_agent_tools, tmp_path, mcp={"enabled": True, "known": ["serena"]})
    registry = repo / "mcp-out" / "mcp.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"mcpServers": {"serena": {"command": "serena"}, "rogue": {"command": "x"}}}), encoding="utf-8")
    report = detect(plan, scan_mcp_files=[registry])
    assert [i.item for i in report.by_direction("extra") if i.category == "mcp"] == ["rogue"]
    assert _known(report, "mcp")["serena"].kind == KIND_CONFIG_KNOWN


# ── rendering ──────────────────────────────────────────────────────────────────────────────


def test_every_provenance_kind_has_a_status_label():
    # `_print_known_groups` indexes KIND_LABELS directly: a kind without a label is a KeyError in
    # `rig status`, not a degraded line — pin the pairing here
    from riglib import provenance

    kinds = {v for k, v in vars(provenance).items() if k.startswith("KIND_") and isinstance(v, str)}
    assert kinds and kinds == set(provenance.KIND_LABELS)


def test_known_group_line_caps_the_names_it_prints(capsys):
    from riglib.cli import _KNOWN_NAMES_SHOWN, _print_known_groups
    from riglib.drift import KnownItem

    items = [KnownItem("skills", f"pack-{i:02d}", Path("/x"), KIND_CONFIG_KNOWN, f"pack-{i:02d}") for i in range(_KNOWN_NAMES_SHOWN + 3)]
    _print_known_groups(items, LoadedConfig(data={}, repo_root=Path("/repo")))
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out  # one line per group, however long the list
    assert f"… and 3 more" in out and f"pack-{_KNOWN_NAMES_SHOWN:02d}" not in out


# ── permissions ────────────────────────────────────────────────────────────────────────────


def _perm_cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "ci": {"enabled": False}, "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"enabled": False},
            "permissions": {"enabled": True, "kind": "claude-code", "settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def _perm_known(report):
    return {k.name: k.kind for k in report.known if k.category == "permissions"}


def test_rig_managed_rg_pre_deny_baseline_never_shows_as_extra(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_perm_cfg(repo, fake_agent_tools, settings), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan).errors
    live = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert {r for r in CLAUDE_CODE_DENY_RULES if "--pre" in r} <= set(live)
    report = detect(plan)
    assert not [i for i in report.items if i.category == "permissions"]
    assert not _perm_known(report)


def test_permission_extras_are_kept_additions_with_their_origin_not_drift(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_perm_cfg(repo, fake_agent_tools, settings), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan).errors
    data = json.loads(settings.read_text(encoding="utf-8"))
    retired = desired_entries("claude-code", list(RETIRED_DEFAULT_TOOLS))
    data["permissions"]["allow"] += [*retired, "Bash(kubectl:*)"]
    data["permissions"]["deny"] += ["Bash(rg --pre*)"]  # a hand-added, stricter deny form
    settings.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert not [i for i in report.items if i.category == "permissions"], "extras are not drift"
    assert report.in_sync
    known = _perm_known(report)
    assert all(known[e] == KIND_RETIRED_DEFAULT for e in retired)
    assert known["Bash(kubectl:*)"] == KIND_USER_EXTRA
    assert known["Bash(rg --pre*)"] == KIND_USER_EXTRA
    # and apply STILL never deletes them
    assert not run_plan(plan).errors
    after = json.loads(settings.read_text(encoding="utf-8"))["permissions"]
    assert "Bash(kubectl:*)" in after["allow"] and "Bash(rg --pre*)" in after["deny"]


def test_baseline_rule_hand_added_under_another_role_is_a_user_extra(fake_agent_tools, tmp_path):
    # an ask-baseline rule spelled into the DENY list was never rig's deny baseline → "your own entry"
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_perm_cfg(repo, fake_agent_tools, settings), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan).errors
    data = json.loads(settings.read_text(encoding="utf-8"))
    ask_rule = DEFAULT_RULES["claude-code"]["ask"][0]
    data["permissions"]["deny"].append(ask_rule)
    data["permissions"]["deny"].append("Bash(git:*)")  # a retired ALLOW default is no deny origin either
    settings.write_text(json.dumps(data), encoding="utf-8")
    known = _perm_known(detect(plan))
    assert known[ask_rule] == KIND_USER_EXTRA
    assert known["Bash(git:*)"] == KIND_USER_EXTRA


def test_status_renders_permission_container_once(tmp_path, capsys, fake_agent_tools, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    settings = home / "settings.json"
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\nagents_md: {enabled: false}\n"
        f"gitignore: {{enabled: false}}\nharness: {{enabled: false}}\npermissions: {{settings_path: {settings}}}\n",
        encoding="utf-8",
    )
    assert main(["apply", "commit", "--yes", "-C", str(repo)]) == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["deny"].append("Bash(shutdown:*)")
    settings.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert main(["status", "-C", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "▸ claude-code:permissions.deny (1)" in out and "permissions/claude-code" not in out
    assert "1 permission addition kept" in out
    # the per-AREA line carries the count too (the area matcher keys permissions off the category,
    # so the container-shaped `item` of a permission KnownItem still rolls up)
    summary = out.split("areas rig manages")[1]
    perm_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("harness permissions"))
    assert "in sync" in perm_line and "(1 your additions, kept)" in perm_line


def test_disabled_baseline_rules_left_on_disk_are_kept_not_drift(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    assert not run_plan(build(_perm_cfg(repo, fake_agent_tools, settings), cat, project_type="unknown")).errors
    plan2 = build(_perm_cfg(repo, fake_agent_tools, settings, deny=[], ask=[]), cat, project_type="unknown")
    report = detect(plan2)
    known = _perm_known(report)
    for rule in DEFAULT_RULES["claude-code"]["deny"] + DEFAULT_RULES["claude-code"]["ask"]:
        assert known[rule] == KIND_DISABLED_BASELINE
    assert report.in_sync


# ── config: the `known` lists ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("block", ["skills", "agent_hooks", "ci", "mcp"])
def test_known_list_is_accepted_and_type_checked(block):
    validate({"version": 1, block: {"known": ["a", "b"]}})
    with pytest.raises(ConfigError) as exc:
        validate({"version": 1, block: {"known": "a"}})
    assert f"{block}.known" in str(exc.value)


# ── `rig status` end to end ────────────────────────────────────────────────────────────────


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    return path


def _status_repo(
    tmp_path: Path, fake_agent_tools: Path, monkeypatch, *, skills_known: str = "", ci: str = "{enabled: true, all: false}",
) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        f"skills: {{enabled: true, target: {home / 'skills'}, universal: {{all: true}}, "
        f"harness_skill_dir: {home / 'harness-skills'}{skills_known}}}\n"
        "agent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        f"git_hooks: {{dispatcher: {{enabled: false}}}}\nci: {ci}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\nharness: {enabled: false}\n"
        "permissions: {enabled: false}\n",
        encoding="utf-8",
    )
    assert main(["apply", "commit", "--yes", "-C", str(repo)]) == 0
    return repo


def test_status_reports_known_items_as_informational_and_exits_clean(
    tmp_path, capsys, fake_agent_tools, monkeypatch
):
    repo = _status_repo(tmp_path, fake_agent_tools, monkeypatch, skills_known=", known: [swiftui-pro]")
    skills = tmp_path / "home" / "skills"
    _plant_skill(skills, "tg")
    _plant_skill(skills, "swiftui-pro")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("name: ci\n", encoding="utf-8")
    capsys.readouterr()
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "disk→config drift" not in out
    assert "known, not managed by rig" in out
    assert "tg" in out and "swiftui-pro" in out and "ci.yml" in out
    summary = out.split("areas rig manages")[1]
    skills_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("skills:"))
    assert "in sync" in skills_line and "2 known" in skills_line
    # ci is on but selects no gate: nothing to reconcile → "not configured", the known count beside it
    ci_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("CI gates:"))
    assert "not configured" in ci_line and "1 known" in ci_line
    assert "in sync — config and disk agree (3 known items not managed by rig)" in out


def test_disabled_area_with_known_items_stays_not_configured(tmp_path, capsys, fake_agent_tools, monkeypatch):
    # the workflows dir is scanned even when THIS config turns CI off (a repo-local target with
    # clear ownership): the repo's own ci.yml is known (not drift), but the area must not claim
    # "in sync" — rig reconciles nothing there. "not configured" + the known count is the honest
    # line (review finding). Skills/hooks dirs are not scanned at all when off, so no such case.
    repo = _status_repo(tmp_path, fake_agent_tools, monkeypatch, ci="{enabled: false}")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("name: ci\n", encoding="utf-8")
    capsys.readouterr()
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0, out
    summary = out.split("areas rig manages")[1]
    ci_line = next(ln for ln in summary.splitlines() if ln.strip().startswith("CI gates:"))
    assert "not configured" in ci_line and "1 known" in ci_line
    assert "in sync" not in ci_line


def test_status_still_flags_unknown_origin_skill_as_drift(tmp_path, capsys, fake_agent_tools, monkeypatch):
    repo = _status_repo(tmp_path, fake_agent_tools, monkeypatch)
    _plant_skill(tmp_path / "home" / "skills", "rogue-skill")
    capsys.readouterr()
    rc = main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == errors.EXIT_DRIFT
    assert "disk→config drift (1)" in out and "rogue-skill" in out


def test_compute_drift_report_reads_known_lists_through_the_cascade(fake_agent_tools, tmp_path, monkeypatch):
    from riglib.detect import Environment, OsInfo

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    # the plan built from the cascaded config carries the known lists (`build` reads them through
    # `loaded.category`, the global/repo cascade) — `rig status` hands the SAME config to both
    repo, plan = _applied(fake_agent_tools, tmp_path, skills={"known": ["hand-pack"]})
    _plant_skill(repo / "skills-out", "hand-pack")
    loaded = _cfg(repo, fake_agent_tools, skills={"known": ["hand-pack"]})
    env = Environment(
        repo_root=repo, is_git_repo=True, stack="unknown", project_type="unknown", skills_dirs={},
        global_hooks_path=None, dispatcher_installed=False, gh_authed=False, is_github_repo=False,
        os=OsInfo("darwin", "brew", "macOS"),
    )
    scan = compute_drift_report(plan, loaded, env)
    assert "hand-pack" not in _skill_extras(scan.report)
    assert _known(scan.report, "skills")["hand-pack"].kind == KIND_CONFIG_KNOWN


# ── rig's own install-skill writes the marker it asks every tool for ───────────────────────


def test_rig_install_skill_writes_installed_by_marker(tmp_path, monkeypatch):
    from riglib.install import install_named_skill

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_CODEX_HOME", str(home / ".codex"))
    assert install_named_skill("demo", "# demo\n") == 0
    marker = home / ".agents" / "skills" / "demo" / INSTALLED_BY_MARKER
    assert marker.read_text(encoding="utf-8") == "rig\n"
    assert install_named_skill("demo", "# demo\n") == 0  # idempotent
    assert marker.read_text(encoding="utf-8") == "rig\n"
    # another tool's marker is never reassigned by a rig re-run over identical content
    marker.write_text("other-tool\n", encoding="utf-8")
    assert install_named_skill("demo", "# demo\n") == 0
    assert marker.read_text(encoding="utf-8") == "other-tool\n"


def test_config_web_drift_payload_lists_known_items_and_stays_in_sync(fake_agent_tools, tmp_path, monkeypatch):
    from riglib import config_web_plan as cwp
    from riglib.config_web_scopes import Scope

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = _git_repo(tmp_path / "repo")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        f"skills: {{enabled: true, target: {home / 'skills'}, universal: {{all: true}}, "
        f"harness_skill_dir: {home / 'harness-skills'}}}\n"
        "agent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\nharness: {enabled: false}\n"
        "permissions: {enabled: false}\n",
        encoding="utf-8",
    )
    assert main(["apply", "commit", "--yes", "-C", str(repo)]) == 0
    _plant_skill(home / "skills", "tg")
    scope = Scope(id="repo-1", label="repo", repo_root=repo, is_global=False)
    drift = cwp.compute_scope_drift(cwp.build_scope_plan(scope))
    assert drift["in_sync"] is True
    assert [k["name"] for k in drift["known"]] == ["tg"]
    assert drift["known"][0]["kind"] == KIND_ECOSYSTEM
