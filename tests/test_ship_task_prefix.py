"""`task.code_prefix` — the rig.yaml source for ship.sh's `.ship-config` SHIP_TASK_CODE_PREFIX.

ship.sh's review-quorum gate only recognizes HYP-123/XX-123 style task codes. A repo whose
task-cli backend is GitHub Issues (bare #NNN, no ticket-code convention of its own) needs a
repo-unique prefix so a bare issue number can be synthesized into a collision-free task code
(review-cli's quorum store is a single global file keyed only by the code string, with no
per-repo scoping). `rig apply` reconciles the configured prefix into `.ship-config`, merging
into any existing content rather than overwriting the file.

Covers: plan gating (unset = no action; set = one action), the idempotent merge-write
(creates, upserts alongside existing keys, no-ops when already correct), and on_conflict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.actions.runner import _do_provision_ship_task_prefix
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.plan import Action, InstallPlan, _build_ship_task_prefix


def _action(repo: Path, prefix: str) -> Action:
    return Action(
        kind="provision_ship_task_prefix",
        category="task",
        item="code_prefix",
        source=repo,
        target=repo,
        options={"code_prefix": prefix},
    )


def _apply(repo: Path, prefix: str, on_conflict: str = "backup"):
    return _do_provision_ship_task_prefix(_action(repo, prefix), on_conflict)


def _loaded(cfg: dict, repo: Path) -> LoadedConfig:
    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


# ── plan gating ──────────────────────────────────────────────────────────────────
def test_build_emits_nothing_when_task_block_absent(tmp_path):
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({}, tmp_path), plan)
    assert plan.actions == []
    assert plan.notes == []  # no task block at all is the normal case — no note needed


def test_validate_rejects_non_mapping_task_block(tmp_path):
    """A `task: somestring` in rig.yaml is rejected by validate() itself (_validate_task in
    config.py) — the primary, fail-closed enforcement point, consistent with every other
    top-level block. This is the normal way an author hits the problem: `rig apply`/`rig config
    set` calls validate() before ever reaching the plan builder."""
    with pytest.raises(ConfigError):
        validate({"task": "not-a-mapping"})


def test_build_notes_non_mapping_task_block_when_validate_is_bypassed(tmp_path):
    """Defense-in-depth: _build_ship_task_prefix's own non-dict guard, for a caller that
    constructs a LoadedConfig directly without going through validate() (validate() itself
    already refuses this case — see test_validate_rejects_non_mapping_task_block — so this
    exercises the plan builder's fallback in isolation, not the normal production path)."""
    plan = InstallPlan()
    cfg = LoadedConfig(data={"task": "not-a-mapping"}, repo_root=tmp_path)
    _build_ship_task_prefix(cfg, plan)
    assert plan.actions == []
    assert any("is not a mapping" in n for n in plan.notes), plan.notes


def test_build_emits_nothing_when_code_prefix_unset(tmp_path):
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {}}, tmp_path), plan)
    assert plan.actions == []


def test_build_notes_non_string_non_int_code_prefix_rather_than_silently_dropping_it(tmp_path):
    """validate() checks block/scalar structure, not a leaf's declared `string` type, so a
    value (e.g. a YAML list, from `code_prefix: [RIG]`) reaches the plan builder undetected.
    It must produce a plan.notes entry — the same operator feedback a malformed STRING
    already gets — not silently do nothing."""
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": ["RIG"]}}, tmp_path), plan)
    assert plan.actions == []
    assert any("is not a string" in n for n in plan.notes), plan.notes


def test_build_emits_one_action_when_code_prefix_set(tmp_path):
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "RIG"}}, tmp_path), plan)
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.kind == "provision_ship_task_prefix"
    assert action.options["code_prefix"] == "RIG"
    assert action.target == tmp_path


def test_build_emits_one_action_at_the_40_char_boundary(tmp_path):
    """Exactly 40 uppercase letters/digits is the longest ACCEPTED value (boundary, not just
    over-length rejection — a fence-post bug could reject this too)."""
    prefix = "A" * 40
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": prefix}}, tmp_path), plan)
    assert len(plan.actions) == 1
    assert plan.actions[0].options["code_prefix"] == prefix


def test_build_accepts_an_all_digits_prefix(tmp_path):
    """The character class allows digits too, not just letters — exercise that branch
    positively (the other accept-path tests are all letters-only)."""
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "12345"}}, tmp_path), plan)
    assert len(plan.actions) == 1
    assert plan.actions[0].options["code_prefix"] == "12345"


def test_build_rejects_an_unquoted_numeric_prefix_through_real_yaml_parsing(tmp_path):
    """An UNQUOTED all-digit rig.yaml value (`code_prefix: 12345`) parses as a YAML/Python int,
    not a str — the JSON schema declares `code_prefix` a string, so this must be rejected with
    a note guiding the author to quote it, not silently coerced (which would accept a value
    every schema-aware editor already flags as invalid). Goes through real yaml.safe_load, not
    a hand-built Python dict, so it actually exercises the type YAML produces for this input."""
    import yaml

    parsed = yaml.safe_load("task:\n  code_prefix: 12345\n")
    assert isinstance(parsed["task"]["code_prefix"], int)  # sanity: confirms the YAML gotcha
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded(parsed, tmp_path), plan)
    assert plan.actions == []
    assert any("quote it" in n for n in plan.notes), plan.notes


def test_build_rejects_non_ascii_lookalike_digit(tmp_path):
    """Python's str.isdigit()/str.isupper() are true for some non-ASCII characters (e.g. the
    superscript two, U+00B2) that are not [A-Z0-9] — the isascii() guard in the character-class
    check must reject those, not silently accept a lookalike."""
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "RIG²"}}, tmp_path), plan)
    assert plan.actions == []
    assert any("not 1-40 uppercase letters/digits" in n for n in plan.notes), plan.notes


def test_build_treats_empty_prefix_as_unset(tmp_path):
    """code_prefix: "" must be silently treated as unset (same as omitting the key) — not a
    rejection. This is the schema<->runtime agreement config_schema.py's not_pattern comment
    depends on: the schema deliberately does NOT reject "" (it means "off"), so the runtime
    check must not reject it either, or the two would disagree in the opposite direction."""
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": ""}}, tmp_path), plan)
    assert plan.actions == []
    assert not any("not 1-40 uppercase letters/digits" in n for n in plan.notes), plan.notes


# ── malformed code_prefix: rejected at plan-build time, not just documented ────────
def test_build_rejects_prefix_over_40_chars(tmp_path):
    """A >40-char prefix must NOT reach the write action — ship.sh would ignore the whole
    .ship-config file for it, silently dropping any hand-committed SHIP_LOCAL_TEST_DIR/CMD."""
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "A" * 41}}, tmp_path), plan)
    assert plan.actions == []
    assert any("not 1-40 uppercase letters/digits" in n for n in plan.notes), plan.notes


def test_build_rejects_lowercase_prefix(tmp_path):
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "rig"}}, tmp_path), plan)
    assert plan.actions == []
    assert any("not 1-40 uppercase letters/digits" in n for n in plan.notes), plan.notes


def test_build_rejects_prefix_with_hyphen(tmp_path):
    plan = InstallPlan()
    _build_ship_task_prefix(_loaded({"task": {"code_prefix": "RIG-CLI"}}, tmp_path), plan)
    assert plan.actions == []
    assert any("not 1-40 uppercase letters/digits" in n for n in plan.notes), plan.notes


def test_validate_accepts_a_malformed_code_prefix(tmp_path):
    """Documents a real, PRE-EXISTING gap (not introduced by this change, and not fixed here
    — it applies to every `not_pattern`/`enum` leaf constraint in config_schema.py, not just
    this one): `validate()` only checks structure (unknown keys, block/scalar types), never
    leaf-level constraints like `not_pattern` — those exist solely to generate the JSON schema
    consumed by editor tooling and `rig schema --check`. So a malformed code_prefix passes
    `validate()` cleanly; ONLY `_build_ship_task_prefix` (plan.py) — exercised by the
    `test_build_rejects_*` tests above — actually refuses it, at `rig apply` plan-build time."""
    validate({"task": {"code_prefix": "not-a-valid-prefix"}})  # must not raise


def test_validate_accepts_well_formed_code_prefix(tmp_path):
    validate({"task": {"code_prefix": "RIG"}})  # must not raise


# ── the merge-write itself ──────────────────────────────────────────────────────
def test_apply_skipped_when_prefix_unset(tmp_path):
    r = _apply(tmp_path, "")
    assert r.status == "skipped"
    assert "not set" in r.detail
    assert not (tmp_path / ".ship-config").exists()


def test_apply_creates_ship_config_when_absent(tmp_path):
    r = _apply(tmp_path, "RIG")
    assert r.status == "created", r.detail
    content = (tmp_path / ".ship-config").read_text(encoding="utf-8")
    assert content == "SHIP_TASK_CODE_PREFIX=RIG\n"


def test_apply_upserts_alongside_existing_ship_config_content(tmp_path):
    """A hand-committed .ship-config with SHIP_LOCAL_TEST_DIR/CMD must keep both lines —
    the action merges its own line in, never replaces the file. This is NOT a conflict
    (the managed line itself is simply absent, the file having OTHER content is irrelevant)
    — status is "updated", the same under every on_conflict policy, and no backup is made."""
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text(
        "# audited local-test override\nSHIP_LOCAL_TEST_DIR=e2e\nSHIP_LOCAL_TEST_CMD=npm test\n",
        encoding="utf-8",
    )
    r = _apply(tmp_path, "RIG")
    assert r.status == "updated", r.detail
    assert r.backup is None
    content = cfg_path.read_text(encoding="utf-8")
    assert "SHIP_LOCAL_TEST_DIR=e2e" in content
    assert "SHIP_LOCAL_TEST_CMD=npm test" in content
    assert "SHIP_TASK_CODE_PREFIX=RIG" in content


def test_apply_updates_unconditionally_under_on_conflict_skip_when_line_is_absent(tmp_path):
    """The keyed-merge fix this test pins: on_conflict=skip must NOT refuse to provision the
    prefix into a repo whose .ship-config merely lacks the managed line — only a DIFFERING
    existing line is a real conflict (see the next test). Before this fix, `rig status` would
    report `missing` forever with no way for `rig apply` to ever converge it under skip."""
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text("SHIP_LOCAL_TEST_CMD=npm test\n", encoding="utf-8")
    r = _apply(tmp_path, "RIG", on_conflict="skip")
    assert r.status == "updated", r.detail
    assert "SHIP_TASK_CODE_PREFIX=RIG" in cfg_path.read_text(encoding="utf-8")


def test_apply_skips_a_differing_line_under_on_conflict_skip(tmp_path):
    """The one real conflict case skip must still honor: the managed line EXISTS with a
    different value."""
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text("SHIP_TASK_CODE_PREFIX=OLD\n", encoding="utf-8")
    r = _apply(tmp_path, "NEW", on_conflict="skip")
    assert r.status == "skipped", r.detail
    assert "SHIP_TASK_CODE_PREFIX=OLD" in cfg_path.read_text(encoding="utf-8")


def test_apply_replaces_a_stale_prefix_line_in_place(tmp_path):
    """A differing existing line under the default on_conflict="backup" IS a real conflict —
    the whole file is backed up (pinned "backed_up", not the looser "updated or backed_up":
    on_conflict="backup" always backs up a real conflict, "updated" would only fire for
    overwrite/skip's no-file-touched-but-succeeded case, which doesn't apply here)."""
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text("SHIP_TASK_CODE_PREFIX=OLD\nSHIP_LOCAL_TEST_CMD=npm test\n", encoding="utf-8")
    r = _apply(tmp_path, "NEW")
    assert r.status == "backed_up", r.detail
    assert r.backup is not None and r.backup.exists()
    assert "SHIP_TASK_CODE_PREFIX=OLD" in r.backup.read_text(encoding="utf-8")
    lines = cfg_path.read_text(encoding="utf-8").splitlines()
    assert lines.count("SHIP_TASK_CODE_PREFIX=NEW") == 1
    assert "SHIP_TASK_CODE_PREFIX=OLD" not in lines
    assert "SHIP_LOCAL_TEST_CMD=npm test" in lines


def test_apply_idempotent_when_already_correct(tmp_path):
    _apply(tmp_path, "RIG")
    r = _apply(tmp_path, "RIG")
    assert r.status == "skipped", r.detail
    assert "identical" in r.detail


def test_apply_backs_up_prior_content_on_conflict_backup(tmp_path):
    """A real conflict (differing existing line) under the default backup policy backs up the
    WHOLE prior file content, not just the one changed line."""
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text("SHIP_TASK_CODE_PREFIX=OLD\nSHIP_LOCAL_TEST_CMD=npm test\n", encoding="utf-8")
    r = _apply(tmp_path, "RIG", on_conflict="backup")
    assert r.status == "backed_up", r.detail
    assert r.backup is not None and r.backup.exists()
    assert "SHIP_LOCAL_TEST_CMD=npm test" in r.backup.read_text(encoding="utf-8")
    assert "SHIP_TASK_CODE_PREFIX=OLD" in r.backup.read_text(encoding="utf-8")


def test_apply_overwrites_a_differing_line_under_on_conflict_overwrite(tmp_path):
    cfg_path = tmp_path / ".ship-config"
    cfg_path.write_text("SHIP_TASK_CODE_PREFIX=OLD\n", encoding="utf-8")
    r = _apply(tmp_path, "NEW", on_conflict="overwrite")
    assert r.status == "updated", r.detail
    assert r.backup is None
    assert cfg_path.read_text(encoding="utf-8") == "SHIP_TASK_CODE_PREFIX=NEW\n"


def test_apply_rejects_a_malformed_prefix_at_the_runner_defense_in_depth_gate(tmp_path):
    """plan.py's builder already refuses a malformed prefix before an action is ever created —
    this exercises the RUNNER's own re-check for a caller that constructs the action directly,
    bypassing the plan builder entirely (see SHIP_TASK_CODE_PREFIX_RE, shared by both sites)."""
    r = _apply(tmp_path, "not-valid!")
    assert r.status == "error", r.detail
    assert "not 1-40 uppercase letters/digits" in r.detail
    assert not (tmp_path / ".ship-config").exists()


# ── registry wiring ──────────────────────────────────────────────────────────────
def test_ship_task_prefix_is_wired_into_every_registry():
    from riglib.actions.runner import _HANDLERS
    from riglib.areas import AREAS
    from riglib.config import _VALID_TOP_KEYS
    from riglib.layers import REPO, layer_for_category

    assert "task" in _VALID_TOP_KEYS
    assert "provision_ship_task_prefix" in _HANDLERS
    assert any(a.key == "task" and a.layer == REPO for a in AREAS)
    assert layer_for_category("task") == REPO


# ── drift (riglib/drift.py::_check_ship_task_prefix) — status/apply parity ────────
def test_drift_flags_missing_file_as_missing(tmp_path):
    from riglib.drift import DriftReport, _check_ship_task_prefix

    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, "RIG"), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "missing"


def test_drift_flags_absent_line_in_existing_file_as_missing(tmp_path):
    from riglib.drift import DriftReport, _check_ship_task_prefix

    (tmp_path / ".ship-config").write_text("SHIP_LOCAL_TEST_CMD=npm test\n", encoding="utf-8")
    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, "RIG"), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "missing"


def test_drift_flags_a_hand_edited_value_as_modified(tmp_path):
    from riglib.drift import DriftReport, _check_ship_task_prefix

    (tmp_path / ".ship-config").write_text("SHIP_TASK_CODE_PREFIX=STALE\n", encoding="utf-8")
    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, "RIG"), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "modified"


def test_drift_flags_duplicate_prefix_lines_as_modified_even_when_correct_one_present(tmp_path):
    """A file with the CORRECT line present ALONGSIDE a stale extra one must still report
    drift, not clean — apply is about to collapse it to a single line on the next run
    (_do_provision_ship_task_prefix dedups), so "the expected line is somewhere in the file"
    alone is not the same as "the file is already correct"."""
    from riglib.drift import DriftReport, _check_ship_task_prefix

    (tmp_path / ".ship-config").write_text(
        "SHIP_TASK_CODE_PREFIX=RIG\nSHIP_TASK_CODE_PREFIX=OLD\n", encoding="utf-8"
    )
    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, "RIG"), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "modified"


def test_drift_is_clean_after_apply(tmp_path):
    """status/apply parity: what apply just wrote must read back as in-sync, not drifted."""
    from riglib.drift import DriftReport, _check_ship_task_prefix

    _apply(tmp_path, "RIG")
    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, "RIG"), report)
    assert report.items == []


def test_drift_is_a_noop_when_prefix_unset(tmp_path):
    from riglib.drift import DriftReport, _check_ship_task_prefix

    report = DriftReport()
    _check_ship_task_prefix(_action(tmp_path, ""), report)
    assert report.items == []
