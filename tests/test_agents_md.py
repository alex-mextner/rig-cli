"""AGENTS.md (canonical) + CLAUDE.md (symlink) provisioning — the ``agents_md`` block.

Covers every resolved ``state`` (create_both / create_link / ok / converge / conflict), the
default-ON + opt-out plan gating, config validation, and drift parity. The guiding invariant:
apply and drift switch on the SAME ``resolve_agents_md`` state, so they can never disagree —
and rig NEVER clobbers a real file, a foreign symlink, or a directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.actions.runner import (
    _do_provision_agents_symlink,
    _is_broken_symlink,
    resolve_agents_md,
    run_plan,
)
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build


def _action(repo: Path) -> Action:
    return Action(
        kind="provision_agents_symlink",
        category="agents_md",
        item="symlink",
        source=repo,
        target=repo,
        options={},
    )


def _apply(repo: Path, on_conflict: str = "backup"):
    return _do_provision_agents_symlink(_action(repo), on_conflict)


def _plan_with_action(repo: Path) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    return plan


# ── create states ─────────────────────────────────────────────────────────────────
def test_both_absent_creates_canonical_and_symlink(tmp_path):
    assert resolve_agents_md(tmp_path).state == "create_both"
    res = _apply(tmp_path)
    agents, claude = tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"
    assert res.status == "created"
    assert agents.is_file() and not agents.is_symlink()  # real canonical
    assert claude.is_symlink() and str(claude.readlink()) == "AGENTS.md"  # relative target
    assert claude.read_text() == agents.read_text()  # link resolves to canonical content


def test_only_agents_real_creates_claude_symlink(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# real agents guide\n", encoding="utf-8")
    assert resolve_agents_md(tmp_path).state == "create_link"
    res = _apply(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    assert res.status == "created"
    assert claude.is_symlink() and str(claude.readlink()) == "AGENTS.md"
    assert claude.read_text() == "# real agents guide\n"


def test_only_claude_real_makes_agents_the_symlink(tmp_path):
    # an existing real CLAUDE.md is the source of truth — never demoted to a link.
    (tmp_path / "CLAUDE.md").write_text("# real claude guide\n", encoding="utf-8")
    res = _apply(tmp_path)
    agents, claude = tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"
    assert res.status == "created"
    assert not claude.is_symlink() and claude.is_file()  # canonical stays real
    assert agents.is_symlink() and str(agents.readlink()) == "CLAUDE.md"


# ── ok (idempotency) ──────────────────────────────────────────────────────────────
def test_idempotent_second_apply_skips(tmp_path):
    assert _apply(tmp_path).status == "created"
    second = _apply(tmp_path)
    assert resolve_agents_md(tmp_path).state == "ok"
    assert second.status == "skipped" and "already links" in second.detail


def test_absolute_symlink_target_is_recognized_as_correct(tmp_path):
    # a CLAUDE.md symlink stored as an ABSOLUTE path to AGENTS.md still counts as correct.
    (tmp_path / "AGENTS.md").write_text("# canonical\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")  # absolute target
    assert resolve_agents_md(tmp_path).state == "ok"
    assert _apply(tmp_path).status == "skipped"


# ── converge (both real & identical) ──────────────────────────────────────────────
def test_both_real_identical_converges_with_backup(tmp_path):
    (tmp_path / "AGENTS.md").write_text("same content\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("same content\n", encoding="utf-8")
    assert resolve_agents_md(tmp_path).state == "converge"
    res = _apply(tmp_path, on_conflict="backup")
    claude = tmp_path / "CLAUDE.md"
    assert res.status == "backed_up"  # status contract: a backup was kept
    assert claude.is_symlink() and str(claude.readlink()) == "AGENTS.md"
    assert res.backup is not None and res.backup.is_file()
    assert any(p.name.startswith("CLAUDE.md.rig-bak-") for p in tmp_path.iterdir())


def test_both_real_identical_skip_policy_leaves_two_real_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("same\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("same\n", encoding="utf-8")
    res = _apply(tmp_path, on_conflict="skip")
    assert res.status == "skipped"
    assert not (tmp_path / "CLAUDE.md").is_symlink()  # on_conflict=skip → don't replace


def test_both_real_identical_overwrite_converges_without_backup(tmp_path):
    (tmp_path / "AGENTS.md").write_text("same\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("same\n", encoding="utf-8")
    res = _apply(tmp_path, on_conflict="overwrite")
    assert res.status == "updated"
    assert res.backup is None  # overwrite → no backup kept
    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert not any(p.name.startswith("CLAUDE.md.rig-bak-") for p in tmp_path.iterdir())


# ── conflict (never mutated) ──────────────────────────────────────────────────────
def test_both_real_different_is_a_conflict_left_untouched(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents content\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("DIFFERENT claude content\n", encoding="utf-8")
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped" and "different content" in res.detail
    assert not (tmp_path / "AGENTS.md").is_symlink()
    assert not (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "AGENTS.md").read_text() == "agents content\n"
    assert (tmp_path / "CLAUDE.md").read_text() == "DIFFERENT claude content\n"


def test_foreign_link_side_symlink_is_not_clobbered(tmp_path):
    # AGENTS.md real, CLAUDE.md a symlink to a THIRD file — rig must not re-point it.
    (tmp_path / "AGENTS.md").write_text("# canonical\n", encoding="utf-8")
    (tmp_path / "elsewhere.md").write_text("# user's own target\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("elsewhere.md")
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert str((tmp_path / "CLAUDE.md").readlink()) == "elsewhere.md"  # untouched


def test_canonical_side_symlink_is_a_conflict_left_intact(tmp_path):
    # AGENTS.md symlinked to an external file, CLAUDE.md absent: rig leaves the symlink alone
    # and does NOT synthesize a placeholder/loop — it's a conflict for the human to resolve.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# real guide elsewhere\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("docs/guide.md")
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert str((tmp_path / "AGENTS.md").readlink()) == "docs/guide.md"  # untouched
    assert not (tmp_path / "CLAUDE.md").exists()  # no synthesized link


def test_peer_symlink_loop_is_a_conflict_not_created(tmp_path):
    # AGENTS.md -> CLAUDE.md with no real CLAUDE.md: creating CLAUDE.md -> AGENTS.md would loop.
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert not (tmp_path / "CLAUDE.md").exists()  # no loop synthesized


def test_directory_at_slot_is_a_conflict_not_clobbered(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# canonical\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").mkdir()  # a directory occupies the link slot
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert (tmp_path / "CLAUDE.md").is_dir()  # untouched


def test_conflict_symmetric_when_claude_is_canonical(tmp_path):
    # the mirror direction: CLAUDE.md is the real canonical and AGENTS.md is a foreign symlink
    # / a directory — same conflict handling, canonical/link roles flipped. Guards the
    # canonical-selection branch from a one-directional regression.
    (tmp_path / "CLAUDE.md").write_text("# real canonical\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("elsewhere.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict" and r.canonical == "CLAUDE.md"
    assert _apply(tmp_path).status == "skipped"
    assert str((tmp_path / "AGENTS.md").readlink()) == "elsewhere.md"  # untouched


def test_broken_canonical_symlink_without_pair_is_a_conflict(tmp_path):
    # AGENTS.md is a broken symlink (target missing) and CLAUDE.md is absent: neither slot is a
    # real file, so it's a conflict — rig never replaces the user's (broken) symlink.
    (tmp_path / "AGENTS.md").symlink_to("does-not-exist.md")
    assert resolve_agents_md(tmp_path).state == "conflict"
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink()  # untouched
    assert not (tmp_path / "CLAUDE.md").exists()


# ── _is_broken_symlink predicate (guards the dangling-link detection directly) ────
def test_is_broken_symlink_only_true_for_a_dangling_link(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("x\n", encoding="utf-8")
    good = tmp_path / "good"
    good.symlink_to("real.md")  # resolves
    bad = tmp_path / "bad"
    bad.symlink_to("missing.md")  # dangling
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    assert _is_broken_symlink(bad) is True
    assert _is_broken_symlink(good) is False  # a healthy symlink is not "broken"
    assert _is_broken_symlink(real) is False  # a regular file
    assert _is_broken_symlink(a_dir) is False  # a directory
    assert _is_broken_symlink(tmp_path / "nope") is False  # a path that does not exist at all


# ── dangling rig-shaped link (the canonical was deleted out from under a provisioned pair) ──
# The spec's "rig status flags a missing or BROKEN link". A dangling link stays a non-mutating
# CONFLICT — rig must NOT silently recreate an empty canonical (that would mask the deletion of
# real curated content, turning a visible recoverable failure into a silent unrecoverable one) —
# but its detail must NAME it a broken symlink so a human restores the canonical (or removes the
# link), instead of the generic "neither is a real file".
def test_dangling_claude_link_to_missing_agents_is_named_broken(tmp_path):
    # CLAUDE.md -> AGENTS.md but AGENTS.md was deleted: a dangling RIG-SHAPED link (target is the
    # paired name, the other slot empty) — the spec's headline "canonical deleted out from under
    # the pair". Named with restore-the-canonical guidance; rig NEVER recreates the canonical.
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "broken symlink" in r.detail
    assert "its canonical target AGENTS.md does not exist" in r.detail  # rig-shaped deletion case
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert (tmp_path / "CLAUDE.md").is_symlink()  # link left exactly as the user has it
    assert not (tmp_path / "AGENTS.md").exists()  # rig NEVER recreates the deleted canonical


def test_dangling_agents_link_to_missing_claude_is_named_broken(tmp_path):
    # the mirror: AGENTS.md -> CLAUDE.md with CLAUDE.md missing. Still a rig-shaped broken link,
    # still a non-mutating conflict (recreating CLAUDE.md would silently flip canonical AND mask
    # a possible deletion).
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "its canonical target CLAUDE.md does not exist" in r.detail  # rig-shaped deletion case
    res = _apply(tmp_path)
    assert res.status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink()  # untouched
    assert not (tmp_path / "CLAUDE.md").exists()


def test_dangling_link_to_foreign_missing_target_is_named_broken_generically(tmp_path):
    # a broken symlink whose target is NOT the paired managed name (a foreign dangling target):
    # surfaced as a broken/dangling link, but WITHOUT the "canonical was deleted" narrative — the
    # canonical name was never involved, so that advice would be wrong. Still never mutated.
    (tmp_path / "CLAUDE.md").symlink_to("does-not-exist.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "broken/dangling symlink" in r.detail and "does not resolve" in r.detail
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "CLAUDE.md").is_symlink()  # untouched


def test_peer_symlink_loop_is_named_dangling_not_canonical_deleted(tmp_path):
    # AGENTS.md -> CLAUDE.md AND CLAUDE.md -> AGENTS.md: a circular loop. Both resolve to nothing
    # (ELOOP), so both are "broken", but NOTHING was deleted — the rig-shaped single-dangling
    # branch must NOT fire (it requires the OTHER slot empty), so no "canonical deleted" claim.
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "broken/dangling symlink" in r.detail and "do not resolve" in r.detail
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink() and (tmp_path / "CLAUDE.md").is_symlink()


def test_directory_beside_dangling_link_is_not_the_canonical_deleted_case(tmp_path):
    # AGENTS.md is a DIRECTORY (a competing occupant), CLAUDE.md is a dangling symlink: the
    # rig-shaped branch requires the OTHER slot to be EMPTY, so it must NOT fire (the directory,
    # not a deletion, is the real problem). The generic broken-link message applies — no "canonical
    # deleted" narrative — and rig never mutates either slot.
    (tmp_path / "AGENTS.md").mkdir()
    (tmp_path / "CLAUDE.md").symlink_to("gone.md")  # dangling (foreign, so no dir resolution)
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_dir()  # untouched
    assert (tmp_path / "CLAUDE.md").is_symlink()


def test_both_slots_dangling_foreign_links_read_as_plural(tmp_path):
    # both slots dangling to foreign targets: plural generic broken-link copy, no deletion claim.
    (tmp_path / "AGENTS.md").symlink_to("gone-a.md")
    (tmp_path / "CLAUDE.md").symlink_to("gone-c.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "are broken/dangling symlinks" in r.detail and "their targets do not resolve" in r.detail
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink() and (tmp_path / "CLAUDE.md").is_symlink()


def test_self_referential_symlink_is_a_conflict_not_a_crash(tmp_path):
    # AGENTS.md → AGENTS.md (a self-loop), CLAUDE.md absent. pathlib raises RuntimeError when it
    # tries to resolve the loop; resolve_agents_md must NOT propagate it (it would crash rig
    # status/apply). It is a generic broken-link conflict — NOT the "canonical deleted" narrative
    # (nothing was deleted; the link eats itself) — and rig never mutates it.
    (tmp_path / "AGENTS.md").symlink_to("AGENTS.md")
    r = resolve_agents_md(tmp_path)  # must not raise
    assert r.state == "conflict"
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert "broken/dangling symlink" in r.detail
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink()  # untouched
    assert not (tmp_path / "CLAUDE.md").exists()


def test_mixed_rig_shaped_and_foreign_dangling_pair_is_generic_conflict(tmp_path):
    # one rig-shaped dangling link (AGENTS.md → CLAUDE.md) beside a foreign dangling link
    # (CLAUDE.md → gone.md). The rig-shaped branch must SKIP (the other slot is a symlink, not
    # empty), so the generic plural branch fires — no "canonical deleted" claim — and rig never
    # mutates either slot.
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    (tmp_path / "CLAUDE.md").symlink_to("gone.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "canonical target" not in r.detail  # not the rig-shaped canonical-missing narrative
    assert "broken/dangling symlink" in r.detail
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink() and (tmp_path / "CLAUDE.md").is_symlink()


def test_mixed_rig_shaped_and_foreign_dangling_pair_mirror_guards_first_iteration(tmp_path):
    # the mirror of the case above: CLAUDE.md → AGENTS.md (rig-shaped) beside AGENTS.md → gone.md
    # (foreign). Here the rig-shaped match is in the loop's FIRST iteration, whose "other slot
    # empty" guard must also hold (AGENTS.md is a symlink, not empty) → skip → generic branch.
    # Locks that BOTH iterations honor the guard, not just the second.
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    (tmp_path / "AGENTS.md").symlink_to("gone.md")
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "canonical target" not in r.detail  # generic, not the rig-shaped narrative
    assert "broken/dangling symlink" in r.detail
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").is_symlink() and (tmp_path / "CLAUDE.md").is_symlink()


def test_dangling_rig_shaped_link_with_absolute_target_is_named_broken(tmp_path):
    # an ABSOLUTE dangling target to the paired name still counts as the rig-shaped case
    # (symlink_points_to accepts an absolute path resolving to the same dir) — lock the message.
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "AGENTS.md")  # absolute, still dangling
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict" and "broken symlink" in r.detail
    assert "its canonical target AGENTS.md does not exist" in r.detail  # rig-shaped deletion case
    assert _apply(tmp_path).status == "skipped"


def test_real_file_with_dangling_peer_link_does_not_reach_broken_branch(tmp_path):
    # guard: a REAL AGENTS.md beside a CLAUDE.md symlink to a missing foreign target is handled by
    # the earlier "one real file + foreign link" branch (a conflict about the foreign symlink),
    # NOT the neither-real broken-symlink branch — so the "restore from git" advice (which assumes
    # the canonical was lost) never fires when a real canonical is sitting right there.
    (tmp_path / "AGENTS.md").write_text("# real canonical\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("gone.md")  # dangling, foreign target
    r = resolve_agents_md(tmp_path)
    assert r.state == "conflict"
    assert "symlink to something other than AGENTS.md" in r.detail  # the foreign-link branch
    assert "broken symlink" not in r.detail  # not the canonical-was-deleted message
    assert _apply(tmp_path).status == "skipped"
    assert (tmp_path / "AGENTS.md").read_text() == "# real canonical\n"  # untouched


def test_drift_flags_dangling_link_as_broken_and_apply_leaves_it(tmp_path):
    # status must FLAG a broken link (the spec) — drift surfaces the broken-symlink detail — and
    # apply must NOT silently fix it (no canonical resurrection), so the breakage stays visible.
    # Assert the category + the broken-link copy, not the drift direction taxonomy (which detect
    # owns and may retune), so this locks behavior, not an incidental classification label.
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    plan = _plan_with_action(tmp_path)
    items = [i for i in detect(plan).items if i.category == "agents_md"]
    assert items and any("broken symlink" in i.detail for i in items)
    run_plan(plan)
    assert (tmp_path / "CLAUDE.md").is_symlink() and not (tmp_path / "AGENTS.md").exists()
    # still flagged after apply (apply did not mutate the dangling link)
    assert any("broken symlink" in i.detail for i in detect(plan).items if i.category == "agents_md")


# ── plan gating: default ON, explicit opt-out ─────────────────────────────────────
def _cfg(data: dict, repo: Path) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def test_plan_includes_agents_md_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, tmp_path), cat, project_type="cli")
    assert any(a.kind == "provision_agents_symlink" for a in plan.actions)


@pytest.mark.parametrize("block", [{"enabled": False}, {"symlink": False}])
def test_plan_opt_out(fake_agent_tools, tmp_path, block):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"agents_md": block}, tmp_path), cat, project_type="cli")
    assert not any(a.kind == "provision_agents_symlink" for a in plan.actions)


# ── config validation (fail-closed) ───────────────────────────────────────────────
def test_validate_rejects_non_bool_knob():
    with pytest.raises(ConfigError, match="agents_md.symlink must be a bool"):
        validate({"version": 1, "agents_md": {"symlink": "yes"}})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown agents_md key"):
        validate({"version": 1, "agents_md": {"canonical": "AGENTS.md"}})


def test_validate_accepts_empty_and_valid_block():
    validate({"version": 1, "agents_md": {}})
    validate({"version": 1, "agents_md": {"enabled": True, "symlink": True}})


# ── drift parity (apply and drift switch on the same state) ───────────────────────
def test_drift_missing_then_in_sync(tmp_path):
    plan = _plan_with_action(tmp_path)
    before = detect(plan)
    assert any(i.category == "agents_md" and i.direction == "missing" for i in before.items)
    run_plan(plan)
    assert not any(i.category == "agents_md" for i in detect(plan).items)


def test_drift_flags_foreign_link_symlink(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# canonical\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("elsewhere.md")
    report = detect(_plan_with_action(tmp_path))
    assert any(i.category == "agents_md" and i.direction == "modified" for i in report.items)


def test_drift_flags_both_real_different(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("b\n", encoding="utf-8")
    report = detect(_plan_with_action(tmp_path))
    assert any(i.category == "agents_md" and i.direction == "modified" for i in report.items)


def test_drift_flags_converge_then_apply_clears(tmp_path):
    # regression: identical real files are NOT silently in-sync — drift flags them (apply WILL
    # converge under the default on_conflict), and after apply drift is clean.
    (tmp_path / "AGENTS.md").write_text("same\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("same\n", encoding="utf-8")
    plan = _plan_with_action(tmp_path)
    assert any(i.category == "agents_md" and i.direction == "modified" for i in detect(plan).items)
    run_plan(plan)  # default on_conflict=backup converges
    assert not any(i.category == "agents_md" for i in detect(plan).items)


def test_converge_drift_reported_even_under_skip_policy(tmp_path):
    # repo-wide semantics: on_conflict governs whether APPLY reconciles, not whether STATUS
    # reports. Two identical real files are drift; an on_conflict=skip apply declines to
    # collapse them (like a skip'd modified skill), so the drift stays visible — honest, and
    # consistent with every other category.
    (tmp_path / "AGENTS.md").write_text("same\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("same\n", encoding="utf-8")
    plan = InstallPlan(on_conflict="skip")
    plan.actions.append(_action(tmp_path))
    assert any(i.category == "agents_md" and i.direction == "modified" for i in detect(plan).items)
    run_plan(plan)  # skip → apply declines to converge
    assert not (tmp_path / "CLAUDE.md").is_symlink()  # both real files left as-is
    assert any(i.category == "agents_md" for i in detect(plan).items)  # drift still reported


def test_drift_and_apply_agree_on_conflict_no_silent_mutation(tmp_path):
    # the core invariant: a conflict shows drift, and apply does NOT silently change disk —
    # drift stays the same before and after apply, and the files are untouched.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("docs/guide.md")
    plan = _plan_with_action(tmp_path)
    assert any(i.category == "agents_md" and i.direction == "modified" for i in detect(plan).items)
    run_plan(plan)
    assert any(i.category == "agents_md" and i.direction == "modified" for i in detect(plan).items)
    assert str((tmp_path / "AGENTS.md").readlink()) == "docs/guide.md"  # never touched
    assert not (tmp_path / "CLAUDE.md").exists()


def test_end_to_end_build_apply_detect_in_sync(fake_agent_tools, tmp_path):
    # the real path: build a plan from config (agents_md default ON) → run_plan → detect.
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, repo), cat, project_type="cli")
    report = run_plan(plan)
    assert not report.errors, [r.detail for r in report.errors]
    assert (repo / "AGENTS.md").is_file() and (repo / "CLAUDE.md").is_symlink()
    second = run_plan(plan)
    assert all(r.status == "skipped" for r in second.results if r.action.category == "agents_md")
    assert not any(i.category == "agents_md" for i in detect(plan).items)
