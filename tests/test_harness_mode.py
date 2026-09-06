"""The per-harness auto/permission-mode write (rig-cli#355, gap 1 of rig-cli#337).

Every configured harness kind — the primary ``harness.kind`` AND every additive ``harness.kinds``
entry — gets its OWN auto-mode key written by ``rig apply``, from the single registry in
:mod:`riglib.harness_mode`:

- claude-code → ``permissions.defaultMode`` (unchanged, the pre-existing writer)
- codex       → ``approvals_reviewer`` (root key of ``~/.codex/config.toml``)
- opencode    → ``permission."*"`` (``~/.config/opencode/opencode.json``)
- omp         → ``tools.approvalMode`` — written by the EXISTING guard-interlocked approval action
                (one owner of the key), whose value now follows the harness auto intent
- pi / commandcode → N/A, recorded with a reason and surfaced as a VISIBLE note, never a silent skip

Hermetic: the fake agent-tools checkout + conftest's isolated HOME; nothing touches a real harness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib.actions import run_plan
from riglib.actions.runner import upsert_toml_root_key
from riglib.catalog import Catalog
from riglib.config import LoadedConfig
from riglib.drift import detect
from riglib.harness_mode import (
    HARNESS_MODE_NA,
    HARNESS_MODES,
    delegated_note,
    harness_auto_intent,
    harness_mode_rows,
)
from riglib.harness_skills import KNOWN_HARNESS_KINDS
from riglib.plan import build


def _cfg(repo: Path, source: Path, **harness) -> LoadedConfig:
    """A harness-only config: every other default-on area is off so the plan stays hermetic."""
    return LoadedConfig(
        data={
            "agent_tools_source": str(source),
            "skills": {"enabled": False}, "agent_hooks": {"enabled": False},
            "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}},
            "agents_md": {"enabled": False}, "tg_ctl": {"enabled": False},
            "gitignore": {"enabled": False}, "ship_delegator": {"enabled": False},
            "github": {"ruleset": {"enabled": False}, "merge": {"enabled": False},
                       "ghas": {"enabled": False}, "actions": {"enabled": False},
                       "browser": {"enabled": False}},
            "permissions": {"enabled": False},
            "harness": {"kind": "claude-code", **harness},
        },
        repo_root=repo,
    )


def _plan(fake_agent_tools, repo: Path, **harness):
    return build(_cfg(repo, fake_agent_tools, **harness), Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def _mode_actions(plan, kind: str):
    return [a for a in plan.actions if a.kind == "apply_harness" and a.options.get("kind") == kind]


def _home() -> Path:
    import os

    return Path(os.environ["HOME"])


# ── registry ───────────────────────────────────────────────────────────────────────────────
def test_every_approval_kind_has_a_mode_entry():
    """The approval action owns a mode key only for kinds the mode registry describes — otherwise
    `rig status` would have nothing to name for that action."""
    from riglib.permissions import HARNESS_APPROVAL

    assert set(HARNESS_APPROVAL) <= set(HARNESS_MODES)


def test_every_known_kind_has_a_mode_entry_or_an_explicit_na_reason():
    """No harness kind may fall through silently: it either has a mode writer or a recorded reason."""
    covered = set(HARNESS_MODES) | set(HARNESS_MODE_NA)
    assert set(KNOWN_HARNESS_KINDS) <= covered, sorted(set(KNOWN_HARNESS_KINDS) - covered)
    assert not set(HARNESS_MODES) & set(HARNESS_MODE_NA)


@pytest.mark.parametrize(
    ("h", "primary", "expected"),
    [
        ({"auto_mode": True}, "claude-code", True),
        ({"auto_mode": False, "mode": "auto"}, "claude-code", True),  # a known pinned mode wins (claude-code writes it verbatim)
        ({"auto_mode": True, "mode": "acceptEdits"}, "claude-code", False),
        ({"auto_mode": False, "mode": "someFutureMode"}, "claude-code", False),  # unknown mode: auto_mode decides
        ({"mode": "auto"}, "claude-code", True),  # Alex's live global config: mode: auto, no auto_mode
        ({"mode": "bypassPermissions"}, "claude-code", True),
        ({"mode": "acceptEdits"}, "claude-code", False),
        ({"mode": "auto_review"}, "codex", True),
        ({"mode": "user"}, "codex", False),
        ({"mode": "ask"}, "opencode", False),
        ({"mode": "bypassPermissions"}, "codex", None),  # a claude-code value on a codex primary: no intent
        ({"mode": "auto"}, "opencode", None),  # `auto` is claude-code's, opencode does not know it
        ({}, "claude-code", None),  # unspecified — the per-kind legacy default decides
    ],
)
def test_harness_auto_intent_inference(h, primary, expected):
    assert harness_auto_intent(h, primary) is expected


# ── codex: approvals_reviewer in ~/.codex/config.toml ──────────────────────────────────────
def _codex_toml(codex_home: Path) -> Path:
    return codex_home / "config.toml"


def test_codex_mode_action_planned_for_additive_kind_from_mode_auto(fake_agent_tools, tmp_path, monkeypatch):
    """`kind: claude-code, kinds: [codex], mode: auto` (the live global config) → codex gets a
    write too, with the auto value, targeting the RIG_CODEX_HOME-resolved config.toml."""
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=["codex"], mode="auto", settings_path=str(repo / ".claude/settings.json"))
    acts = _mode_actions(plan, "codex")
    assert len(acts) == 1
    assert acts[0].target == _codex_toml(codex_home)
    assert acts[0].options["mode_value"] == "auto_review"
    assert acts[0].options["auto_mode"] is True
    # the claude-code primary write is untouched by the fan-out
    assert _mode_actions(plan, "claude-code")


def test_codex_mode_write_preserves_toml_and_is_idempotent(fake_agent_tools, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"; codex_home.mkdir()
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    existing = (
        'model = "gpt-5"\n'
        "\n"
        "[mcp_servers.haft]\n"
        'command = "haft"\n'
        "\n"
        '[projects."/Users/x/repo"]\n'
        'trust_level = "trusted"\n'
    )
    _codex_toml(codex_home).write_text(existing, encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    res = [r for r in first.results if r.action.kind == "apply_harness"]
    assert res and res[0].status == "backed_up", res
    text = _codex_toml(codex_home).read_text(encoding="utf-8")
    # the root key lands BEFORE the first table (a root key after a header would belong to it)
    assert text.index('approvals_reviewer = "auto_review"') < text.index("[mcp_servers.haft]")
    for line in existing.splitlines():
        assert line in text, f"lost: {line!r}"
    # a backup of the prior file exists (default on_conflict=backup)
    assert any(p.name.startswith("config.toml.rig-bak-") for p in codex_home.iterdir())

    second = run_plan(plan)
    res2 = [r for r in second.results if r.action.kind == "apply_harness"]
    assert res2 and res2[0].status == "skipped", res2
    assert _codex_toml(codex_home).read_text(encoding="utf-8") == text


def test_codex_mode_drift_missing_then_modified_then_converged(fake_agent_tools, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    miss = [d for d in detect(plan).by_direction("missing") if d.category == "harness"]
    assert miss and "approvals_reviewer" in miss[0].detail, miss

    run_plan(plan)
    assert not [d for d in detect(plan).items if d.category == "harness"]

    path = _codex_toml(codex_home)
    path.write_text(path.read_text(encoding="utf-8").replace('"auto_review"', '"user"'), encoding="utf-8")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "harness"]
    assert mod and "'user'" in mod[0].detail and "auto_review" in mod[0].detail, mod

    run_plan(plan)
    assert 'approvals_reviewer = "auto_review"' in path.read_text(encoding="utf-8")
    assert not [d for d in detect(plan).items if d.category == "harness"]


def test_codex_interactive_value_when_auto_mode_false(fake_agent_tools, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=False)
    run_plan(plan)
    assert 'approvals_reviewer = "user"' in _codex_toml(codex_home).read_text(encoding="utf-8")


def test_codex_mode_never_clobbers_a_non_string_value(fake_agent_tools, tmp_path, monkeypatch):
    """A root key rig cannot safely rewrite as a string (an inline table) is left alone under
    skip AND under backup — surfaced as drift, never overwritten blind."""
    codex_home = tmp_path / "codex-home"; codex_home.mkdir()
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    _codex_toml(codex_home).write_text("approvals_reviewer = { weird = true }\n", encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "apply_harness"][0]
    assert res.status == "skipped", res.detail
    assert "approvals_reviewer = { weird = true }" in _codex_toml(codex_home).read_text(encoding="utf-8")
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "harness"]
    assert mod, "a non-string managed value must be visible drift"


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("", 'approvals_reviewer = "auto_review"\n'),
        ('model = "x"\n', 'model = "x"\napprovals_reviewer = "auto_review"\n'),
        ('approvals_reviewer = "user"  # mine\n', 'approvals_reviewer = "auto_review"  # mine\n'),
        ('model = "x"\n\n[t]\na = 1\n', 'model = "x"\napprovals_reviewer = "auto_review"\n\n[t]\na = 1\n'),
        ('[t]\napprovals_reviewer = "user"\n', 'approvals_reviewer = "auto_review"\n\n[t]\napprovals_reviewer = "user"\n'),
    ],
)
def test_upsert_toml_root_key(existing, expected):
    merged, conflict = upsert_toml_root_key(existing, "approvals_reviewer", "auto_review")
    assert conflict is None
    assert merged == expected


def test_upsert_toml_root_key_refuses_multiline_strings_and_inline_tables():
    _, c1 = upsert_toml_root_key('x = """a\nb"""\n', "approvals_reviewer", "auto_review")
    assert c1
    # a multiline string INSIDE a later table is irrelevant to the root key — no false conflict
    merged, c3 = upsert_toml_root_key('[projects."/x"]\nnotes = """multi\nline"""\n', "approvals_reviewer", "auto_review")
    assert c3 is None and merged.startswith('approvals_reviewer = "auto_review"\n')
    _, c2 = upsert_toml_root_key("approvals_reviewer = { a = 1 }\n", "approvals_reviewer", "auto_review")
    assert c2


@pytest.mark.parametrize(
    ("kind", "mode", "expected"),
    [
        ("codex", "auto_review", "auto_review"),  # a codex value → inferred auto → the registry value
        ("codex", "user", "user"),  # codex's own interactive value → the interactive registry value
        ("opencode", "ask", "ask"),
    ],
)
def test_primary_non_claude_kind_never_writes_the_raw_mode_string(fake_agent_tools, tmp_path, monkeypatch, kind, mode, expected):
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind=kind, mode=mode)
    acts = _mode_actions(plan, kind)
    assert acts and acts[0].options["mode_value"] == expected


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        ("codex", "bypassPermissions"),  # a claude-code value pasted onto a codex primary
        ("opencode", "auto"),  # `auto` is claude-code's, not an opencode permission value
    ],
)
def test_unknown_mode_on_non_claude_primary_writes_nothing_and_says_so(fake_agent_tools, tmp_path, monkeypatch, kind, mode):
    """The user asked for (what they think is) maximum auto; silently writing the INTERACTIVE
    value — the old `not in auto_values → False` — was the opposite of the ask. Now: no write for
    any kind, one elevated note naming the kind's real values."""
    from riglib.cli import _note_needs_attention

    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind=kind, kinds=["codex", "opencode", "claude-code"], mode=mode)
    assert not [a for a in plan.actions if a.kind == "apply_harness"], plan.actions
    notes = [n for n in plan.notes if f"auto-mode write skipped — mode '{mode}' is not a known {kind} value" in n]
    assert len(notes) == 1, plan.notes
    assert _note_needs_attention(notes[0])
    assert HARNESS_MODES[kind].values[True] in notes[0] and HARNESS_MODES[kind].values[False] in notes[0]
    # the intent is resolved ONCE: no per-kind note may claim "not declared (no harness.mode)"
    # while mode: IS set — every affected kind says "not written" instead, and stays on rig status
    assert not [n for n in plan.notes if "not declared" in n], plan.notes
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    for k in ("codex", "opencode", "claude-code"):
        assert rows[k].value is None and "not written" in rows[k].note and mode in rows[k].note, rows


def test_pinned_mode_precedence_is_the_same_for_every_kind(fake_agent_tools, tmp_path, monkeypatch):
    """`kind: claude-code, kinds: [codex], auto_mode: false, mode: auto` — claude-code has always
    written the pinned `mode:` verbatim (auto), so codex must follow the SAME intent (auto_review),
    not the contradicting auto_mode (interactive)."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=["codex"], auto_mode=False, mode="auto",
                 settings_path=str(repo / ".claude/settings.json"))
    assert _mode_actions(plan, "claude-code")[0].options["mode_value"] == "auto"
    assert _mode_actions(plan, "codex")[0].options["mode_value"] == "auto_review"


def test_unknown_mode_on_claude_code_primary_writes_it_verbatim_but_skips_the_other_kinds(fake_agent_tools, tmp_path, monkeypatch):
    """`kind: claude-code, mode: someFutureMode, kinds: [codex]` — claude-code's vocabulary can
    outgrow the registry: its own write stays verbatim (pre-existing contract), while codex cannot
    infer an intent from it and must say "not written" — never "not declared" while mode: IS set."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=["codex"], mode="someFutureMode",
                 settings_path=str(repo / ".claude/settings.json"))
    assert _mode_actions(plan, "claude-code")[0].options["mode_value"] == "someFutureMode"
    assert not _mode_actions(plan, "codex")
    assert not [n for n in plan.notes if "not declared" in n], plan.notes
    assert any("not a known claude-code value" in n for n in plan.notes), plan.notes
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    assert rows["codex"].value is None and "not written" in rows["codex"].note
    # a lone claude-code primary with an unknown mode has nothing to warn about (verbatim write)
    alone = _plan(fake_agent_tools, repo, mode="someFutureMode", settings_path=str(repo / ".claude/settings.json"))
    assert not [n for n in alone.notes if "auto-mode write skipped" in n], alone.notes


def test_additive_claude_code_interactive_intent_targets_the_user_settings(fake_agent_tools, tmp_path, monkeypatch):
    """`kind: codex, auto_mode: false, kinds: [claude-code]` — the additive claude-code write is a
    per-MACHINE posture like every other additive kind: `default` goes to ~/.claude/settings.json,
    never to the committed project .claude/settings.json (the primary-kind rule for non-auto)."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", kinds=["claude-code"], auto_mode=False)
    cc = _mode_actions(plan, "claude-code")
    assert cc and cc[0].options["mode_value"] == "default"
    assert cc[0].target == _home() / ".claude" / "settings.json"
    assert cc[0].target != repo / ".claude" / "settings.json"


def test_additive_claude_code_does_not_inherit_the_primary_mode_or_settings_path(fake_agent_tools, tmp_path):
    """`kind: opencode, kinds: [claude-code], mode: allow, settings_path: <plugin.js>` — claude-code
    must get its own registry value from the inferred intent and its own user settings file."""
    repo = tmp_path / "repo"; repo.mkdir()
    plugin = repo / ".opencode" / "plugins" / "zz-agent-tools-hook-bridge.js"
    plan = _plan(fake_agent_tools, repo, kind="opencode", kinds=["claude-code"], mode="allow",
                 settings_path=str(plugin))
    cc = _mode_actions(plan, "claude-code")
    assert cc and cc[0].options["mode_value"] == "auto"  # `allow` is opencode's auto → intent True
    assert cc[0].target == _home() / ".claude" / "settings.json"
    assert cc[0].target != plugin
    oc = _mode_actions(plan, "opencode")
    assert oc and oc[0].options["mode_value"] == "allow"


def test_additive_claude_code_with_undeclared_intent_writes_nothing(fake_agent_tools, tmp_path):
    """`kind: opencode, kinds: [claude-code]` and no auto_mode/mode: claude-code is listed for skill
    discovery only — its permissions.defaultMode must not be flipped to `default` behind the user."""
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", kinds=["claude-code"])
    assert not _mode_actions(plan, "claude-code")
    assert any(n.startswith("harness: claude-code auto-mode not declared") for n in plan.notes), plan.notes
    run_plan(plan)
    assert not (_home() / ".claude" / "settings.json").exists()


def test_opencode_scalar_permission_shorthand_is_migrated_not_rejected(fake_agent_tools, tmp_path):
    """opencode documents `"permission": "ask"` as shorthand for `{"*": "ask"}` — a valid file rig
    must converge (with a backup), not refuse as 'not an object'."""
    path = _opencode_json()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permission": "ask", "model": "m"}), encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", auto_mode=True)
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "harness"]
    assert mod and "'ask'" in mod[0].detail, mod
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "backed_up", res.detail
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permission"] == {"*": "allow"} and data["model"] == "m"
    assert not [d for d in detect(plan).items if d.category == "harness"]
    # the shorthand already at the desired value is simply in sync (no rewrite)
    path.write_text(json.dumps({"permission": "allow"}), encoding="utf-8")
    res2 = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res2.status == "skipped", res2.detail


def test_codex_toml_write_fails_closed_without_any_toml_parser(fake_agent_tools, tmp_path, monkeypatch):
    """Python 3.10 without the tomli backport: rig cannot verify the file, so it must NOT edit it
    (a malformed config.toml would otherwise be written back still malformed) — an explicit error
    naming the remedy, and drift that says the same, never a silent fail-open."""
    import sys

    monkeypatch.setitem(sys.modules, "tomllib", None)  # `import tomllib` → ImportError
    monkeypatch.setitem(sys.modules, "tomli", None)
    codex_home = tmp_path / "codex-home"; codex_home.mkdir()
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    before = 'model = "gpt-5"\n'
    _codex_toml(codex_home).write_text(before, encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    drift = [d for d in detect(plan).items if d.category == "harness"]
    assert drift and drift[0].direction == "modified" and "tomli" in drift[0].detail, drift
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "error" and "tomli" in res.detail, res.detail
    assert _codex_toml(codex_home).read_text(encoding="utf-8") == before


def test_toml_mode_keys_are_root_keys():
    """The TOML probe/writer handle ROOT keys only (`read_toml_root_key(text, key_path[0])`); a
    nested TOML key in the registry would be silently mis-probed — pin the invariant."""
    for spec in HARNESS_MODES.values():
        if spec.format == "toml":
            assert len(spec.key_path) == 1, spec


def test_unknown_mode_row_text_is_exact(fake_agent_tools, tmp_path, monkeypatch):
    """The rig status row for an unknown-mode kind is cut out of the note's text — pin the exact
    rendering so a rewording of unknown_mode_kind_note fails here instead of corrupting the row."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", mode="bypassPermissions")
    row = {r.kind: r for r in harness_mode_rows(plan)}["codex"]
    assert row.note == "not written — harness.mode 'bypassPermissions' is not a codex value — not managed"


def test_codex_malformed_toml_is_an_error_and_visible_drift(fake_agent_tools, tmp_path, monkeypatch):
    """A config.toml that does not parse is never edited (rig must not hand codex a file it cannot
    load) and shows as drift to fix by hand."""
    pytest.importorskip("tomllib")
    codex_home = tmp_path / "codex-home"; codex_home.mkdir()
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    bad = "model = [\n"
    _codex_toml(codex_home).write_text(bad, encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "error" and "malformed" in res.detail, res.detail
    assert _codex_toml(codex_home).read_text(encoding="utf-8") == bad
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "harness"]
    assert mod and "malformed" in mod[0].detail, mod


# ── opencode: permission."*" in ~/.config/opencode/opencode.json ───────────────────────────
def _opencode_json() -> Path:
    return _home() / ".config" / "opencode" / "opencode.json"


def test_opencode_mode_write_preserves_siblings_and_is_idempotent(fake_agent_tools, tmp_path):
    path = _opencode_json()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "m", "permission": {"bash": {"git *": "allow"}}}), encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", auto_mode=True)
    acts = _mode_actions(plan, "opencode")
    assert acts and acts[0].target == path and acts[0].options["mode_value"] == "allow"
    first = run_plan(plan)
    assert not first.errors, [r.detail for r in first.errors]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permission"]["*"] == "allow"
    assert data["permission"]["bash"] == {"git *": "allow"}
    assert data["model"] == "m"
    second = run_plan(plan)
    res2 = [r for r in second.results if r.action.kind == "apply_harness"]
    assert res2 and res2[0].status == "skipped", res2


def test_opencode_mode_drift_and_conflict_backup(fake_agent_tools, tmp_path):
    path = _opencode_json()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permission": {"*": "ask"}}), encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", auto_mode=True)
    mod = [d for d in detect(plan).by_direction("modified") if d.category == "harness"]
    assert mod and "permission.*" in mod[0].detail and "'ask'" in mod[0].detail, mod
    report = run_plan(plan)
    res = [r for r in report.results if r.action.kind == "apply_harness"][0]
    assert res.status == "backed_up", res.detail
    assert any(p.name.startswith("opencode.json.rig-bak-") for p in path.parent.iterdir())
    assert json.loads(path.read_text(encoding="utf-8"))["permission"]["*"] == "allow"
    assert not [d for d in detect(plan).items if d.category == "harness"]


def test_opencode_mode_key_is_not_a_permissions_extra(fake_agent_tools, tmp_path):
    """The managed wildcard sits beside the rig-managed permission.bash allowlist; the permissions
    drift check must not report it as a stray user entry."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools, kind="opencode", auto_mode=True)
    cfg.data["permissions"] = {"enabled": True}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert json.loads(_opencode_json().read_text(encoding="utf-8"))["permission"]["*"] == "allow"
    extras = [d for d in detect(plan).by_direction("extra") if d.category == "permissions"]
    assert not extras, [d.detail for d in extras]


# ── omp: tools.approvalMode via the guard-interlocked approval action ───────────────────────
def _omp_yml() -> Path:
    return _home() / ".omp" / "agent" / "config.yml"


def _omp_plan(fake_agent_tools, repo: Path, **harness):
    cfg = _cfg(repo, fake_agent_tools, kind="omp", **harness)
    cfg.data["permissions"] = {"enabled": True}
    return build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")


def _approval_value(plan) -> str:
    acts = [a for a in plan.actions if a.kind == "provision_harness_approval"]
    assert len(acts) == 1, acts
    return acts[0].options["mode_value"]


def test_omp_mode_is_owned_by_the_approval_action_and_follows_intent(fake_agent_tools, tmp_path):
    import yaml

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _omp_plan(fake_agent_tools, repo, auto_mode=True)
    assert not _mode_actions(plan, "omp"), "omp must have ONE owner of tools.approvalMode"
    assert _approval_value(plan) == "yolo"
    assert any("harness: omp" in n and "tools.approvalMode" in n and "skipped" not in n for n in plan.notes), plan.notes
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "yolo"


def test_omp_converges_back_to_always_ask_after_rig_wrote_yolo(fake_agent_tools, tmp_path):
    """auto_mode true → false must re-enable prompts: the `yolo` rig itself installed (receipt) is
    rig-owned and converged, not mistaken for a user-set value and left relaxed forever. A value the
    USER set (no receipt) stays untouched — `test_approval_never_clobbers_a_differing_user_value`."""
    import yaml

    repo = tmp_path / "repo"; repo.mkdir()
    first = run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=True))
    assert not first.errors, [r.detail for r in first.errors]
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "yolo"

    plan = _omp_plan(fake_agent_tools, repo, auto_mode=False)
    drift = [d for d in detect(plan).items if d.category == "permissions"]
    # status says what apply will DO — a rig-owned value converges, it is not a hand fix
    assert drift and "apply converges" in drift[0].detail and "never clobbers" not in drift[0].detail, drift
    second = run_plan(plan)
    assert not second.errors, [r.detail for r in second.errors]
    res = [r for r in second.results if r.action.kind == "provision_harness_approval"][0]
    assert res.status in ("updated", "backed_up"), res.detail
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "always-ask"
    receipt = json.loads((_omp_yml().parent / ".rig-permissions-receipt.json").read_text())
    # `previous` stays the ORIGINAL pre-rig state (the key was absent), never rig's own `yolo` —
    # a deprovision must restore what the user had, not rig's intermediate write
    assert receipt["managed"]["tools.approvalMode"] == {"previous": None, "installed": "always-ask"}
    assert not [d for d in detect(plan).items if d.category == "permissions"]
    third = run_plan(plan)
    assert [r for r in third.results if r.action.kind == "provision_harness_approval"][0].status == "skipped"
    # a value the USER changed afterwards (differs from what the receipt says rig installed) is a
    # conflict again — rig-ownership is exactly "still holds what rig wrote", nothing wider
    _omp_yml().write_text("tools:\n  approvalMode: write\n", encoding="utf-8")
    drift4 = [d for d in detect(plan).items if d.category == "permissions"]
    assert drift4 and "never clobbers" in drift4[0].detail, drift4
    fourth = run_plan(plan)
    res4 = [r for r in fourth.results if r.action.kind == "provision_harness_approval"][0]
    assert res4.status == "skipped" and "differs" in res4.detail, res4.detail
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "write"


def test_omp_tightening_is_not_blocked_by_a_missing_guard(fake_agent_tools, tmp_path):
    """The guard interlock exists to stop RELAXING to yolo without the belt; tightening back to
    always-ask is the safe direction and must go through even when the guard file is gone —
    otherwise a user who removed the guard is stuck in yolo. Drift must not call an always-ask
    posture "relaxed" either."""
    import yaml

    from riglib.actions.runner import resolve_guard_target

    repo = tmp_path / "repo"; repo.mkdir()
    first = run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=True))
    assert not first.errors, [r.detail for r in first.errors]
    plan = _omp_plan(fake_agent_tools, repo, auto_mode=False)
    approval = [a for a in plan.actions if a.kind == "provision_harness_approval"][0]
    guard = resolve_guard_target(approval)
    assert guard is not None and guard.is_file()
    guard.unlink()
    plan.actions = [a for a in plan.actions if a.kind != "install_harness_guard"]  # the belt stays gone
    res = [r for r in run_plan(plan).results if r.action.kind == "provision_harness_approval"][0]
    assert res.status in ("updated", "backed_up"), res.detail
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "always-ask"
    assert not [d for d in detect(plan).items if d.category == "permissions" and "relaxed" in d.detail]
    # the relaxing direction keeps the interlock — yolo AND the intermediate `write` (a pinned
    # primary mode that auto-approves edits is a relaxed posture too, not a tightening)
    for relaxed in ({"auto_mode": True}, {"mode": "write"}):
        relax = _omp_plan(fake_agent_tools, repo, **relaxed)
        relax.actions = [a for a in relax.actions if a.kind != "install_harness_guard"]
        res2 = [r for r in run_plan(relax).results if r.action.kind == "provision_harness_approval"][0]
        assert res2.status == "error" and "guard" in res2.detail, (relaxed, res2.detail)
        assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "always-ask"


def test_omp_receipt_backup_stays_the_pre_rig_restore_point(fake_agent_tools, tmp_path):
    """First write: no config.yml existed → receipt `backup: null`. A later convergence backs up
    rig's OWN prior write, but the receipt must keep `null` — the pre-rig state was "absent", and a
    file-level restore from the new backup would bring back rig's yolo, not the user's file."""
    repo = tmp_path / "repo"; repo.mkdir()
    assert not run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=True)).errors
    receipt_file = _omp_yml().parent / ".rig-permissions-receipt.json"
    assert json.loads(receipt_file.read_text())["backup"] is None
    res = [r for r in run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=False)).results
           if r.action.kind == "provision_harness_approval"][0]
    assert res.status == "backed_up", res.detail
    assert json.loads(receipt_file.read_text())["backup"] is None


def test_primary_omp_pinned_write_mode_is_written_verbatim(fake_agent_tools, tmp_path):
    """`kind: omp, mode: write` is omp's own value — the documented exact override, kept as is.
    The FULL plan (guard action included, nothing filtered) applies cleanly: a relaxed pinned mode
    is interlocked on the guard, and the planner emits that guard in the same plan."""
    import yaml

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _omp_plan(fake_agent_tools, repo, mode="write")
    assert _approval_value(plan) == "write"
    assert any(a.kind == "install_harness_guard" for a in plan.actions)
    outcome = run_plan(plan)
    assert not outcome.errors, [r.detail for r in outcome.errors]
    res = [r for r in outcome.results if r.action.kind == "provision_harness_approval"][0]
    assert res.status == "created", res.detail
    assert yaml.safe_load(_omp_yml().read_text(encoding="utf-8"))["tools"]["approvalMode"] == "write"


def test_omp_malformed_receipt_does_not_hide_this_runs_backup(fake_agent_tools, tmp_path):
    """A receipt file without managed entries (hand-damaged `{"template": 1}`) is no provenance:
    the write backs the user's file up and the receipt records THAT backup, never a null that would
    claim rig created the file."""
    _omp_yml().parent.mkdir(parents=True)
    _omp_yml().write_text("model: x\n", encoding="utf-8")
    receipt_file = _omp_yml().parent / ".rig-permissions-receipt.json"
    receipt_file.write_text('{"template": 1}\n', encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    res = [r for r in run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=True)).results
           if r.action.kind == "provision_harness_approval"][0]
    assert res.status == "backed_up", res.detail
    backup = json.loads(receipt_file.read_text())["backup"]
    assert backup and Path(backup).read_text(encoding="utf-8") == "model: x\n"


def test_omp_interactive_value_when_auto_mode_false(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    assert _approval_value(_omp_plan(fake_agent_tools, repo, auto_mode=False)) == "always-ask"


def test_omp_unspecified_intent_keeps_the_legacy_yolo_posture(fake_agent_tools, tmp_path):
    """rig-cli#202 owner decision: omp without an explicit intent stays yolo (guard-belted)."""
    repo = tmp_path / "repo"; repo.mkdir()
    assert _approval_value(_omp_plan(fake_agent_tools, repo)) == "yolo"


def test_omp_mode_skipped_note_when_permissions_disabled(fake_agent_tools, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="omp", auto_mode=True)  # permissions.enabled: false
    assert not [a for a in plan.actions if a.kind == "provision_harness_approval"]
    from riglib.cli import _note_needs_attention

    notes = [n for n in plan.notes if "harness: auto-mode write skipped" in n and "omp" in n]
    assert notes, plan.notes
    assert _note_needs_attention(notes[0]), "a config that asked for a mode nobody writes must be elevated"
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    assert rows["omp"].value is None and "disabled" in rows["omp"].note


def test_omp_skipped_note_when_omp_is_not_among_the_permissions_kinds(fake_agent_tools, tmp_path):
    """permissions is ENABLED but pinned to another kind, so no approval action carries omp's
    write — the note must say skipped (elevated) and the status row must still exist. This is
    the gate mismatch a re-derived `written` guess got wrong: it looked only at
    permissions.enabled and would have claimed "written by the approval action"."""
    from riglib.cli import _note_needs_attention

    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools, kind="omp", auto_mode=True)
    cfg.data["permissions"] = {"enabled": True, "kind": "claude-code"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_harness_approval" and a.options.get("kind") == "omp"]
    notes = [n for n in plan.notes if "harness: auto-mode write skipped" in n and "omp" in n]
    assert notes and _note_needs_attention(notes[0]), plan.notes
    assert "permissions.enabled: false" not in notes[0]  # the wording covers this case too
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    assert rows["omp"].value is None and "skipped" in rows["omp"].note


def test_omp_row_survives_a_delegated_note_without_an_owning_action():
    """Defense in depth: even if a plan ever carried the written=True note with no approval
    action, the kind degrades to a visible row instead of vanishing from `rig status`."""
    from types import SimpleNamespace

    plan = SimpleNamespace(actions=[], notes=[delegated_note("omp", written=True)])
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    assert rows["omp"].value is None and "no such action" in rows["omp"].note


def test_omp_posture_ignores_a_harness_block_that_does_not_configure_omp(fake_agent_tools, tmp_path):
    """`harness: {kind: claude-code, auto_mode: false}` + `permissions: {kind: omp}`: the omp
    allowlist is provisioned without omp ever being a configured harness kind — its approval
    posture must keep the legacy yolo, not inherit an interactive intent meant for claude-code."""
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools, kind="claude-code", auto_mode=False,
               settings_path=str(repo / ".claude/settings.json"))
    cfg.data["permissions"] = {"enabled": True, "kind": "omp"}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    assert _approval_value(plan) == "yolo"


# ── N/A kinds: explicit, visible, never silent ─────────────────────────────────────────────
@pytest.mark.parametrize("kind", sorted(HARNESS_MODE_NA))
def test_na_kind_records_a_visible_note_even_without_auto_mode_set(fake_agent_tools, tmp_path, kind):
    """A plain `kind: pi` (nothing asked) still gets a VISIBLE note — informational, not an
    alarm on every apply, since there is nothing actionable in it."""
    from riglib.cli import _note_needs_attention

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind=kind)
    assert not _mode_actions(plan, kind)
    notes = [n for n in plan.notes if n.startswith("harness:") and kind in n]
    assert notes and HARNESS_MODE_NA[kind] in notes[0], plan.notes
    assert not _note_needs_attention(notes[0]), "nothing was asked — the N/A note is informational"
    assert {r.kind: r for r in harness_mode_rows(plan)}[kind].value is None


@pytest.mark.parametrize("kind", sorted(HARNESS_MODE_NA))
def test_na_kind_note_is_elevated_when_a_mode_was_asked_for(fake_agent_tools, tmp_path, kind):
    """`kind: pi, auto_mode: true` asked for something rig cannot write for pi — elevated."""
    from riglib.cli import _note_needs_attention

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind=kind, auto_mode=True)
    notes = [n for n in plan.notes if n.startswith("harness:") and kind in n]
    assert notes and "no auto/permission-mode setting" in notes[0], plan.notes
    assert _note_needs_attention(notes[0]), "the config asked for a mode nobody can write"


@pytest.mark.parametrize("kind", sorted(HARNESS_MODE_NA))
def test_na_kind_as_additive_next_to_auto_mode_is_informational(fake_agent_tools, tmp_path, kind):
    """`kind: claude-code, auto_mode: true, kinds: [pi]` — the documented skills-only listing (and
    the live global config's shape). The intent targets claude-code; pi's n/a note must stay
    informational, not an attention item on every apply. Elevation is for a PRIMARY n/a kind only."""
    from riglib.cli import _note_needs_attention

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=[kind], auto_mode=True, settings_path=str(repo / ".claude/settings.json"))
    assert _mode_actions(plan, "claude-code")
    notes = [n for n in plan.notes if n.startswith("harness:") and kind in n]
    assert notes and HARNESS_MODE_NA[kind] in notes[0], plan.notes
    assert not _note_needs_attention(notes[0]), notes[0]
    assert {r.kind: r for r in harness_mode_rows(plan)}[kind].value is None


# ── rig status rows: one line per configured kind ──────────────────────────────────────────
def test_harness_mode_rows_cover_every_configured_kind(fake_agent_tools, tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    cfg = _cfg(repo, fake_agent_tools, kinds=["codex", "opencode", "omp", "pi"], mode="auto",
               settings_path=str(repo / ".claude/settings.json"))
    cfg.data["permissions"] = {"enabled": True}
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")
    rows = harness_mode_rows(plan)
    by_kind = {r.kind: r for r in rows}
    assert list(by_kind) == ["claude-code", "codex", "opencode", "omp", "pi"]
    assert by_kind["claude-code"].key == "permissions.defaultMode" and by_kind["claude-code"].value == "auto"
    assert by_kind["codex"].key == "approvals_reviewer" and by_kind["codex"].value == "auto_review"
    assert by_kind["codex"].path == _codex_toml(codex_home)
    assert by_kind["opencode"].key == "permission.*" and by_kind["opencode"].value == "allow"
    assert by_kind["omp"].key == "tools.approvalMode" and by_kind["omp"].value == "yolo"
    assert by_kind["pi"].value is None and HARNESS_MODE_NA["pi"] in by_kind["pi"].note


def test_status_prints_a_harness_mode_line_per_kind(fake_agent_tools, tmp_path, monkeypatch, capsys):
    from riglib.cli import _print_harness_mode_status

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="claude-code", kinds=["pi"], auto_mode=True,
                 settings_path=str(repo / ".claude/settings.json"))
    _print_harness_mode_status(plan, detect(plan))  # nothing on disk yet → drift
    out = capsys.readouterr().out
    assert "permissions.defaultMode" in out and "auto" in out and "drift" in out
    assert "pi" in out and "n/a" in out
    run_plan(plan)
    _print_harness_mode_status(plan, detect(plan))
    assert "in sync" in capsys.readouterr().out


def test_status_omp_row_tracks_the_permissions_approval_drift(fake_agent_tools, tmp_path, capsys):
    """omp's drift is filed under the permissions approval action, not ("harness", "omp") — the
    status row must follow the owning action or it prints "in sync" over a drifted key."""
    from riglib.cli import _print_harness_mode_status

    repo = tmp_path / "repo"; repo.mkdir()
    plan = _omp_plan(fake_agent_tools, repo, auto_mode=True)
    run_plan(plan)
    path = _omp_yml()
    path.write_text(path.read_text(encoding="utf-8").replace("yolo", "always-ask"), encoding="utf-8")
    report = detect(plan)
    _print_harness_mode_status(plan, report)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("omp"))
    assert "tools.approvalMode" in line and "drift" in line and "in sync" not in line


@pytest.mark.parametrize("kind", ["codex", "opencode"])
def test_undeclared_intent_on_additive_kind_writes_nothing_but_says_so(fake_agent_tools, tmp_path, monkeypatch, kind):
    """`kinds: [opencode]` with no auto_mode/mode (the skills-only additive shape) must not tighten
    opencode to `ask` or create codex's config.toml behind the user's back."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=[kind], settings_path=str(repo / ".claude/settings.json"))
    assert not _mode_actions(plan, kind)
    notes = [n for n in plan.notes if n.startswith(f"harness: {kind} auto-mode not declared")]
    assert notes, plan.notes
    run_plan(plan)
    assert not (tmp_path / "codex-home" / "config.toml").exists()
    assert not _opencode_json().exists()
    rows = {r.kind: r for r in harness_mode_rows(plan)}
    assert rows[kind].value is None and "not declared" in rows[kind].note


def test_harness_disabled_keeps_the_legacy_omp_posture(fake_agent_tools, tmp_path):
    """`harness.enabled: false` = leave the harness posture alone: auto_mode must not leak."""
    repo = tmp_path / "repo"; repo.mkdir()
    assert _approval_value(_omp_plan(fake_agent_tools, repo, enabled=False, auto_mode=False)) == "yolo"


# ── writer edge cases ─────────────────────────────────────────────────────────────────────
def test_opencode_empty_file_is_created_not_a_permanent_malformed_error(fake_agent_tools, tmp_path):
    """A touched, 0-byte opencode.json (plausible: created by hand, never written) must be
    treated as missing — `json.loads("")` used to make it a malformed error forever."""
    repo = tmp_path / "repo"; repo.mkdir()
    target = _home() / ".config" / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    plan = _plan(fake_agent_tools, repo, kind="opencode", auto_mode=True)
    harness_drift = [d for d in detect(plan).items if d.category == "harness"]
    assert harness_drift and all(d.direction == "missing" for d in harness_drift), harness_drift
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    res = [r for r in report.results if r.action.kind == "apply_harness"][0]
    assert res.status in ("created", "updated", "backed_up"), res.detail
    import json

    assert json.loads(target.read_text())["permission"]["*"] == "allow"


def test_codex_root_key_probe_ignores_a_multiline_marker_inside_a_comment():
    """A triple-quote marker inside a root-section comment is a comment, not a multiline string;
    it must not turn every apply into a permanent skip."""
    from riglib.actions.runner import read_toml_root_key

    text = '# use """ for multiline values\napprovals_reviewer = "user"\n'
    assert read_toml_root_key(text, "approvals_reviewer") == ("present", "user")
    merged, conflict = upsert_toml_root_key(text, "approvals_reviewer", "auto_review")
    assert conflict is None and 'approvals_reviewer = "auto_review"' in merged and merged.startswith("# use")


def test_generic_mode_writer_refuses_an_unknown_format(tmp_path):
    """Only the registry's formats are serialized; a yaml action (omp's key belongs to the
    approval action) must never fall through to the JSON writer."""
    from riglib.actions.runner import _do_apply_harness_mode
    from riglib.plan import Action

    action = Action(kind="apply_harness", category="harness", item="omp", source=tmp_path,
                    target=tmp_path / "config.yml",
                    options={"kind": "omp", "auto_mode": True, "mode_value": "yolo", "format": "yaml"})
    with pytest.raises(ValueError, match="no mode writer for format 'yaml'"):
        _do_apply_harness_mode(action, "backup")


# ── a pinned harness.mode is the PRIMARY kind's exact override ────────────────────────────
def test_primary_opencode_pinned_deny_is_written_verbatim(fake_agent_tools, tmp_path):
    """`kind: opencode, mode: deny` is opencode's own value: written as `deny` (the documented exact
    override), never reduced to the interactive intent and relaxed to `ask`; `rig status` shows it."""
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", mode="deny")
    acts = _mode_actions(plan, "opencode")
    assert len(acts) == 1 and acts[0].options["mode_value"] == "deny" and acts[0].options["auto_mode"] is False
    assert {r.kind: r.value for r in harness_mode_rows(plan)}["opencode"] == "deny"
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "created", res.detail
    assert json.loads(_opencode_json().read_text(encoding="utf-8"))["permission"]["*"] == "deny"


def test_additive_kind_never_gets_the_primary_raw_mode(fake_agent_tools, tmp_path, monkeypatch):
    """The primary's string is harness-specific: `kind: claude-code, mode: plan, kinds: [opencode,
    codex]` gives the additive kinds the MAPPED interactive value, not `plan`; and a primary
    opencode `deny` maps to `user` for an additive codex."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kinds=["opencode", "codex"], mode="plan",
                 settings_path=str(repo / ".claude/settings.json"))
    assert _mode_actions(plan, "opencode")[0].options["mode_value"] == "ask"
    assert _mode_actions(plan, "codex")[0].options["mode_value"] == "user"
    plan2 = _plan(fake_agent_tools, repo, kind="opencode", kinds=["codex"], mode="deny")
    assert _mode_actions(plan2, "opencode")[0].options["mode_value"] == "deny"
    assert _mode_actions(plan2, "codex")[0].options["mode_value"] == "user"


def test_codex_absent_toml_is_not_created_without_a_parser(fake_agent_tools, tmp_path, monkeypatch):
    """No config.toml yet AND no TOML parser: rig cannot verify what it would create, so the file
    is NOT created — the error (and drift) name the tomli remedy, never a misleading "would not
    parse" about rig's own valid TOML."""
    import sys

    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", None)
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("RIG_CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", auto_mode=True)
    drift = [d for d in detect(plan).items if d.category == "harness"]
    assert drift and drift[0].direction == "modified" and "tomli" in drift[0].detail, drift
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "error" and "tomli" in res.detail and "would not parse" not in res.detail, res.detail
    assert not _codex_toml(codex_home).exists()


def test_unknown_mode_row_survives_a_delimiter_in_the_mode(fake_agent_tools, tmp_path, monkeypatch):
    """A user mode containing the note's own ` — ` delimiter must render intact in `rig status`
    (the row is sliced on the note's known suffix, never split on the delimiter)."""
    monkeypatch.setenv("RIG_CODEX_HOME", str(tmp_path / "codex-home"))
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="codex", mode="bad — forged")
    row = {r.kind: r for r in harness_mode_rows(plan)}["codex"]
    assert row.note == "not written — harness.mode 'bad — forged' is not a codex value — not managed"


def test_opencode_object_under_the_mode_key_is_a_conflict_left_untouched(fake_agent_tools, tmp_path):
    """`permission."*"` holding an OBJECT is not a plain value rig may rewrite blind: skipped with
    the file byte-identical, and drift says so — the same contract the TOML inline-table path keeps."""
    path = _opencode_json()
    path.parent.mkdir(parents=True)
    before = json.dumps({"permission": {"*": {"read": "allow"}}})
    path.write_text(before, encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    plan = _plan(fake_agent_tools, repo, kind="opencode", auto_mode=True)
    drift = [d for d in detect(plan).items if d.category == "harness"]
    assert drift and "not a plain value" in drift[0].detail, drift
    res = [r for r in run_plan(plan).results if r.action.kind == "apply_harness"][0]
    assert res.status == "skipped" and "not a plain value" in res.detail, res.detail
    assert path.read_text(encoding="utf-8") == before


def test_omp_adopted_config_gets_its_first_backup_recorded_on_convergence(fake_agent_tools, tmp_path):
    """A user config that already matched was ADOPTED (receipt `backup: null`, file untouched —
    nothing to back up yet). The first convergence that rewrites it backs the ORIGINAL up, and the
    receipt must record THAT backup (a recorded null means "rig created the file" only)."""
    original = "# my omp config\ntools:\n  approvalMode: always-ask\n"
    _omp_yml().parent.mkdir(parents=True)
    _omp_yml().write_text(original, encoding="utf-8")
    repo = tmp_path / "repo"; repo.mkdir()
    adopt = [r for r in run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=False)).results
             if r.action.kind == "provision_harness_approval"][0]
    assert adopt.status == "updated" and "adopted" in adopt.detail, adopt.detail
    receipt_file = _omp_yml().parent / ".rig-permissions-receipt.json"
    assert json.loads(receipt_file.read_text())["backup"] is None
    assert _omp_yml().read_text(encoding="utf-8") == original
    res = [r for r in run_plan(_omp_plan(fake_agent_tools, repo, auto_mode=True)).results
           if r.action.kind == "provision_harness_approval"][0]
    assert res.status == "backed_up", res.detail
    backup = json.loads(receipt_file.read_text())["backup"]
    assert backup and Path(backup).read_text(encoding="utf-8") == original
