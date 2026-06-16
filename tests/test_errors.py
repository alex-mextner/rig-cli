"""Unit tests for the error system v2 — structured what/why/fix errors + heuristics.

Covers:
- the RigError dataclass-style exception (what/why/fix/exit_code) + per-class stable codes,
- the consistent CLI rendering (every error prints WHAT, WHY, HOW-to-fix),
- did-you-mean (Levenshtein-nearest catalog name),
- the empty-category message ("no slots; remove the block"),
- the removed/deprecated-slot registry (mcp.items.review → names agent-tools #32 + the fix),
- missing-target (a config path/binary that's gone → names the file + how to regenerate).

All offline; no disk, no network.
"""

from __future__ import annotations


from riglib import errors


# ── exit-code table: stable + distinct per failure class ──────────────────────────
def test_exit_codes_are_distinct_and_stable():
    codes = {
        errors.EXIT_CONFIG,
        errors.EXIT_DRIFT,
        errors.EXIT_UNKNOWN_ITEM,
        errors.EXIT_MISSING_TARGET,
        errors.EXIT_NOT_A_REPO,
        errors.EXIT_MISSING_DEP,
    }
    # every class has its OWN code (no collisions)
    assert len(codes) == 6
    # pinned values — scripts depend on these; a change here is a breaking change
    assert errors.EXIT_CONFIG == 2
    assert errors.EXIT_DRIFT == 3
    assert errors.EXIT_UNKNOWN_ITEM == 4
    assert errors.EXIT_MISSING_TARGET == 5
    assert errors.EXIT_NOT_A_REPO == 6
    assert errors.EXIT_MISSING_DEP == 127


def test_rig_error_carries_what_why_fix_and_code():
    e = errors.RigError(
        what="something broke",
        why="because of a reason",
        fix="run `rig fix`",
        exit_code=errors.EXIT_CONFIG,
    )
    assert e.what == "something broke"
    assert e.why == "because of a reason"
    assert e.fix == "run `rig fix`"
    assert e.exit_code == errors.EXIT_CONFIG
    # str() includes the WHAT so a bare log line is still useful
    assert "something broke" in str(e)


def test_subclasses_pin_their_exit_code():
    assert errors.ConfigError(what="x", why="y", fix="z").exit_code == errors.EXIT_CONFIG
    assert errors.UnknownItemError(what="x", why="y", fix="z").exit_code == errors.EXIT_UNKNOWN_ITEM
    assert errors.MissingTargetError(what="x", why="y", fix="z").exit_code == errors.EXIT_MISSING_TARGET
    assert errors.NotARepoError(what="x", why="y", fix="z").exit_code == errors.EXIT_NOT_A_REPO
    assert errors.MissingDepError(what="x", why="y", fix="z").exit_code == errors.EXIT_MISSING_DEP


# ── consistent rendering: every error shows the 3 parts ───────────────────────────
def test_render_shows_three_parts():
    e = errors.UnknownItemError(
        what="unknown mcp item: reviewr",
        why="not in the mcp catalog (declared in /tmp/rig.yaml)",
        fix="did you mean 'review'? edit mcp.items in /tmp/rig.yaml",
    )
    text = errors.render(e, color=False)
    # the three load-bearing labels are present and ordered
    assert "what:" in text.lower() or "error:" in text.lower()
    assert "why" in text.lower()
    assert "fix" in text.lower()
    # the actual content survives rendering
    assert "reviewr" in text
    assert "review" in text
    assert "/tmp/rig.yaml" in text


# ── did-you-mean (Levenshtein) ────────────────────────────────────────────────────
def test_did_you_mean_picks_nearest():
    assert errors.did_you_mean("reviewr", {"review", "naming", "shell-timeouts"}) == "review"
    assert errors.did_you_mean("dispatcherr", {"dispatcher", "templates"}) == "dispatcher"


def test_did_you_mean_returns_none_when_too_far():
    # a wildly different token shouldn't fabricate a bogus suggestion
    assert errors.did_you_mean("zzzzzzzz", {"review", "naming"}) is None


def test_did_you_mean_returns_none_on_empty_catalog():
    assert errors.did_you_mean("anything", set()) is None


# ── unknown-item error builder: did-you-mean OR empty-category message ─────────────
def test_unknown_item_suggests_nearest():
    e = errors.unknown_item_error(
        category="mcp",
        key="mcp.items.reviewr",
        bad="reviewr",
        known={"review", "context7"},
        config_path="/tmp/rig.yaml",
    )
    assert isinstance(e, errors.UnknownItemError)
    assert e.exit_code == errors.EXIT_UNKNOWN_ITEM
    assert "review" in e.fix  # the suggestion
    assert "/tmp/rig.yaml" in e.fix  # the offending config file path
    assert "reviewr" in e.what


def test_unknown_item_empty_category_says_remove_the_block():
    # the category has NO slots → "remove the mcp block", NOT "known: none".
    # use a NON-removed name so this exercises the empty-category branch (not removed-slot).
    e = errors.unknown_item_error(
        category="mcp",
        key="mcp.items.context7",
        bad="context7",
        known=set(),
        config_path="/tmp/rig.yaml",
    )
    assert "no slots" in e.why.lower() or "no slots" in e.fix.lower()
    assert "remove" in e.fix.lower()
    assert "mcp" in e.fix.lower()
    # explicitly NOT the old useless phrasing
    assert "known: none" not in (e.what + e.why + e.fix).lower()


# ── removed/deprecated slots registry ─────────────────────────────────────────────
def test_removed_slot_registry_has_mcp_review():
    rec = errors.removed_slot("mcp", "review")
    assert rec is not None
    # names the PR/source and says it's a CLI+skill, not an MCP
    assert "#32" in rec.reason
    assert "cli" in rec.reason.lower()


def test_removed_slot_error_names_it_and_the_fix():
    e = errors.unknown_item_error(
        category="mcp",
        key="mcp.items.review",
        bad="review",
        known={"context7"},  # 'review' is NOT in the catalog (removed)
        config_path="/home/u/.config/rig/config.yaml",
    )
    blob = (e.what + " " + e.why + " " + e.fix).lower()
    # removed-slot path wins over did-you-mean: it must cite the removal + the exact fix
    assert "#32" in (e.what + e.why + e.fix)
    assert "remove" in blob
    assert "mcp.items.review" in (e.what + e.why + e.fix)
    assert "/home/u/.config/rig/config.yaml" in (e.what + e.why + e.fix)


def test_removed_slot_unknown_returns_none():
    assert errors.removed_slot("mcp", "context7") is None
    assert errors.removed_slot("skills", "naming") is None


# ── missing-target ────────────────────────────────────────────────────────────────
def test_missing_target_error_names_file_and_regen():
    e = errors.missing_target_error(
        what_kind="hook",
        target="/home/u/.claude/hooks/block-no-verify.json",
        why="settings.json references a hook descriptor that no longer exists on disk",
        regen="run `rig apply` to reinstall the agent-hooks",
    )
    assert isinstance(e, errors.MissingTargetError)
    assert e.exit_code == errors.EXIT_MISSING_TARGET
    assert "/home/u/.claude/hooks/block-no-verify.json" in e.what
    assert "rig apply" in e.fix


def test_not_a_repo_error_code():
    e = errors.NotARepoError(what="not a git repo", why="no .git", fix="cd into a repo")
    assert e.exit_code == errors.EXIT_NOT_A_REPO


# ── the CLI guard turns a RigError into render+exit-code ───────────────────────────
def test_guard_renders_and_returns_exit_code(capsys):
    def boom():
        raise errors.UnknownItemError(what="bad item", why="reason", fix="the fix")

    rc = errors.guard(boom)
    out = capsys.readouterr().out
    assert rc == errors.EXIT_UNKNOWN_ITEM
    assert "bad item" in out
    assert "the fix" in out


def test_guard_passes_through_success():
    assert errors.guard(lambda: 0) == 0
    assert errors.guard(lambda: 3) == 3
