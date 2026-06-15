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
            "skills": {"universal": {"all": True}, "by_type": {"enable": ["cli"]}},
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
