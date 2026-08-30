"""Direct unit tests for riglib.drift.compute_drift_report / DriftScan (rig-cli#310).

This is the shared engine `rig status` (riglib.cli.cmd_status) and config-web's drift panel
(riglib.config_web_plan.compute_scope_drift) both call. Prior to review it mutated the caller's
`plan.actions` in place in the non-git case -- a second call on the SAME plan object then silently
reported zero dropped actions (found independently by two reviewers). These tests pin the fixed,
non-mutating, idempotent contract directly (previously only exercised indirectly via `rig status`
stdout in tests/test_status_layers.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from riglib.config import LoadedConfig
from riglib.detect import Environment, OsInfo
from riglib.drift import compute_drift_report
from riglib.plan import Action, InstallPlan


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Never let compute_drift_report's disabled-category/scan-dir checks read the REAL machine.

    `check_disabled_dispatcher` reads `git config --global core.hooksPath` and the skills-target
    scan resolves against HOME by default; without isolation these tests would read the dev
    machine's real state (harmless today since no assertion inspects report.items, but a future
    assertion that does would flake on a rig-managed dev machine -- found in review).
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))


def _env(*, is_git_repo: bool, repo_root: Path) -> Environment:
    return Environment(
        repo_root=repo_root,
        is_git_repo=is_git_repo,
        stack="unknown",
        project_type="unknown",
        skills_dirs={},
        global_hooks_path=None,
        dispatcher_installed=False,
        gh_authed=False,
        is_github_repo=False,
        os=OsInfo("darwin", "brew", "macOS"),
    )


def _mixed_plan(tmp_path: Path) -> InstallPlan:
    """A plan with both a GLOBAL-layer action (skills) and a REPO-layer action (ci)."""
    plan = InstallPlan()
    plan.actions = [
        Action(
            kind="copy_skill", category="skills", item="naming",
            source=tmp_path / "src", target=tmp_path / "home" / ".agents" / "skills" / "naming",
        ),
        Action(
            kind="install_ci", category="ci", item="secret-scan",
            source=tmp_path / "src", target=tmp_path / "repo" / ".github" / "workflows",
        ),
    ]
    return plan


def _loaded(tmp_path: Path) -> LoadedConfig:
    return LoadedConfig(
        data={"skills": {"enabled": True}, "agent_hooks": {"enabled": False}},
        repo_root=tmp_path / "repo",
    )


def test_non_git_scan_does_not_mutate_caller_plan(tmp_path):
    plan = _mixed_plan(tmp_path)
    original_actions = list(plan.actions)
    loaded = _loaded(tmp_path)
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    compute_drift_report(plan, loaded, env)

    assert plan.actions == original_actions, "compute_drift_report must not mutate the input plan"


def test_non_git_scan_is_idempotent_across_repeated_calls(tmp_path):
    plan = _mixed_plan(tmp_path)
    loaded = _loaded(tmp_path)
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    first = compute_drift_report(plan, loaded, env)
    second = compute_drift_report(plan, loaded, env)

    assert first.repo_actions_dropped == second.repo_actions_dropped == 1
    assert len(first.plan.actions) == len(second.plan.actions) == 1
    assert first.plan.actions[0].category == "skills"


def test_non_git_effective_plan_is_global_only(tmp_path):
    plan = _mixed_plan(tmp_path)
    loaded = _loaded(tmp_path)
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    scan = compute_drift_report(plan, loaded, env)

    assert [a.category for a in scan.plan.actions] == ["skills"]
    assert scan.repo_actions_dropped == 1


def test_git_repo_effective_plan_is_the_same_object(tmp_path):
    plan = _mixed_plan(tmp_path)
    loaded = _loaded(tmp_path)
    env = _env(is_git_repo=True, repo_root=tmp_path / "repo")

    scan = compute_drift_report(plan, loaded, env)

    assert scan.plan is plan
    assert scan.repo_actions_dropped == 0
    assert scan.dropped_ship_delegator == []


def test_compute_drift_report_prints_nothing(tmp_path, capsys):
    plan = _mixed_plan(tmp_path)
    loaded = _loaded(tmp_path)
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    compute_drift_report(plan, loaded, env)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_restrict_scan_categories_suppresses_unrestricted_dir_scans(tmp_path, monkeypatch):
    """restrict_scan_categories (config-web's Global tab) must exclude skills/agent_hooks/mcp
    scan-dirs entirely when NOT in the restricted set -- without it, a skill installed by some
    OTHER (repo) scope shows as permanent, unresolvable "extra" drift (found in review).
    """
    home = Path(os.environ["HOME"])  # the _isolate_home-set isolated HOME
    extra_skill = home / ".agents" / "skills" / "orphan"
    extra_skill.mkdir(parents=True)
    (extra_skill / "SKILL.md").write_text("---\nname: orphan\n---\n# orphan\n", encoding="utf-8")

    empty_plan = InstallPlan()  # no skills action at all -> orphan is unambiguously "extra"
    loaded = LoadedConfig(data={"skills": {"enabled": True}}, repo_root=tmp_path / "repo")
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    unrestricted = compute_drift_report(empty_plan, loaded, env)
    assert any(i.category == "skills" for i in unrestricted.report.items), (
        "sanity check: without restriction the orphan skill IS flagged as extra"
    )

    restricted = compute_drift_report(
        empty_plan, loaded, env, restrict_scan_categories=frozenset()
    )
    assert not any(i.category == "skills" for i in restricted.report.items), (
        "restrict_scan_categories=frozenset() must suppress the skills scan entirely"
    )


def test_restrict_scan_categories_suppresses_disabled_category_augmentation_checks(tmp_path):
    """The disabled-but-installed augmentation checks (dispatcher/env) must ALSO respect
    restrict_scan_categories -- gating only the scan-dirs (skills/agent_hooks/mcp) was not
    enough: config-web's Global tab hides "env"/"git_hooks" from its view/plan too, so a disabled
    env/dispatcher elsewhere in the global config must not show as drift there either (found in
    review, a second pass after the scan-dirs fix).
    """
    plan = InstallPlan()  # no actions at all
    loaded = LoadedConfig(
        data={
            "git_hooks": {"dispatcher": {"enabled": False}},
            "env": {"enabled": False},
        },
        repo_root=tmp_path / "repo",
    )
    env = _env(is_git_repo=False, repo_root=tmp_path / "repo")

    # restricted to the writable-global set (gitignore/spotlight/tg_ctl/tmux/mode) -- neither
    # "git_hooks" nor "env" is in it, so both checks must be suppressed. Neither check should
    # raise even though nothing is actually installed on disk (both are no-ops when nothing to
    # find, so this also proves the restriction short-circuits before hitting the filesystem).
    restricted = compute_drift_report(
        plan, loaded, env,
        restrict_scan_categories=frozenset({"gitignore", "spotlight", "tg_ctl", "tmux", "mode"}),
    )
    assert not any(i.category in ("git_hooks", "env") for i in restricted.report.items)
