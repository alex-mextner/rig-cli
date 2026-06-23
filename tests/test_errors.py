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

from pathlib import Path

import pytest

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


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_rig_yaml_declares_no_removed_slot():
    """The repo's OWN shipped rig.yaml must not declare a slot rig itself classifies as removed.

    Regression for the dead ``mcp.items.review`` (``review --mcp`` was dropped in agent-tools
    #32; the real catalog has no ``mcp/review`` item). A removed slot left in the SHIPPED
    template makes ``rig status --config rig.yaml`` against the repo's own config exit 4 — and
    the smoke clean-sample never exercised the real template, so it slipped through.

    This is the fast, hermetic guard: it reads the actual committed file and the in-code
    removed-slot registry — no catalog needed, so it runs in every CI leg. The end-to-end leg
    below drives the REAL ``plan.build`` validation against the REAL catalog.
    """
    # Positive control: the detector MUST see the motivating slot, else a green result here would
    # be meaningless (e.g. the registry got cleared) rather than proof the template is clean.
    assert errors.removed_slot("mcp", "review") is not None

    import yaml  # local import: stdlib-only at module top is the rig test convention

    rig_yaml = _REPO_ROOT / "rig.yaml"
    data = yaml.safe_load(rig_yaml.read_text(encoding="utf-8")) or {}

    offenders: list[str] = []
    for category, block in data.items():
        if not isinstance(block, dict):
            continue
        items = block.get("items")
        if not isinstance(items, dict):
            continue
        for name in items:
            if errors.removed_slot(category, name) is not None:
                offenders.append(f"{category}.items.{name}")
    assert not offenders, (
        f"rig.yaml ships removed slot(s) {offenders} — remove them from the template "
        f"(see riglib.errors._REMOVED_SLOTS for why each was dropped)"
    )


def test_repo_rig_yaml_plans_clean_against_the_real_catalog():
    """End-to-end: the repo's real rig.yaml plans against the REAL catalog with no item error.

    The fast guard above checks the proxy registry; THIS exercises the same validation path the
    ``rig status`` CLI runs — ``plan.build`` resolving every declared item against the live
    agent-tools catalog (which, unlike the test's fake catalog, has NO ``mcp/review`` item). A
    dead ``mcp.items.review`` in the template raised an unknown/removed-item error here (exit 4);
    a clean template plans without raising. Skipped when no agent-tools checkout is reachable
    (the catalog-less CI leg), exactly like the other real-catalog tests.
    """
    from riglib import config
    from riglib.catalog import Catalog, CatalogError
    from riglib.plan import build

    try:
        source = Catalog.scan(None)  # resolves the real agent-tools checkout; raises if absent
    except CatalogError as exc:
        pytest.skip(f"no real agent-tools catalog reachable: {exc}")

    cfg = config.load(_REPO_ROOT, explicit_config=_REPO_ROOT / "rig.yaml", include_global=False)
    # build() raises UnknownItemError (which the removed-slot path produces for `mcp.items.review`)
    # if any declared item is absent from the real catalog. No raise == the template is clean.
    build(cfg, source, project_type="cli")


def test_real_catalog_provisions_require_ticket_guard():
    """The strict ticket guard MUST be provisioned by rig into every repo (agent-tools #92).

    The CTO mandate: task-cli is strictly advertised AND ENFORCED everywhere. Enforcement
    rides on the ``require-ticket-before-commit`` agent-hook, which rig installs from the
    agent-tools catalog. This asserts BOTH halves end-to-end against the REAL catalog:

    1. the hook EXISTS in the catalog scan (so a rename/move that drops it is caught), and
    2. a plan built from rig's DEFAULT agent-hooks resolution emits an ``install_agent_hook``
       action for it — i.e. ``rig apply`` would actually write its descriptor into
       ``~/.claude/hooks``.

    Half 2 deliberately passes a config with NO ``agent_hooks`` block at all, so it exercises
    rig's own default: ``plan.build`` treats an absent/enabled agent_hooks block as on and
    does ``setdefault("all", True)`` for it (see ``riglib/plan.py``), so every catalog hook —
    require-ticket included — is planned. That is the regression that matters: if someone flips
    the default to opt-in (or makes the absent block resolve to off, or drops the guard from
    the default set), a hand-forced ``all: true`` config would mask it but this default-config
    build catches it. A regression
    that silently un-provisions the guard (the enforcement vanishing in every repo) fails HERE,
    not only when a human notices ticketless commits sailing through. Skipped when no
    agent-tools checkout is reachable (the catalog-less CI leg).
    """
    from riglib.catalog import Catalog, CatalogError
    from riglib.config import LoadedConfig
    from riglib.plan import build

    hook_name = "require-ticket-before-commit"
    try:
        source = Catalog.scan(None)  # real agent-tools checkout; raises if absent
    except CatalogError as exc:
        pytest.skip(f"no real agent-tools catalog reachable: {exc}")

    # Positive control: the scan must see a non-empty agent-hook catalog. An empty scan (a
    # broken discovery walk) would make the membership assertion below fail for the wrong
    # reason — "guard removed" — instead of surfacing that discovery itself is broken.
    hook_names = source.names("agent_hooks")
    assert hook_names, (
        "agent-tools catalog scan found NO agent-hooks — discovery is broken; the "
        "require-ticket assertion below would be meaningless"
    )

    # 1. the guard is in the catalog
    assert hook_name in hook_names, (
        f"{hook_name} missing from the real agent-tools catalog — the strict ticket "
        f"enforcement would not be provisioned by rig in any repo"
    )

    # 2. rig's DEFAULT plan (no agent_hooks overrides → rig's own all-on default) installs it.
    # Building from the default — not a hand-forced ``all: true`` — is what guards against the
    # default itself regressing to opt-in or dropping the guard.
    cfg = LoadedConfig(
        data={"skills": {"enabled": False}},
        repo_root=_REPO_ROOT,
    )
    plan = build(cfg, source, project_type="cli")
    hook_actions = {
        a.item
        for a in plan.actions
        if a.category == "agent_hooks" and a.kind == "install_agent_hook"
    }
    assert hook_name in hook_actions, (
        f"rig's default plan does not install {hook_name} (got {sorted(hook_actions)}) — the "
        f"strict ticket guard would not be wired by `rig apply`"
    )


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
