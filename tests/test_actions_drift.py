"""Action execution (idempotency, backup, absolute-cmd rewrite) + two-way drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build


def _full_cfg(repo_root: Path, source: Path) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {
                "skills_target": str(repo_root / "skills-out"),
                "hooks_target": str(repo_root / "hooks-out"),
                "ci_target": str(repo_root / ".github/workflows"),
                "mcp_target": str(repo_root / "mcp-out"),
                "on_conflict": "backup",
            },
            "skills": {
                "universal": {"all": True},
                "by_type": {"enable": ["cli"]},
                # keep the harness skill-link target inside the repo so tests stay hermetic
                # (the real default is ~/.claude/skills); dedicated link tests below isolate HOME.
                "harness_skill_dir": str(repo_root / "harness-skills"),
            },
            "agent_hooks": {"all": True},
            "ci": {"items": {"codeql": {"enabled": True, "variant": "selfgate"}, "secret-scan": {"enabled": True}}},
            "mcp": {"items": {"review": {"enabled": True, "command": "review --mcp"}}},
        },
        repo_root=repo_root,
    )


def test_apply_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    assert first.changed > 0

    second = run_plan(plan)
    assert not second.errors
    # second run changes nothing — everything skips
    assert second.changed == 0
    assert all(r.status in ("skipped",) for r in second.results), second.summary()


def test_dispatcher_idempotent_status(fake_agent_tools, tmp_path, monkeypatch):
    """The dispatcher action rolls sub-outcomes up: a no-op second run reports 'skipped'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": False,  # don't touch global git config in tests
                    "install_local_retrofit_script": False,
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    disp1 = next(r for r in first.results if r.action.item == "dispatcher")
    assert disp1.status in ("created", "backed_up")
    second = run_plan(plan)
    disp2 = next(r for r in second.results if r.action.item == "dispatcher")
    assert disp2.status == "skipped", disp2.detail


def test_dispatcher_installs_composers_and_points_hookspath(fake_agent_tools, tmp_path, monkeypatch):
    """core.hooksPath must point at the composer dir (hooks/), which must be installed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    recorded = {}

    def _fake_set(key, value):
        recorded[key] = value
        return 0

    monkeypatch.setattr("riglib.actions.runner._set_git_global", _fake_set)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda key: None)

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": True,
                    "install_local_retrofit_script": False,
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    composer = home / "hooks"
    assert (composer / "pre-commit").is_file()  # composers installed
    assert recorded["core.hooksPath"] == str(composer)  # points at composer dir, not runner parent


def test_disabled_dispatcher_still_wired_is_drift(tmp_path, monkeypatch):
    from riglib.drift import DriftReport, check_disabled_dispatcher

    composer = tmp_path / "hooks"
    composer.mkdir()
    (composer / "pre-commit").write_text("#!/bin/sh\nexec ../run-global-hooks pre-commit\n", encoding="utf-8")
    monkeypatch.setattr("riglib.drift._git_global", lambda k: str(composer))
    report = DriftReport()
    check_disabled_dispatcher(tmp_path, report)
    assert any(i.category == "git_hooks" and i.direction == "extra" for i in report.items)


def test_disabled_dispatcher_unrelated_hookspath_not_flagged(tmp_path, monkeypatch):
    from riglib.drift import DriftReport, check_disabled_dispatcher

    composer = tmp_path / "hooks"
    composer.mkdir()
    (composer / "pre-commit").write_text("#!/bin/sh\necho someone elses hook\n", encoding="utf-8")
    monkeypatch.setattr("riglib.drift._git_global", lambda k: str(composer))
    report = DriftReport()
    check_disabled_dispatcher(tmp_path, report)
    assert not report.items  # not a rig dispatcher → not flagged


def test_drift_dispatcher_composer_modified(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # edit a non-pre-commit composer → must be flagged
    (home / "hooks" / "commit-msg").write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "git_hooks" and i.item == "commit-msg" and i.direction == "modified" for i in report.items)


def test_write_file_replaces_stale_directory(tmp_path):
    from riglib.actions import fsutil

    target = tmp_path / "codeql.yml"
    target.mkdir()  # a stale DIRECTORY where a file should be
    (target / "junk").write_text("x", encoding="utf-8")
    out = fsutil.write_file(target, "name: codeql\n", "backup")
    assert out.status == "backed_up"
    assert target.is_file()
    assert target.read_text() == "name: codeql\n"


def test_drift_dispatcher_fragment_deleted(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # delete an installed fragment → config→disk drift
    (home / "ghd" / "pre-commit" / "10-secret-scan").unlink()
    report = detect(plan)
    assert any(i.category == "git_hooks" and i.direction == "missing" and "secret-scan" in i.item for i in report.items)


def test_dispatcher_fragments_filtered(fake_agent_tools, tmp_path, monkeypatch):
    """A disabled fragment is not copied into global-hooks.d."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "git_hooks": {
                "dispatcher": {
                    "enabled": True,
                    "dir": str(home / "ghd"),
                    "runner": str(home / "run-global-hooks"),
                    "set_global_hooks_path": False,
                    "install_local_retrofit_script": False,
                    "fragments": {"conventional-commit": {"enabled": False}},
                }
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    ghd = home / "ghd"
    # secret-scan fragment present, conventional-commit fragment filtered out
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()
    assert not (ghd / "commit-msg" / "10-conventional-commit").exists()


def test_drift_ci_content_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # tamper an installed workflow in place
    (repo / ".github/workflows/codeql.yml").write_text("name: tampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "ci" and i.item == "codeql" for i in report.items)


def test_drift_ci_companion_deleted(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    (repo / "ci/leftover-grep/leftover-grep.sh").unlink()
    report = detect(plan)
    assert any(i.category == "ci" and i.direction == "missing" and "leftover-grep" in i.item for i in report.items)


def test_drift_disabled_fragment_still_on_disk(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    base = {
        "agent_tools_source": str(fake_agent_tools),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
    }
    disp = {"enabled": True, "dir": str(home / "ghd"), "runner": str(home / "run-global-hooks"),
            "set_global_hooks_path": False, "install_local_retrofit_script": False}
    # install everything (fragment present)
    cfg_on = LoadedConfig(data={**base, "git_hooks": {"dispatcher": disp}}, repo_root=repo)
    run_plan(build(cfg_on, cat, project_type="unknown"))
    assert (home / "ghd" / "commit-msg" / "10-conventional-commit").is_file()
    # now the config disables conventional-commit, but the file remains → drift
    cfg_off = LoadedConfig(
        data={**base, "git_hooks": {"dispatcher": {**disp, "fragments": {"conventional-commit": {"enabled": False}}}}},
        repo_root=repo,
    )
    report = detect(build(cfg_off, cat, project_type="unknown"))
    assert any(i.category == "git_hooks" and i.direction == "extra" and "conventional-commit" in i.item for i in report.items)


def test_drift_extra_workflow_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    (repo / ".github/workflows/manual.yml").write_text("name: manual\n", encoding="utf-8")
    report = detect(plan)
    extras = report.by_direction("extra")
    assert any(i.category == "ci" and i.item == "manual" for i in extras), [(i.category, i.item) for i in extras]


def test_drift_ship_content_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ship_dir = repo / "bin"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"items": {"ship": {"enabled": True, "install_to": str(ship_dir)}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    (ship_dir / "ship").write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "ci" and i.item == "ship" for i in report.items)


def test_drift_extra_hook_descriptor_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    hooks_dir = repo / "hooks-out"
    (hooks_dir / "rogue-hook.pre-bash.json").write_text("{}", encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "agent_hooks" and i.direction == "extra" for i in report.items)


def test_overwrite_file_where_dir_expected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    skills_out = repo / "skills-out"
    skills_out.mkdir(parents=True)
    # a stale FILE sits where the skill DIR should go
    (skills_out / "naming").write_text("stale file\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"skills_target": str(skills_out), "on_conflict": "overwrite"},
            "skills": {"universal": {"enable": ["naming"], "all": False}}, "by_type": {},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (skills_out / "naming").is_dir()  # the file was replaced by the dir


def test_drift_extra_mcp_detected(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["rogue"] = {"command": "rogue", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.category == "mcp" and i.item == "rogue" for i in report.by_direction("extra"))


def test_fragments_merge_preserves_unrelated(fake_agent_tools, tmp_path, monkeypatch):
    """Installing fragments must not clobber an unrelated drop-in already present."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    ghd = home / "ghd"
    # a user fragment that rig does not ship
    (ghd / "pre-commit").mkdir(parents=True)
    (ghd / "pre-commit" / "99-my-own").write_text("#!/bin/sh\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(ghd),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    assert (ghd / "pre-commit" / "99-my-own").is_file()  # unrelated fragment survives
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()  # rig's fragment installed


def test_mcp_malformed_config_backed_up_not_discarded(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_dir = repo / "mcp-out"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text("{ this is not json", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors, [r.detail for r in report.errors]
    # a backup of the malformed file exists
    assert any(p.name.startswith("mcp.json.rig-bak-") for p in mcp_dir.iterdir())


# ── skill → harness-discovery symlink ──────────────────────────────────────────────
def _skill_link_cfg(repo: Path, source: Path, *, harness_dir: Path | None,
                    skills_target: Path | None = None, harness_link=None) -> LoadedConfig:
    skills: dict = {"universal": {"enable": ["naming"], "all": False}, "by_type": {}}
    if harness_dir is not None:
        skills["harness_skill_dir"] = str(harness_dir)
    if harness_link is not None:
        skills["harness_link"] = harness_link
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {"skills_target": str(skills_target or repo / "skills-out"), "on_conflict": "backup"},
            "skills": skills,
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
        },
        repo_root=repo,
    )


def test_skill_harness_link_created_and_resolves(fake_agent_tools, tmp_path):
    """Apply symlinks each installed skill into the harness dir; the link resolves to it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    skills_out = repo / "skills-out"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = harness / "naming"
    assert link.is_symlink(), "skill not symlinked into the harness dir"
    assert link.resolve() == (skills_out / "naming").resolve()
    assert (link / "SKILL.md").is_file()  # the link resolves to a real skill


def test_skill_harness_link_idempotent(fake_agent_tools, tmp_path):
    """A re-apply with a correct existing symlink is a no-op (skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    run_plan(plan)
    second = run_plan(plan)
    link_results = [r for r in second.results if r.action.kind == "link_skill_harness"]
    assert link_results, "no link action emitted"
    assert all(r.status == "skipped" for r in link_results), [r.detail for r in link_results]


def test_skill_harness_link_repoints_wrong_symlink(fake_agent_tools, tmp_path):
    """A symlink pointing at the WRONG destination is re-pointed, not left stale."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    skills_out = repo / "skills-out"
    bogus = repo / "somewhere-else"
    bogus.mkdir()
    (harness / "naming").symlink_to(bogus)  # stale link to the wrong place
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = harness / "naming"
    assert link.resolve() == (skills_out / "naming").resolve()  # re-pointed to the real skill
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert any(r.status == "updated" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_leaves_real_dir_untouched(fake_agent_tools, tmp_path):
    """A REAL (non-symlink) skill dir already at the harness path is never clobbered."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    # a hand-authored skill (like h-reason / debate-swarm) lives at the harness path
    real_skill = harness / "naming"
    real_skill.mkdir()
    (real_skill / "SKILL.md").write_text("hand-authored\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert not real_skill.is_symlink()  # left as a real dir
    assert (real_skill / "SKILL.md").read_text() == "hand-authored\n"  # content preserved
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert all(r.status == "skipped" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_disabled_emits_no_action(fake_agent_tools, tmp_path):
    """skills.harness_link: false → no link actions planned at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, harness_link=False),
                 cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"]
    run_plan(plan)
    assert not (harness / "naming").exists()  # nothing linked


def test_skill_harness_link_default_dir_under_home(fake_agent_tools, tmp_path, monkeypatch):
    """With no harness_skill_dir set, the default is ~/.claude/skills (HOME-expanded)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=None), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / ".claude" / "skills" / "naming"
    assert link.is_symlink()
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()


def test_skill_harness_link_no_self_link_when_target_is_harness_dir(fake_agent_tools, tmp_path):
    """If skills_target already IS the harness dir, no self-referential link is planned."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "shared-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=shared, skills_target=shared),
                 cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"]


# ── per-harness skill/instruction discovery (rig-cli#9) ─────────────────────────────
def _harness_skill_cfg(repo: Path, source: Path, kind: str) -> LoadedConfig:
    """A config that pins ``harness.kind`` and lets the PER-HARNESS default discovery dir resolve.

    No ``harness_skill_dir`` override: the skill-link dir (or the lack of one, for an
    instruction-file harness) comes purely from the harness kind — exactly what these tests
    exercise. HOME must be isolated by the caller (the default dirs are HOME-relative).
    """
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "defaults": {"skills_target": str(repo / "skills-out"), "on_conflict": "backup"},
            "skills": {"universal": {"enable": ["naming"], "all": False}, "by_type": {}},
            "harness": {"kind": kind},
            "agent_hooks": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # default-on areas that would otherwise try to reach HOME / a github remote — off, to
            # keep these unit tests hermetic and focused on the skill-discovery surface.
            "agents_md": {"enabled": False}, "github": {"ruleset": {"enabled": False},
            "merge": {"enabled": False}, "ghas": {"enabled": False}, "actions": {"enabled": False},
            "browser": {"enabled": False}}, "tg_ctl": {"enabled": False},
            "gitignore": {"enabled": False}, "ship_delegator": {"enabled": False},
            "permissions": {"enabled": False},
        },
        repo_root=repo,
    )


@pytest.mark.parametrize(
    "kind, rel_dir",
    [("claude-code", ".claude/skills"), ("codex", ".codex/skills")],
)
def test_skills_dir_harness_links_into_its_own_dir(fake_agent_tools, tmp_path, monkeypatch, kind, rel_dir):
    """A skills-DIRECTORY harness (claude-code, codex) gets each skill symlinked into ITS dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / rel_dir / "naming"
    assert link.is_symlink(), f"{kind}: skill not symlinked into {rel_dir}"
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()
    assert (link / "SKILL.md").is_file()


@pytest.mark.parametrize("kind", ["claude-code", "codex"])
def test_skills_dir_harness_link_idempotent_and_drift(fake_agent_tools, tmp_path, monkeypatch, kind):
    """A skills-dir harness link is idempotent (re-apply = skipped) and drift-free once synced."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    # before apply: the missing harness link is reported as config→disk drift
    pre = detect(plan)
    assert [d for d in pre.items if "harness link" in d.item], f"{kind}: missing link not flagged"
    run_plan(plan)
    # after apply: no harness-link drift remains
    post = detect(plan)
    assert not [d for d in post.items if "harness link" in d.item], f"{kind}: drift after apply"
    # re-apply is a pure no-op for the link
    second = run_plan(plan)
    links = [r for r in second.results if r.action.kind == "link_skill_harness"]
    assert links and all(r.status == "skipped" for r in links), [r.detail for r in links]


@pytest.mark.parametrize(
    "kind, instr_marker",
    [("gemini", "GEMINI.md"), ("pi", "AGENTS.md"), ("commandcode", "AGENTS.md")],
)
def test_instruction_file_harness_emits_no_link_but_a_note(fake_agent_tools, tmp_path, monkeypatch, kind, instr_marker):
    """An INSTRUCTION-FILE harness (gemini/pi/commandcode) links no skill but records WHY.

    No skills dir exists for these kinds, so rig emits zero ``link_skill_harness`` actions (it never
    guesses a dir). The skill is still COPIED to skills_target; a plan note explains the kind reads a
    global AGENTS.md/GEMINI.md instead — so ``rig status`` isn't a silent empty area.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    # no harness symlink action for an instruction-file harness
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"], f"{kind}: unexpected link"
    # the skill is still installed (copy_skill) — it reaches the harness via the instruction file
    assert [a for a in plan.actions if a.kind == "copy_skill"], f"{kind}: skill not even copied"
    # a note names the instruction file (so the empty link area is explained, not silent)
    notes = " ".join(plan.notes)
    assert kind in notes and instr_marker in notes, f"{kind}: no explanatory note ({plan.notes})"
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


def test_instruction_file_harness_with_explicit_dir_does_link(fake_agent_tools, tmp_path):
    """An explicit ``harness_skill_dir`` overrides the no-link default even for gemini (user opt-in).

    gemini is instruction-file-only (no skills dir), so it links nothing by default; an explicit
    dir forces a real link and suppresses the instruction-file note.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "gemini-skills"
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "gemini")
    cfg.data["skills"]["harness_skill_dir"] = str(harness)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    # the explicit dir forces a real link (and suppresses the instruction-file note)
    assert [a for a in plan.actions if a.kind == "link_skill_harness"], "explicit dir did not link"
    assert not any("instruction file" in n for n in plan.notes), plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (harness / "naming").resolve() == (repo / "skills-out" / "naming").resolve()


@pytest.mark.parametrize("kind", ["opencode"])
def test_native_discovery_harness_links_nothing_but_notes_autoload(fake_agent_tools, tmp_path, monkeypatch, kind):
    """A NATIVE-DISCOVERY harness (opencode) auto-loads skills_target, so rig links NOTHING but
    records WHY — ``rig status`` says "discovers natively", not a silent empty area or a pointless
    symlink into a dir the harness never reads.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_skill_cfg(repo, fake_agent_tools, kind), cat, project_type="unknown")
    # no harness symlink for a native-discovery harness (it reads skills_target directly)...
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"], f"{kind}: unexpected link"
    # ...but the skill IS still copied to skills_target (which the harness auto-scans)...
    assert [a for a in plan.actions if a.kind == "copy_skill"], f"{kind}: skill not copied"
    # ...and a note explains it auto-loads natively (so the empty link area is not a silent gap).
    notes = " ".join(plan.notes)
    assert kind in notes and "natively" in notes and ".agents/skills" in notes, plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


@pytest.mark.parametrize("kind", ["opencode", "codex", "gemini", "pi", "commandcode"])
def test_auto_mode_on_non_claude_kind_skips_write_with_note(fake_agent_tools, tmp_path, kind):
    """A kind with no auto/permission-MODE writer self-skips the write — but says so, not silently.

    The schema now accepts these kinds (for skill provisioning); the auto-mode WRITE is
    claude-code-only. Setting ``auto_mode`` on such a kind must NOT emit an ``apply_harness``
    action AND must leave a note, so the request is never a silent no-op (the failure mode this
    PR set out to remove).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["harness"]["auto_mode"] = True
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "apply_harness"], f"{kind}: unexpected write"
    assert any("auto-mode write skipped" in n and kind in n for n in plan.notes), plan.notes


@pytest.mark.parametrize("kind", ["opencode", "codex"])
def test_explicit_hook_bridge_on_non_claude_kind_notes_skip(fake_agent_tools, tmp_path, kind):
    """Explicitly enabling hook_bridge on a non-claude kind is reported skipped, not silently dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["harness"]["hook_bridge"] = {"enabled": True}
    cfg.data["agent_hooks"] = {"all": True}  # bridge is only relevant with hooks present
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "register_hook_bridge"], f"{kind}: unexpected bridge"
    assert any("hook_bridge: skipped" in n and kind in n for n in plan.notes), plan.notes


def test_no_skill_discovery_note_when_skills_disabled(fake_agent_tools, tmp_path):
    """With skills.enabled: false, an instruction-file harness gets NO discovery note (nothing installed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "codex")
    cfg.data["skills"] = {"enabled": False}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    assert not any("instruction file" in n for n in plan.notes), plan.notes
    assert not [a for a in plan.actions if a.category == "skills"], "no skill actions when disabled"


def test_skill_harness_link_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Drift: a missing harness link is config→disk drift; after apply it is in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing")
            if d.category == "skills" and "harness link" in d.item]
    assert miss, "expected a missing harness-link drift item"
    run_plan(plan)
    rep2 = detect(plan)
    assert not [d for d in rep2.items if "harness link" in d.item]


def test_skill_harness_link_drift_wrong_dest_is_modified(fake_agent_tools, tmp_path):
    """A symlink to the wrong destination surfaces as 'modified' drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    bogus = repo / "elsewhere"
    bogus.mkdir()
    (harness / "naming").symlink_to(bogus)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified")
           if d.category == "skills" and "harness link" in d.item]
    assert mod, "expected a modified harness-link drift item for the wrong symlink"


def test_skill_harness_link_repoints_broken_symlink(fake_agent_tools, tmp_path):
    """A BROKEN symlink (target does not exist) is re-pointed to the real installed skill."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    skills_out = repo / "skills-out"
    (harness / "naming").symlink_to(repo / "ghost")  # dangling — target never created
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness, skills_target=skills_out),
                 cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (harness / "naming").resolve() == (skills_out / "naming").resolve()
    link_res = [r for r in report.results if r.action.kind == "link_skill_harness"]
    assert any(r.status == "updated" for r in link_res), [r.detail for r in link_res]


def test_skill_harness_link_errors_when_install_target_missing(fake_agent_tools, tmp_path):
    """If the installed skill the link should point at is absent, the link action errors
    (and does not leave a dangling symlink) — exercised by running the link action alone."""
    from riglib.actions.runner import _do_link_skill_harness
    from riglib.plan import Action

    repo = tmp_path / "repo"
    harness = repo / "harness-skills"
    action = Action(
        kind="link_skill_harness", category="skills", item="naming",
        source=repo / "skills-out" / "naming",  # never created
        target=harness / "naming",
    )
    res = _do_link_skill_harness(action, "backup")
    assert res.status == "error"
    assert not (harness / "naming").exists()  # no dangling link created


def test_skill_harness_link_unknown_kind_emits_no_link(fake_agent_tools, tmp_path):
    """An unknown harness.kind with no harness_skill_dir → rig does not guess a path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    # bypass config validation (it would reject an unknown kind) to exercise the plan guard:
    # the resolver must return None for a kind with no known discovery dir.
    from riglib.plan import _resolve_harness_skill_dir

    cfg = LoadedConfig(
        data={"harness": {"kind": "claude-code"}, "skills": {}},  # known kind, link on
        repo_root=repo,
    )
    assert _resolve_harness_skill_dir(cfg) is not None  # known kind resolves
    cfg_unknown = LoadedConfig(data={"skills": {}}, repo_root=repo)
    # monkeypatch the kind map to simulate a kind rig knows of but has no dir for
    import riglib.plan as planmod

    saved = dict(planmod._HARNESS_SKILL_DIRS)
    try:
        planmod._HARNESS_SKILL_DIRS.clear()  # no known dir for any kind
        assert _resolve_harness_skill_dir(cfg_unknown) is None
        plan = build(LoadedConfig(
            data={"agent_tools_source": str(fake_agent_tools),
                  "defaults": {"skills_target": str(repo / "skills-out")},
                  "skills": {"universal": {"enable": ["naming"], "all": False}, "by_type": {}},
                  "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
                  "mcp": {"enabled": False}, "git_hooks": {"dispatcher": {"enabled": False}}},
            repo_root=repo), cat, project_type="unknown")
        assert not [a for a in plan.actions if a.kind == "link_skill_harness"]
    finally:
        planmod._HARNESS_SKILL_DIRS.clear()
        planmod._HARNESS_SKILL_DIRS.update(saved)


def test_skill_harness_link_follows_harness_kind(fake_agent_tools, tmp_path):
    """The skill-link discovery dir follows harness.kind when a harness block pins one."""
    from riglib.plan import _resolve_harness_skill_dir

    repo = tmp_path / "repo"
    cfg = LoadedConfig(
        data={"harness": {"kind": "claude-code", "auto_mode": True}, "skills": {}},
        repo_root=repo,
    )
    resolved = _resolve_harness_skill_dir(cfg)
    assert resolved is not None
    assert resolved.name == "skills" and ".claude" in str(resolved)


def test_skill_harness_link_real_dir_not_flagged_as_drift(fake_agent_tools, tmp_path):
    """A real dir at the harness path is NOT reported as drift (rig won't touch it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "harness-skills"
    harness.mkdir()
    real = harness / "naming"
    real.mkdir()
    (real / "SKILL.md").write_text("real\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_skill_link_cfg(repo, fake_agent_tools, harness_dir=harness), cat, project_type="unknown")
    rep = detect(plan)
    assert not [d for d in rep.items if "harness link" in d.item]


def test_mcp_command_shell_quoting(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"),
                    "items": {"review": {"enabled": True, "command": '"/opt/my tools/run" --flag "a b"'}}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    entry = data["mcpServers"]["review"]
    assert entry["command"] == "/opt/my tools/run"  # quoted path with spaces kept whole
    assert entry["args"] == ["--flag", "a b"]  # quotes consumed, inner spaces preserved


# ── harness (auto-mode / permission provisioning) ─────────────────────────────────
def _harness_cfg(repo: Path, source: Path, **harness) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(repo / ".claude/settings.json"), **harness},
        },
        repo_root=repo,
    )


def test_harness_apply_writes_mode_and_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert any(r.status == "created" for r in first.results if r.action.category == "harness")

    second = run_plan(plan)  # idempotent: same value → skipped, no change
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)


def test_harness_apply_preserves_other_keys(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "permissions": {"allow": ["Bash"]}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text(encoding="utf-8"))
    # the managed key is set; unrelated keys survive
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash"]
    assert data["model"] == "opus"


def test_harness_conflicting_value_backed_up(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors
    # a differing prior value is backed up before converging (default policy = backup)
    assert any(p.name.startswith("settings.json.rig-bak-") for p in (repo / ".claude").iterdir())
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"


def test_harness_drift_missing_then_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_harness_cfg(repo, fake_agent_tools, mode="acceptEdits"), cat, project_type="unknown")

    # nothing on disk yet → missing
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing") if d.category == "harness"]
    assert miss, "expected a missing harness drift item"

    # apply, then flip the value → modified drift
    run_plan(plan)
    settings = repo / ".claude" / "settings.json"
    d = json.loads(settings.read_text(encoding="utf-8"))
    d["permissions"]["defaultMode"] = "default"
    settings.write_text(json.dumps(d), encoding="utf-8")
    rep2 = detect(plan)
    mod = [x for x in rep2.by_direction("modified") if x.category == "harness"]
    assert mod and "acceptEdits" in mod[0].detail

    # re-apply converges, back in sync
    run_plan(plan)
    rep3 = detect(plan)
    assert not [x for x in rep3.items if x.category == "harness"]


def test_harness_auto_writes_user_settings_not_repo(fake_agent_tools, tmp_path):
    # auto_mode:true with NO settings_path → CC `auto` is written to the USER settings file
    # (~/.claude/settings.json, HOME-isolated to tmp by the autouse fixture), NOT the repo —
    # CC ignores defaultMode:auto at project scope, so committing it per-repo would be a no-op.
    import os

    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # ship_delegator is default-ON and writes <repo>/.claude/scripts/pr-ship.sh, which would
            # create repo/.claude — off here so the assertion below isolates the HARNESS behavior.
            "ship_delegator": {"enabled": False},
            "harness": {"kind": "claude-code", "auto_mode": True},  # no settings_path → user scope
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    user_settings = Path(os.path.expanduser("~/.claude/settings.json"))

    run_plan(plan)
    assert not (repo / ".claude").exists(), "auto must NOT write into the repo's project settings"
    data = json.loads(user_settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"

    # idempotent
    second = run_plan(plan)
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)

    # flip the value → modified drift detected on the user settings file
    data["permissions"]["defaultMode"] = "default"
    user_settings.write_text(json.dumps(data), encoding="utf-8")
    mod = [x for x in detect(plan).by_direction("modified") if x.category == "harness"]
    assert mod and "auto" in mod[0].detail


def test_mcp_registers_under_configured_server_name(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"), "items": {"review": {"enabled": True, "server": "custom-name", "command": "x --mcp"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert "custom-name" in data["mcpServers"]  # registered under the server name
    assert "review" not in data["mcpServers"]


def test_drift_agent_hook_descriptor_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    descriptor = repo / "hooks-out" / "block-no-verify.pre-bash.json"
    spec = json.loads(descriptor.read_text())
    spec["cmd"] = "/tampered/path"
    descriptor.write_text(json.dumps(spec), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "agent_hooks" for i in report.items)


def test_drift_mcp_command_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["review"] = {"command": "different", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "review" for i in report.items)


def test_agent_hook_cmd_rewritten_absolute(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    descriptor = repo / "hooks-out" / "block-no-verify.pre-bash.json"
    assert descriptor.is_file()
    spec = json.loads(descriptor.read_text())
    assert "/ABSOLUTE/PATH/TO/" not in spec["cmd"]
    assert spec["cmd"].startswith(str(fake_agent_tools))
    assert Path(spec["cmd"]).is_absolute()


def test_ci_companion_root_with_custom_target(fake_agent_tools, tmp_path):
    """With a non-default ci.target, companions still vendor at the checkout root ci/<slot>/."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".ci/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".ci/workflows/leftover-grep.yml").is_file()  # workflow at custom target
    assert (repo / "ci/leftover-grep/leftover-grep.sh").is_file()  # companion at CHECKOUT root
    assert not (repo / ".ci/ci").exists()  # NOT mis-anchored under the custom target


def test_ci_missing_variant_fails_closed(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"),
                   "items": {"codeql": {"enabled": True, "variant": "nonexistent"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert report.errors  # requested variant missing → error, not silent default
    assert any("variant" in r.detail for r in report.errors)


def test_identical_content_restores_exec_bit(fake_agent_tools, tmp_path, monkeypatch):
    """A managed script with identical content but a lost exec bit gets it restored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(home / "run-global-hooks"),
                                         "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    runner = home / "run-global-hooks"
    runner.chmod(0o644)  # lose the exec bit (content unchanged)
    run_plan(plan)  # re-apply: identical content, but exec bit must be restored
    assert (runner.stat().st_mode & 0o111) != 0


def test_mcp_backup_converges_differing_entry(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "backup"},
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {"target": str(repo / "mcp-out"), "items": {"review": {"enabled": True, "command": "review --mcp"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # tamper the registered entry, then re-apply under backup → must converge + back up
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["review"] = {"command": "stale", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = run_plan(plan)
    assert not report.errors
    converged = json.loads(mcp_json.read_text())
    assert converged["mcpServers"]["review"] == {"command": "review", "args": ["--mcp"]}
    assert any(p.name.startswith("mcp.json.rig-bak-") for p in (repo / "mcp-out").iterdir())


def test_skip_conflict_does_not_chmod(fake_agent_tools, tmp_path, monkeypatch):
    """on_conflict=skip must leave an existing different file's mode untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    runner_path = home / "run-global-hooks"
    runner_path.parent.mkdir(parents=True)
    runner_path.write_text("#!/bin/sh\nexisting\n", encoding="utf-8")
    runner_path.chmod(0o644)  # NOT executable
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "skip"},
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": True, "dir": str(home / "ghd"),
                                         "runner": str(runner_path), "set_global_hooks_path": False,
                                         "install_local_retrofit_script": False}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    # skip left the existing runner alone → mode unchanged (still 0o644, no exec bit)
    assert (runner_path.stat().st_mode & 0o111) == 0


def test_ci_companion_script_vendored(fake_agent_tools, tmp_path):
    """A workflow that shells out to ci/<slot>/<slot>.sh must get that script vendored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ci": {"target": str(repo / ".github/workflows"), "items": {"leftover-grep": {"enabled": True}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".github/workflows/leftover-grep.yml").is_file()
    assert (repo / "ci/leftover-grep/leftover-grep.sh").is_file()  # companion vendored


def test_secret_scan_workflow_resolves_slotnamed(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / ".github/workflows/secret-scan.yml").is_file()


def test_mcp_idempotent_merge(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    assert data["mcpServers"]["review"]["command"] == "review"
    assert data["mcpServers"]["review"]["args"] == ["--mcp"]


def test_backup_on_conflict(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _full_cfg(repo, fake_agent_tools)
    plan = build(cfg, cat, project_type="cli")
    run_plan(plan)
    # mutate an installed CI workflow, then re-apply: backup must be made
    wf = repo / ".github/workflows/codeql.yml"
    wf.write_text("name: tampered\n", encoding="utf-8")
    report = run_plan(plan)
    backed = [r for r in report.results if r.status == "backed_up"]
    assert any(r.action.item == "codeql" for r in backed)
    assert any(p.name.startswith("codeql.yml.rig-bak-") for p in wf.parent.iterdir())


def test_dry_run_writes_nothing(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    report = run_plan(plan, dry_run=True)
    assert all(r.status == "planned" for r in report.results)
    assert not (repo / "skills-out").exists()


def test_drift_missing_then_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    # before apply: everything declared is missing
    before = detect(plan)
    assert not before.in_sync
    assert before.by_direction("missing")
    # after apply: in sync
    run_plan(plan)
    after = detect(plan)
    assert after.in_sync, [i.detail for i in after.items]


def test_drift_detects_extra_skill(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # plant an undeclared skill on disk
    extra = repo / "skills-out" / "rogue-skill"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text("---\nname: rogue\n---\n", encoding="utf-8")
    report = detect(plan)
    extras = report.by_direction("extra")
    assert any(i.item == "rogue-skill" for i in extras), [i.item for i in extras]


def test_drift_detects_modified_skill(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_full_cfg(repo, fake_agent_tools), cat, project_type="cli")
    run_plan(plan)
    # tamper an installed skill
    (repo / "skills-out" / "shell-timeouts" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.item == "shell-timeouts" for i in report.items)


# ── hook bridge: register the cc_hook_bridge dispatcher in settings.json ─────────────
def _bridge_cfg(repo_root: Path, source: Path, *, settings_path: Path,
                hook_bridge: dict | None = None) -> LoadedConfig:
    harness: dict = {"kind": "claude-code", "auto_mode": True,
                     "settings_path": str(settings_path)}
    if hook_bridge is not None:
        harness["hook_bridge"] = hook_bridge
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": harness,
        },
        repo_root=repo_root,
    )


def _bridge_results(report):
    return [r for r in report.results if r.action.kind == "register_hook_bridge"]


def test_hook_bridge_registers_dispatcher_in_settings(fake_agent_tools, tmp_path):
    """Apply wires PreToolUse (Bash + write tools) and Stop hooks into settings.json."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"

    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    # PreToolUse: two matchers (Bash + the write-tool alternation), both → cc_hook_bridge
    pre = hooks["PreToolUse"]
    matchers = {b["matcher"] for b in pre}
    assert "Bash" in matchers
    assert "Edit|Write|MultiEdit|NotebookEdit" in matchers
    for b in pre:
        cmd = b["hooks"][0]["command"]
        assert "cc_hook_bridge PreToolUse" in cmd
        # PYTHONPATH anchors on the agent-tools checkout lib/
        assert str(fake_agent_tools / "lib") in cmd
    # Stop: one block, no matcher (match-all), → cc_hook_bridge Stop
    stop = hooks["Stop"]
    assert len(stop) == 1 and "matcher" not in stop[0]
    assert "cc_hook_bridge Stop" in stop[0]["hooks"][0]["command"]


def test_hook_bridge_idempotent_reapply(fake_agent_tools, tmp_path):
    """A second apply is a no-op (skipped) — no duplicate hook blocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    # exactly one Bash block, not duplicated
    data = json.loads(settings.read_text())
    bash_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Bash"]
    assert len(bash_blocks) == 1


def test_hook_bridge_preserves_existing_unrelated_hooks(fake_agent_tools, tmp_path):
    """The user's own hooks (rtk-rewrite, tg-ctl) survive registration untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "permissions": {"defaultMode": "default"},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/Users/x/.claude/hooks/rtk-rewrite.sh"}]},
            ],
            "Notification": [
                {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "afplay glass.aiff"}]},
            ],
        },
    }, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = json.loads(settings.read_text())
    # the rtk hook is still there (now ALONGSIDE our Bash block, not replacing it)
    bash_cmds = [hk["command"] for b in data["hooks"]["PreToolUse"]
                 for hk in b["hooks"] if b.get("matcher") == "Bash"]
    assert any("rtk-rewrite.sh" in c for c in bash_cmds), bash_cmds
    assert any("cc_hook_bridge PreToolUse" in c for c in bash_cmds), bash_cmds
    # unrelated event preserved verbatim
    assert data["hooks"]["Notification"][0]["hooks"][0]["command"] == "afplay glass.aiff"
    # the permissions block survives the hooks merge (apply_harness, which also runs under
    # auto_mode, may set its mode — the point here is the hook-bridge merge doesn't drop it).
    assert "permissions" in data


def test_hook_bridge_repoints_drifted_command(fake_agent_tools, tmp_path):
    """A managed block whose command's lib path drifted is rewritten in place (not dup'd)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    # a stale managed entry pointing at an OLD lib path
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
    ]}}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Bash"]
    assert len(bash_blocks) == 1  # rewritten, not duplicated
    cmd = bash_blocks[0]["hooks"][0]["command"]
    assert "/old/path/lib" not in cmd
    assert str(fake_agent_tools / "lib") in cmd


def test_hook_bridge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Before apply the bridge is missing drift; after apply, in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "missing" for i in before.items), \
        [i.detail for i in before.items]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_detects_stale_command(fake_agent_tools, tmp_path):
    """A managed hook whose command drifted (old lib path) is reported MODIFIED, not in-sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
        ],
        "Stop": [{"hooks": [{"type": "command",
                  "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge Stop"}]}],
    }}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = detect(plan)
    modified = [i for i in report.items if i.item == "hook-bridge" and i.direction == "modified"]
    assert modified, [i.detail for i in report.items if i.item == "hook-bridge"]
    # and apply converges it back to in-sync
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_skip_leaves_stale_command_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip must NOT rewrite a drifted managed command (file-level skip parity)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    stale = "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": stale}]},
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": stale}]},
    ], "Stop": [{"hooks": [{"type": "command", "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge Stop"}]}]}}, indent=2))
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    data = json.loads(settings.read_text())
    bash_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Bash")
    assert bash_cmd == stale  # left untouched under skip


def test_hook_bridge_skip_leaves_malformed_settings_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip leaves a malformed settings.json untouched (returns skipped)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ this is not json")
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text() == "{ this is not json"  # untouched


def test_hook_bridge_quotes_python_interpreter(fake_agent_tools, tmp_path):
    """A configured python path is shlex-quoted in the command (spaces / injection safe)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings,
                      hook_bridge={"python": "/opt/my py/bin/python3"})
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Bash")
    # the space-containing interpreter path must be quoted, not left bare
    assert "'/opt/my py/bin/python3'" in bash_cmd, bash_cmd
