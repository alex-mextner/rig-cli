"""Tests for the post-apply verify framework (registry, dogfood spotlight + tmux verifiers)."""

from __future__ import annotations

from pathlib import Path

from riglib import spotlight, verify
from riglib.actions.runner import ActionResult
from riglib.plan import Action, InstallPlan


def _spotlight_action(root: Path, label: str) -> Action:
    return Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=root, target=Path(label),
        options={"roots": [str(root)], "deny": sorted(spotlight.DEFAULT_DENY),
                 "label": label, "max_depth": 8},
    )


def test_registry_dispatches_by_kind():
    # every provisioner declares its check by registering one function keyed on its action kind.
    assert "provision_spotlight" in verify._VERIFIERS
    assert "provision_tmux" in verify._VERIFIERS
    assert "provision_tg_ctl" in verify._VERIFIERS


def test_register_verifier_is_trivial_to_extend():
    @verify.register_verifier("provision_test_only")
    def _v(action):
        return [verify.VerifyResult("x", "y", True, "ok")]

    try:
        plan = InstallPlan()
        plan.actions.append(Action("provision_test_only", "x", "y", Path("."), Path(".")))
        report = verify.verify_plan(plan)
        assert report.ok and report.results[0].evidence == "ok"
    finally:
        verify._VERIFIERS.pop("provision_test_only", None)


def test_verify_spotlight_passes_when_sentinels_present(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")  # skip live launchctl in verify
    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)
    (root / "proj/dist").mkdir(parents=True)
    spotlight.perform_sweep((root,), frozenset(spotlight.DEFAULT_DENY))
    plan = InstallPlan()
    plan.actions.append(_spotlight_action(root, "ai.hyperide.spotlight-exclude"))
    report = verify.verify_plan(plan)
    sweep = [r for r in report.results if r.item == "sweep"][0]
    assert sweep.passed is True
    # the launchd loaded check is SKIPPED under the dry-run flag (not a failure).
    agent = [r for r in report.results if r.item == "agent" and "loaded" in r.evidence]
    assert all(r.passed is None for r in agent) or not agent


def test_verify_spotlight_fails_when_sentinel_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")
    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)  # NO sweep → no sentinel
    plan = InstallPlan()
    plan.actions.append(_spotlight_action(root, "ai.hyperide.spotlight-exclude"))
    report = verify.verify_plan(plan)
    sweep = [r for r in report.results if r.item == "sweep"][0]
    assert sweep.passed is False
    assert not report.ok


def test_verify_skips_actions_that_were_not_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")
    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)  # no sentinel → would FAIL if verified
    action = _spotlight_action(root, "ai.hyperide.spotlight-exclude")
    plan = InstallPlan()
    plan.actions.append(action)
    # the apply result says this action was skipped (e.g. stubbed / no-op) → do not verify it.
    results = [ActionResult(action, "skipped", "stubbed")]
    report = verify.verify_plan(plan, results)
    assert report.results == []
    assert report.ok


def test_verify_tmux_skips_launchd_under_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")
    action = Action(
        kind="provision_tmux", category="tmux", item="config",
        source=tmp_path, target=tmp_path / ".tmux.conf",
        options={"boot": {"enabled": True, "label": "ai.hyperide.tmux-boot"}},
    )
    plan = InstallPlan()
    plan.actions.append(action)
    report = verify.verify_plan(plan)
    # the loaded check is skipped under RIG_TMUX_DRY_RUN → no hard failure from an absent daemon.
    loaded = [r for r in report.results if "loaded" in r.evidence.lower()]
    assert all(r.passed is None for r in loaded)


def test_verify_report_summary_counts_states():
    report = verify.VerifyReport(results=[
        verify.VerifyResult("a", "1", True, "ok"),
        verify.VerifyResult("a", "2", False, "bad"),
        verify.VerifyResult("a", "3", None, "n/a"),
    ])
    assert report.summary() == {"pass": 1, "FAIL": 1, "skipped": 1}
    assert not report.ok and len(report.failures) == 1


def test_verify_tg_ctl_checks_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_is_darwin", lambda: True)  # exercise the macOS path on any host
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RIG_TG_CTL_DRY_RUN", "1")  # skip live launchctl
    action = Action(
        kind="provision_tg_ctl", category="tg_ctl", item="boot",
        source=tmp_path, target=Path("ai.hyperide.tg-ctl"),
        options={"label": "ai.hyperide.tg-ctl"},
    )
    plan = InstallPlan()
    plan.actions.append(action)
    report = verify.verify_plan(plan)
    # plist is absent in the isolated HOME → the presence check fails; the loaded check is skipped.
    presence = [r for r in report.results if "plist" in r.evidence]
    assert presence and presence[0].passed is False
    loaded = [r for r in report.results if "loaded" in r.evidence.lower()]
    assert all(r.passed is None for r in loaded)


def test_verify_tmux_boot_disabled_is_skipped(tmp_path):
    action = Action(
        kind="provision_tmux", category="tmux", item="config",
        source=tmp_path, target=tmp_path / ".tmux.conf",
        options={"boot": {"enabled": False}},
    )
    plan = InstallPlan()
    plan.actions.append(action)
    report = verify.verify_plan(plan)
    assert len(report.results) == 1
    assert report.results[0].passed is None
    assert "disabled" in report.results[0].evidence


def test_verify_launchd_skipped_on_non_macos(monkeypatch, tmp_path):
    # a non-macOS host has no launchd → the whole agent check is SKIPPED, never a false FAIL.
    monkeypatch.setattr(verify, "_is_darwin", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)
    spotlight.perform_sweep((root,), frozenset(spotlight.DEFAULT_DENY))
    plan = InstallPlan()
    plan.actions.append(_spotlight_action(root, "ai.hyperide.spotlight-exclude"))
    report = verify.verify_plan(plan)
    agent = [r for r in report.results if r.item == "agent"]
    assert agent and all(r.passed is None for r in agent)  # no FAIL from an absent plist
    assert report.ok


def test_verify_plan_converts_verifier_exception_to_failure():
    @verify.register_verifier("provision_boom")
    def _boom(action):
        raise RuntimeError("kaboom")

    try:
        plan = InstallPlan()
        plan.actions.append(Action("provision_boom", "x", "y", Path("."), Path(".")))
        report = verify.verify_plan(plan)
        assert not report.ok
        assert "RuntimeError" in report.results[0].evidence
    finally:
        verify._VERIFIERS.pop("provision_boom", None)


def test_verify_tg_ctl_uses_gui_domain_check(tmp_path, monkeypatch):
    # tg_ctl loads in gui/<uid>; the verifier must consult the GUI-domain predicate, not the
    # legacy `launchctl list`. Prove it: gui-check True but legacy-check False → result is LOADED.
    from riglib.actions import runner

    monkeypatch.setattr(verify, "_is_darwin", lambda: True)
    monkeypatch.delenv("RIG_TG_CTL_DRY_RUN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library/LaunchAgents/ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: True)
    monkeypatch.setattr(verify, "_launchctl_loaded", lambda label: False)  # legacy would say NOT loaded
    action = Action(
        kind="provision_tg_ctl", category="tg_ctl", item="boot",
        source=tmp_path, target=Path("ai.hyperide.tg-ctl"),
        options={"label": "ai.hyperide.tg-ctl"},
    )
    plan = InstallPlan()
    plan.actions.append(action)
    report = verify.verify_plan(plan)
    loaded = [r for r in report.results if "loaded" in r.evidence.lower()]
    assert loaded and loaded[0].passed is True  # gui-domain check won
