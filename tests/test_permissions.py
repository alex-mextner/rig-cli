"""Permission-allowlist provisioning — resolve, plan, ADDITIVE merge, idempotency, drift.

The load-bearing invariant under test (the task's hard requirement): the merge into the harness
allowlist is ADDITIVE — it preserves every pre-existing entry (auto-mode, the user's accumulated
list), merges in the desired ecosystem/external tools, dedupes, and a re-apply is a true no-op.
It NEVER clobbers. These tests assert that against the claude-code array form and the opencode
object form, plus the config-driven tool list (tools/extra/disable) and N/A harnesses.
"""

from __future__ import annotations

import json
from pathlib import Path

from riglib.actions import run_plan
from riglib.catalog import Catalog
from riglib.config import LoadedConfig, validate, ConfigError
from riglib.drift import detect
from riglib.permissions import (
    DEFAULT_TOOLS,
    HARNESS_ALLOWLIST_NA,
    desired_entries,
    harness_supported,
    resolve_tools,
)
from riglib.plan import build

import pytest


# ── the harness module (registry + renderers) ─────────────────────────────────────
def test_default_tools_cover_ecosystem_and_external():
    for t in ("tg", "review", "draw", "3d", "rig", "task"):  # our CLIs
        assert t in DEFAULT_TOOLS
    for t in ("gh", "git", "rg", "uv", "bun", "jq", "gitleaks"):  # safe external dev tools
        assert t in DEFAULT_TOOLS
    # NOTHING destructive is blanket-allowed
    for t in ("rm", "sudo", "dd"):
        assert t not in DEFAULT_TOOLS


def test_resolve_tools_replace_add_remove_and_dedup():
    # explicit `tools` REPLACES the default set
    assert resolve_tools(["git", "gh"], None, None) == ["git", "gh"]
    # extra ADDS, disable REMOVES, both against the DEFAULT set when tools is None
    out = resolve_tools(None, ["kubectl"], ["gitleaks"])
    assert "kubectl" in out and "gitleaks" not in out and "git" in out
    # dedup, first-seen order preserved
    assert resolve_tools(["git", "git", "gh"], ["gh"], None) == ["git", "gh"]


def test_claude_code_entry_shape_matches_live_settings():
    # the live ~/.claude/settings.json uses Bash(gh:*) / Bash(git:*) — match it so a re-apply dedups
    assert desired_entries("claude-code", ["gh", "git"]) == ["Bash(gh:*)", "Bash(git:*)"]


def test_opencode_entry_shape_is_glob_key():
    assert desired_entries("opencode", ["git", "gh"]) == ["git *", "gh *"]


def test_codex_and_gemini_are_na():
    assert not harness_supported("codex")
    assert not harness_supported("gemini")
    assert harness_supported("claude-code")
    assert harness_supported("opencode")
    assert "codex" in HARNESS_ALLOWLIST_NA and "gemini" in HARNESS_ALLOWLIST_NA


# ── validation (fail-closed) ───────────────────────────────────────────────────────
def test_validate_rejects_unknown_key_and_bad_types():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"toolz": ["git"]}})  # typo
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": "git"}})  # not a list
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"enabled": "yes"}})  # not a bool
    # the good shape passes
    validate({"version": 1, "permissions": {"enabled": True, "tools": ["git"], "extra": ["gh"], "disable": []}})


# ── plan + apply (claude-code array form) ───────────────────────────────────────────
def _cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            # harness present but no auto-mode write to the same file (use a different settings_path
            # for the harness so the two actions don't both target `settings`); here we just disable
            # the harness block entirely to isolate the permissions action.
            "permissions": {"settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def _perm_results(report):
    return [r for r in report.results if r.action.category == "permissions"]


def test_plan_default_on_when_block_absent(fake_agent_tools, tmp_path):
    # an ABSENT permissions block still provisions (default-on) the default tool set
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(settings)},
        },
        repo_root=repo,
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm_actions = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert len(perm_actions) == 1
    assert perm_actions[0].options["kind"] == "claude-code"
    assert list(perm_actions[0].options["tools"]) == list(DEFAULT_TOOLS)


def test_plan_disabled_emits_no_action(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, enabled=False),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_permissions"]


def test_apply_creates_allowlist_and_is_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh", "tg"]), cat, project_type="unknown")

    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(git:*)", "Bash(gh:*)", "Bash(tg:*)"]
    assert any(r.status == "created" for r in _perm_results(first))

    second = run_plan(plan)  # idempotent — all entries already present → skipped
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == \
        ["Bash(git:*)", "Bash(gh:*)", "Bash(tg:*)"]


def test_apply_is_additive_and_never_clobbers_existing(fake_agent_tools, tmp_path):
    """THE invariant: existing entries (auto-mode, accumulated list) survive; ours merge in; dedup."""
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # a pre-existing settings.json with an accumulated allow list + other keys + defaultMode
    settings.write_text(json.dumps({
        "model": "opus",
        "permissions": {
            "defaultMode": "auto",
            "allow": ["WebFetch", "Bash(docker ps:*)", "Bash(git:*)"],  # git already there → must dedup
            "deny": ["Bash(rm:*)"],
        },
    }), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    report = run_plan(plan)
    assert not report.errors
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    # every pre-existing entry preserved, in order
    assert allow[:3] == ["WebFetch", "Bash(docker ps:*)", "Bash(git:*)"]
    # the missing one (gh) is appended; the already-present one (git) is NOT duplicated
    assert "Bash(gh:*)" in allow
    assert allow.count("Bash(git:*)") == 1
    # unrelated keys + defaultMode + deny all intact
    assert data["model"] == "opus"
    assert data["permissions"]["defaultMode"] == "auto"
    assert data["permissions"]["deny"] == ["Bash(rm:*)"]


def test_apply_backs_up_before_change_under_backup_policy(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    assert any(p.name.startswith("settings.json.rig-bak-") for p in repo.iterdir())


# ── opencode object form ────────────────────────────────────────────────────────────
def test_opencode_object_form_merge(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    # existing permission.bash object with a user deny — must NOT be downgraded
    settings.write_text(json.dumps({
        "permission": {"bash": {"rm *": "deny", "git *": "allow"}},
    }), encoding="utf-8")
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},  # reserved for harness write, but permissions supports it
            "permissions": {"settings_path": str(settings), "tools": ["git", "gh"]},
        },
        repo_root=repo,
    )
    # the harness block validate() rejects opencode kind — but permissions resolves the kind from
    # harness.kind. Build the config WITHOUT going through validate (LoadedConfig is pre-validated
    # in tests), so we can exercise the opencode permissions path directly.
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    data = json.loads(settings.read_text(encoding="utf-8"))
    bash = data["permission"]["bash"]
    assert bash["rm *"] == "deny"          # user override preserved, never clobbered
    assert bash["git *"] == "allow"        # already present, untouched
    assert bash["gh *"] == "allow"         # added


# ── drift ─────────────────────────────────────────────────────────────────────────
def test_drift_missing_then_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")

    # nothing on disk → missing for each desired entry
    rep = detect(plan)
    miss = [d for d in rep.by_direction("missing") if d.category == "permissions"]
    assert miss, "expected missing permissions drift"

    run_plan(plan)
    rep2 = detect(plan)
    assert not [d for d in rep2.items if d.category == "permissions"]


def test_drift_modified_when_user_partially_present(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # git present, gh absent → gh is missing drift, git is in sync
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}), encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    rep = detect(plan)
    missing = [d for d in rep.by_direction("missing") if d.category == "permissions"]
    assert len(missing) == 1 and "Bash(gh:*)" in missing[0].detail


# ── opencode-specific (kind override + object-form drift) ───────────────────────────
def _opencode_cfg(repo: Path, source: Path, settings: Path, **perm) -> LoadedConfig:
    # target opencode INDEPENDENTLY via permissions.kind (harness.kind: opencode is rejected by
    # validate; permissions.kind: opencode is the supported way to provision the opencode allowlist).
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"kind": "opencode", "settings_path": str(settings), **perm},
        },
        repo_root=repo,
    )


def test_permissions_kind_override_targets_opencode(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    perm = [a for a in plan.actions if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"


def test_validate_permissions_kind_na_and_unknown_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "codex"}})   # N/A harness
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "gemini"}})  # N/A harness
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"kind": "bogus"}})   # unknown
    validate({"version": 1, "permissions": {"kind": "opencode"}})    # supported
    validate({"version": 1, "permissions": {"kind": "claude-code"}})


def test_opencode_drift_object_form(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    # nothing on disk → missing
    assert [d for d in detect(plan).by_direction("missing") if d.category == "permissions"]
    # a user deny on one entry → modified (apply leaves it, status surfaces it)
    settings.write_text(json.dumps({"permission": {"bash": {"git *": "deny", "gh *": "allow"}}}), encoding="utf-8")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod and "git *" in mod[0].detail
    # converge gh stays allow, git deny preserved → no MISSING for gh
    assert not [d for d in rep.by_direction("missing") if "gh *" in d.detail]


# ── malformed JSON + error paths ────────────────────────────────────────────────────
def test_malformed_json_skip_leaves_untouched(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "skip"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped"
    assert settings.read_text(encoding="utf-8") == "{ this is not json"  # untouched


def test_non_array_allow_is_error(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": "not-a-list"}}), encoding="utf-8")
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not an array" in perm[0].detail


def test_non_dict_root_is_error(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not a JSON object" in perm[0].detail


# ── status: updated (not created) when modifying an existing file (non-backup policy) ─
def test_status_updated_not_created_for_existing_file(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "overwrite"}  # non-backup → no backup, but file existed
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "updated"  # not "created" — the file already existed


# ── disable + default-on, end to end through apply ───────────────────────────────────
def test_disable_drops_tool_from_desired_set_end_to_end(fake_agent_tools, tmp_path):
    # `disable` removes a tool from rig's DESIRED set so it is never ADDED to the allowlist.
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, disable=["gitleaks", "draw", "3d", "rig",
                 "task", "tg", "review", "uv", "bun", "jq", "rg"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gitleaks:*)" not in allow  # disabled → never added
    assert "Bash(git:*)" in allow and "Bash(gh:*)" in allow  # the rest survive


def test_default_on_apply_writes_default_set_and_is_in_sync(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    # absent permissions block, but pin settings_path so we don't touch ~/.claude
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "permissions": {"settings_path": str(settings)},  # default-on, default tool set
        },
        repo_root=repo,
    )
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(cfg, cat, project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [f"Bash({t}:*)" for t in DEFAULT_TOOLS]
    assert not [d for d in detect(plan).items if d.category == "permissions"]  # in sync after apply


def test_empty_tools_list_is_noop(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    report = run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=[]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "skipped"  # zero entries → nothing added


def test_malformed_json_backup_then_rewrites_under_backup(fake_agent_tools, tmp_path):
    # on_conflict != skip → back up the malformed file, reset to {}, write the allowlist
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text("{ broken", encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "backed_up" and "malformed" in perm[0].detail
    assert any(p.name.startswith("settings.json.rig-bak-") for p in repo.iterdir())
    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash(git:*)"]


def test_status_backed_up_when_modifying_existing_under_backup(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["WebFetch"]}}), encoding="utf-8")
    cfg = _cfg(repo, fake_agent_tools, settings, tools=["git"])
    cfg.data["defaults"] = {"on_conflict": "backup"}
    report = run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "backed_up"


def test_drift_wrong_type_container_is_modified(fake_agent_tools, tmp_path):
    # permissions.allow as a STRING → apply errors, so drift must say `modified` (not `missing`),
    # matching apply (B2/B3: status and apply agree on the shape problem).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": "nope"}}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    assert [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert not [d for d in rep.by_direction("missing") if d.category == "permissions"]


def test_drift_non_dict_root_is_modified(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    mod = [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert mod and "not a JSON object" in mod[0].detail


def test_dual_target_permissions_and_harness_distinct_files(fake_agent_tools, tmp_path):
    # permissions.kind: opencode writes opencode.json; harness.kind: claude-code writes auto-mode
    # to a SEPARATE settings.json — the two land in different files in one apply.
    repo = tmp_path / "repo"; repo.mkdir()
    cc_settings = repo / "cc-settings.json"
    oc_settings = repo / "opencode.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "claude-code", "settings_path": str(cc_settings), "mode": "acceptEdits"},
            "permissions": {"kind": "opencode", "settings_path": str(oc_settings), "tools": ["git"]},
        },
        repo_root=repo,
    )
    run_plan(build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    # harness auto-mode landed in cc-settings.json; the allowlist landed in opencode.json
    assert json.loads(cc_settings.read_text())["permissions"]["defaultMode"] == "acceptEdits"
    assert json.loads(oc_settings.read_text())["permission"]["bash"]["git *"] == "allow"


def test_validate_rejects_bad_tool_name_and_non_json_settings_path():
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["git status"]}})  # space → broken entry
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"extra": ["foo; rm -rf"]}})  # metachars
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["../../bin/x"]}})  # path traversal
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"tools": ["-rf"]}})  # leading dash → bogus entry
    with pytest.raises(ConfigError):
        validate({"version": 1, "permissions": {"settings_path": "~/.codex/config.toml"}})  # not .json
    # a plain tool name (incl. 3d), an absolute path, + a .json path pass
    validate({"version": 1, "permissions": {"tools": ["git", "gh", "3d", "/opt/bin/tool"],
                                            "settings_path": "~/.claude/settings.json"}})


def test_opencode_idempotent(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git", "gh"]), cat, project_type="unknown")
    run_plan(plan)
    before = settings.read_text(encoding="utf-8")
    second = run_plan(plan)  # all keys already allow → skipped, byte-stable
    assert all(r.status == "skipped" for r in _perm_results(second))
    assert settings.read_text(encoding="utf-8") == before


def test_harness_kind_cascades_into_permissions_when_no_explicit_kind(fake_agent_tools, tmp_path):
    # no permissions.kind → the kind follows harness.kind (opencode here), not the claude-code default
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},
            "permissions": {"settings_path": str(settings), "tools": ["git"]},  # no kind here
        },
        repo_root=repo,
    )
    perm = [a for a in build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown").actions
            if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "opencode"  # cascaded from harness.kind


def test_disable_never_deletes_an_existing_user_entry(fake_agent_tools, tmp_path):
    # the headline safety invariant: `disable` only stops rig ADDING a tool; an entry the user
    # ALREADY has in their allowlist is NEVER removed by rig (additive-only — never clobbers).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(gitleaks:*)", "WebFetch"]}}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], disable=["gitleaks"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(gitleaks:*)" in allow  # user's pre-existing entry survives a disable
    assert "WebFetch" in allow
    assert "Bash(git:*)" in allow       # the desired tool was added


def test_disable_not_reported_as_missing_drift(fake_agent_tools, tmp_path):
    # symmetry: a disabled tool must not appear as `missing` drift either — apply won't add it, so
    # status must not claim it's missing (the two resolve the tool list independently).
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"], disable=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    run_plan(plan)
    rep = detect(plan)
    assert not [d for d in rep.items if d.category == "permissions" and "git" in d.detail]


def test_explicit_kind_claude_code_with_harness_opencode(fake_agent_tools, tmp_path):
    # permissions.kind wins over harness.kind: explicit claude-code beats a harness.kind: opencode
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "harness": {"kind": "opencode"},
            "permissions": {"kind": "claude-code", "settings_path": str(settings), "tools": ["git"]},
        },
        repo_root=repo,
    )
    perm = [a for a in build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown").actions
            if a.kind == "provision_permissions"]
    assert perm and perm[0].options["kind"] == "claude-code"


def test_extra_added_end_to_end(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    run_plan(build(_cfg(repo, fake_agent_tools, settings, tools=["git"], extra=["kubectl"]),
                   Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Bash(git:*)" in allow and "Bash(kubectl:*)" in allow


def test_drift_non_object_intermediate_is_modified(fake_agent_tools, tmp_path):
    # `permissions` itself is a scalar → apply errors on the intermediate; drift must say modified
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "settings.json"
    settings.write_text(json.dumps({"permissions": "TODO"}), encoding="utf-8")
    plan = build(_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                 Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rep = detect(plan)
    assert [d for d in rep.by_direction("modified") if d.category == "permissions"]
    assert not [d for d in rep.by_direction("missing") if d.category == "permissions"]


def test_opencode_apply_errors_on_non_object_bash(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    settings = repo / "opencode.json"
    settings.write_text(json.dumps({"permission": {"bash": ["git *"]}}), encoding="utf-8")  # list, not obj
    report = run_plan(build(_opencode_cfg(repo, fake_agent_tools, settings, tools=["git"]),
                            Catalog.scan(str(fake_agent_tools)), project_type="unknown"))
    perm = _perm_results(report)
    assert perm and perm[0].status == "error" and "not an object" in perm[0].detail
