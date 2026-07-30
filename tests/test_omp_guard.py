"""omp permissions provisioning — guard extension codegen, approval policy merge,
instruction-file advisory policy, plan fan-out, two-way drift, and the activation probe.

Mirrors the execpolicy/permissions discipline: idempotent + additive, backup-on-conflict,
never clobbers the user's own values ('compatible unmanaged' is adopted, never rewritten),
and a re-apply is a true no-op. The omp guard TS is GENERATED from the single rule registry
in riglib.permissions — never a hand-copied second list (rig-cli#202).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError
from riglib.drift import detect
from riglib.omp_guard import (
    GUARD_TEMPLATE_VERSION,
    INCOMPATIBLE_MARKER_NAME,
    guard_provenance,
    render_guard_ts,
)
from riglib.permissions import (
    HARNESS_GUARD,
    HARNESS_PERMISSION_TIERS,
    OMP_GUARD_ASK_RULES,
    OMP_GUARD_DENY_RULES,
    guard_supported,
)
from riglib.plan import build


def _omp_cfg(repo: Path, source: Path, **perm) -> LoadedConfig:
    """A config with harness.kind: omp so the permissions feature fans out to it."""
    data = {
        "agent_tools_source": str(source),
        "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
        "ci": {"enabled": False}, "mcp": {"enabled": False},
        "git_hooks": {"dispatcher": {"enabled": False}},
        "harness": {"enabled": False, "kind": "omp"},
    }
    if perm:
        data["permissions"] = perm
    return LoadedConfig(data=data, repo_root=repo)


def _instr_cfg(repo: Path, source: Path, kind: str) -> LoadedConfig:
    cfg = _omp_cfg(repo, source)
    cfg.data["harness"]["kind"] = kind
    return cfg


def _pin_home(monkeypatch, home: Path) -> None:
    """The instruction-file targets are XDG-aware (~/.config maps to $XDG_CONFIG_HOME), so a
    test pinning HOME must pin XDG under it too or the action lands in conftest's isolated home."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _actions(plan, kind):
    return [a for a in plan.actions if a.kind == kind]


# ── the rule registry + codegen ──────────────────────────────────────────────────────
def test_guard_registry_covers_the_baseline_intents():
    deny_ids = {r.id for r in OMP_GUARD_DENY_RULES}
    ask_ids = {r.id for r in OMP_GUARD_ASK_RULES}
    # the deny baseline (same intent as the claude-code/opencode belts)
    assert "gh-pr-merge" in deny_ids
    assert "git-push-force" in deny_ids
    assert "git-commit-no-verify" in deny_ids
    assert "sudo-rm" in deny_ids
    assert "screencapture" in deny_ids
    # the ask baseline
    assert "pkill" in ask_ids
    assert "killall" in ask_ids
    assert "git-reset-hard" in ask_ids
    # ids unique across both sets; every rule has a hint (the block reason — never
    # command contents, so no secret can leak through a reason string)
    all_ids = [r.id for r in (*OMP_GUARD_DENY_RULES, *OMP_GUARD_ASK_RULES)]
    assert len(all_ids) == len(set(all_ids))
    assert all(r.hint for r in (*OMP_GUARD_DENY_RULES, *OMP_GUARD_ASK_RULES))


def test_render_guard_ts_embeds_rules_and_provenance():
    ts = render_guard_ts()
    # every rule id + hint from the SINGLE registry is in the generated file
    for rule in (*OMP_GUARD_DENY_RULES, *OMP_GUARD_ASK_RULES):
        assert rule.id in ts
        assert rule.hint in ts
    # the provenance header round-trips: template version + the exact rule-id sets
    prov = guard_provenance(ts)
    assert prov is not None
    assert prov["template"] == GUARD_TEMPLATE_VERSION
    assert prov["deny"] == [r.id for r in OMP_GUARD_DENY_RULES]
    assert prov["ask"] == [r.id for r in OMP_GUARD_ASK_RULES]
    # the runtime contract pieces exist: fast-path bash-only, event-shape self-check
    # (fail-closed incompatible marker), ask confirm-gating with a headless block,
    # and the quote-aware tokenizer / pipeline-stage matcher
    assert 'toolName !== "bash"' in ts
    assert INCOMPATIBLE_MARKER_NAME in ts
    assert "hasUI" in ts and "confirm" in ts
    assert "tokenize" in ts and "stages" in ts
    # the safe force must NOT be denied: exact-token matching means --force-with-lease
    # never satisfies the --force flag — the generated matcher is exact-token based
    assert "includes" in ts


def test_render_guard_ts_is_deterministic():
    assert render_guard_ts() == render_guard_ts()


# ── validation + tier registry ───────────────────────────────────────────────────────
def test_permissions_kind_omp_now_validates():
    validate({"version": 1, "permissions": {"kind": "omp"}})


def test_permissions_kind_every_surface_kind_validates():
    """Every known harness has a permissions surface (allowlist/execpolicy/guard/advisory),
    so every one is pinnable; only unknown or deprecated kinds are rejected."""
    for kind in ("claude-code", "opencode", "codex", "omp", "pi", "commandcode"):
        validate({"version": 1, "permissions": {"kind": kind}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "bogus"}})


def test_permission_tiers_cover_every_known_kind():
    from riglib.harness_skills import KNOWN_HARNESS_KINDS

    assert set(HARNESS_PERMISSION_TIERS) == set(KNOWN_HARNESS_KINDS)
    # tier 1 = command-granular enforced; tier 3 = advisory. omp is tier 1 via the guard.
    assert HARNESS_PERMISSION_TIERS["omp"] == 1
    assert HARNESS_PERMISSION_TIERS["pi"] == 3
    assert HARNESS_PERMISSION_TIERS["commandcode"] == 3
    assert HARNESS_PERMISSION_TIERS["claude-code"] == 1


# ── plan fan-out ───────────────────────────────────────────────────────────────────
def test_plan_omp_fan_out_emits_guard_and_approval_actions(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    plan = build(_omp_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    guards = _actions(plan, "install_harness_guard")
    assert [a.options["kind"] for a in guards] == ["omp"]
    assert guards[0].target == home / ".omp" / "agent" / "extensions" / "rig-permissions-guard.ts"
    approvals = _actions(plan, "provision_harness_approval")
    assert [a.options["kind"] for a in approvals] == ["omp"]
    assert approvals[0].target == home / ".omp" / "agent" / "config.yml"
    # the tier note replaces the bare N/A skip note
    assert any("omp" in n and "tier 1" in n for n in plan.notes), plan.notes
    assert not any("has no allowlist to provision" in n and "omp" in n for n in plan.notes), plan.notes


def test_plan_pinned_permissions_kind_omp_provisions(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, kind="omp")
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert _actions(plan, "install_harness_guard")
    assert _actions(plan, "provision_harness_approval")


def test_plan_instruction_file_kinds_get_advisory_policy(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    for kind, rel in (("pi", ".config/pi/AGENTS.md"), ("commandcode", ".commandcode/AGENTS.md")):
        plan = build(_instr_cfg(repo, fake_agent_tools, kind),
                     Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        acts = _actions(plan, "provision_instruction_policy")
        assert [a.options["kind"] for a in acts] == [kind], f"{kind}: no advisory action"
        assert acts[0].target == home / rel
        assert not _actions(plan, "install_harness_guard")
        assert any("tier 3" in n and kind in n for n in plan.notes), plan.notes


def test_plan_permissions_disabled_emits_no_guard_actions(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, enabled=False)
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "install_harness_guard")
    assert not _actions(plan, "provision_harness_approval")
    assert not _actions(plan, "provision_instruction_policy")


def test_plan_codex_fan_out_unchanged_no_guard_actions(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "codex")
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not _actions(plan, "install_harness_guard")
    assert not _actions(plan, "provision_harness_approval")
    assert not _actions(plan, "provision_instruction_policy")


# ── install_harness_guard (apply) ────────────────────────────────────────────────────
def _guard_path(home: Path) -> Path:
    return home / ".omp" / "agent" / "extensions" / "rig-permissions-guard.ts"


def _apply_guard(fake_agent_tools, tmp_path, monkeypatch, on_conflict="backup"):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "install_harness_guard"][0]
    return home, res, plan


def test_install_guard_creates_extension_file(fake_agent_tools, tmp_path, monkeypatch):
    home, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "created", res.detail
    path = _guard_path(home)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == render_guard_ts(marker_dir=str(path.parent))


def test_install_guard_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    home, res1, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    text = _guard_path(home).read_text(encoding="utf-8")
    _, res2, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert res2.status == "skipped"
    assert _guard_path(home).read_text(encoding="utf-8") == text


def test_install_guard_backs_up_and_replaces_hand_edit(fake_agent_tools, tmp_path, monkeypatch):
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    path.write_text("// hand edit\n", encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "backed_up", res.detail
    assert list(path.parent.glob("rig-permissions-guard.ts.rig-bak-*"))
    assert path.read_text(encoding="utf-8") == render_guard_ts(marker_dir=str(path.parent))


def test_install_guard_skip_leaves_drift_untouched(fake_agent_tools, tmp_path, monkeypatch):
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    path.write_text("// hand edit\n", encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    assert res.status == "skipped"
    assert path.read_text(encoding="utf-8") == "// hand edit\n"


# ── provision_harness_approval (apply) ───────────────────────────────────────────────
def _config_yml(home: Path) -> Path:
    return home / ".omp" / "agent" / "config.yml"


def _receipt_path(home: Path) -> Path:
    return home / ".omp" / "agent" / ".rig-permissions-receipt.json"


def _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="backup"):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_harness_approval"][0]
    return home, res, plan


def _read_yml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_approval_creates_config_and_receipt(fake_agent_tools, tmp_path, monkeypatch):
    home, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "created", res.detail
    data = _read_yml(_config_yml(home))
    assert data["tools"]["approvalMode"] == "yolo"
    receipt = json.loads(_receipt_path(home).read_text(encoding="utf-8"))
    assert receipt["managed"]["tools.approvalMode"]["previous"] is None
    assert receipt["managed"]["tools.approvalMode"]["installed"] == "yolo"


def test_approval_merges_additively_preserving_siblings(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("modelRoles:\n  default: kimi-code/k3\ntheme:\n  dark: titanium\n",
                       encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_harness_approval"][0]
    assert res.status in {"updated", "backed_up"}, res.detail
    data = _read_yml(cfg_yml)
    assert data["tools"]["approvalMode"] == "yolo"
    assert data["modelRoles"]["default"] == "kimi-code/k3"  # untouched
    assert data["theme"]["dark"] == "titanium"  # untouched
    assert list(cfg_yml.parent.glob("config.yml.rig-bak-*"))
    receipt = json.loads(_receipt_path(home).read_text(encoding="utf-8"))
    assert receipt["managed"]["tools.approvalMode"]["previous"] is None


def test_approval_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    home, res1, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    first = _config_yml(home).read_text(encoding="utf-8")
    _, res2, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    assert res2.status == "skipped"
    assert _config_yml(home).read_text(encoding="utf-8") == first
    # exactly one receipt, no second backup
    assert len(list(_config_yml(home).parent.glob("config.yml.rig-bak-*"))) <= 1


def test_approval_adopts_compatible_unmanaged_value(fake_agent_tools, tmp_path, monkeypatch):
    """A user who already set approvalMode: yolo by hand is 'compatible unmanaged': the value
    is never rewritten — rig only records its receipt (adopting the state as managed)."""
    home = tmp_path / "home"
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("tools:\n  approvalMode: yolo\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    plan = build(_omp_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_harness_approval"][0]
    assert "adopt" in res.detail
    assert _read_yml(cfg_yml)["tools"]["approvalMode"] == "yolo"
    assert _receipt_path(home).is_file()


def test_approval_never_clobbers_a_differing_user_value(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("tools:\n  approvalMode: always-ask\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    plan = build(_omp_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_harness_approval"][0]
    assert res.status == "skipped", res.detail
    assert _read_yml(cfg_yml)["tools"]["approvalMode"] == "always-ask"  # untouched
    assert not _receipt_path(home).exists()


def test_approval_malformed_yaml_is_a_hard_error(fake_agent_tools, tmp_path, monkeypatch):
    """A config rig cannot parse is never rewritten — fail loud, fix by hand (the same
    discipline as unbalanced markers), under ANY on_conflict policy."""
    home = tmp_path / "home"
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("tools: [not: valid: yaml:\n", encoding="utf-8")
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    assert res.status == "error", res.detail
    assert cfg_yml.read_text(encoding="utf-8") == "tools: [not: valid: yaml:\n"  # untouched


# ── provision_instruction_policy (apply) ─────────────────────────────────────────────
def _apply_policy(fake_agent_tools, tmp_path, monkeypatch, kind="pi", on_conflict="backup"):
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, kind)
    cfg.data["defaults"] = {"on_conflict": on_conflict}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "provision_instruction_policy"][0]
    return home, res


def _policy_path(home: Path, kind: str) -> Path:
    rel = ".config/pi/AGENTS.md" if kind == "pi" else ".commandcode/AGENTS.md"
    return home / rel


def test_instruction_policy_creates_file_with_advisory_block(fake_agent_tools, tmp_path, monkeypatch):
    home, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "created", res.detail
    text = _policy_path(home, "pi").read_text(encoding="utf-8")
    # worded as INTENT, never claimed as execution-layer enforcement (an agent that trusts
    # a nonexistent boundary stops self-guarding — the dangerous lie the panel flagged)
    assert "advisory" in text and "not enforced" in text
    assert "gh pr merge" in text and "pkill" in text


def test_instruction_policy_splices_preserving_user_content(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    path = _policy_path(home, "pi")
    path.parent.mkdir(parents=True)
    path.write_text("# my own pi notes\n", encoding="utf-8")
    _, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    text = path.read_text(encoding="utf-8")
    assert "# my own pi notes" in text
    assert "advisory" in text


def test_instruction_policy_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    home, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    _, res2 = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res2.status == "skipped"


def test_instruction_policy_stale_block_backed_up_and_rewritten(fake_agent_tools, tmp_path, monkeypatch):
    home, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    path = _policy_path(home, "pi")
    text = path.read_text(encoding="utf-8").replace("pkill", "EDITED")
    path.write_text(text, encoding="utf-8")
    _, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "backed_up", res.detail
    assert "pkill" in path.read_text(encoding="utf-8")


def test_instruction_policy_unbalanced_markers_error(fake_agent_tools, tmp_path, monkeypatch):
    home, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    path = _policy_path(home, "pi")
    text = path.read_text(encoding="utf-8")
    # drop the END marker → unbalanced; rig refuses to guess
    end_line = [ln for ln in text.splitlines() if "rig-managed instruction policy" in ln][-1]
    path.write_text(text.replace(end_line + "\n", ""), encoding="utf-8")
    _, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "error", res.detail


# ── drift (two-way) ──────────────────────────────────────────────────────────────────
def _detect_omp(fake_agent_tools, tmp_path, monkeypatch, kind="omp"):
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools) if kind == "omp" else _instr_cfg(repo, fake_agent_tools, kind)
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    return home, detect(plan)


def test_drift_guard_missing_and_stale_and_clean(fake_agent_tools, tmp_path, monkeypatch):
    home, rep = _detect_omp(fake_agent_tools, tmp_path, monkeypatch)
    assert any(d.direction == "missing" and "guard" in d.detail for d in rep.items)
    # after apply: clean
    _, _, plan = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    plan2 = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep2 = detect(plan2)
    assert not [d for d in rep2.items if d.category == "permissions"], [d.detail for d in rep2.items]
    # hand-edit → modified
    _guard_path(home).write_text("// tampered\n", encoding="utf-8")
    rep3 = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "guard" in d.detail for d in rep3.items)


def test_drift_guard_incompatible_marker_surfaces(fake_agent_tools, tmp_path, monkeypatch):
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    marker = _guard_path(home).parent / INCOMPATIBLE_MARKER_NAME
    marker.write_text("tool_call event shape changed\n", encoding="utf-8")
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any("incompatible" in d.detail for d in rep.items), [d.detail for d in rep.items]


def test_drift_approval_missing_and_modified(fake_agent_tools, tmp_path, monkeypatch):
    home, rep = _detect_omp(fake_agent_tools, tmp_path, monkeypatch)
    assert any(d.direction == "missing" and "approval" in d.detail for d in rep.items)
    # a differing user value is modified drift — and apply never clobbers it
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True, exist_ok=True)
    cfg_yml.write_text("tools:\n  approvalMode: write\n", encoding="utf-8")
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep2 = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "approvalMode" in d.detail for d in rep2.items)


def test_drift_approval_compatible_unmanaged_is_not_drift(fake_agent_tools, tmp_path, monkeypatch):
    """Values match but no rig receipt → 'compatible unmanaged': NO drift item, never clobbered."""
    home = tmp_path / "home"
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True)
    cfg_yml.write_text("tools:\n  approvalMode: yolo\n", encoding="utf-8")
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    # install the guard too: the escalated guard-missing correlation is a DIFFERENT finding
    run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert not [d for d in rep.items if "approval" in d.detail], [d.detail for d in rep.items]


def test_drift_instruction_policy_missing_and_clean(fake_agent_tools, tmp_path, monkeypatch):
    home, rep = _detect_omp(fake_agent_tools, tmp_path, monkeypatch, kind="pi")
    assert any(d.direction == "missing" and "policy" in d.detail for d in rep.items)
    _, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    rep2 = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert not [d for d in rep2.items if "policy" in d.detail], [d.detail for d in rep2.items]


# ── the activation probe (opt-in, doctor-level) ─────────────────────────────────────
def _fake_omp(bin_dir: Path, home: Path, *, blocks: bool) -> Path:
    """A fake `omp` that emulates the extension channel: when a rig-probe fixture exists in
    the extensions dir it prints the fixture's block reason (the channel works), else not."""
    script = bin_dir / "omp"
    block_side_effect = (
        f"  touch \"{home}/.omp/agent/extensions/rig-probe-$nonce.blocked\"\n"
        "  echo \"the model narrates: running echo $nonce\"\n"  # prompt-echo red herring
        if blocks else
        f"  touch \"{home}/.omp/agent/extensions/rig-probe-$nonce.executed\"\n"
        "  echo \"ran: $nonce\"\n"
    )
    script.write_text(
        "#!/bin/sh\n"
        "nonce=$(echo \"$@\" | grep -o 'echo [a-f0-9]*' | cut -d' ' -f2)\n"
        f"if [ -f \"{home}/.omp/agent/extensions/rig-probe-$nonce.ts\" ]; then\n"
        + block_side_effect +
        "else\n"
        "  echo \"ran: $nonce\"\n"
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_probe_skips_without_omp_binary(tmp_path, monkeypatch):
    from riglib.omp_probe import probe_omp_guard

    monkeypatch.setenv("PATH", str(tmp_path))  # no omp on PATH
    res = probe_omp_guard(timeout=1)
    assert res.ok is None and "not found" in res.detail


def test_probe_proves_block_channel_with_fixture(tmp_path, monkeypatch):
    from riglib.omp_probe import probe_omp_guard

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    fake = _fake_omp(tmp_path, home, blocks=True)
    res = probe_omp_guard(omp_bin=fake, timeout=10)
    assert res.ok is True, res.detail
    # the fixture extension AND the side-channel record clean themselves up
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*.ts"))
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*.blocked"))
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*.executed"))


def test_probe_fails_when_block_reason_never_appears(tmp_path, monkeypatch):
    from riglib.omp_probe import probe_omp_guard

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    fake = _fake_omp(tmp_path, home, blocks=False)
    res = probe_omp_guard(omp_bin=fake, timeout=10)
    assert res.ok is False and "NOT working" in res.detail
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*.ts"))


def test_probe_fails_when_blocked_and_executed_both_signal(tmp_path, monkeypatch):
    """omp invoking the handler but IGNORING its block decision leaves both markers —
    executed must win over blocked (and over a narrated block string), or doctor would
    certify a broken guard channel as working."""
    from riglib.omp_probe import probe_omp_guard

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    script = tmp_path / "omp"
    script.write_text(
        "#!/bin/sh\n"
        "nonce=$(echo \"$@\" | grep -o 'echo [a-f0-9]*' | cut -d' ' -f2)\n"
        f"if [ -f \"{home}/.omp/agent/extensions/rig-probe-$nonce.ts\" ]; then\n"
        f"  touch \"{home}/.omp/agent/extensions/rig-probe-$nonce.blocked\"\n"
        f"  touch \"{home}/.omp/agent/extensions/rig-probe-$nonce.executed\"\n"
        "  echo \"rig probe block $nonce\"\n"  # even the narrated block string must lose
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    res = probe_omp_guard(omp_bin=str(script), timeout=10)
    assert res.ok is False and "NOT working" in res.detail
    # the FAIL early-return must still clean up fixture and both markers
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*"))


def test_run_probes_empty_without_opt_in(monkeypatch):
    from riglib.probes import run_probes

    monkeypatch.delenv("RIG_OMP_PROBE", raising=False)
    assert run_probes() == []


def test_diagnose_stays_pure_no_probe_side_effects(monkeypatch):
    """diagnose() must never spawn a model turn, even opted in — the doctor COMMAND wires
    probes explicitly, so every other diagnose() caller stays offline."""
    from riglib import doctor

    monkeypatch.setenv("RIG_OMP_PROBE", "1")
    report = doctor.diagnose()
    assert report.probes == []


def test_run_probes_fires_when_opted_in(tmp_path, monkeypatch):
    from riglib.probes import run_probes

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_OMP_PROBE", "1")
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ.get('PATH', '')}")
    _fake_omp(tmp_path, home, blocks=True)
    probes = run_probes()
    assert len(probes) == 1
    assert probes[0].ok is True, probes[0].detail


# ── the generated guard EXECUTED under bun (the enforcement logic itself, not greps) ─
_BUN_FIXTURE_RUNNER = r'''
import factory from "./guard.ts";
const handlers: Record<string, any> = {};
const pi = {
  setLabel: () => {},
  on: (ev: string, h: any) => { handlers[ev] = h; },
  getAllTools: () => [{ name: "bash" }, { name: "read" }],
};
factory(pi);
const noUI = { hasUI: false };
const uiYes = { hasUI: true, ui: { confirm: async () => true } };
const uiNo = { hasUI: true, ui: { confirm: async () => false } };
type Case = [string, any, any, string];
const cases: Case[] = [
  ["deny gh pr merge", { toolName: "bash", input: { command: "gh pr merge" } }, noUI, "gh-pr-merge"],
  ["env-strip prefix", { toolName: "bash", input: { command: "FOO=1 gh pr merge origin main" } }, noUI, "gh-pr-merge"],
  ["deny push --force", { toolName: "bash", input: { command: "git push --force" } }, noUI, "git-push-force"],
  ["deny push -f mid", { toolName: "bash", input: { command: "git push origin main -f" } }, noUI, "git-push-force"],
  ["safe force-with-lease", { toolName: "bash", input: { command: "git push origin main --force-with-lease" } }, noUI, "allow"],
  ["no-verify any position", { toolName: "bash", input: { command: "git commit -m 'x' --no-verify" } }, noUI, "git-commit-no-verify"],
  ["no-verify inside message ok", { toolName: "bash", input: { command: 'git commit -m "mentions --no-verify"' } }, noUI, "allow"],
  ["deny sudo rm", { toolName: "bash", input: { command: "sudo rm -rf /" } }, noUI, "sudo-rm"],
  ["sudo cat ok", { toolName: "bash", input: { command: "sudo cat /etc/hosts" } }, noUI, "allow"],
  ["deny screencapture", { toolName: "bash", input: { command: "screencapture /tmp/x.png" } }, noUI, "screencapture"],
  ["ask pkill headless blocks", { toolName: "bash", input: { command: "pkill -f worker" } }, noUI, "ask pkill"],
  ["ask killall confirm-yes allows", { toolName: "bash", input: { command: "killall worker" } }, uiYes, "allow"],
  ["ask killall confirm-no blocks", { toolName: "bash", input: { command: "killall worker" } }, uiNo, "killall"],
  ["ask reset --hard headless", { toolName: "bash", input: { command: "git reset --hard" } }, noUI, "git-reset-hard"],
  ["ask reset --hard with ref", { toolName: "bash", input: { command: "git reset --hard HEAD~1" } }, noUI, "git-reset-hard"],
  ["reset --soft ok", { toolName: "bash", input: { command: "git reset --soft HEAD~1" } }, noUI, "allow"],
  ["plain ls ok", { toolName: "bash", input: { command: "ls -la" } }, noUI, "allow"],
  ["stage split deny", { toolName: "bash", input: { command: "ls && gh pr merge" } }, noUI, "gh-pr-merge"],
  ["ask confirmed then later-stage deny", { toolName: "bash", input: { command: "pkill worker && gh pr merge" } }, uiYes, "gh-pr-merge"],
  ["unspaced operator deny", { toolName: "bash", input: { command: "git push --force&&true" } }, noUI, "git-push-force"],
  ["git -C path force push denied", { toolName: "bash", input: { command: "git -C /repo push --force" } }, noUI, "git-push-force"],
  ["git --no-pager -f denied", { toolName: "bash", input: { command: "git --no-pager push -f" } }, noUI, "git-push-force"],
  ["git -c kv commit no-verify denied", { toolName: "bash", input: { command: "git -c x=y commit --no-verify" } }, noUI, "git-commit-no-verify"],
  ["git -c kv commit clean ok", { toolName: "bash", input: { command: "git -c x=y commit -m z" } }, noUI, "allow"],
  ["sudo -E rm denied", { toolName: "bash", input: { command: "sudo -E rm -rf /" } }, noUI, "sudo-rm"],
  ["sudo -u root rm denied", { toolName: "bash", input: { command: "sudo -u root rm /x" } }, noUI, "sudo-rm"],
  ["sudo -E cat ok", { toolName: "bash", input: { command: "sudo -E cat /etc/hosts" } }, noUI, "allow"],
  ["newline-separated deny", { toolName: "bash", input: { command: "ls\ngit push --force" } }, noUI, "git-push-force"],
  ["gh -R pr merge denied", { toolName: "bash", input: { command: "gh -R owner/repo pr merge" } }, noUI, "gh-pr-merge"],
  ["gh --repo pr merge denied", { toolName: "bash", input: { command: "gh --repo owner/repo pr merge 42" } }, noUI, "gh-pr-merge"],
  ["gh --repo= joined form denied", { toolName: "bash", input: { command: "gh --repo=owner/repo pr merge" } }, noUI, "gh-pr-merge"],
  ["git --git-dir= joined form denied", { toolName: "bash", input: { command: "git --git-dir=/repo/.git push --force" } }, noUI, "git-push-force"],
  ["env -i force push denied", { toolName: "bash", input: { command: "env -i git push --force" } }, noUI, "git-push-force"],
  ["command -p pr merge denied", { toolName: "bash", input: { command: "command -p gh pr merge" } }, noUI, "gh-pr-merge"],
  ["redirect-glued force denied", { toolName: "bash", input: { command: "git push --force>/dev/null" } }, noUI, "git-push-force"],
  ["stderr redirect force denied", { toolName: "bash", input: { command: "git push --force 2>/dev/null" } }, noUI, "git-push-force"],
  ["git -c hooksPath evasion denied", { toolName: "bash", input: { command: "git -c core.hooksPath=/dev/null commit -m x" } }, noUI, "git-config-injection"],
  ["git -c alias evasion denied", { toolName: "bash", input: { command: "git -c alias.x=push x --force" } }, noUI, "git-config-injection"],
  ["git -c safe config ok", { toolName: "bash", input: { command: "git -c user.name=x commit -m y" } }, noUI, "allow"],
  ["sudo --user root rm denied", { toolName: "bash", input: { command: "sudo --user root rm /x" } }, noUI, "sudo-rm"],
  ["sudo --preserve-env=V rm denied", { toolName: "bash", input: { command: "sudo --preserve-env=HOME rm /x" } }, noUI, "sudo-rm"],
  ["timeout force push denied", { toolName: "bash", input: { command: "timeout 5 git push --force" } }, noUI, "git-push-force"],
  ["timeout -k opts force denied", { toolName: "bash", input: { command: "timeout -k 1 5 git push --force" } }, noUI, "git-push-force"],
  ["nice force push denied", { toolName: "bash", input: { command: "nice -n 5 git push --force" } }, noUI, "git-push-force"],
  ["nohup force push denied", { toolName: "bash", input: { command: "nohup git push --force" } }, noUI, "git-push-force"],
  ["sudo -- rm denied", { toolName: "bash", input: { command: "sudo -- rm /x" } }, noUI, "sudo-rm"],
  ["env VAR force push denied", { toolName: "bash", input: { command: "env FOO=1 git push --force" } }, noUI, "git-push-force"],
  ["command prefix denied", { toolName: "bash", input: { command: "command gh pr merge" } }, noUI, "gh-pr-merge"],
  ["command -v probe allowed", { toolName: "bash", input: { command: "command -v gh" } }, noUI, "allow"],
  ["non-bash tool ignored", { toolName: "read", input: { command: "gh pr merge" } }, noUI, "allow"],
  ["comment hides operator stage", { toolName: "bash", input: { command: "echo ok # note; git reset --hard" } }, noUI, "allow"],
  ["comment to end of input", { toolName: "bash", input: { command: "ls # git push --force" } }, noUI, "allow"],
  ["comment after real command still matches", { toolName: "bash", input: { command: "git reset --hard # careful" } }, noUI, "git-reset-hard"],
  ["newline after comment revives parsing", { toolName: "bash", input: { command: "echo ok # note\ngit push --force" } }, noUI, "git-push-force"],
  ["hash mid-word is literal", { toolName: "bash", input: { command: "echo a#b" } }, noUI, "allow"],
  ["line-continuation before comment", { toolName: "bash", input: { command: "echo ok \\\n# note; git reset --hard" } }, noUI, "allow"],
  ["quoted hash is literal", { toolName: "bash", input: { command: "echo '# x; git reset --hard'" } }, noUI, "allow"],
  ["missing toolName fail-closed", { input: { command: "ls" } }, noUI, "incompatible"],
  ["missing command fail-closed", { toolName: "bash", input: {} }, noUI, "incompatible"],
];
let failures = 0;
for (const [name, event, ctx, expect] of cases) {
  const res = await handlers["tool_call"](event, ctx);
  const reason = res?.reason ?? "";
  const ok = expect === "allow" ? res === undefined || res === null : reason.includes(expect);
  if (!ok) { failures++; console.log(`FAIL ${name}: expected ${expect}, got ${JSON.stringify(res)}`); }
}
await handlers["session_start"]({}, { hasUI: false });
console.log(failures === 0 ? "ALL PASS" : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
'''


def _bun_bin() -> str | None:
    import shutil

    found = shutil.which("bun")
    if found:
        return found
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return str(fallback) if fallback.is_file() else None


def test_generated_guard_enforces_the_baseline_under_bun(tmp_path):
    """The generated TS is the tier-1 claim — execute its tokenizer/matchers against a
    fixture table (deny fires, safe shapes allowed, ask confirm-gates, fail-closed on a
    changed event contract). Skipped when no bun runtime is available."""
    import subprocess

    bun = _bun_bin()
    if not bun:
        pytest.skip("bun runtime not available")
    (tmp_path / "guard.ts").write_text(render_guard_ts(), encoding="utf-8")
    (tmp_path / "run.mts").write_text(_BUN_FIXTURE_RUNNER, encoding="utf-8")
    proc = subprocess.run([bun, "run.mts"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL PASS" in proc.stdout


# ── env-override plan targets + fan-out purity + probe environmental classification ──
def test_plan_guard_targets_honor_pi_coding_agent_dir(fake_agent_tools, tmp_path, monkeypatch):
    """A profile/custom agent dir must steer where the guard + approval land (and the
    generated marker lives next to the installed guard via import.meta.dir)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    agent_dir = tmp_path / "omp-profile-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    plan = build(_omp_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    guards = _actions(plan, "install_harness_guard")
    assert guards[0].target == agent_dir / "extensions" / "rig-permissions-guard.ts"
    approvals = _actions(plan, "provision_harness_approval")
    assert approvals[0].target == agent_dir / "config.yml"


def test_plan_guard_targets_honor_pi_config_dir(fake_agent_tools, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CONFIG_DIR", ".omp-custom")
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    plan = build(_omp_cfg(repo, fake_agent_tools), Catalog.scan(str(fake_agent_tools)),
                 project_type="unknown")
    guards = _actions(plan, "install_harness_guard")
    assert guards[0].target == home / ".omp-custom" / "agent" / "extensions" / "rig-permissions-guard.ts"


def test_tier1_kinds_fan_out_without_instruction_policy(fake_agent_tools, tmp_path, monkeypatch):
    """claude-code/opencode/codex have REAL enforcement — the advisory instruction policy
    must never be spliced into their files (explicit membership, not 'has a file')."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    for kind in ("claude-code", "opencode", "codex"):
        plan = build(_instr_cfg(repo, fake_agent_tools, kind),
                     Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        assert not _actions(plan, "provision_instruction_policy"), kind


def test_probe_skips_on_environmental_failure(tmp_path, monkeypatch):
    """omp erroring before any tool call (creds/quota/unknown model) degrades to SKIPPED —
    only a demonstrably UNBLOCKED run is a hard failure."""
    from riglib.omp_probe import probe_omp_guard

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    script = tmp_path / "omp"
    script.write_text("#!/bin/sh\necho 'error: insufficient quota' >&2\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    res = probe_omp_guard(omp_bin=str(script), timeout=10)
    assert res.ok is None and "skipped" in res.detail
    assert not list((home / ".omp" / "agent" / "extensions").glob("rig-probe-*.ts"))


# ── the approval interlock + YAML edge cases + pinned advisory kinds ───────────────
def _approval_action(target: Path, guard_target: Path | None):
    from riglib.plan import Action

    options = {"kind": "omp"}
    if guard_target is not None:
        options["guard_target"] = str(guard_target)
    return Action(kind="provision_harness_approval", category="permissions", item="omp",
                  source=target.parent, target=target, options=options)


def test_approval_refuses_without_guard_in_place(tmp_path):
    """The yolo posture must NEVER land without the enforcement layer: missing or drifted
    guard → hard error, config untouched (the interlock)."""
    from riglib.actions.runner import _do_provision_harness_approval

    cfg = tmp_path / "config.yml"
    guard = tmp_path / "extensions" / "rig-permissions-guard.ts"
    res = _do_provision_harness_approval(_approval_action(cfg, guard), "backup")
    assert res.status == "error" and "refusing to relax" in res.detail
    assert not cfg.exists()
    # a DRIFTED guard (e.g. left by on_conflict=skip) also blocks the posture write
    guard.parent.mkdir(parents=True)
    guard.write_text("// tampered\n", encoding="utf-8")
    res = _do_provision_harness_approval(_approval_action(cfg, guard), "backup")
    assert res.status == "error" and "refusing to relax" in res.detail
    assert not cfg.exists()


def test_approval_writes_once_guard_is_in_place(tmp_path):
    from riglib.actions.runner import _do_install_harness_guard, _do_provision_harness_approval
    from riglib.plan import Action

    guard = tmp_path / "extensions" / "rig-permissions-guard.ts"
    gact = Action(kind="install_harness_guard", category="permissions", item="omp",
                  source=tmp_path, target=guard, options={"kind": "omp"})
    res = _do_install_harness_guard(gact, "backup")
    assert res.status == "created"
    cfg = tmp_path / "config.yml"
    res = _do_provision_harness_approval(_approval_action(cfg, guard), "backup")
    assert res.status == "created", res.detail
    assert _read_yml(cfg)["tools"]["approvalMode"] == "yolo"


def test_approval_null_parent_and_null_value_edges(tmp_path):
    """`tools:` with a null value (commented-out children) must not crash, and an explicit
    `approvalMode: null` is a conflict (never silently treated as absent or clobbered)."""
    from riglib.actions.runner import _do_install_harness_guard, _do_provision_harness_approval
    from riglib.plan import Action

    guard = tmp_path / "extensions" / "rig-permissions-guard.ts"
    _do_install_harness_guard(
        Action(kind="install_harness_guard", category="permissions", item="omp",
               source=tmp_path, target=guard, options={"kind": "omp"}), "backup")
    cfg = tmp_path / "config.yml"
    cfg.write_text("tools:\n", encoding="utf-8")  # tools: null
    res = _do_provision_harness_approval(_approval_action(cfg, guard), "backup")
    assert res.status in {"created", "updated", "backed_up"}, res.detail
    assert _read_yml(cfg)["tools"]["approvalMode"] == "yolo"
    # explicit null VALUE on the managed key → conflict, never clobbered
    cfg.write_text("tools:\n  approvalMode:\n", encoding="utf-8")
    (tmp_path / ".rig-permissions-receipt.json").unlink(missing_ok=True)
    res = _do_provision_harness_approval(_approval_action(cfg, guard), "backup")
    assert res.status == "skipped" and "differs" in res.detail
    assert _read_yml(cfg)["tools"]["approvalMode"] is None


def test_plan_pinned_pi_and_commandcode_get_advisory(fake_agent_tools, tmp_path, monkeypatch):
    """permissions.kind: pi / commandcode (newly pinnable) provisions the advisory block."""
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    for kind, rel in (("pi", ".config/pi/AGENTS.md"), ("commandcode", ".commandcode/AGENTS.md")):
        cfg = _instr_cfg(repo, fake_agent_tools, "claude-code")
        cfg.data["permissions"] = {"kind": kind}
        plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        acts = _actions(plan, "provision_instruction_policy")
        assert [a.options["kind"] for a in acts] == [kind]
        assert acts[0].target == home / rel


# ── doctor cli probe wiring + harness/policy file-collision guard ──────────────────
def test_cmd_doctor_probe_exit_codes(tmp_path, monkeypatch, capsys):
    """A FAILED opted-in probe flips the clean-bill exit to 1; a SKIPPED probe does not."""
    import argparse

    from riglib import cli
    from riglib.doctor import DoctorReport, diagnose
    from riglib.omp_probe import PROBE_NAME
    from riglib.probes import ProbeResult

    args = argparse.Namespace(yes=False, optional=False, fix=False)
    monkeypatch.setattr(cli, "_handle_core_bare", lambda do_fix: False)
    monkeypatch.setattr(cli, "_scan_missing_targets", lambda settings_paths=None: [])

    # cmd_doctor lazy-imports from the modules, so patch there, not on cli's namespace
    import riglib.doctor
    import riglib.omp_probe

    real = diagnose()
    report = DoctorReport(os=real.os, statuses=real.statuses)
    monkeypatch.setattr(riglib.doctor, "diagnose", lambda: report)
    monkeypatch.setenv("RIG_OMP_PROBE", "1")

    import riglib.probes

    monkeypatch.setattr(riglib.probes, "run_probes", lambda: [ProbeResult(PROBE_NAME, True, "ok")])
    assert cli.cmd_doctor(args) == 0

    import riglib.probes

    monkeypatch.setattr(riglib.probes, "run_probes", lambda: [ProbeResult(PROBE_NAME, None, "skipped")])
    assert cli.cmd_doctor(args) == 0

    import riglib.probes

    from riglib import errors

    monkeypatch.setattr(riglib.probes, "run_probes", lambda: [ProbeResult(PROBE_NAME, False, "BROKEN")])
    assert cli.cmd_doctor(args) == errors.EXIT_PROBE_FAILED
    assert "FAILED" in capsys.readouterr().out


def test_policy_survives_harness_area_enabled(fake_agent_tools, tmp_path, monkeypatch):
    """With the harness area ENABLED (not just permissions), nothing else targets the
    instruction files — the advisory block is the only writer (no apply-order flapping)."""
    home = tmp_path / "home"
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    for kind in ("pi", "commandcode"):
        cfg = _instr_cfg(repo, fake_agent_tools, kind)
        cfg.data["harness"]["enabled"] = True
        plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
        target = _policy_path(home, kind)
        writers = [a for a in plan.actions if a.target == target]
        assert [a.kind for a in writers] == ["provision_instruction_policy"], kind


# ── pinned codex, marker lifecycle, registry-derived interlock ─────────────────────
def test_plan_pinned_codex_provisions_execpolicy(fake_agent_tools, tmp_path, monkeypatch):
    """permissions.kind: codex (newly pinnable) must emit the execpolicy action — a
    validated no-op would be the worst outcome (review finding)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _instr_cfg(repo, fake_agent_tools, "claude-code")
    cfg.data["permissions"] = {"kind": "codex", "tools": ["git"]}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    acts = [a for a in plan.actions if a.kind == "provision_execpolicy"]
    assert [a.options["kind"] for a in acts] == ["codex"]


def test_apply_clears_stale_incompatible_marker(fake_agent_tools, tmp_path, monkeypatch):
    """rig apply IS the recovery path the drift message points at: a byte-current guard
    with a stale fail-closed marker gets the marker cleared (no live session needed)."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    marker = _guard_path(home).parent / INCOMPATIBLE_MARKER_NAME
    marker.write_text("stale\n", encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert not marker.exists()
    assert "cleared" in res.detail
    # and drift goes clean immediately
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert not [d for d in rep.items if "incompatible" in d.detail]


def test_interlock_derives_guard_from_registry_without_option(tmp_path, monkeypatch):
    """An approval action with NO guard_target option is STILL interlocked (registry-derived
    path) — the interlock must not depend on the plan's option wiring (fail closed)."""
    from riglib.actions.runner import _do_provision_harness_approval
    from riglib.plan import Action

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = home / ".omp" / "agent" / "config.yml"
    act = Action(kind="provision_harness_approval", category="permissions", item="omp",
                 source=tmp_path, target=cfg, options={"kind": "omp"})
    res = _do_provision_harness_approval(act, "backup")
    assert res.status == "error" and "refusing to relax" in res.detail


def test_probe_fixture_ts_parses_under_bun(tmp_path):
    """The probe fixture is generated TS — a syntax error in it makes omp silently skip the
    fixture and the probe report a false 'channel broken'. Parse-check it (this exact bug
    class shipped once)."""
    import shutil
    import subprocess

    from riglib.omp_probe import _fixture_ts

    bun = _bun_bin()
    if not bun:
        pytest.skip("bun runtime not available")
    f = tmp_path / "fixture.ts"
    f.write_text(_fixture_ts("deadbeef1234", tmp_path), encoding="utf-8")
    proc = subprocess.run([bun, "-e", f'import("{f}").then(() => console.log("parse ok"))'],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "parse ok" in proc.stdout, proc.stderr


# ── parity, lookup edges, inert knobs, disabled end-state, marker_dir quoting ──────
def test_guard_rules_cover_the_same_intents_as_the_glob_belts():
    """The GuardRule registry and the claude-code/opencode glob baselines are two renderings
    of ONE policy — a rule added to one belt and forgotten in the other must fail loudly."""
    from riglib.permissions import (
        CLAUDE_CODE_ASK_RULES,
        CLAUDE_CODE_DENY_RULES,
        OPENCODE_ASK_RULES,
        OPENCODE_DENY_RULES,
    )

    # intent id → a substring at least one glob per belt must carry
    deny_intents = {
        "gh-pr-merge": "gh pr merge",
        "git-push-force": "--force",
        "git-commit-no-verify": "--no-verify",
        "sudo-rm": "sudo rm",
        "screencapture": "screencapture",
    }
    ask_intents = {"pkill": "pkill", "killall": "killall", "git-reset-hard": "--hard"}
    # belt intents are the shared subset; argv-precision EXTRAS the glob dialects cannot
    # express are listed explicitly (adding one still requires touching this map).
    guard_extras = {"git-config-injection"}
    assert {r.id for r in OMP_GUARD_DENY_RULES} == set(deny_intents) | guard_extras
    assert {r.id for r in OMP_GUARD_ASK_RULES} == set(ask_intents)
    for marker in (*deny_intents.values(), *ask_intents.values()):
        assert any(marker in g for g in (*CLAUDE_CODE_DENY_RULES, *CLAUDE_CODE_ASK_RULES)), marker
        assert any(marker in g for g in (*OPENCODE_DENY_RULES, *OPENCODE_ASK_RULES)), marker


def test_approval_lookup_three_segment_null_intermediate():
    from riglib.actions.runner import approval_lookup

    assert approval_lookup({"a": None}, ("a", "b", "c")) == ("missing", None)
    assert approval_lookup({"a": {"b": None}}, ("a", "b", "c")) == ("missing", None)
    assert approval_lookup({"a": "nope"}, ("a", "b", "c")) == ("shape", "a")
    assert approval_lookup({"a": {"b": {"c": "x"}}}, ("a", "b", "c")) == ("present", "x")


def test_plan_notes_inert_allowlist_knobs_for_guard_kinds(fake_agent_tools, tmp_path, monkeypatch):
    """A user pinning omp with a custom deny list gets the baseline AND a visible note —
    never a silent no-op."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, kind="omp", deny=["Bash(docker:*)"])
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("inert" in n and "omp" in n and "deny" in n for n in plan.notes), plan.notes


def test_plan_notes_leftover_artifacts_when_permissions_disabled(fake_agent_tools, tmp_path, monkeypatch):
    """permissions.enabled: false leaves rig's relaxed artifacts on disk (rig never
    auto-deletes) — that must be a visible note, not a silent persistent yolo — but ONLY
    when something was actually provisioned (no noise on fresh machines)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools, enabled=False)
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not any("never auto-deletes" in n for n in plan.notes), plan.notes
    # seed a provisioned guard → the note fires
    guard = _guard_path(home)
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text("// rig guard\n", encoding="utf-8")
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert any("disabled" in n and "never auto-deletes" in n for n in plan.notes), plan.notes


def test_render_guard_ts_bakes_marker_dir_safely():
    ts = render_guard_ts(marker_dir='/tmp/weird "quoted" \\ dir')
    assert 'BAKED_DIR = "/tmp/weird \\"quoted\\" \\\\ dir";' in ts
    # and the plain render has an empty baked dir (runtime import.meta.dir fallback)
    assert 'BAKED_DIR = "";' in render_guard_ts()


def test_drift_escalates_relaxed_posture_with_missing_guard(fake_agent_tools, tmp_path, monkeypatch):
    """The most dangerous combined state — yolo provisioned, guard belt gone — must render
    as an escalated item, not a routine missing file."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status in {"created", "skipped"}, res.detail
    _guard_path(home).unlink()  # guard deleted AFTER apply; yolo persists
    repo = tmp_path / "repo"
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any("enforcement is OFF" in d.detail for d in rep.items), [d.detail for d in rep.items]


def test_approval_skip_policy_never_rewrites_existing_yaml(fake_agent_tools, tmp_path, monkeypatch):
    """on_conflict=skip + an existing (valid) config missing the managed key: the file is
    NOT re-serialized (yaml comments would be destroyed) — skipped, byte-identical, drift
    stays visible. Fresh files are still created under skip."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    cfg_yml = _config_yml(home)
    original = "# my hand comment\ntools:\n  approvalMode: always-ask\n"
    cfg_yml.parent.mkdir(parents=True, exist_ok=True)
    cfg_yml.write_text(original, encoding="utf-8")
    _, res, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    # a DIFFERING value is the never-clobber path (already covered); the rewrite path needs
    # a config that LACKS the key but is otherwise valid — use a fresh sibling key only:
    cfg_yml.write_text("# my hand comment\ntheme:\n  dark: titanium\n", encoding="utf-8")
    before = cfg_yml.read_text(encoding="utf-8")
    _, res2, _ = _apply_approval(fake_agent_tools, tmp_path, monkeypatch, on_conflict="skip")
    assert res2.status == "skipped", res2.detail
    assert "comments are not preserved" in res2.detail
    assert cfg_yml.read_text(encoding="utf-8") == before


def test_instruction_policy_writes_through_symlinked_target(fake_agent_tools, tmp_path, monkeypatch):
    """If the global AGENTS.md is a symlink (user aliased it to a file elsewhere), the
    splice writes THROUGH it — that file IS what the harness reads; drift follows the same
    link and reads clean. Pinned deliberately: rig never unlinks the user's alias."""
    home = tmp_path / "home"
    real = tmp_path / "elsewhere" / "pi-guide.md"
    real.parent.mkdir(parents=True)
    link = _policy_path(home, "pi")
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    _, res = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    assert res.status == "created", res.detail
    assert "advisory" in real.read_text(encoding="utf-8")
    assert link.is_symlink()  # the alias itself is preserved


def test_cmd_doctor_failed_probe_yields_to_missing_required_dep(tmp_path, monkeypatch, capsys):
    """Precedence pinned: a missing REQUIRED dep keeps its (more actionable) guidance path
    even when an activation probe also failed."""
    import argparse

    import riglib.doctor
    import riglib.probes
    from riglib import cli, errors
    from riglib.doctor import DoctorReport, diagnose
    from riglib.omp_probe import PROBE_NAME
    from riglib.probes import ProbeResult

    args = argparse.Namespace(yes=False, optional=False, fix=False)
    monkeypatch.setattr(cli, "_handle_core_bare", lambda do_fix: False)
    monkeypatch.setattr(cli, "_scan_missing_targets", lambda settings_paths=None: [])
    real = diagnose()
    report = DoctorReport(os=real.os, statuses=real.statuses)
    # force a required dep missing
    for st in report.statuses:
        if st.dep.required:
            st.present = False
            st.install_cmd = ["echo", "install"]
            break
    monkeypatch.setattr(riglib.doctor, "diagnose", lambda: report)
    monkeypatch.setenv("RIG_OMP_PROBE", "1")
    monkeypatch.setattr(riglib.probes, "run_probes", lambda: [ProbeResult(PROBE_NAME, False, "BROKEN")])
    rc = cli.cmd_doctor(args)
    assert rc == errors.EXIT_MISSING_DEP


def test_guard_provenance_corrupt_header_returns_none():
    from riglib.omp_guard import guard_provenance, render_guard_ts

    ts = render_guard_ts()
    corrupted = ts.replace('"template": 1', '"template": {', 1)
    assert guard_provenance(corrupted) is None
    assert guard_provenance("// no header at all\n") is None


def test_drift_reports_shape_conflict_for_non_dict_parent(fake_agent_tools, tmp_path, monkeypatch):
    """A managed key's parent existing as a non-dict (tools: "yolo") is modified drift,
    matching apply's conflict-skip — never a false 'missing (apply sets)' promise."""
    home = tmp_path / "home"
    _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    cfg_yml = _config_yml(home)
    cfg_yml.parent.mkdir(parents=True, exist_ok=True)
    cfg_yml.write_text('tools: "yolo"\n', encoding="utf-8")
    _pin_home(monkeypatch, home)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    cfg = _omp_cfg(repo, fake_agent_tools)
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "not an object" in d.detail for d in rep.items), \
        [d.detail for d in rep.items]


def test_install_guard_reports_template_upgrade_origin(fake_agent_tools, tmp_path, monkeypatch):
    """A rig-generated file from an OLDER template is named as such in the apply detail
    (the provenance header's purpose), not lumped with a foreign file."""
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    old = path.read_text(encoding="utf-8").replace('"template": 1', '"template": 0', 1)
    path.write_text(old, encoding="utf-8")
    _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    assert "was rig template v0" in res.detail, res.detail


def test_install_guard_unreadable_file_is_an_error(fake_agent_tools, tmp_path, monkeypatch):
    home, _, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
    path = _guard_path(home)
    path.write_text("// drifted\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        _, res, _ = _apply_guard(fake_agent_tools, tmp_path, monkeypatch)
        assert res.status == "error" and "cannot read" in res.detail
    finally:
        path.chmod(0o644)


def test_drift_instruction_policy_unbalanced_markers(fake_agent_tools, tmp_path, monkeypatch):
    """Unbalanced managed markers are modified DRIFT (fix by hand), mirroring the apply-side
    hard error — the two surfaces never disagree on what the operator must do."""
    home, _ = _apply_policy(fake_agent_tools, tmp_path, monkeypatch)
    path = _policy_path(home, "pi")
    text = path.read_text(encoding="utf-8")
    end_line = [ln for ln in text.splitlines() if "rig-managed instruction policy" in ln][-1]
    path.write_text(text.replace(end_line + "\n", ""), encoding="utf-8")
    repo = tmp_path / "repo"
    cfg = _instr_cfg(repo, fake_agent_tools, "pi")
    rep = detect(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    assert any(d.direction == "modified" and "markers" in d.detail for d in rep.items), \
        [d.detail for d in rep.items]
