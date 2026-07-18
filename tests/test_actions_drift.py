"""Action execution (idempotency, backup, absolute-cmd rewrite) + two-way drift."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.actions.runner import (
    SELF_MERGE_CARVE_OUT,
    SELF_MERGE_PERMISSIONS_ALLOW,
    desired_mcp_server_entry,
)
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.plan import build

def _user_settings() -> Path:
    # computed per call, NOT a module constant: the autouse fixture monkeypatches HOME per test,
    # so a constant captured at import time would point at the real ~/.claude and break isolation.
    return Path(os.path.expanduser("~/.claude/settings.json"))


def test_cc_and_codex_dispatchers_read_custom_hooks_dir_env(fake_agent_tools, tmp_path, monkeypatch):
    """Bridge commands and dispatcher modules must agree on custom descriptor-dir env names."""
    lib_dir = fake_agent_tools / "lib"
    monkeypatch.syspath_prepend(str(lib_dir))
    monkeypatch.setenv("CC_HOOKS_DIR", str(tmp_path / "cc-hooks"))
    monkeypatch.setenv("CODEX_HOOKS_DIR", str(tmp_path / "codex-hooks"))
    for name in (
        "cc_hook_bridge",
        "cc_hook_bridge.dispatch",
        "codex_hook_bridge",
        "codex_hook_bridge.dispatch",
    ):
        sys.modules.pop(name, None)

    from cc_hook_bridge import dispatch as cc_dispatch
    from codex_hook_bridge import dispatch as codex_dispatch

    assert cc_dispatch.hooks_dir() == tmp_path / "cc-hooks"
    assert codex_dispatch.hooks_dir() == tmp_path / "codex-hooks"


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
            "mcp": {"items": {"fake-mcp": {"enabled": True, "command": "fake-mcp --serve"}}},
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


def test_multi_descriptor_hook_installs_all_stays_clean(fake_agent_tools, tmp_path):
    """A hook dir with TWO descriptors: both write, re-apply skips both, drift is clean (#184).

    End-to-end proof (not just planned actions): the fake ``dual-guard`` hook ships a pre-bash
    AND a pre-write descriptor. Both must land on disk, a second ``run_plan`` must skip both,
    and ``detect`` must report neither missing nor extra.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    hooks_dir = tmp_path / "hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(hooks_dir)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    assert (hooks_dir / "dual-guard.pre-bash.json").is_file()
    assert (hooks_dir / "dual-guard.pre-write.json").is_file()

    second = run_plan(plan)
    dual = [r for r in second.results if r.action.item == "dual-guard"]
    assert len(dual) == 2
    assert all(r.status == "skipped" for r in dual), [r.detail for r in dual]

    report = detect(plan, scan_hook_dirs=[hooks_dir])
    hook_drift = [i for i in report.items if i.category == "agent_hooks" and "dual-guard" in i.item]
    assert not hook_drift, [(i.direction, i.item) for i in hook_drift]


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


def _dispatcher_cfg(fake_agent_tools, repo, home, fragments=None):
    """A minimal LoadedConfig with ONLY the dispatcher enabled (shared by the
    pre-push fragment tests below)."""
    disp = {
        "enabled": True,
        "dir": str(home / "ghd"),
        "runner": str(home / "run-global-hooks"),
        "set_global_hooks_path": False,
        "install_local_retrofit_script": False,
    }
    if fragments is not None:
        disp["fragments"] = fragments
    return LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": disp},
        },
        repo_root=repo,
    )


def test_prepush_fragment_installed_executable_and_drifts(fake_agent_tools, tmp_path, monkeypatch):
    """A PRE-PUSH event fragment (protect-main, HYP-856) is provisioned like any other
    fragment: installed per-file + executable, and a deletion is config→disk drift.

    Pins that the fragment pipeline is EVENT-GENERIC — nothing in install/drift may
    special-case pre-commit; protect-main is the first shipped pre-push fragment."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_dispatcher_cfg(fake_agent_tools, repo, home), cat, project_type="unknown")
    run_plan(plan)
    frag = home / "ghd" / "pre-push" / "10-protect-main"
    assert frag.is_file()
    assert os.access(frag, os.X_OK)  # git ignores a non-executable hook
    # content-modified drift is detected too (the pipeline is event-generic for
    # BOTH directions, matching test_drift_ci_content_modified for ci items)
    frag.write_text("#!/bin/sh\ntampered\n", encoding="utf-8")
    report = detect(plan)
    assert any(
        i.category == "git_hooks" and i.direction == "modified" and "protect-main" in i.item
        for i in report.items
    )
    frag.unlink()
    report = detect(plan)
    assert any(
        i.category == "git_hooks" and i.direction == "missing" and "protect-main" in i.item
        for i in report.items
    )


def test_prepush_fragment_disabled_via_config(fake_agent_tools, tmp_path, monkeypatch):
    """`fragments.protect-main.enabled: false` (rig.yaml) opts the pre-push gate out
    without touching the other events' fragments."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setattr("riglib.actions.runner._set_git_global", lambda k, v: 0)
    monkeypatch.setattr("riglib.actions.runner._git_global", lambda k: None)
    monkeypatch.setattr("riglib.drift._git_global", lambda k: None)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _dispatcher_cfg(
        fake_agent_tools, repo, home, fragments={"protect-main": {"enabled": False}}
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    ghd = home / "ghd"
    assert not (ghd / "pre-push" / "10-protect-main").exists()
    assert (ghd / "pre-commit" / "10-secret-scan").is_file()  # others unaffected


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


# ── skill conflict-backup lands OUTSIDE the scanned skills dir (rig-cli#57) ──────────
def test_skill_conflict_backup_is_outside_scanned_skills_dir(fake_agent_tools, tmp_path):
    """A conflicting skill install backs the prior skill up OUTSIDE skills_target.

    skills_target (``~/.agents/skills``) is auto-scanned by opencode, so a same-parent
    ``<name>.rig-bak-*/`` backup — still carrying a ``SKILL.md`` — would be re-loaded as a
    duplicate skill. The backup must relocate out of the scanned dir entirely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    skills_out = repo / "skills-out"  # the natively-scanned skills_target
    # a DIFFERING prior skill already occupies the install path → forces an on_conflict=backup move
    prior = skills_out / "naming"
    prior.mkdir(parents=True)
    (prior / "SKILL.md").write_text("stale prior skill\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _skill_link_cfg(repo, fake_agent_tools, harness_dir=repo / "harness-skills",
                        skills_target=skills_out),
        cat, project_type="unknown",
    )
    report = run_plan(plan)  # default on_conflict=backup
    assert not report.errors, [r.detail for r in report.errors]

    skill_res = [r for r in report.results if r.action.kind == "copy_skill"]
    backed = [r for r in skill_res if r.status == "backed_up"]
    assert backed, [r.detail for r in skill_res]
    bak = backed[0].backup
    assert bak is not None

    # 1) the backup is NOT under the scanned skills dir (the bug: it used to be a sibling there).
    assert skills_out not in bak.parents, f"backup {bak} still under scanned skills dir {skills_out}"
    # 2) and there is no *.rig-bak-* dir loitering INSIDE the scanned skills dir at all.
    assert not list(skills_out.glob("*.rig-bak-*")), \
        f"a rig-bak dir leaked into the scanned skills dir: {list(skills_out.glob('*.rig-bak-*'))}"
    # 3) the prior skill's content is preserved at the relocated backup (data not lost).
    assert (bak / "SKILL.md").read_text(encoding="utf-8") == "stale prior skill\n"
    # 4) the fresh skill is installed at the canonical path.
    assert (skills_out / "naming" / "SKILL.md").is_file()


def test_skill_backup_under_skills_target_parent_sibling(fake_agent_tools, tmp_path):
    """The relocated backup sits in a ``.rig-backups`` sibling of the skills dir (one level up)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # nest skills_target so its parent is meaningfully distinct (mirrors ~/.agents/skills)
    skills_out = repo / "agents" / "skills"
    prior = skills_out / "naming"
    prior.mkdir(parents=True)
    (prior / "SKILL.md").write_text("stale\n", encoding="utf-8")

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _skill_link_cfg(repo, fake_agent_tools, harness_dir=repo / "harness-skills",
                        skills_target=skills_out),
        cat, project_type="unknown",
    )
    report = run_plan(plan)
    bak = [r for r in report.results if r.status == "backed_up"][0].backup
    assert bak is not None
    # backup dir is <skills_target>/../.rig-backups (a sibling of the skills dir, not inside it)
    assert bak.parent == repo / "agents" / ".rig-backups"


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
    [("pi", "AGENTS.md"), ("commandcode", "AGENTS.md")],
)
def test_instruction_file_harness_emits_no_link_but_a_note(fake_agent_tools, tmp_path, monkeypatch, kind, instr_marker):
    """An INSTRUCTION-FILE harness (pi/commandcode) links no skill but records WHY.

    No skills dir exists for these kinds, so rig emits zero ``link_skill_harness`` actions (it never
    guesses a dir). The skill is still COPIED to skills_target; a plan note explains the kind reads a
    global AGENTS.md instead — so ``rig status`` isn't a silent empty area.
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
    """An explicit ``harness_skill_dir`` overrides the no-link default even for pi (user opt-in).

    pi is instruction-file-only (no skills dir), so it links nothing by default; an explicit
    dir forces a real link and suppresses the instruction-file note.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    harness = repo / "pi-skills"
    cfg = _harness_skill_cfg(repo, fake_agent_tools, "pi")
    cfg.data["skills"]["harness_skill_dir"] = str(harness)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    # the explicit dir forces a real link (and suppresses the instruction-file note)
    assert [a for a in plan.actions if a.kind == "link_skill_harness"], "explicit dir did not link"
    assert not any("instruction file" in n for n in plan.notes), plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (harness / "naming").resolve() == (repo / "skills-out" / "naming").resolve()


def test_native_discovery_harness_links_nothing_when_target_is_native(
    fake_agent_tools, tmp_path, monkeypatch
):
    """A native-discovery harness with the default target needs no harness symlink."""
    kind = "opencode"
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _harness_skill_cfg(repo, fake_agent_tools, kind)
    cfg.data["defaults"].pop("skills_target")
    plan = build(cfg, cat, project_type="unknown")
    # no harness symlink for a native-discovery harness (it reads skills_target directly)...
    assert not [a for a in plan.actions if a.kind == "link_skill_harness"], f"{kind}: unexpected link"
    # ...but the skill IS still copied to skills_target (which the harness auto-scans)...
    assert [a for a in plan.actions if a.kind == "copy_skill"], f"{kind}: skill not copied"
    # ...and a note explains it auto-loads natively (so the empty link area is not a silent gap).
    notes = " ".join(plan.notes)
    assert kind in notes and "natively" in notes and ".agents/skills" in notes, plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]


def test_native_discovery_harness_custom_target_links_back_to_native_root(
    fake_agent_tools, tmp_path, monkeypatch
):
    """A native-discovery harness still sees skills when skills_target is customized."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    cat = Catalog.scan(str(fake_agent_tools))

    plan = build(_harness_skill_cfg(repo, fake_agent_tools, "opencode"), cat, project_type="unknown")

    links = [a for a in plan.actions if a.kind == "link_skill_harness"]
    assert links
    assert {a.target for a in links} == {home / ".agents" / "skills" / "naming"}
    notes = " ".join(plan.notes)
    assert "opencode" in notes and "skills_target" in notes and "native discovery dir" in notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    link = home / ".agents" / "skills" / "naming"
    assert link.is_symlink()
    assert link.resolve() == (repo / "skills-out" / "naming").resolve()


@pytest.mark.parametrize("kind", ["opencode", "codex", "pi", "commandcode"])
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


@pytest.mark.parametrize("kind", ["pi", "commandcode"])
def test_explicit_hook_bridge_on_non_claude_kind_notes_skip(fake_agent_tools, tmp_path, kind):
    """Explicitly enabling hook_bridge on an unsupported kind is reported skipped, not silently dropped."""
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
                    "items": {"fake-mcp": {"enabled": True, "command": '"/opt/my tools/run" --flag "a b"'}}},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    entry = data["mcpServers"]["fake-mcp"]
    assert entry["command"] == "/opt/my tools/run"  # quoted path with spaces kept whole
    assert entry["args"] == ["--flag", "a b"]  # quotes consumed, inner spaces preserved


def test_desired_mcp_server_entry_legacy_explicit_args_and_env():
    assert desired_mcp_server_entry({"command": 'node "server path.js" --flag'}) == {
        "command": "node",
        "args": ["server path.js", "--flag"],
    }
    assert desired_mcp_server_entry({"command": '"/opt/my tools/run" --flag "a b"'}) == {
        "command": "/opt/my tools/run",
        "args": ["--flag", "a b"],
    }
    assert desired_mcp_server_entry({"command": ""}) == {"command": "", "args": []}
    assert desired_mcp_server_entry({
        "command": "/opt/my tools/run",
        "args": ["server.js"],
        "env": {"NODE_ENV": "test"},
    }) == {
        "command": "/opt/my tools/run",
        "args": ["server.js"],
        "env": {"NODE_ENV": "test"},
    }
    assert desired_mcp_server_entry({"command": "mycmd --implicit-arg", "args": ["explicit-arg"]}) == {
        "command": "mycmd --implicit-arg",
        "args": ["explicit-arg"],
    }
    assert desired_mcp_server_entry({"command": "node", "args": [], "env": {}}) == {
        "command": "node",
        "args": [],
    }
    assert desired_mcp_server_entry({"command": "mycmd", "env": None}) == {
        "command": "mycmd",
        "args": [],
    }


def test_mcp_explicit_args_keep_command_exact_and_add_env(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {
                    "fake-mcp": {
                        "enabled": True,
                        "command": "/opt/my tools/run",
                        "args": ["server.js", "--flag", "a b"],
                        "env": {"NODE_ENV": "test", "TOKEN": "abc"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {
        "command": "/opt/my tools/run",
        "args": ["server.js", "--flag", "a b"],
        "env": {"NODE_ENV": "test", "TOKEN": "abc"},
    }


def test_mcp_explicit_args_do_not_shell_split_command(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {
                    "fake-mcp": {
                        "enabled": True,
                        "command": "node server.js",
                        "args": ["--verbose"],
                    }
                },
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {
        "command": "node server.js",
        "args": ["--verbose"],
    }


def test_mcp_empty_env_is_not_written(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {"fake-mcp": {"enabled": True, "command": "node", "args": [], "env": {}}},
            },
        },
        repo_root=repo,
    )
    run_plan(build(cfg, cat, project_type="unknown"))
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert data["mcpServers"]["fake-mcp"] == {"command": "node", "args": []}
    assert [i for i in detect(build(cfg, cat, project_type="unknown")).items if i.category == "mcp"] == []


def test_drift_mcp_existing_without_env_matches_when_env_is_omitted(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {"fake-mcp": {"enabled": True, "command": "node", "args": ["server.js"]}},
            },
        },
        repo_root=repo,
    )
    mcp_dir = repo / "mcp-out"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fake-mcp": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )
    report = detect(build(cfg, cat, project_type="unknown"))
    assert [i for i in report.items if i.category == "mcp"] == []


def test_mcp_empty_command_skips_even_with_args_and_env(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {
                    "fake-mcp": {
                        "enabled": True,
                        "command": "",
                        "args": ["server.js"],
                        "env": {"NODE_ENV": "test"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    report = run_plan(build(cfg, cat, project_type="unknown"))
    result = next(r for r in report.results if r.action.kind == "register_mcp")
    assert result.status == "skipped"
    assert "no command set" in result.detail
    assert not (repo / "mcp-out" / "mcp.json").exists()


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


def test_harness_and_permissions_use_explicit_suffixed_settings_file(fake_agent_tools, tmp_path):
    """A suffixed Claude settings_path is a shared settings file, not a directory target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.jsonc"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "ship_delegator": {"enabled": False},
            "harness": {
                "kind": "claude-code",
                "settings_path": str(settings),
                "mode": "acceptEdits",
                "hook_bridge": {"enabled": False},
            },
            "permissions": {
                "kind": "claude-code",
                "settings_path": str(settings),
                "tools": ["git"],
                "deny": [],
                "ask": [],
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    kinds = {a.kind for a in plan.actions}
    assert {"apply_harness", "provision_permissions"} <= kinds

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert settings.is_file()
    assert not (settings / "settings.json").exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert data["permissions"]["allow"] == ["Bash(git:*)"]
    drift = detect(plan)
    assert not [i for i in drift.items if i.category in {"harness", "permissions"}]


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


def _auto_harness_cfg(repo: Path, source: Path, **harness) -> LoadedConfig:
    """Like ``_harness_cfg`` but auto_mode → USER-scope write (no repo settings_path).

    Reuses the shared baseline so the two helpers can't drift; drops the ``settings_path`` that
    ``_harness_cfg`` forces in (auto writes to ~/.claude, not the repo) and turns ship_delegator
    off so it never creates repo/.claude and muddies the user-scope assertions.
    """
    cfg = _harness_cfg(repo, source, auto_mode=True, **harness)
    cfg.data["harness"].pop("settings_path", None)
    cfg.data["ship_delegator"] = {"enabled": False}
    return cfg


def test_self_merge_carveout_written_to_fresh_user_settings(fake_agent_tools, tmp_path):
    # auto_mode + self_merge (default ON) → a fresh ~/.claude/settings.json gets the carve-out
    # merged into autoMode.allow as ["$defaults", "<carve-out>"]; re-apply is a no-op.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    assert data["permissions"]["defaultMode"] == "auto"
    # the HARD unblock: the ship rules land in permissions.allow (the auto-mode Bash gate bypass)
    for rule in SELF_MERGE_PERMISSIONS_ALLOW:
        assert rule in data["permissions"]["allow"], f"{rule} missing from permissions.allow"

    # idempotent: re-apply changes nothing, and the no-op detail names the self-merge provisioning
    second = run_plan(plan)
    h2 = [r for r in second.results if r.action.category == "harness"]
    assert h2 and all(r.status == "skipped" for r in h2)
    assert "self-merge ship rules + carve-out present" in h2[0].detail
    assert json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"] == [
        "$defaults", SELF_MERGE_CARVE_OUT
    ]


def test_self_merge_preserves_existing_allow_and_other_sections(fake_agent_tools, tmp_path):
    # an existing autoMode.allow with other entries + sibling soft_deny/hard_deny sections must
    # survive: the carve-out is appended once, nothing else in autoMode is touched.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "autoMode": {
                "allow": ["$defaults", "Some prior carve-out"],
                "soft_deny": ["$defaults", "keep me"],
                "hard_deny": ["$defaults"],
            },
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", "Some prior carve-out", SELF_MERGE_CARVE_OUT]
    # sibling sections untouched
    assert data["autoMode"]["soft_deny"] == ["$defaults", "keep me"]
    assert data["autoMode"]["hard_deny"] == ["$defaults"]

    # idempotent second apply
    run_plan(plan)
    assert json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"] == [
        "$defaults", "Some prior carve-out", SELF_MERGE_CARVE_OUT
    ]


def test_self_merge_seeds_defaults_when_allow_is_non_list(fake_agent_tools, tmp_path):
    # a malformed autoMode.allow (not a list) is re-seeded with $defaults, then the carve-out
    # appended — never crashes on the bad shape.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"autoMode": {"allow": "oops-a-string", "soft_deny": ["$defaults"]}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    assert data["autoMode"]["soft_deny"] == ["$defaults"]


def test_self_merge_off_writes_no_carveout(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert "autoMode" not in data


def test_self_merge_off_never_removes_an_existing_carveout(fake_agent_tools, tmp_path):
    # the "additive only" promise: flipping self_merge:false must NOT strip a carve-out the user
    # already has on disk — rig never removes autoMode.allow entries.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]}}),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]


def test_self_merge_inert_without_auto_mode(fake_agent_tools, tmp_path):
    # self_merge only makes sense under auto (the classifier only runs then); with an
    # interactive/acceptEdits mode the carve-out is NOT written even if self_merge:true.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _harness_cfg(repo, fake_agent_tools, mode="acceptEdits", self_merge=True),
        cat, project_type="unknown",
    )
    run_plan(plan)
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "autoMode" not in data


def test_self_merge_and_mode_change_in_one_apply(fake_agent_tools, tmp_path):
    # combined write: the defaultMode key changes (backed up) AND the carve-out is added in a
    # single apply — the most complex status/detail branch.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    # a differing prior mode value → backed up before converging
    assert any(p.name.startswith("settings.json.rig-bak-") for p in _user_settings().parent.iterdir())
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and "carve-out added" in hres[0].detail
    assert "ship rules" in hres[0].detail


def test_self_merge_carveout_added_when_mode_already_correct(fake_agent_tools, tmp_path):
    # mode key already 'auto' on disk but the carve-out is absent → only the carve-out is written,
    # status "updated", detail has no on_conflict-skip suffix.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and hres[0].status == "updated"
    assert "on_conflict=skip" not in hres[0].detail


def test_self_merge_carveout_added_while_mode_key_left_under_skip(fake_agent_tools, tmp_path):
    # on_conflict=skip + a differing prior defaultMode: the mode key is LEFT (skip), but the
    # additive carve-out is still appended, and the detail carries the "mode key left" note.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _auto_harness_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "default"  # mode key left untouched
    assert data["autoMode"]["allow"] == ["$defaults", SELF_MERGE_CARVE_OUT]  # carve-out still added
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and "mode key left, on_conflict=skip" in hres[0].detail


def test_self_merge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    # drop the carve-out on disk → drift
    data["autoMode"]["allow"] = ["$defaults"]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    mod = [x for x in detect(plan).items if x.category == "harness" and "self-merge" in x.detail.lower()]
    assert mod, "expected a self-merge carve-out drift item"

    # re-apply converges
    run_plan(plan)
    assert SELF_MERGE_CARVE_OUT in json.loads(_user_settings().read_text(encoding="utf-8"))["autoMode"]["allow"]
    assert not [x for x in detect(plan).items if x.category == "harness"]


def test_self_merge_writes_ship_rules_to_permissions_allow(fake_agent_tools, tmp_path):
    # THE fix: auto_mode + self_merge (default ON) writes the ship allow rules to permissions.allow
    # — the explicit allow the auto-mode Bash classifier needs to STOP vetoing `gh ship`. This is
    # the piece the natural-language autoMode.allow carve-out alone never provided.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    for rule in ("Bash(gh ship:*)", "Bash(*/pr-ship.sh:*)", "Bash(*/ship.sh:*)"):
        assert rule in allow, f"{rule} not written to permissions.allow"

    # idempotent: re-apply adds nothing more
    run_plan(plan)
    allow2 = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow2.count("Bash(gh ship:*)") == 1


def test_self_merge_preserves_permissions_allow_and_keeps_gh_pr_merge_deny(fake_agent_tools, tmp_path):
    # the ship-rule append is additive AND leaves the `Bash(gh pr merge:*)` DENY in force: `gh ship`
    # stays the only merge path (ship.sh runs gh pr merge as a child process, not a gated call).
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"allow": ["Bash(docker:*)"]}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)

    perms = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]
    assert "Bash(docker:*)" in perms["allow"]  # the user's own entry survives
    assert "Bash(gh ship:*)" in perms["allow"]
    # the deny provisioned by provision_permissions is intact — never relaxed by the ship allow
    assert "Bash(gh pr merge:*)" in perms["deny"]


def test_self_merge_off_writes_no_ship_rules(fake_agent_tools, tmp_path):
    # self_merge:false → apply_harness adds NO ship rules (provision_permissions never carried them)
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"].get("allow", [])
    assert "Bash(gh ship:*)" not in allow


def test_self_merge_ship_rules_drift_missing_then_synced(fake_agent_tools, tmp_path):
    # drift is surfaced BOTH ways: dropping the ship rules from permissions.allow trips a harness
    # drift item; re-apply converges; and the rig-owned ship rules are NEVER miscounted as
    # permissions "extras" (provision_permissions drift filters them out).
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    # a clean apply produces no permissions "extra" drift for the rig-written ship rules
    assert not [x for x in detect(plan).items if x.category == "permissions" and x.direction == "extra"]

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [r for r in data["permissions"]["allow"] if r not in SELF_MERGE_PERMISSIONS_ALLOW]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    miss = [x for x in detect(plan).items if x.category == "harness" and "ship rules" in x.detail]
    assert miss, "expected a self-merge ship-rules drift item"

    run_plan(plan)
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert all(r in allow for r in SELF_MERGE_PERMISSIONS_ALLOW)
    assert not [x for x in detect(plan).items if x.category == "harness"]


def test_self_merge_leaves_malformed_permissions_allow_untouched(fake_agent_tools, tmp_path):
    # a pre-existing NON-list permissions.allow is a shape error provision_permissions fail-closes on
    # — apply_harness must NOT silently overwrite it with the ship-rule list (that would destroy the
    # user's value AND mask the error). The malformed value survives; no ship rules are written.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"allow": "oops-a-string"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)  # provision_permissions errors on the shape; apply_harness must not clobber
    allow = json.loads(_user_settings().read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == "oops-a-string", "apply_harness clobbered a malformed permissions.allow"


def test_self_merge_noop_detail_does_not_claim_absent_ship_rules(fake_agent_tools, tmp_path):
    # mode key already correct + carve-out present + a MALFORMED permissions.allow (left untouched):
    # apply_harness skips, and the 'nothing changed' detail must NOT falsely claim ship rules present.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "permissions": {"defaultMode": "auto", "allow": "oops-a-string"},
            "autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]},
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    report = run_plan(plan)
    hres = [r for r in report.results if r.action.category == "harness"]
    assert hres and hres[0].status == "skipped"
    assert "ship rules" not in hres[0].detail, "no-op detail falsely claimed ship rules present"
    assert "carve-out present" in hres[0].detail


def test_self_merge_drift_no_false_absent_row_for_malformed_allow(fake_agent_tools, tmp_path):
    # a MALFORMED permissions.allow surfaces ONE permissions shape-drift row; the harness self-merge
    # check must NOT also emit an inaccurate "ship rules absent" row for the same file.
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({
            "permissions": {"defaultMode": "auto", "allow": "oops-a-string"},
            "autoMode": {"allow": ["$defaults", SELF_MERGE_CARVE_OUT]},
        }),
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    items = detect(plan).items
    assert not [x for x in items if x.category == "harness" and "ship rules absent" in x.detail], \
        "malformed allow must not be reported as 'ship rules absent'"
    # the shape problem is still surfaced by the permissions check
    assert [x for x in items if x.category == "permissions" and x.direction == "modified"]


def test_self_merge_settings_file_helpers_normalize_identically(fake_agent_tools, tmp_path):
    # the extras-suppression correctness hinges on harness_settings_file(harness_action) and
    # permissions_settings_file(perms_action) returning EQUAL Paths for the same user settings file
    # (set membership in detect()). Pin that invariant so a future divergence can't silently turn the
    # rig-owned ship rules into false-positive `extra` drift.
    from riglib.actions.runner import harness_settings_file, permissions_settings_file

    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    harness_action = next(a for a in plan.actions if a.kind == "apply_harness")
    perms_action = next(a for a in plan.actions if a.kind == "provision_permissions")
    assert harness_settings_file(harness_action) == permissions_settings_file(perms_action)


def test_self_merge_off_stray_ship_rules_reported_as_extra(fake_agent_tools, tmp_path):
    # two-way drift stays honest: with self_merge:false, ship rules left on disk (e.g. from a prior
    # self_merge:true) are NOT rig-owned for this plan → reported as permissions allow-extras, never
    # silently suppressed.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools, self_merge=False), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [*data["permissions"].get("allow", []), *SELF_MERGE_PERMISSIONS_ALLOW]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    extras = [x for x in detect(plan).items if x.category == "permissions" and x.direction == "extra"]
    assert extras, "stray ship rules under self_merge:false must surface as permissions extras"


def test_self_merge_no_ship_rules_when_skip_leaves_non_auto_mode(fake_agent_tools, tmp_path):
    # on_conflict=skip + an existing conflicting defaultMode ('default'): the mode key is LEFT
    # interactive. The ACTIVE permissions.allow ship rules (checked in EVERY mode) must NOT be written
    # — writing them would pre-approve `gh ship` while the harness is still interactive (codex #159 P2).
    # drift must NOT flag the ship rules missing while the mode stays non-auto (the mode-key drift row
    # already signals non-convergence).
    repo = tmp_path / "repo"
    repo.mkdir()
    _user_settings().parent.mkdir(parents=True, exist_ok=True)
    _user_settings().write_text(
        json.dumps({"permissions": {"defaultMode": "default"}}), encoding="utf-8"
    )
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _auto_harness_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)

    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "default"  # mode left interactive under skip
    assert "Bash(gh ship:*)" not in data["permissions"].get("allow", []), \
        "ship rules pre-approved gh ship while defaultMode stayed interactive"
    assert not [x for x in detect(plan).items if x.category == "harness" and "ship rules absent" in x.detail], \
        "drift flagged ship rules missing while the mode is non-auto"


def test_self_merge_drift_no_ship_rules_missing_when_mode_non_auto(fake_agent_tools, tmp_path):
    # a clean auto apply, then defaultMode flipped to a non-auto value on disk AND the ship rules
    # dropped: auto is no longer in effect → drift must NOT report the ship rules missing (only the
    # mode-key drift row). apply couples the ship rules to the resulting auto mode.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_auto_harness_cfg(repo, fake_agent_tools), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(_user_settings().read_text(encoding="utf-8"))
    data["permissions"]["defaultMode"] = "default"
    data["permissions"]["allow"] = [r for r in data["permissions"]["allow"] if r not in SELF_MERGE_PERMISSIONS_ALLOW]
    _user_settings().write_text(json.dumps(data), encoding="utf-8")
    items = detect(plan).items
    assert not [x for x in items if x.category == "harness" and "ship rules absent" in x.detail], \
        "drift flagged ship rules missing while defaultMode is non-auto"
    assert [x for x in items if x.category == "harness" and "defaultMode" in x.detail], \
        "the mode-key drift must still be reported"


def test_harness_auto_writes_user_settings_not_repo(fake_agent_tools, tmp_path):
    # auto_mode:true with NO settings_path → CC `auto` is written to the USER settings file
    # (~/.claude/settings.json, HOME-isolated to tmp by the autouse fixture), NOT the repo —
    # CC ignores defaultMode:auto at project scope, so committing it per-repo would be a no-op.
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    # self_merge off keeps this test focused on the mode-key write (no autoMode.allow noise).
    cfg = _auto_harness_cfg(repo, fake_agent_tools, self_merge=False)
    plan = build(cfg, cat, project_type="unknown")
    user_settings = _user_settings()

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
            "mcp": {"target": str(repo / "mcp-out"), "items": {"fake-mcp": {"enabled": True, "server": "custom-name", "command": "x --mcp"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    data = json.loads((repo / "mcp-out" / "mcp.json").read_text())
    assert "custom-name" in data["mcpServers"]  # registered under the server name
    assert "fake-mcp" not in data["mcpServers"]


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
    data["mcpServers"]["fake-mcp"] = {"command": "different", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


def test_drift_mcp_env_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {
                    "fake-mcp": {
                        "enabled": True,
                        "command": "node",
                        "args": ["server.js"],
                        "env": {"NODE_ENV": "test"},
                    }
                },
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"]["env"]["NODE_ENV"] = "prod"
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


def test_drift_mcp_args_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
            "mcp": {
                "target": str(repo / "mcp-out"),
                "items": {"fake-mcp": {"enabled": True, "command": "node", "args": ["server.js"]}},
            },
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"]["args"] = ["other.js"]
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = detect(plan)
    assert any(i.direction == "modified" and i.category == "mcp" and i.item == "fake-mcp" for i in report.items)


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
            "mcp": {"target": str(repo / "mcp-out"), "items": {"fake-mcp": {"enabled": True, "command": "fake-mcp --serve"}}},
        },
        repo_root=repo,
    )
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    # tamper the registered entry, then re-apply under backup → must converge + back up
    mcp_json = repo / "mcp-out" / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["fake-mcp"] = {"command": "stale", "args": []}
    mcp_json.write_text(json.dumps(data), encoding="utf-8")
    report = run_plan(plan)
    assert not report.errors
    converged = json.loads(mcp_json.read_text())
    assert converged["mcpServers"]["fake-mcp"] == {"command": "fake-mcp", "args": ["--serve"]}
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
    assert data["mcpServers"]["fake-mcp"]["command"] == "fake-mcp"
    assert data["mcpServers"]["fake-mcp"]["args"] == ["--serve"]


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


# ── hook bridge: register dispatchers in harness config ──────────────────────────────
def _bridge_cfg(repo_root: Path, source: Path, *, settings_path: Path,
                hook_bridge: dict | None = None,
                kind: str = "claude-code",
                on_conflict: str | None = None) -> LoadedConfig:
    harness: dict = {"kind": kind, "auto_mode": True,
                     "settings_path": str(settings_path)}
    if hook_bridge is not None:
        harness["hook_bridge"] = hook_bridge
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False},
        "agent_hooks": {"all": True},
        "ci": {"enabled": False},
        "mcp": {"enabled": False},
        "harness": harness,
    }
    if on_conflict is not None:
        data["defaults"] = {"on_conflict": on_conflict}
    return LoadedConfig(
        data=data,
        repo_root=repo_root,
    )


def _bridge_results(report):
    return [r for r in report.results if r.action.kind == "register_hook_bridge"]


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_hook_bridge_registers_dispatcher_in_settings(fake_agent_tools, tmp_path):
    """Apply wires PreToolUse (Bash + writes + Agent|Task), PostToolUse and Stop hooks."""
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
    # PreToolUse: Bash + file-write tools + subagent dispatch all → cc_hook_bridge
    pre = hooks["PreToolUse"]
    matchers = {b["matcher"] for b in pre}
    assert "Bash" in matchers
    assert "Edit|Write|MultiEdit|NotebookEdit" in matchers
    assert "Agent|Task" in matchers
    for b in pre:
        cmd = b["hooks"][0]["command"]
        assert "cc_hook_bridge PreToolUse" in cmd
        # PYTHONPATH anchors on the agent-tools checkout lib/
        assert str(fake_agent_tools / "lib") in cmd
    # PostToolUse: the write-tool matcher → the post-write FEEDBACK point (lint-on-write,
    # format-on-write). Without this entry the whole agent-tools post-write point is dead
    # (agent-tools#160) — pin it so it can't silently regress.
    post = hooks["PostToolUse"]
    assert len(post) == 1
    assert post[0]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    post_cmd = post[0]["hooks"][0]["command"]
    assert "cc_hook_bridge PostToolUse" in post_cmd
    assert str(fake_agent_tools / "lib") in post_cmd
    # Stop: one block, no matcher (match-all), → cc_hook_bridge Stop
    stop = hooks["Stop"]
    assert len(stop) == 1 and "matcher" not in stop[0]
    assert "cc_hook_bridge Stop" in stop[0]["hooks"][0]["command"]


def test_hook_bridge_registers_explicit_non_json_suffix_settings_file(fake_agent_tools, tmp_path):
    """An explicit Claude settings_path with a suffix is a file path, not a directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.jsonc"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert settings.is_file()
    assert not (settings / "settings.json").exists()
    data = json.loads(settings.read_text())
    assert "cc_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_codex_hook_bridge_registers_dispatcher_in_config_toml(fake_agent_tools, tmp_path):
    """Apply wires Codex TOML hooks while preserving unrelated config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('model = "gpt-5"\n\n[profiles.default]\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"

    text = settings.read_text(encoding="utf-8")
    assert 'model = "gpt-5"' in text
    assert "[profiles.default]" in text
    data = _read_toml(settings)
    hooks = data["hooks"]
    pre = hooks["PreToolUse"]
    pre_by_matcher = {entry["matcher"]: entry["hooks"][0]["command"] for entry in pre}
    assert "codex_hook_bridge PreToolUse" in pre_by_matcher["Bash"]
    assert "codex_hook_bridge PreToolUse" in pre_by_matcher["apply_patch"]
    assert str(fake_agent_tools / "lib") in pre_by_matcher["Bash"]
    post = hooks["PostToolUse"]
    assert post[0]["matcher"] == "apply_patch"
    assert "codex_hook_bridge PostToolUse" in post[0]["hooks"][0]["command"]
    stop = hooks["Stop"]
    assert "matcher" not in stop[0]
    assert "codex_hook_bridge Stop" in stop[0]["hooks"][0]["command"]


def test_opencode_hook_bridge_links_plugin(fake_agent_tools, tmp_path):
    """Apply wires opencode by symlinking the bridge plugin into the plugin directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")

    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_opencode_hook_bridge_custom_hook_target_writes_wrapper(fake_agent_tools, tmp_path):
    """A custom agent_hooks.target keeps the primary opencode bridge wired to that descriptor dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert _bridge_results(report), "no register_hook_bridge action ran"
    assert plugin.is_file()
    assert not plugin.is_symlink()
    text = plugin.read_text(encoding="utf-8")
    assert f'process.env.OPENCODE_HOOKS_DIR = "{custom_hooks}"' in text
    assert str((fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js").resolve().as_uri()) in text
    assert detect(plan).in_sync


def test_opencode_hook_bridge_custom_wrapper_drift_is_repaired(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    plugin.write_text("// stale wrapper\n", encoding="utf-8")

    drift = detect(plan)

    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "wrapper differs from config" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    assert f'process.env.OPENCODE_HOOKS_DIR = "{custom_hooks}"' in plugin.read_text(encoding="utf-8")
    assert detect(plan).in_sync


def test_opencode_hook_bridge_custom_wrapper_skip_leaves_existing_file(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// user-managed plugin\n", encoding="utf-8")
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "defaults": {"on_conflict": "skip"},
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    plan = build(cfg, cat, project_type="unknown")

    report = run_plan(plan)

    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert plugin.read_text(encoding="utf-8") == "// user-managed plugin\n"
    assert not plugin.is_symlink()


def test_opencode_hook_bridge_custom_wrapper_is_importable(fake_agent_tools, tmp_path):
    """The generated wrapper must match the plugin module export contract."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    custom_hooks = repo / "custom-hooks"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(custom_hooks)},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo / "rig.yaml",
        layers=[f"repo:{repo / 'rig.yaml'}"],
    )
    report = run_plan(build(cfg, cat, project_type="unknown"))
    assert not report.errors, [r.detail for r in report.errors]

    script = (
        "const mod = await import(process.argv[1]);\n"
        "if (typeof mod.AgentToolsHookBridge !== 'function') process.exit(2);\n"
        "const plugin = await mod.AgentToolsHookBridge();\n"
        f"if (plugin.hooksDirAtImport !== {json.dumps(str(custom_hooks))}) {{\n"
        "  console.error(`hooks dir not visible at import: ${plugin.hooksDirAtImport}`);\n"
        "  process.exit(3);\n"
        "}\n"
    )
    res = subprocess.run(
        [node, "--input-type=module", "-e", script, plugin.resolve().as_uri()],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert res.returncode == 0, res.stderr


def test_opencode_hook_bridge_default_path_is_repo_local_ignored_and_in_sync(fake_agent_tools, tmp_path):
    """The shipped opencode default is a repo-local ordered plugin symlink that rig git-ignores."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    cat = Catalog.scan(str(fake_agent_tools))
    repo_cfg = repo / "rig.yaml"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "opencode", "auto_mode": True, "hook_bridge": {"enabled": True}},
        },
        repo_root=repo,
        repo_path=repo_cfg,
        layers=[f"repo:{repo_cfg}"],
    )
    plan = build(cfg, cat, project_type="unknown")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    exclude = repo / ".git" / "info" / "exclude"
    exclude_text = exclude.read_text(encoding="utf-8")
    assert "/.opencode/plugins/zz-agent-tools-hook-bridge.js" in exclude_text
    assert "/.opencode/plugins/zz-agent-tools-hook-bridge.js.rig-bak-*" in exclude_text
    assert detect(plan).in_sync


def test_opencode_hook_bridge_drift_when_repo_local_plugin_not_ignored(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")

    before = detect(plan)

    assert any(
        i.item == "hook-bridge" and i.direction == "missing" and "git-ignored" in i.detail
        for i in before.items
    )
    repaired = run_plan(plan)
    assert not repaired.errors, [r.detail for r in repaired.errors]
    assert detect(plan).in_sync


def test_opencode_hook_bridge_removes_legacy_global_managed_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert not old.exists()
    assert "removed legacy global opencode plugin" in _bridge_results(report)[0].detail


def test_opencode_hook_bridge_errors_when_legacy_cleanup_fails(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    monkeypatch.setattr(
        "riglib.actions.runner._remove_legacy_opencode_bridge_symlink",
        lambda _plugin_path, _dest: (
            False,
            f"legacy global opencode plugin still present (could not remove {old}: denied)",
        ),
    )

    report = run_plan(plan)

    assert report.errors
    result = _bridge_results(report)[0]
    assert result.status == "error"
    assert "legacy global opencode plugin still present" in result.detail


def test_opencode_hook_bridge_drift_detects_legacy_global_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    old.symlink_to(fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")

    before = detect(plan)

    assert any(
        i.item == "hook-bridge" and i.direction == "modified" and "legacy global" in i.detail
        for i in before.items
    )
    repaired = run_plan(plan)
    assert not repaired.errors, [r.detail for r in repaired.errors]
    assert not old.exists()
    assert detect(plan).in_sync


def test_opencode_hook_bridge_removes_legacy_symlink_to_prior_checkout(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    prior = tmp_path / "old-agent-tools" / "lib" / "opencode_hook_bridge" / "plugin.js"
    prior.parent.mkdir(parents=True)
    prior.write_text("export const Old = async () => ({});\n", encoding="utf-8")
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(prior)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert not old.exists()
    assert plugin.is_symlink()
    old.symlink_to(prior)
    before = detect(plan)
    assert any(i.item == "hook-bridge" and "legacy global" in i.detail for i in before.items)


def test_opencode_hook_bridge_leaves_unrelated_legacy_global_symlink(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    other = tmp_path / "other-plugin.js"
    other.write_text("export const Other = async () => ({});\n", encoding="utf-8")
    old = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    old.parent.mkdir(parents=True)
    old.symlink_to(other)
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert old.is_symlink()
    assert old.resolve() == other
    assert not any("legacy global" in i.detail for i in detect(plan).items)


def test_opencode_hook_bridge_legacy_settings_path_does_not_delete_itself(fake_agent_tools, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    plugin = xdg / "opencode" / "plugins" / "agent-tools-hook-bridge.js"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")


def test_opencode_hook_bridge_backs_up_existing_plugin_file(fake_agent_tools, tmp_path):
    """A user file at the opencode plugin path is backed up before rig links the bridge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// user plugin\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
            on_conflict="backup",
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "backed_up"
    assert plugin.is_symlink()
    backups = list(plugin.parent.glob("zz-agent-tools-hook-bridge.js.rig-bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "// user plugin\n"
    assert detect(plan).in_sync


def test_opencode_hook_bridge_overwrites_existing_plugin_directory(fake_agent_tools, tmp_path):
    """on_conflict=overwrite removes a directory collision before linking the opencode plugin."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.mkdir(parents=True)
    (plugin / "old.js").write_text("// stale directory content\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
            on_conflict="overwrite",
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "updated"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert not (plugin / "old.js").exists()
    assert detect(plan).in_sync


def test_opencode_hook_bridge_repoints_existing_plugin_symlink(fake_agent_tools, tmp_path):
    """A stale opencode plugin symlink is re-pointed without backup noise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = repo / "opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plugin.parent.mkdir(parents=True)
    wrong_dest = repo / "wrong-plugin.js"
    wrong_dest.write_text("// wrong bridge\n", encoding="utf-8")
    plugin.symlink_to(wrong_dest)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=plugin,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "modified" for i in before.items)

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    result = _bridge_results(report)[0]
    assert result.status == "updated"
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert wrong_dest.read_text(encoding="utf-8") == "// wrong bridge\n"
    assert detect(plan).in_sync


def test_opencode_hook_bridge_suffixless_settings_path_uses_ordered_plugin_name(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings_dir = repo / ".opencode" / "plugins"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(
            repo,
            fake_agent_tools,
            settings_path=settings_dir,
            kind="opencode",
            hook_bridge={"enabled": True},
        ),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    plugin = settings_dir / "zz-agent-tools-hook-bridge.js"
    assert not report.errors, [r.detail for r in report.errors]
    assert plugin.is_symlink()
    assert plugin.resolve() == (fake_agent_tools / "lib" / "opencode_hook_bridge" / "plugin.js")
    assert detect(plan).in_sync


def test_codex_hook_bridge_enables_disabled_features_hooks(fake_agent_tools, tmp_path):
    """A wired Codex bridge must not report synced while [features].hooks disables hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[features]\n'
        'hooks = false # user disabled hooks\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        and "apply will enable" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    text = settings.read_text(encoding="utf-8")
    assert "hooks = true # user disabled hooks" in text
    data = _read_toml(settings)
    assert data["features"]["hooks"] is True
    assert data["profiles"]["default"]["model"] == "gpt-5"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_enables_deprecated_codex_hooks_flag(fake_agent_tools, tmp_path):
    """Deprecated top-level codex_hooks=false also disables hooks and must be converged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('codex_hooks = false\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "codex_hooks" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["codex_hooks"] is True
    assert data["model"] == "gpt-5"
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_enables_top_level_features_hooks(fake_agent_tools, tmp_path):
    """Top-level dotted features.hooks=false is equivalent to [features].hooks=false."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('features.hooks = false\nmodel = "gpt-5"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    before = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        for i in before.items
    ), [i.detail for i in before.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["features"]["hooks"] is True
    assert data["model"] == "gpt-5"
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_disabled_hooks_with_toml_conflict_reports_conflict(fake_agent_tools, tmp_path):
    """If apply cannot merge TOML, status must not promise the disabled flag will be enabled."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[features]\nhooks = false\nnotes = """\n[hooks]\nPreToolUse = []\n"""\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "multiline strings" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]
    assert not any("apply will enable" in i.detail for i in drift.items)

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == original


def test_codex_hook_bridge_status_flags_disabled_hooks_after_bridge_written(fake_agent_tools, tmp_path):
    """A present managed block is still drift if Codex globally disables hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    settings.write_text(
        settings.read_text(encoding="utf-8") + "\n[features]\nhooks = false\n",
        encoding="utf-8",
    )

    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "features.hooks" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert _read_toml(settings)["features"]["hooks"] is True
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_preserves_existing_hooks_table(fake_agent_tools, tmp_path):
    """A user's unrelated Codex hook entries survive the managed bridge insertion."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[hooks]\n'
        'Notification = [{matcher = "idle", hooks = [{type = "command", command = "notify-send idle"}]}]\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]

    data = _read_toml(settings)
    assert data["hooks"]["Notification"][0]["hooks"][0]["command"] == "notify-send idle"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]
    text = settings.read_text(encoding="utf-8")

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == text


def test_codex_hook_bridge_existing_hooks_table_multiline_values_idempotent(fake_agent_tools, tmp_path):
    """Multiline arrays inside [hooks] are values, not table boundaries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '[hooks]\n'
        'Notification = [\n'
        '  [1, 2],\n'
        '  ["#"],\n'
        ']\n\n'
        '[profiles.default]\n'
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    text = settings.read_text(encoding="utf-8")
    assert text.index("codex hook bridge") < text.index("[profiles.default]")
    data = _read_toml(settings)
    assert data["hooks"]["Notification"] == [[1, 2], ["#"]]
    assert data["profiles"]["default"]["model"] == "gpt-5"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == text


def test_codex_hook_bridge_idempotent_reapply(fake_agent_tools, tmp_path):
    """A second Codex apply is a no-op and does not duplicate the managed TOML block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    before = settings.read_text(encoding="utf-8")

    second = run_plan(plan)
    res = _bridge_results(second)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == before
    assert before.count("# >>> rig managed: codex hook bridge") == 1
    data = _read_toml(settings)
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_and_haft_mcp_second_apply_idempotent(fake_agent_tools, tmp_path):
    """Codex hook bridge and Haft MCP can co-manage .codex/config.toml without block churn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                      hook_bridge={"enabled": True})
    cfg.data["project_tools"] = {
        "enabled": True,
        "haft": {"enabled": True},
        "serena": {"enabled": False},
        "sverklo": {"enabled": False},
    }
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    text = settings.read_text(encoding="utf-8")
    assert "rig managed: codex hook bridge" in text
    assert "rig managed: haft mcp" in text

    second = run_plan(plan)
    assert not second.errors, [r.detail for r in second.errors]
    assert settings.read_text(encoding="utf-8") == text
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_conflicts_with_existing_hook_event(fake_agent_tools, tmp_path):
    """An unmanaged Codex event key is not clobbered by the managed bridge block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks]\nPreToolUse = []\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.PreToolUse already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    drift = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "will not overwrite unmanaged Codex hooks TOML" in i.detail
        for i in drift.items
    ), \
        [i.detail for i in drift.items]


def test_codex_hook_bridge_inline_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Inline hooks tables cannot be merged safely, so apply leaves them untouched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = 'model = "gpt-5"\nhooks = { Notification = [] }\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks inline table already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert "rig managed: codex hook bridge" not in settings.read_text(encoding="utf-8")
    assert _read_toml(settings)["hooks"]["Notification"] == []


def test_codex_hook_bridge_dotted_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Top-level dotted hooks keys cannot be combined safely with a managed [hooks] block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = "hooks.Notification = []\n"
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.Notification uses dotted hooks TOML" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"]["Notification"] == []


def test_codex_hook_bridge_nested_hooks_table_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Nested [hooks.*] tables are user-owned hooks TOML, so the bridge fails closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks.foo]\ncommand = "legacy"\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged [hooks.foo] table already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"]["foo"]["command"] == "legacy"


@pytest.mark.parametrize(
    ("original", "assert_state"),
    [
        (
            '[hooks.state]\n',
            lambda data: data["hooks"]["state"] == {},
        ),
        (
            '[hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:abc"\n',
            lambda data: data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
            == "sha256:abc",
        ),
        (
            'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n',
            lambda data: data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
            == "sha256:abc",
        ),
        (
            'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n'
            "[profile.default]\nmodel = \"gpt-5\"\n",
            lambda data: (
                data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"]
                == "sha256:abc"
                and data["profile"]["default"]["model"] == "gpt-5"
            ),
        ),
    ],
)
def test_codex_hook_bridge_preserves_hooks_state_table(fake_agent_tools, tmp_path, original, assert_state):
    """Codex hook trust metadata under [hooks.state] is not an event hook and must be preserved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status in {"created", "updated", "backed_up"}, [r.detail for r in res]
    data = _read_toml(settings)
    assert_state(data)
    assert "PreToolUse" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_migrates_existing_managed_block_when_hooks_state_is_dotted(
    fake_agent_tools, tmp_path
):
    """A later Codex dotted hooks.state key must not conflict with an older managed [hooks] block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    managed_table_block = settings.read_text(encoding="utf-8")
    assert "[hooks]" in managed_table_block

    state_key = 'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n'
    settings.write_text(state_key + "\n" + managed_table_block, encoding="utf-8")

    before_migration = detect(plan)
    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "stale" in i.detail
        for i in before_migration.items
    ), [i.detail for i in before_migration.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    text = settings.read_text(encoding="utf-8")
    data = _read_toml(settings)
    assert data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]["trusted_hash"] == "sha256:abc"
    assert "hooks.PreToolUse" in text
    assert "[hooks]" not in text
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_allows_multiline_hooks_state_dotted_key(fake_agent_tools, tmp_path):
    """Multiline Codex trust metadata under hooks.state is not an event-hook conflict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hashes = [\n'
        '  "sha256:abc",\n'
        ']\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    state = data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]
    assert state["trusted_hashes"] == ["sha256:abc"]
    assert "PreToolUse" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_allows_hooks_state_continuation_with_equals(fake_agent_tools, tmp_path):
    """Continuation lines with '=' inside hooks.state metadata must stay inside the ignored value."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hashes = [\n'
        '  "sha256:abc=def",\n'
        ']\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)

    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    state = data["hooks"]["state"]["/Users/ultra/.codex/hooks.json:stop:0:0"]
    assert state["trusted_hashes"] == ["sha256:abc=def"]
    assert "PreToolUse" in data["hooks"]
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_drift_flags_dotted_block_moved_below_table(fake_agent_tools, tmp_path):
    """Dotted managed keys must stay before TOML tables or they become table-relative."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'hooks.state."/Users/ultra/.codex/hooks.json:stop:0:0".trusted_hash = "sha256:abc"\n\n'
        "[profile.default]\n"
        'model = "gpt-5"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]

    text = settings.read_text(encoding="utf-8")
    begin = "# >>> rig managed: codex hook bridge"
    end_marker = "# <<< rig managed: codex hook bridge"
    start = text.index(begin)
    end = text.index(end_marker, start) + len(end_marker)
    if end < len(text) and text[end] == "\n":
        end += 1
    block = text[start:end]
    settings.write_text((text[:start] + text[end:]).rstrip() + "\n\n" + block, encoding="utf-8")

    before_repair = detect(plan)

    assert any(
        i.item == "hook-bridge"
        and i.direction == "modified"
        and "stale" in i.detail
        for i in before_repair.items
    ), [i.detail for i in before_repair.items]

    second = run_plan(plan)

    assert not second.errors, [r.detail for r in second.errors]
    repaired = settings.read_text(encoding="utf-8")
    assert repaired.index(begin) < repaired.index("[profile.default]")
    assert not any(i.target == settings for i in detect(plan).items), [i.detail for i in detect(plan).items]


def test_codex_hook_bridge_quoted_event_key_conflicts(fake_agent_tools, tmp_path):
    """Quoted TOML keys are semantically equivalent to bare event keys."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[hooks]\n"PreToolUse" = []\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged hooks.PreToolUse already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original


def test_codex_hook_bridge_quoted_hooks_table_is_preserved(fake_agent_tools, tmp_path):
    """A quoted ["hooks"] table is still the hooks table; merge into it instead of duplicating it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('["hooks"]\nNotification = []\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["hooks"]["Notification"] == []
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_marker_text_inside_string_is_ignored(fake_agent_tools, tmp_path):
    """Managed markers only count on standalone lines, not inside user TOML strings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        'note = "# >>> rig managed: codex hook bridge # <<< rig managed: codex hook bridge"\n',
        encoding="utf-8",
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["note"].startswith("# >>> rig managed")
    assert "codex_hook_bridge Stop" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_array_hooks_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Array-of-tables hooks TOML is not mistaken for a normal [hooks] table."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = '[[hooks]]\nname = "legacy"\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "unmanaged [[hooks]] array-of-tables already exists" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    assert _read_toml(settings)["hooks"][0]["name"] == "legacy"


def test_codex_hook_bridge_multiline_string_conflict_stays_valid(fake_agent_tools, tmp_path):
    """Multiline strings can contain table-looking text, so the merge fails closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    original = 'notes = """\n[hooks]\nPreToolUse = []\n"""\n'
    settings.write_text(original, encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and res[0].status == "error", [r.detail for r in res]
    assert "TOML multiline strings are unsupported" in res[0].detail
    assert settings.read_text(encoding="utf-8") == original
    drift = detect(plan)
    assert any(
        i.item == "hook-bridge" and i.direction == "modified" and "multiline strings" in i.detail
        for i in drift.items
    ), [i.detail for i in drift.items]


def test_codex_hook_bridge_allows_profile_local_hooks_key(fake_agent_tools, tmp_path):
    """A hooks key inside another TOML table is unrelated to Codex's top-level hooks table."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text('[profiles.default]\nhooks = "profile-local"\n', encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = _read_toml(settings)
    assert data["profiles"]["default"]["hooks"] == "profile-local"
    assert "codex_hook_bridge PreToolUse" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_codex_hook_bridge_skip_leaves_stale_managed_block_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip leaves a stale managed Codex block untouched, matching JSON bridge parity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    stale = settings.read_text(encoding="utf-8").replace(str(fake_agent_tools / "lib"), "/old/lib")
    assert "/old/lib" in stale
    settings.write_text(stale, encoding="utf-8")

    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                      hook_bridge={"enabled": True})
    cfg.data["defaults"] = {"on_conflict": "skip"}
    skip_plan = build(cfg, cat, project_type="unknown")
    report = run_plan(skip_plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    assert settings.read_text(encoding="utf-8") == stale


def test_codex_hook_bridge_backup_rewrites_stale_managed_block(fake_agent_tools, tmp_path):
    """Default backup policy preserves a stale managed block before converging it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    stale = settings.read_text(encoding="utf-8").replace(str(fake_agent_tools / "lib"), "/old/lib")
    assert "/old/lib" in stale
    settings.write_text(stale, encoding="utf-8")

    report = run_plan(plan)
    res = _bridge_results(report)
    assert res and all(r.status == "backed_up" for r in res), [r.detail for r in res]
    assert "/old/lib" not in settings.read_text(encoding="utf-8")
    backups = sorted(settings.parent.glob("config.toml.rig-bak-*"))
    assert backups
    assert backups[-1].read_text(encoding="utf-8") == stale
    assert not any(i.item == "hook-bridge" for i in detect(plan).items), \
        [i.detail for i in detect(plan).items]


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
    # exactly one managed block per matcher, not duplicated
    data = json.loads(settings.read_text())
    bash_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Bash"]
    write_blocks = [
        b for b in data["hooks"]["PreToolUse"]
        if b.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit"
    ]
    subagent_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Agent|Task"]
    assert len(bash_blocks) == 1
    assert len(write_blocks) == 1
    assert len(subagent_blocks) == 1
    assert len(data["hooks"]["PostToolUse"]) == 1


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
                {"matcher": "Agent", "hooks": [{"type": "command", "command": "/Users/x/bin/agent-wrapper.sh"}]},
            ],
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "/Users/x/bin/format-on-save.sh"}]},
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
    agent_cmds = [hk["command"] for b in data["hooks"]["PreToolUse"]
                  for hk in b["hooks"] if b.get("matcher") == "Agent"]
    subagent_cmds = [hk["command"] for b in data["hooks"]["PreToolUse"]
                     for hk in b["hooks"] if b.get("matcher") == "Agent|Task"]
    assert agent_cmds == ["/Users/x/bin/agent-wrapper.sh"]
    assert any("cc_hook_bridge PreToolUse" in c for c in subagent_cmds), subagent_cmds
    # the user's own PostToolUse hook (different matcher) survives ALONGSIDE our new
    # managed PostToolUse block — the bridge must never clobber a format-on-save-style hook
    post = data["hooks"]["PostToolUse"]
    post_cmds = [hk["command"] for b in post for hk in b["hooks"]]
    assert any("format-on-save.sh" in c for c in post_cmds), post_cmds
    assert any("cc_hook_bridge PostToolUse" in c for c in post_cmds), post_cmds
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
    # stale managed entries pointing at an OLD lib path (PreToolUse AND PostToolUse)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
        {"matcher": "Agent|Task", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PreToolUse"}]},
    ], "PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge PostToolUse"}]},
    ], "Stop": [{"hooks": [{"type": "command",
              "command": "PYTHONPATH=/old/path/lib python3 -m cc_hook_bridge Stop"}]}]}}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_blocks = [b for b in data["hooks"]["PreToolUse"] if b.get("matcher") == "Bash"]
    assert len(bash_blocks) == 1  # rewritten, not duplicated
    cmd = bash_blocks[0]["hooks"][0]["command"]
    assert "/old/path/lib" not in cmd
    assert str(fake_agent_tools / "lib") in cmd
    # the drifted PostToolUse command is likewise rewritten in place, not duplicated
    post_blocks = data["hooks"]["PostToolUse"]
    assert len(post_blocks) == 1
    post_cmd = post_blocks[0]["hooks"][0]["command"]
    assert "/old/path/lib" not in post_cmd
    assert "cc_hook_bridge PostToolUse" in post_cmd
    assert str(fake_agent_tools / "lib") in post_cmd


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


def test_codex_hook_bridge_drift_missing_then_synced(fake_agent_tools, tmp_path):
    """Before apply the Codex bridge is missing drift; after apply, in sync."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".codex" / "config.toml"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _bridge_cfg(repo, fake_agent_tools, settings_path=settings, kind="codex",
                    hook_bridge={"enabled": True}),
        cat,
        project_type="unknown",
    )
    before = detect(plan)
    assert any(i.item == "hook-bridge" and i.direction == "missing" for i in before.items), \
        [i.detail for i in before.items]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_posttooluse_only(fake_agent_tools, tmp_path):
    """The pre-0.8.0 upgrade state — PreToolUse + Stop wired, PostToolUse absent — is
    reported as missing drift, and a re-apply converges it (this is exactly what every
    existing installation shows until its next `rig apply`)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    # strip the PostToolUse event wholesale → the pre-0.8.0 on-disk state
    data = json.loads(settings.read_text())
    del data["hooks"]["PostToolUse"]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("PostToolUse" in i.detail for i in missing), [i.detail for i in missing]
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_missing_agent_task_only(fake_agent_tools, tmp_path):
    """The pre-agent upgrade state has Bash/write/Stop wired but no Agent|Task matcher."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"] = [
        block for block in data["hooks"]["PreToolUse"]
        if block.get("matcher") != "Agent|Task"
    ]
    settings.write_text(json.dumps(data, indent=2))
    before = detect(plan)
    missing = [i for i in before.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert missing, [i.detail for i in before.items]
    assert any("Agent|Task" in i.detail for i in missing), [i.detail for i in missing]
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
            {"matcher": "Agent|Task", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PreToolUse"}]},
        ],
        "PostToolUse": [
            {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
             "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PostToolUse"}]},
        ],
        "Stop": [{"hooks": [{"type": "command",
                  "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge Stop"}]}],
    }}, indent=2))
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    report = detect(plan)
    modified = [i for i in report.items if i.item == "hook-bridge" and i.direction == "modified"]
    missing = [i for i in report.items if i.item == "hook-bridge" and i.direction == "missing"]
    assert modified, [i.detail for i in report.items if i.item == "hook-bridge"]
    assert not missing, [i.detail for i in missing]
    # and apply converges it back to in-sync
    run_plan(plan)
    after = detect(plan)
    assert not any(i.item == "hook-bridge" for i in after.items), [i.detail for i in after.items]


def test_hook_bridge_drift_detects_wrong_hook_type(fake_agent_tools, tmp_path):
    """A managed hook with the right command but wrong CC hook type is modified drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_hook = next(
        b["hooks"][0] for b in data["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    bash_hook["type"] = "prompt"
    settings.write_text(json.dumps(data, indent=2))
    report = detect(plan)
    modified = [i for i in report.items if i.item == "hook-bridge" and i.direction == "modified"]
    assert modified, [i.detail for i in report.items if i.item == "hook-bridge"]
    assert any("PreToolUse[Bash]" in i.detail for i in modified), [i.detail for i in modified]
    run_plan(plan)
    repaired = json.loads(settings.read_text())
    repaired_hook = next(
        b["hooks"][0] for b in repaired["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    assert repaired_hook["type"] == "command"
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
        {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": stale}]},
    ], "PostToolUse": [
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
         "command": "PYTHONPATH=/old/lib python3 -m cc_hook_bridge PostToolUse"}]},
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
    subagent_cmd = next(b["hooks"][0]["command"] for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Agent|Task")
    assert bash_cmd == stale  # left untouched under skip
    assert subagent_cmd == stale


def test_hook_bridge_skip_leaves_wrong_hook_type_untouched(fake_agent_tools, tmp_path):
    """on_conflict=skip must NOT repair a managed hook with the wrong CC hook type."""
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_bridge_cfg(repo, fake_agent_tools, settings_path=settings), cat, project_type="unknown")
    run_plan(plan)
    data = json.loads(settings.read_text())
    bash_hook = next(
        b["hooks"][0] for b in data["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    bash_hook["type"] = "prompt"
    settings.write_text(json.dumps(data, indent=2))
    cfg = _bridge_cfg(repo, fake_agent_tools, settings_path=settings)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    skip_plan = build(cfg, cat, project_type="unknown")
    report = run_plan(skip_plan)
    res = _bridge_results(report)
    assert res and all(r.status == "skipped" for r in res), [r.detail for r in res]
    skipped = json.loads(settings.read_text())
    skipped_hook = next(
        b["hooks"][0] for b in skipped["hooks"]["PreToolUse"]
        if b.get("matcher") == "Bash"
    )
    assert skipped_hook["type"] == "prompt"


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
