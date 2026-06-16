"""rig-managed block in the GLOBAL git excludes file — the ``gitignore`` block.

This is the GLOBAL counterpart of the per-repo gitignore approach (superseded #23): rig owns ONE
marker-delimited block in git's global ``core.excludesfile`` so harness artifacts (chiefly Claude
Code's throwaway ``**/.claude/worktrees/``) are ignored in EVERY repo on the machine — with zero
per-repo commits and no per-repo ``rig apply``.

Covers: target resolution BOTH ways (``core.excludesfile`` already set vs unset), every resolved
``state`` (create / update / ok / conflict / io_error), STRICT idempotent re-apply (byte-identical
after a 2nd apply), the dedup-of-managed-region collapse (a prior non-idempotent appender left
several blocks), the default-ON + opt-out plan gating, config validation, and drift parity. The
guiding invariant: apply and drift switch on the SAME ``resolve_global_excludes`` state, so they can
never disagree — and rig only ever edits its OWN marker-fenced lines plus the ``core.excludesfile``
git-config setting, preserving every other line in the file verbatim (CRLF and trailing blanks
included).

Every test INJECTS the git-config read/write seams (``_git_global`` / ``_set_git_global``) and the
target file path, so no test ever runs real ``git config --global`` or writes the real
``~/.gitignore``. ``.serena/`` is deliberately NOT ignored (Serena state is committed shared
project memory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import drift as driftmod
from riglib.actions import runner as runnermod
from riglib.actions.runner import (
    _do_provision_global_excludes,
    global_excludes_block_text,
    resolve_global_excludes,
    run_plan,
)
from riglib.config import (
    GITIGNORE_BEGIN_MARKER,
    GITIGNORE_DEFAULT_ENTRIES,
    GITIGNORE_END_MARKER,
    ConfigError,
    LoadedConfig,
    validate,
)
from riglib.drift import (
    DriftReport,
    check_disabled_global_excludes,
    detect,
)
from riglib.plan import Action, InstallPlan, build

DEFAULT_ENTRIES = list(GITIGNORE_DEFAULT_ENTRIES)


# ── seams: inject the git-config read/write so no test touches real `git config --global` ──
@pytest.fixture
def git_config(monkeypatch):
    """A controllable in-memory ``git config --global`` for the global-excludes seams.

    Patches ``_git_global`` (read) and ``_set_git_global`` (write) on BOTH the runner and the
    drift module (drift imports ``_git_global`` by name). Returns the backing dict so a test can
    pre-seed ``core.excludesfile`` (the "already set" path) or assert what apply WROTE.
    """
    store: dict[str, str] = {}

    def _read(key: str):
        return store.get(key)

    def _write(key: str, value: str) -> int:
        store[key] = value
        return 0

    for mod in (runnermod, driftmod):
        monkeypatch.setattr(mod, "_git_global", _read, raising=False)
    monkeypatch.setattr(runnermod, "_set_git_global", _write)
    return store


def _action(target: Path | None = None, entries=None, *, override: str | None = None) -> Action:
    options: dict = {
        "entries": entries if entries is not None else DEFAULT_ENTRIES,
        "xdg_default": "~/.config/git/ignore",
    }
    if override is not None:
        options["excludesfile"] = override
    return Action(
        kind="provision_global_excludes",
        category="gitignore",
        item="block",
        source=Path("/repo"),
        target=target if target is not None else Path("~/.config/git/ignore"),
        options=options,
    )


def _apply(target: Path | None = None, entries=None, *, override: str | None = None):
    return _do_provision_global_excludes(_action(target, entries, override=override), "backup")


def _block(entries) -> str:
    return global_excludes_block_text(entries)


# ── target resolution: core.excludesfile ALREADY set → manage THAT file, no git-config write ──
def test_target_resolution_honors_existing_excludesfile(git_config, tmp_path):
    existing = tmp_path / "my-global-ignore"
    git_config["core.excludesfile"] = str(existing)
    res = _apply()
    assert res.status == "created"
    assert existing.is_file()  # the block landed in the user's existing file
    assert _block(DEFAULT_ENTRIES) in existing.read_text()
    # rig did NOT move/rewrite core.excludesfile — the user's choice is respected.
    assert git_config["core.excludesfile"] == str(existing)


# ── target resolution: core.excludesfile UNSET → set it to XDG default AND write the block ──
def test_target_resolution_sets_excludesfile_when_unset(git_config, monkeypatch, tmp_path):
    # isolate HOME so the XDG default (~/.config/git/ignore) expands under the throwaway dir.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert "core.excludesfile" not in git_config
    res = _apply()
    assert res.status == "created"
    # clean-machine: rig wrote git config AND created the file at the XDG default.
    assert git_config["core.excludesfile"] == "~/.config/git/ignore"
    written = home / ".config" / "git" / "ignore"
    assert written.is_file()
    assert _block(DEFAULT_ENTRIES) in written.read_text()


def test_target_resolution_respects_xdg_config_home(git_config, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    res = _apply()
    assert res.status == "created"
    written = xdg / "git" / "ignore"
    assert written.is_file()  # follows XDG_CONFIG_HOME, where git actually reads
    assert _block(DEFAULT_ENTRIES) in written.read_text()


def test_explicit_excludesfile_override_sets_config_to_it(git_config, tmp_path):
    forced = tmp_path / "forced-ignore"
    git_config["core.excludesfile"] = "/some/other/file"  # config points elsewhere
    res = _apply(override=str(forced))
    assert res.status == "created"
    assert forced.is_file()
    # override wins: rig set core.excludesfile to the override since git's value didn't match.
    assert git_config["core.excludesfile"] == str(forced)


# ── zero-churn no-op: the canonical block is byte-identical to a provisioned machine's ──
def test_canonical_block_text_is_byte_stable():
    """Pin the EXACT default block so a re-apply against an already-provisioned machine is a true
    no-op (the CTO's hard requirement). A reword of the marker/comment/entry would rewrite every
    provisioned machine's block — this regression test makes that an explicit, visible change.
    """
    expected = (
        "# >>> rig-managed (do not edit) >>>\n"
        "# Claude Code creates throwaway worktrees under each repo's .claude/worktrees/; "
        "rig ignores them globally.\n"
        "**/.claude/worktrees/\n"
        "# <<< rig-managed (do not edit) <<<"
    )
    assert global_excludes_block_text(DEFAULT_ENTRIES) == expected


def test_reapply_is_noop_against_already_provisioned_block(git_config, tmp_path):
    """A file that already contains the exact canonical block (among other user lines) resolves to
    ``ok`` and a re-apply leaves it byte-identical — the provisioned-machine steady state.
    """
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(
        "*.pyc\n**/.claude/settings.local.json\n\n" + _block(DEFAULT_ENTRIES) + "\n",
        encoding="utf-8",
    )
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "ok"
    before = gi.read_bytes()
    assert _apply(gi).status == "skipped"
    assert gi.read_bytes() == before  # zero churn


# ── default entry is the harness worktrees dir, never .serena/ ────────────────────
def test_default_entries_ignore_worktrees_not_serena():
    assert "**/.claude/worktrees/" in DEFAULT_ENTRIES
    assert ".serena/" not in DEFAULT_ENTRIES  # Serena state is committed, never ignored
    block = global_excludes_block_text(DEFAULT_ENTRIES)
    assert block.startswith(GITIGNORE_BEGIN_MARKER)
    assert block.endswith(GITIGNORE_END_MARKER)
    assert "**/.claude/worktrees/" in block


# ── create: fresh file ─────────────────────────────────────────────────────────────
def test_fresh_create_when_no_file(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "create"
    res = _apply(gi)
    assert res.status == "created"
    assert gi.is_file()
    text = gi.read_text()
    assert _block(DEFAULT_ENTRIES) in text
    assert text.endswith("\n")


def test_create_appends_to_existing_file_preserving_lines(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text("node_modules/\n*.log\n", encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "create"
    res = _apply(gi)
    assert res.status == "created"
    text = gi.read_text()
    assert "node_modules/\n*.log\n" in text  # user lines preserved verbatim
    assert _block(DEFAULT_ENTRIES) in text
    assert text.index("node_modules/") < text.index(GITIGNORE_BEGIN_MARKER)
    assert "*.log\n\n# >>> rig-managed" in text  # single blank-line separator


# ── ok: STRICT idempotent re-apply (byte-identical) ────────────────────────────────
def test_idempotent_second_apply_byte_identical(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    assert _apply(gi).status == "created"
    before = gi.read_bytes()
    second = _apply(gi)
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "ok"
    assert second.status == "skipped" and "already correct" in second.detail
    assert gi.read_bytes() == before  # byte-identical, no churn, no append


def test_idempotent_when_block_already_present_among_other_lines(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"node_modules/\n\n{_block(DEFAULT_ENTRIES)}\n\n*.tmp\n", encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "ok"
    res = _apply(gi)
    assert res.status == "skipped"
    text = gi.read_text()
    assert "node_modules/" in text and "*.tmp" in text  # both sides preserved


# ── update: block differs, just the block is replaced ──────────────────────────────
def test_update_replaces_just_the_block_preserving_other_lines(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    stale = global_excludes_block_text([".claude/old-cruft/"])
    gi.write_text(f"# top user line\nbuild/\n\n{stale}\n\n# bottom user line\ndist/\n", encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "update"
    res = _apply(gi)
    assert res.status == "updated"
    text = gi.read_text()
    assert "**/.claude/worktrees/" in text
    assert ".claude/old-cruft/" not in text
    assert "# top user line" in text and "build/" in text
    assert "# bottom user line" in text and "dist/" in text
    assert text.index("build/") < text.index(GITIGNORE_BEGIN_MARKER) < text.index("# bottom user line")


def test_update_then_idempotent(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"x/\n{global_excludes_block_text(['.claude/old/'])}\ny/\n", encoding="utf-8")
    assert _apply(gi).status == "updated"
    before = gi.read_bytes()
    assert _apply(gi).status == "skipped"
    assert gi.read_bytes() == before  # converged → strictly idempotent


# ── DEDUP: a prior non-idempotent appender left SEVERAL managed blocks → collapse to one ──
def test_dedup_collapses_multiple_managed_blocks(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    block = _block(DEFAULT_ENTRIES)
    # user line, then the SAME managed block appended 3 times (the bug the CTO described).
    gi.write_text(f"node_modules/\n{block}\n{block}\n{block}\n*.tmp\n", encoding="utf-8")
    r = resolve_global_excludes(gi, DEFAULT_ENTRIES)
    assert r.state == "update"  # duplicates are a reconcile, not a conflict
    res = _apply(gi)
    assert res.status == "updated"
    text = gi.read_text()
    # collapsed to EXACTLY one begin/one end marker; user lines on both sides survive.
    assert text.count(GITIGNORE_BEGIN_MARKER) == 1
    assert text.count(GITIGNORE_END_MARKER) == 1
    assert "node_modules/" in text and "*.tmp" in text
    # and it is now strictly idempotent.
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "ok"
    assert _apply(gi).status == "skipped"


def test_dedup_preserves_user_line_between_managed_blocks(git_config, tmp_path):
    """A user-added ignore that landed BETWEEN two duplicated rig blocks must survive the collapse.

    The dedup splices out each marker-pair region individually (not first-begin..last-end), so any
    content between blocks — which is the user's, outside every marker pair — is preserved.
    """
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    block = _block(DEFAULT_ENTRIES)
    gi.write_text(f"head/\n{block}\nuser-between/\n{block}\ntail/\n", encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "update"
    _apply(gi)
    text = gi.read_text()
    assert text.count(GITIGNORE_BEGIN_MARKER) == 1  # collapsed to one block
    assert "head/" in text and "tail/" in text
    assert "user-between/" in text  # the line BETWEEN the two blocks is NOT deleted
    # and strictly idempotent after the collapse.
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "ok"
    before = gi.read_bytes()
    assert _apply(gi).status == "skipped"
    assert gi.read_bytes() == before


def test_nested_or_misordered_markers_is_conflict(git_config, tmp_path):
    """Two begin markers before any end (a nested/overlapping block) is ambiguous → conflict."""
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    original = f"{GITIGNORE_BEGIN_MARKER}\n{GITIGNORE_BEGIN_MARKER}\nx/\n{GITIGNORE_END_MARKER}\n"
    gi.write_text(original, encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "conflict"
    res = _apply(gi)
    assert res.status == "skipped"
    assert gi.read_text() == original  # untouched


def test_dedup_collapses_duplicated_drifted_blocks(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    # two DIFFERENT stale blocks stacked — collapse the whole region to the one desired block.
    a = global_excludes_block_text([".claude/old-a/"])
    b = global_excludes_block_text([".claude/old-b/"])
    gi.write_text(f"head/\n{a}\n{b}\ntail/\n", encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "update"
    _apply(gi)
    text = gi.read_text()
    assert text.count(GITIGNORE_BEGIN_MARKER) == 1
    assert ".claude/old-a/" not in text and ".claude/old-b/" not in text
    assert "**/.claude/worktrees/" in text
    assert "head/" in text and "tail/" in text


# ── custom entries (configurable) ──────────────────────────────────────────────────
def test_custom_entries_are_used(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    entries = ["**/.claude/worktrees/", ".cache/agent-tools/", "scratch/"]
    res = _apply(gi, entries=entries)
    assert res.status == "created"
    text = gi.read_text()
    for e in entries:
        assert e in text
    assert _block(entries) in text  # in the given order, inside the markers


# ── conflict: unbalanced markers left untouched ────────────────────────────────────
def test_unbalanced_markers_is_conflict_left_untouched(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    original = f"node_modules/\n{GITIGNORE_BEGIN_MARKER}\n**/.claude/worktrees/\n"
    gi.write_text(original, encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "conflict"
    res = _apply(gi)
    assert res.status == "skipped" and "unbalanced" in res.detail
    assert gi.read_text() == original  # untouched


def test_end_before_begin_is_conflict(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    original = f"{GITIGNORE_END_MARKER}\n**/.claude/worktrees/\n{GITIGNORE_BEGIN_MARKER}\n"
    gi.write_text(original, encoding="utf-8")
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "conflict"
    res = _apply(gi)
    assert res.status == "skipped"
    assert gi.read_text() == original  # untouched


# ── plan gating: default ON, explicit opt-out ─────────────────────────────────────
def _cfg(data: dict, repo: Path) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def test_plan_includes_global_excludes_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, tmp_path), cat, project_type="cli")
    actions = [a for a in plan.actions if a.kind == "provision_global_excludes"]
    assert len(actions) == 1
    assert actions[0].options["entries"] == DEFAULT_ENTRIES
    assert actions[0].options["xdg_default"] == "~/.config/git/ignore"


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"gitignore": {"enabled": False}}, tmp_path), cat, project_type="cli")
    assert not any(a.kind == "provision_global_excludes" for a in plan.actions)


def test_plan_honors_custom_entries(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    entries = ["**/.claude/worktrees/", "tmp/"]
    plan = build(_cfg({"gitignore": {"entries": entries}}, tmp_path), cat, project_type="cli")
    action = next(a for a in plan.actions if a.kind == "provision_global_excludes")
    assert action.options["entries"] == entries


def test_plan_empty_entries_falls_back_to_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"gitignore": {"entries": []}}, tmp_path), cat, project_type="cli")
    action = next(a for a in plan.actions if a.kind == "provision_global_excludes")
    assert action.options["entries"] == DEFAULT_ENTRIES


def test_plan_carries_excludesfile_override(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"gitignore": {"excludesfile": "~/.gitignore"}}, tmp_path), cat, project_type="cli")
    action = next(a for a in plan.actions if a.kind == "provision_global_excludes")
    assert action.options["excludesfile"] == "~/.gitignore"


# ── config validation (fail-closed) ───────────────────────────────────────────────
def test_validate_rejects_non_bool_enabled():
    with pytest.raises(ConfigError, match="gitignore.enabled must be a bool"):
        validate({"version": 1, "gitignore": {"enabled": "yes"}})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown gitignore key"):
        validate({"version": 1, "gitignore": {"markers": "..."}})


def test_validate_rejects_non_string_list_entries():
    with pytest.raises(ConfigError, match="gitignore.entries must be a list of strings"):
        validate({"version": 1, "gitignore": {"entries": ["**/.claude/worktrees/", 5]}})
    with pytest.raises(ConfigError, match="gitignore.entries must be a list of strings"):
        validate({"version": 1, "gitignore": {"entries": "**/.claude/worktrees/"}})


def test_validate_rejects_non_string_excludesfile():
    with pytest.raises(ConfigError, match="gitignore.excludesfile must be a string"):
        validate({"version": 1, "gitignore": {"excludesfile": 5}})


def test_validate_rejects_non_mapping_block():
    with pytest.raises(ConfigError, match="gitignore must be a mapping"):
        validate({"version": 1, "gitignore": ["x"]})


def test_validate_accepts_empty_and_valid_block():
    validate({"version": 1, "gitignore": {}})
    validate({"version": 1, "gitignore": {"enabled": True, "entries": ["**/.claude/worktrees/"]}})
    validate({"version": 1, "gitignore": {"excludesfile": "~/.gitignore"}})


def test_validate_rejects_entry_containing_marker():
    with pytest.raises(ConfigError, match="may not contain a rig-managed marker"):
        validate({"version": 1, "gitignore": {"entries": [GITIGNORE_BEGIN_MARKER]}})
    with pytest.raises(ConfigError, match="may not contain a rig-managed marker"):
        validate({"version": 1, "gitignore": {"entries": [f"x {GITIGNORE_END_MARKER}"]}})


# ── drift parity (apply and drift switch on the same state) ───────────────────────
def _plan_with_action(target: Path, entries=None) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(target, entries))
    return plan


def test_drift_missing_then_in_sync(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    plan = _plan_with_action(gi)
    before = detect(plan)
    assert any(i.category == "gitignore" and i.item == "block" and i.direction == "missing" for i in before.items)
    run_plan(plan)
    assert not any(i.category == "gitignore" for i in detect(plan).items)


def test_drift_flags_unset_excludesfile(git_config, monkeypatch, tmp_path):
    # core.excludesfile unset → drift surfaces "apply will set it" (a GLOBAL drift item).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    plan = _plan_with_action(home / ".config" / "git" / "ignore")
    report = detect(plan)
    assert any(
        i.category == "gitignore" and i.item == "core.excludesfile" and i.direction == "missing"
        for i in report.items
    )


def test_drift_flags_modified_block(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"{global_excludes_block_text(['.claude/old/'])}\n", encoding="utf-8")
    report = detect(_plan_with_action(gi))
    assert any(i.category == "gitignore" and i.item == "block" and i.direction == "modified" for i in report.items)


def test_drift_flags_duplicated_blocks_as_modified(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    block = _block(DEFAULT_ENTRIES)
    gi.write_text(f"{block}\n{block}\n", encoding="utf-8")  # duplicated, not yet collapsed
    report = detect(_plan_with_action(gi))
    assert any(i.category == "gitignore" and i.item == "block" and i.direction == "modified" for i in report.items)


def test_drift_flags_conflict(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"{GITIGNORE_BEGIN_MARKER}\n**/.claude/worktrees/\n", encoding="utf-8")
    report = detect(_plan_with_action(gi))
    gi_items = [i for i in report.items if i.category == "gitignore" and i.item == "block"]
    assert gi_items and "unbalanced" in gi_items[0].detail


def test_drift_clean_when_block_correct_among_other_lines(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"node_modules/\n{_block(DEFAULT_ENTRIES)}\n*.log\n", encoding="utf-8")
    report = detect(_plan_with_action(gi))
    assert not any(i.category == "gitignore" for i in report.items)


# ── verbatim preservation: CRLF, trailing blanks, no-final-newline ─────────────────
def test_update_preserves_crlf_and_trailing_blanks_outside_block(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    stale = global_excludes_block_text([".claude/old/"]).replace("\n", "\r\n")
    raw = f"node_modules/\r\n{stale}\r\nbuild/\r\n\r\n"
    gi.write_bytes(raw.encode("utf-8"))
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "update"
    _apply(gi)
    out = gi.read_bytes().decode("utf-8")
    assert "node_modules/\r\n" in out
    assert "build/\r\n\r\n" in out
    assert "**/.claude/worktrees/" in out and ".claude/old/" not in out


def test_create_appends_to_crlf_file_preserving_user_crlf(git_config, tmp_path):
    """Appending the block to a CRLF file with no managed block keeps the user's CRLF lines intact.

    rig's own block is canonically LF (documented), but the user's existing CRLF content before it
    must survive byte-for-byte — no stripped \\r, no clobbered line.
    """
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_bytes(b"node_modules/\r\n*.log\r\n")  # CRLF, no managed block
    assert resolve_global_excludes(gi, DEFAULT_ENTRIES).state == "create"
    _apply(gi)
    out = gi.read_bytes().decode("utf-8")
    assert "node_modules/\r\n*.log\r\n" in out  # user CRLF lines preserved verbatim
    assert _block(DEFAULT_ENTRIES) in out


def test_create_appends_without_clobbering_no_final_newline(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text("*.log", encoding="utf-8")  # no trailing newline
    res = _apply(gi)
    assert res.status == "created"
    text = gi.read_text()
    assert text.startswith("*.log\n\n")
    assert _block(DEFAULT_ENTRIES) in text


def test_empty_existing_file_creates_block_without_leading_blank(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text("", encoding="utf-8")
    res = _apply(gi)
    assert res.status == "created" and "added block to" in res.detail
    assert gi.read_text() == _block(DEFAULT_ENTRIES) + "\n"  # no spurious leading blank line


# ── io_error: unreadable path is an ERROR, not a silent skip ───────────────────────
def test_directory_at_path_is_io_error(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.mkdir()  # a directory sits where the excludes file should be
    r = resolve_global_excludes(gi, DEFAULT_ENTRIES)
    assert r.state == "io_error"
    res = _apply(gi)
    assert res.status == "error" and "cannot read" in res.detail
    assert gi.is_dir()  # untouched


def test_io_error_surfaces_in_drift_not_in_sync(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.mkdir()
    report = detect(_plan_with_action(gi))
    assert any(i.category == "gitignore" and i.item == "block" and i.direction == "modified" for i in report.items)


# ── empty entries: an empty block round-trips as ok ────────────────────────────────
def test_empty_entries_produce_empty_block_that_round_trips(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    res = _apply(gi, entries=[])
    assert res.status == "created"
    text = gi.read_text()
    # even with no entries the block still carries the fixed explanatory comment (it is part of the
    # canonical block), so the on-disk text is exactly the rendered empty block + a trailing newline.
    assert text == _block([]) + "\n"
    assert GITIGNORE_BEGIN_MARKER in text and GITIGNORE_END_MARKER in text
    assert resolve_global_excludes(gi, []).state == "ok"
    assert _apply(gi, entries=[]).status == "skipped"


# ── disabled-but-installed block surfaces as drift ─────────────────────────────────
def test_disabled_category_still_flags_leftover_block(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text(f"node_modules/\n{_block(DEFAULT_ENTRIES)}\n", encoding="utf-8")
    report = DriftReport()
    check_disabled_global_excludes(_action(gi), report)
    assert any(i.category == "gitignore" and i.direction == "extra" for i in report.items)


def test_disabled_category_no_block_is_clean(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)
    gi.write_text("node_modules/\n", encoding="utf-8")
    report = DriftReport()
    check_disabled_global_excludes(_action(gi), report)
    assert not any(i.category == "gitignore" for i in report.items)


def test_disabled_category_no_file_is_clean(git_config, tmp_path):
    gi = tmp_path / "ignore"
    git_config["core.excludesfile"] = str(gi)  # file does not exist
    report = DriftReport()
    check_disabled_global_excludes(_action(gi), report)
    assert not report.items


# ── full loop: build → apply → detect in sync, second apply a no-op ────────────────
def test_end_to_end_build_apply_detect_in_sync(git_config, fake_agent_tools, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, repo), cat, project_type="cli")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    written = home / ".config" / "git" / "ignore"
    assert written.is_file()
    assert "**/.claude/worktrees/" in written.read_text()
    assert git_config["core.excludesfile"] == "~/.config/git/ignore"
    # second apply is a no-op for the global-excludes action
    second = run_plan(plan)
    assert all(r.status == "skipped" for r in second.results if r.action.category == "gitignore")
    assert not any(i.category == "gitignore" for i in detect(plan).items)
