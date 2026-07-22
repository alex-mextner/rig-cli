"""codex execpolicy .rules provisioning — render, plan, marker-block apply, idempotency, drift.

codex has no config-array allowlist; rig delivers the "allow safe commands + coarse deny" effect by
writing a marker-delimited block of Starlark ``prefix_rule(...)`` lines into
``~/.codex/rules/rig-managed.rules`` (codex auto-scans that dir at startup). These tests assert the
same discipline the permissions allowlist has: idempotent + additive (user lines outside the markers
survive), backup-on-conflict, and two-way drift surfaced by ``rig status``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.actions.runner import EXECPOLICY_BEGIN_MARKER, EXECPOLICY_END_MARKER
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.permissions import (
    CODEX_DENY_RULES,
    _render_codex_deny,
    _render_codex_rule,
    execpolicy_rule_lines,
)
from riglib.plan import PlanError, build


def _codex_cfg(repo: Path, source: Path, **perm) -> LoadedConfig:
    # codex has no allowlist and no harness auto-mode writer; declare it as the configured harness
    # (auto-mode self-skips) so the permissions feature fans execpolicy out to it.
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "harness": {"enabled": False, "kind": "codex"},
    }
    if perm:
        data["permissions"] = perm
    return LoadedConfig(data=data, repo_root=repo)


def _execpolicy_action(plan):
    acts = [a for a in plan.actions if a.kind == "provision_execpolicy"]
    assert acts, "expected a provision_execpolicy action"
    return acts[0]


# ── renderers ────────────────────────────────────────────────────────────────────────
def test_render_codex_rule_allow_shape():
    assert _render_codex_rule("tg") == 'prefix_rule(pattern=["tg"], decision="allow", justification="rig-managed")'
    # a multi-word tool name splits into multiple tokens
    assert _render_codex_rule("gh pr") == 'prefix_rule(pattern=["gh", "pr"], decision="allow", justification="rig-managed")'


def test_render_codex_deny_forbidden_shape():
    assert _render_codex_deny(("gh", "pr", "merge")) == \
        'prefix_rule(pattern=["gh", "pr", "merge"], decision="forbidden", justification="rig-managed")'


def test_execpolicy_rule_lines_allow_then_deny():
    lines = execpolicy_rule_lines("codex", ["tg", "git"])
    assert lines[0].startswith('prefix_rule(pattern=["tg"], decision="allow"')
    assert lines[1].startswith('prefix_rule(pattern=["git"], decision="allow"')
    # the coarse deny follows, minimal + unambiguous full-command bans only
    assert any('decision="forbidden"' in ln and '["gh", "pr", "merge"]' in ln for ln in lines)
    assert any('["sudo", "rm"]' in ln for ln in lines)
    assert any('["screencapture"]' in ln for ln in lines)
    # coarse-deny stays MINIMAL: no bare git/gh forbidden (would over-block all pushes/commands)
    assert not any('["git"], decision="forbidden"' in ln for ln in lines)
    assert len([ln for ln in lines if 'decision="forbidden"' in ln]) == len(CODEX_DENY_RULES)


# ── plan ───────────────────────────────────────────────────────────────────────────
def test_plan_emits_execpolicy_for_codex(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _execpolicy_action(plan)
    assert act.options["kind"] == "codex"
    assert act.target == tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"


def test_plan_execpolicy_target_honors_rig_codex_home(fake_agent_tools, tmp_path, monkeypatch):
    # Codex skills/hooks/config all resolve through RIG_CODEX_HOME when set; the execpolicy
    # .rules target must use the same resolver, not a hard-coded ~/.codex.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    codex_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _execpolicy_action(plan)
    assert act.target == codex_home / "rules" / "rig-managed.rules"
    # The default (unset RIG_CODEX_HOME) root must NOT also appear anywhere in the plan.
    default_root = tmp_path / "home" / ".codex"
    assert all(default_root not in a.target.parents for a in plan.actions if a.target is not None)


def test_execpolicy_rules_path_suffix_matches_spec():
    # riglib.plan._build_execpolicy derives the RIG_CODEX_HOME-relative suffix by stripping the
    # "~/.codex/" prefix from HARNESS_EXECPOLICY["codex"].rules_path (raising if it doesn't start
    # with that prefix) — this pins the precondition that derivation relies on, without also
    # pinning the filename it strips off.
    from riglib.permissions import HARNESS_EXECPOLICY

    assert HARNESS_EXECPOLICY["codex"].rules_path.startswith("~/.codex/")


def test_plan_execpolicy_raises_when_codex_rules_path_root_diverges(fake_agent_tools, tmp_path, monkeypatch):
    # If HARNESS_EXECPOLICY["codex"].rules_path ever moves off the "~/.codex/" root without the
    # derivation in plan.py being updated, fail loudly at plan-build time instead of silently
    # producing a corrupt target (a literal "~" path component).
    import dataclasses

    from riglib import permissions

    diverged = dataclasses.replace(
        permissions.HARNESS_EXECPOLICY["codex"],
        rules_path="~/.config/codex/rules/rig-managed.rules",
    )
    monkeypatch.setitem(permissions.HARNESS_EXECPOLICY, "codex", diverged)
    repo = tmp_path / "repo"; repo.mkdir()
    # PlanError (not a bare ValueError) so this surfaces through the SAME error path
    # apply/status/init already handle, instead of an uncaught traceback (Opus review of #169).
    with pytest.raises(PlanError, match="must live under"):
        build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_execpolicy_raises_when_codex_rules_path_suffix_escapes_root(fake_agent_tools, tmp_path, monkeypatch):
    # A `..`-carrying (or absolute) suffix must not be allowed to escape RIG_CODEX_HOME — even
    # though the suffix is a hardcoded spec constant today, not user input, this closes the seam
    # a future spec/config change could exploit (Codex review of #169).
    import dataclasses

    from riglib import permissions

    diverged = dataclasses.replace(
        permissions.HARNESS_EXECPOLICY["codex"],
        rules_path="~/.codex/../outside.rules",
    )
    monkeypatch.setitem(permissions.HARNESS_EXECPOLICY, "codex", diverged)
    repo = tmp_path / "repo"; repo.mkdir()
    with pytest.raises(PlanError, match="must not escape"):
        build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_execpolicy_raises_when_codex_rules_path_suffix_is_empty(fake_agent_tools, tmp_path, monkeypatch):
    # An empty suffix (rules_path == the bare "~/.codex/" root) would otherwise silently target
    # the codex home DIRECTORY itself as the rules "file" — reject it rather than let a degenerate
    # spec value pass validation quietly (Fable review of #169).
    import dataclasses

    from riglib import permissions

    diverged = dataclasses.replace(permissions.HARNESS_EXECPOLICY["codex"], rules_path="~/.codex/")
    monkeypatch.setitem(permissions.HARNESS_EXECPOLICY, "codex", diverged)
    repo = tmp_path / "repo"; repo.mkdir()
    with pytest.raises(PlanError, match="must not escape"):
        build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def test_plan_execpolicy_target_derives_from_spec_suffix_not_hardcoded(fake_agent_tools, tmp_path, monkeypatch):
    # A change to HARNESS_EXECPOLICY["codex"].rules_path's SUFFIX (same "~/.codex/" root) must be
    # reflected in the planned target — pins that the derivation actually reads the spec rather
    # than hardcoding "rules/rig-managed.rules" (Codex review of #169: the divergence/traversal
    # tests above only pin the ROOT-mismatch and escape cases, not that the suffix is honored).
    import dataclasses

    from riglib import permissions

    diverged = dataclasses.replace(
        permissions.HARNESS_EXECPOLICY["codex"],
        rules_path="~/.codex/policies/custom.rules",
    )
    monkeypatch.setitem(permissions.HARNESS_EXECPOLICY, "codex", diverged)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    codex_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _execpolicy_action(plan)
    assert act.target == codex_home / "policies" / "custom.rules"


def test_plan_execpolicy_target_default_when_rig_codex_home_unset(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.delenv("RIG_CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    act = _execpolicy_action(plan)
    assert act.target == tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"


def test_plan_no_execpolicy_when_permissions_disabled(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools, enabled=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_execpolicy"]


# ── apply (marker block) ─────────────────────────────────────────────────────────────
def test_apply_writes_block_and_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"

    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    text = rules.read_text(encoding="utf-8")
    assert EXECPOLICY_BEGIN_MARKER in text and EXECPOLICY_END_MARKER in text
    assert 'prefix_rule(pattern=["tg"], decision="allow"' in text
    assert 'prefix_rule(pattern=["gh", "pr", "merge"], decision="forbidden"' in text

    # re-apply is a byte-identical no-op
    report2 = run_plan(build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert all(r.status == "skipped" for r in report2.results if r.action.kind == "provision_execpolicy")
    assert rules.read_text(encoding="utf-8") == text


def test_apply_preserves_user_lines_outside_markers(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text('prefix_rule(pattern=["mycli"], decision="allow")\n', encoding="utf-8")
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    assert not run_plan(plan).errors
    text = rules.read_text(encoding="utf-8")
    assert 'prefix_rule(pattern=["mycli"], decision="allow")' in text  # user line survives
    assert EXECPOLICY_BEGIN_MARKER in text


def test_apply_backs_up_before_rewrite_under_backup_policy(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    # a STALE managed block (wrong body) forces a rewrite
    rules.write_text(f"{EXECPOLICY_BEGIN_MARKER}\nprefix_rule(pattern=[\"old\"], decision=\"allow\")\n{EXECPOLICY_END_MARKER}\n", encoding="utf-8")
    cfg = _codex_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    res = [r for r in report.results if r.action.kind == "provision_execpolicy"][0]
    assert res.status == "backed_up"
    assert list(rules.parent.glob("rig-managed.rules.rig-bak-*"))


def test_apply_appends_block_under_skip_when_no_managed_block(fake_agent_tools, tmp_path, monkeypatch):
    # on_conflict=skip must NOT block the ADDITIVE append of a fresh block to a pre-seeded file
    # (only an in-place REWRITE of an existing managed block honors skip). Else a user who already
    # has a .rules file gets no deny baseline while a clean machine does.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text('prefix_rule(pattern=["mycli"], decision="allow")\n', encoding="utf-8")
    cfg = _codex_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert not report.errors
    text = rules.read_text(encoding="utf-8")
    assert EXECPOLICY_BEGIN_MARKER in text                       # block WAS appended under skip
    assert 'prefix_rule(pattern=["mycli"], decision="allow")' in text  # user line preserved


def test_apply_leaves_stale_block_untouched_under_skip(fake_agent_tools, tmp_path, monkeypatch):
    # a STALE existing managed block IS a real in-place rewrite → skip leaves it, drift stays visible
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    stale = f"{EXECPOLICY_BEGIN_MARKER}\nprefix_rule(pattern=[\"old\"], decision=\"allow\")\n{EXECPOLICY_END_MARKER}\n"
    rules.write_text(stale, encoding="utf-8")
    cfg = _codex_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": "skip"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    res = [r for r in report.results if r.action.kind == "provision_execpolicy"][0]
    assert res.status == "skipped"
    assert rules.read_text(encoding="utf-8") == stale  # untouched


# ── drift ────────────────────────────────────────────────────────────────────────────
def test_drift_missing_then_converged_then_stale(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    # nothing on disk → missing
    missing = [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    assert any("execpolicy" in d.detail for d in missing)
    # apply → converged (clean)
    assert not run_plan(plan).errors
    assert not [d for d in detect(plan).items if d.category == "permissions" and d.item == "codex"]
    # a stale managed block → modified
    text = rules.read_text(encoding="utf-8").replace('decision="allow"', 'decision="prompt"', 1)
    rules.write_text(text, encoding="utf-8")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "permissions"]
    assert any("stale" in d.detail for d in mod)


def test_drift_unbalanced_markers_is_modified(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(f"{EXECPOLICY_BEGIN_MARKER}\nprefix_rule(pattern=[\"x\"], decision=\"allow\")\n", encoding="utf-8")  # no end marker
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "permissions"]
    assert any("unbalanced" in d.detail for d in mod)


# ── validation against the real codex binary (opt-in when available) ──────────────────
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex binary not installed")
def test_generated_block_passes_codex_execpolicy_check(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = build(_codex_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not run_plan(plan).errors
    rules = tmp_path / "home" / ".codex" / "rules" / "rig-managed.rules"

    # parse-validate: a syntactically-broken .rules file makes `check` exit non-zero
    parse = subprocess.run(
        ["codex", "execpolicy", "check", "--rules", str(rules), "true"],
        capture_output=True, text=True, timeout=30,
    )
    assert parse.returncode == 0, parse.stderr
    # the coarse deny actually forbids `gh pr merge`
    verdict = subprocess.run(
        ["codex", "execpolicy", "check", "--rules", str(rules), "gh", "pr", "merge", "x"],
        capture_output=True, text=True, timeout=30,
    )
    assert json.loads(verdict.stdout).get("decision") == "forbidden"
    # …and pre-allows a safe ecosystem CLI
    allow = subprocess.run(
        ["codex", "execpolicy", "check", "--rules", str(rules), "tg", "hello"],
        capture_output=True, text=True, timeout=30,
    )
    assert json.loads(allow.stdout).get("decision") == "allow"
