from pathlib import Path

from riglib.actions.runner import run_plan
from riglib.plan import Action, InstallPlan
from riglib.drift import detect
from riglib.managed_config import GENERATED, MARKER_MANAGED, SOURCE_BACKED, managed_header

def test_blocked_lint_action_does_not_short_circuit_other_targets(tmp_path):
    blocked = Action("lint_policy_blocked", "linters", "rules", tmp_path, tmp_path, {"reason": "missing Oxc", "agent_prompt": "migrate"})
    later = Action("record_mode", "mode", "later-target", tmp_path, tmp_path, {})
    report = run_plan(InstallPlan(actions=[blocked, later]))
    assert [r.status for r in report.results] == ["error", "skipped"]
    assert report.errors and report.results[1].action.item == "later-target"

def test_status_surfaces_blocked_lint_policy(tmp_path):
    action = Action("lint_policy_blocked", "linters", "rules", tmp_path, tmp_path, {"reason": "missing Oxc"})
    report = detect(InstallPlan(actions=[action]))
    assert not report.in_sync
    assert report.items[0].category == "linters" and "missing Oxc" in report.items[0].detail

def test_managed_file_classes_have_distinct_edit_contracts():
    g = managed_header("x.ts", management_class=GENERATED)
    s = managed_header("x.toml", source="agent-tools/x", management_class=SOURCE_BACKED)
    m = managed_header("x.yml", management_class=MARKER_MANAGED)
    assert "whole file" in g and "Canonical source" in s and "outside Rig BEGIN/END" in m
