"""rig-managed ``.gitignore`` block provisioning — the ``gitignore`` block.

Covers every resolved ``state`` (create / update / ok / conflict / io_error), the default-ON +
opt-out plan gating, config validation, and drift parity. The guiding invariant: apply and drift
switch on the SAME ``resolve_gitignore`` state, so they can never disagree — and rig only ever
edits its OWN marker-fenced lines, preserving every other line in the file verbatim (CRLF and
trailing blanks included).

The motivation (CTO ask): Claude Code creates throwaway worktrees under each repo's
``.claude/worktrees/`` — those must be gitignored DECLARATIVELY by rig (reconciled like every
other category), not by a hand-edited global ignore. ``.serena/`` is deliberately NOT ignored
(Serena state is committed shared project memory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.actions.runner import (
    _do_provision_gitignore,
    gitignore_block_text,
    resolve_gitignore,
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
from riglib.drift import DriftReport, check_disabled_gitignore, detect
from riglib.plan import Action, InstallPlan, build

DEFAULT_ENTRIES = list(GITIGNORE_DEFAULT_ENTRIES)


def _action(repo: Path, entries: list[str] | None = None) -> Action:
    return Action(
        kind="provision_gitignore",
        category="gitignore",
        item="block",
        source=repo,
        target=repo / ".gitignore",
        options={"entries": entries if entries is not None else DEFAULT_ENTRIES},
    )


def _apply(repo: Path, entries: list[str] | None = None, on_conflict: str = "backup"):
    return _do_provision_gitignore(_action(repo, entries), on_conflict)


def _plan_with_action(repo: Path, entries: list[str] | None = None) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo, entries))
    return plan


def _block(entries: list[str]) -> str:
    return gitignore_block_text(entries)


# ── the default entry is the harness worktrees dir, never .serena/ ─────────────────
def test_default_entries_ignore_worktrees_not_serena():
    assert ".claude/worktrees/" in DEFAULT_ENTRIES
    assert ".serena/" not in DEFAULT_ENTRIES  # Serena state is committed, never ignored
    block = gitignore_block_text(DEFAULT_ENTRIES)
    assert block.startswith(GITIGNORE_BEGIN_MARKER)
    assert block.endswith(GITIGNORE_END_MARKER)
    assert ".claude/worktrees/" in block


# ── create: fresh file ─────────────────────────────────────────────────────────────
def test_fresh_create_when_no_gitignore(tmp_path):
    gi = tmp_path / ".gitignore"
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "create"
    res = _apply(tmp_path)
    assert res.status == "created"
    assert gi.is_file()
    text = gi.read_text()
    assert _block(DEFAULT_ENTRIES) in text
    assert text.endswith("\n")  # trailing newline


def test_create_appends_to_existing_file_preserving_lines(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n*.log\n", encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "create"
    res = _apply(tmp_path)
    assert res.status == "created"
    text = gi.read_text()
    # the user's prior lines are preserved verbatim, the block is appended after a blank line
    assert "node_modules/\n*.log\n" in text
    assert _block(DEFAULT_ENTRIES) in text
    assert text.index("node_modules/") < text.index(GITIGNORE_BEGIN_MARKER)
    assert "*.log\n\n# >>> rig-managed" in text  # single blank-line separator


# ── ok: idempotent re-apply ────────────────────────────────────────────────────────
def test_idempotent_second_apply_skips(tmp_path):
    assert _apply(tmp_path).status == "created"
    before = (tmp_path / ".gitignore").read_text()
    second = _apply(tmp_path)
    assert resolve_gitignore(tmp_path / ".gitignore", DEFAULT_ENTRIES).state == "ok"
    assert second.status == "skipped" and "already correct" in second.detail
    assert (tmp_path / ".gitignore").read_text() == before  # byte-identical, no churn


def test_idempotent_when_block_already_present_among_other_lines(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(f"node_modules/\n\n{_block(DEFAULT_ENTRIES)}\n\n*.tmp\n", encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "ok"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    # user lines on BOTH sides of the block are preserved untouched
    text = gi.read_text()
    assert "node_modules/" in text and "*.tmp" in text


# ── update: block differs, just the block is replaced ──────────────────────────────
def test_update_replaces_just_the_block_preserving_other_lines(tmp_path):
    gi = tmp_path / ".gitignore"
    # a stale managed block (different entries) sandwiched between user lines.
    stale = gitignore_block_text([".claude/old-cruft/"])
    gi.write_text(f"# top user line\nbuild/\n\n{stale}\n\n# bottom user line\ndist/\n", encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "update"
    res = _apply(tmp_path)
    assert res.status == "updated"
    text = gi.read_text()
    # the new block is present, the stale entry is gone
    assert ".claude/worktrees/" in text
    assert ".claude/old-cruft/" not in text
    # every user line OUTSIDE the markers is preserved verbatim, on both sides
    assert "# top user line" in text and "build/" in text
    assert "# bottom user line" in text and "dist/" in text
    # the block stayed in place (between the user lines), not moved to the end
    assert text.index("build/") < text.index(GITIGNORE_BEGIN_MARKER) < text.index("# bottom user line")


def test_update_then_idempotent(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(f"x/\n{gitignore_block_text(['.claude/old/'])}\ny/\n", encoding="utf-8")
    assert _apply(tmp_path).status == "updated"
    assert _apply(tmp_path).status == "skipped"  # converged → idempotent


# ── custom entries (configurable) ──────────────────────────────────────────────────
def test_custom_entries_are_used(tmp_path):
    entries = [".claude/worktrees/", ".cache/agent-tools/", "scratch/"]
    res = _apply(tmp_path, entries=entries)
    assert res.status == "created"
    text = (tmp_path / ".gitignore").read_text()
    for e in entries:
        assert e in text
    # entries appear in the given order, inside the markers
    block = _block(entries)
    assert block in text


# ── conflict: unbalanced markers left untouched ────────────────────────────────────
def test_unbalanced_markers_is_conflict_left_untouched(tmp_path):
    gi = tmp_path / ".gitignore"
    # a begin marker with no matching end — rig won't guess where the block ends.
    original = f"node_modules/\n{GITIGNORE_BEGIN_MARKER}\n.claude/worktrees/\n"
    gi.write_text(original, encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped" and "unbalanced" in res.detail
    assert gi.read_text() == original  # untouched


def test_duplicate_begin_markers_is_conflict(tmp_path):
    gi = tmp_path / ".gitignore"
    block = _block(DEFAULT_ENTRIES)
    gi.write_text(f"{block}\n{block}\n", encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "conflict"
    assert _apply(tmp_path).status == "skipped"


def test_end_before_begin_is_conflict(tmp_path):
    gi = tmp_path / ".gitignore"
    original = f"{GITIGNORE_END_MARKER}\n.claude/worktrees/\n{GITIGNORE_BEGIN_MARKER}\n"
    gi.write_text(original, encoding="utf-8")
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert gi.read_text() == original  # untouched


# ── plan gating: default ON, explicit opt-out ─────────────────────────────────────
def _cfg(data: dict, repo: Path) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def test_plan_includes_gitignore_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, tmp_path), cat, project_type="cli")
    actions = [a for a in plan.actions if a.kind == "provision_gitignore"]
    assert len(actions) == 1
    # default entries land in the action options, anchored at the repo's .gitignore
    assert actions[0].options["entries"] == DEFAULT_ENTRIES
    assert actions[0].target == tmp_path / ".gitignore"


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"gitignore": {"enabled": False}}, tmp_path), cat, project_type="cli")
    assert not any(a.kind == "provision_gitignore" for a in plan.actions)


def test_plan_honors_custom_entries(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    entries = [".claude/worktrees/", "tmp/"]
    plan = build(_cfg({"gitignore": {"entries": entries}}, tmp_path), cat, project_type="cli")
    action = next(a for a in plan.actions if a.kind == "provision_gitignore")
    assert action.options["entries"] == entries


def test_plan_empty_entries_falls_back_to_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"gitignore": {"entries": []}}, tmp_path), cat, project_type="cli")
    action = next(a for a in plan.actions if a.kind == "provision_gitignore")
    assert action.options["entries"] == DEFAULT_ENTRIES


# ── config validation (fail-closed) ───────────────────────────────────────────────
def test_validate_rejects_non_bool_enabled():
    with pytest.raises(ConfigError, match="gitignore.enabled must be a bool"):
        validate({"version": 1, "gitignore": {"enabled": "yes"}})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown gitignore key"):
        validate({"version": 1, "gitignore": {"markers": "..."}})


def test_validate_rejects_non_string_list_entries():
    with pytest.raises(ConfigError, match="gitignore.entries must be a list of strings"):
        validate({"version": 1, "gitignore": {"entries": [".claude/worktrees/", 5]}})
    with pytest.raises(ConfigError, match="gitignore.entries must be a list of strings"):
        validate({"version": 1, "gitignore": {"entries": ".claude/worktrees/"}})


def test_validate_rejects_non_mapping_block():
    with pytest.raises(ConfigError, match="gitignore must be a mapping"):
        validate({"version": 1, "gitignore": ["x"]})


def test_validate_accepts_empty_and_valid_block():
    validate({"version": 1, "gitignore": {}})
    validate({"version": 1, "gitignore": {"enabled": True, "entries": [".claude/worktrees/"]}})


# ── drift parity (apply and drift switch on the same state) ───────────────────────
def test_drift_missing_then_in_sync(tmp_path):
    plan = _plan_with_action(tmp_path)
    before = detect(plan)
    assert any(i.category == "gitignore" and i.direction == "missing" for i in before.items)
    run_plan(plan)
    assert not any(i.category == "gitignore" for i in detect(plan).items)


def test_drift_flags_modified_block(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(f"{gitignore_block_text(['.claude/old/'])}\n", encoding="utf-8")
    report = detect(_plan_with_action(tmp_path))
    assert any(i.category == "gitignore" and i.direction == "modified" for i in report.items)


def test_drift_flags_conflict(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(f"{GITIGNORE_BEGIN_MARKER}\n.claude/worktrees/\n", encoding="utf-8")
    report = detect(_plan_with_action(tmp_path))
    # assert the user-facing meaning (a surfaced unbalanced-marker conflict), not the incidental
    # direction it maps to — so the test survives a future direction rename.
    gi_items = [i for i in report.items if i.category == "gitignore"]
    assert gi_items and "unbalanced" in gi_items[0].detail


def test_drift_clean_when_block_correct_among_other_lines(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(f"node_modules/\n{_block(DEFAULT_ENTRIES)}\n*.log\n", encoding="utf-8")
    report = detect(_plan_with_action(tmp_path))
    assert not any(i.category == "gitignore" for i in report.items)


# ── verbatim preservation: CRLF, trailing blanks, no-final-newline (review P3) ─────
def test_update_preserves_crlf_and_trailing_blanks_outside_block(tmp_path):
    gi = tmp_path / ".gitignore"
    # CRLF line endings + a trailing blank line outside the (stale) managed block.
    stale = gitignore_block_text([".claude/old/"]).replace("\n", "\r\n")
    raw = f"node_modules/\r\n{stale}\r\nbuild/\r\n\r\n"
    gi.write_bytes(raw.encode("utf-8"))
    assert resolve_gitignore(gi, DEFAULT_ENTRIES).state == "update"
    _apply(tmp_path)
    out = gi.read_bytes().decode("utf-8")
    # the user's CRLF lines and the trailing blank survive byte-for-byte; only the block changed.
    assert "node_modules/\r\n" in out
    assert "build/\r\n\r\n" in out
    assert ".claude/worktrees/" in out and ".claude/old/" not in out


def test_create_appends_without_clobbering_no_final_newline(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("*.log", encoding="utf-8")  # no trailing newline
    res = _apply(tmp_path)
    assert res.status == "created"
    text = gi.read_text()
    assert text.startswith("*.log\n\n")  # the user's line kept, separated by one blank line
    assert _block(DEFAULT_ENTRIES) in text


def test_empty_existing_file_creates_block_without_leading_blank(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("", encoding="utf-8")  # exists but empty (0 bytes)
    res = _apply(tmp_path)
    assert res.status == "created" and "added block to" in res.detail
    assert gi.read_text() == _block(DEFAULT_ENTRIES) + "\n"  # no spurious leading blank line


# ── io_error: unreadable path is an ERROR, not a silent skip (review P2) ───────────
def test_directory_at_gitignore_path_is_io_error(tmp_path):
    # a directory sits where .gitignore should be — read_text raises, so it's io_error → error.
    (tmp_path / ".gitignore").mkdir()
    r = resolve_gitignore(tmp_path / ".gitignore", DEFAULT_ENTRIES)
    assert r.state == "io_error"
    res = _apply(tmp_path)
    assert res.status == "error" and "cannot read" in res.detail
    assert (tmp_path / ".gitignore").is_dir()  # untouched


def test_io_error_surfaces_in_drift_not_in_sync(tmp_path):
    (tmp_path / ".gitignore").mkdir()
    report = detect(_plan_with_action(tmp_path))
    assert any(i.category == "gitignore" and i.direction == "modified" for i in report.items)


# ── empty entries: an empty block round-trips as ok (review #12) ───────────────────
def test_empty_entries_produce_empty_block_that_round_trips(tmp_path):
    res = _apply(tmp_path, entries=[])
    assert res.status == "created"
    text = (tmp_path / ".gitignore").read_text()
    assert text == f"{GITIGNORE_BEGIN_MARKER}\n{GITIGNORE_END_MARKER}\n"
    # idempotent: an empty managed block is recognized as ok, not endlessly re-created.
    assert resolve_gitignore(tmp_path / ".gitignore", []).state == "ok"
    assert _apply(tmp_path, entries=[]).status == "skipped"


# ── config: an entry colliding with a marker is rejected (review #11) ──────────────
def test_validate_rejects_entry_containing_marker():
    with pytest.raises(ConfigError, match="may not contain a rig-managed marker"):
        validate({"version": 1, "gitignore": {"entries": [GITIGNORE_BEGIN_MARKER]}})
    with pytest.raises(ConfigError, match="may not contain a rig-managed marker"):
        validate({"version": 1, "gitignore": {"entries": [f"x {GITIGNORE_END_MARKER}"]}})


# ── disabled-but-installed block surfaces as drift (review P1) ─────────────────────
def test_disabled_category_still_flags_leftover_block(tmp_path):
    # config opts out, but a prior apply left the managed block — status must NOT read "in sync".
    (tmp_path / ".gitignore").write_text(f"node_modules/\n{_block(DEFAULT_ENTRIES)}\n", encoding="utf-8")
    report = DriftReport()
    check_disabled_gitignore(tmp_path, report)
    assert any(i.category == "gitignore" and i.direction == "extra" for i in report.items)


def test_disabled_category_no_block_is_clean(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    report = DriftReport()
    check_disabled_gitignore(tmp_path, report)
    assert not any(i.category == "gitignore" for i in report.items)


def test_disabled_category_no_file_is_clean(tmp_path):
    report = DriftReport()
    check_disabled_gitignore(tmp_path, report)  # no .gitignore at all
    assert not report.items


# ── update through the plan→apply path leaves drift clean (full loop) ──────────────
def test_end_to_end_build_apply_detect_in_sync(fake_agent_tools, tmp_path):
    # the real path: build a plan from config (gitignore default ON) → run_plan → detect.
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("# pre-existing user ignore\n.env\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, repo), cat, project_type="cli")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    text = (repo / ".gitignore").read_text()
    assert ".env" in text  # the user's prior content survives
    assert ".claude/worktrees/" in text
    # second apply is a no-op for the gitignore action
    second = run_plan(plan)
    assert all(r.status == "skipped" for r in second.results if r.action.category == "gitignore")
    assert not any(i.category == "gitignore" for i in detect(plan).items)
