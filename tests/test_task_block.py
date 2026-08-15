"""The ``task`` block — task-cli's per-repo ticket-tracker routing (tg#11652).

Unlike every other block, rig writes NOTHING to disk for ``task:`` — task-cli reads it
directly at runtime. This module only covers the validator (``riglib.config._validate_task``)
and the schema registry entry (``riglib.config_schema._TASK_BLOCK``); see task-cli's own repo
for the runtime overlay logic (``tasklib.config.rig_task_overlay``).
"""

from __future__ import annotations

import pytest

from riglib import config_schema
from riglib.config import ConfigError, validate


def test_task_block_with_flat_shorthand_is_valid():
    # the shape this feature specifically unblocks: `task:` used to be a hard "unknown
    # top-level key" ConfigError (exit 2) on every `rig status`/`rig apply` in a repo that had
    # one — see hyperide's rig.yaml, which worked around it with a native task.yaml instead.
    validate({"version": 1, "task": {"backend": "linear", "team": "HYP", "attachment_mode": "link"}})


def test_task_block_github_backend_happy_path_is_valid():
    # coverage gap noted in review: the github-issues side of the enum/coordinate happy path was
    # only ever exercised via the flat-shorthand test above (repo: acme/web), never the nested
    # form with the 'auto' detect-from-remote sentinel.
    validate({"version": 1, "task": {"backend": "github-issues", "github": {"repo": "auto"}}})


def test_task_block_with_nested_sections_is_valid():
    validate(
        {
            "version": 1,
            "task": {
                "backend": "linear",
                "linear": {"team": "HYP", "project": "proj-1", "attachment_mode": "native"},
                "github": {"repo": "acme/web"},
            },
        }
    )


def test_task_block_empty_or_absent_is_valid():
    validate({"version": 1})
    validate({"version": 1, "task": {}})


def test_task_block_must_be_a_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": "linear"})


def test_task_block_null_value_is_rejected_not_treated_as_empty():
    # review finding: a bare `task:` header in YAML (no value) parses to `None`, which is
    # REJECTED here ("task must be a mapping") rather than silently treated as `{}` — this
    # matches the repo-wide convention every other block already follows (e.g.
    # `_validate_tg_ctl(data.get("tg_ctl", {}))` has the identical `None`-vs-absent asymmetry),
    # so this is a deliberate, pinned choice, not an unintentional regression specific to `task`.
    with pytest.raises(ConfigError, match="task must be a mapping"):
        validate({"version": 1, "task": None})


def test_task_block_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"nope": 1}})


def test_task_backend_invalid_enum_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"backend": "jira"}})


@pytest.mark.parametrize("key", ["team", "project", "repo"])
def test_task_flat_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {key: 123}})


def test_task_attachment_mode_invalid_enum_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"attachment_mode": "carrier-pigeon"}})


def test_task_github_must_be_a_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"github": "acme/web"}})


def test_task_github_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"github": {"nope": 1}}})


def test_task_github_repo_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"github": {"repo": 123}}})


def test_task_github_repo_null_is_tolerated_same_as_the_flat_form():
    # review finding: an EXPLICIT `repo: null` (a common way to clear a key in YAML) must be
    # tolerated the SAME way for the nested `github.repo` field as it already is for the flat
    # `repo` shorthand (both use `is not None`, neither treats null as "present, wrong type").
    validate({"version": 1, "task": {"repo": None}})
    validate({"version": 1, "task": {"github": {"repo": None}}})


@pytest.mark.parametrize("key", ["github", "linear"])
def test_task_nested_section_null_is_tolerated_like_absent(key):
    # review finding (2nd round): a bare `github:`/`linear:` header with nothing under it (a
    # common YAML placeholder) parses to null — must be tolerated the SAME way the sibling
    # enforce/classify/session/projects containers already tolerate it (`is not None` guards),
    # not rejected as "must be a mapping". The one deliberate exception is the TOP-level `task:
    # null` itself, kept as-is (see test_task_block_null_value_is_rejected_not_treated_as_empty).
    validate({"version": 1, "task": {key: None}})


@pytest.mark.parametrize("key", ["github", "linear"])
def test_task_nested_section_false_is_still_rejected(key):
    # review finding: the null->{} normalization must be null-SPECIFIC, not "any falsy value" —
    # `github: false` (or `linear: false`) is a genuinely malformed value and must still raise,
    # the same as it always did (pins that `or {}` was NOT used, which would have silently
    # swallowed this case too).
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {key: False}})


@pytest.mark.parametrize("key", ["enforce", "classify", "session", "projects"])
def test_task_passthrough_key_null_is_tolerated(key):
    # locks in the SAME null-tolerance for the passthrough containers the section fix above was
    # made consistent WITH (review finding, 2nd round: this was flagged as untested either way).
    validate({"version": 1, "task": {key: None}})


def test_task_linear_must_be_a_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"linear": "HYP"}})


def test_task_linear_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"linear": {"nope": 1}}})


@pytest.mark.parametrize("key", ["team", "project"])
def test_task_linear_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"linear": {key: 123}}})


def test_task_linear_attachment_mode_invalid_enum_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"linear": {"attachment_mode": "carrier-pigeon"}}})


@pytest.mark.parametrize("key", ["enforce", "classify", "session"])
def test_task_passthrough_object_keys_must_be_mappings(key):
    # deliberately shallow — task-cli owns the deep shape of these; rig only checks the type.
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {key: "not-a-mapping"}})


@pytest.mark.parametrize("key", ["enforce", "classify", "session"])
def test_task_passthrough_object_keys_accept_any_mapping(key):
    validate({"version": 1, "task": {key: {"anything": "goes", "nested": {"too": True}}}})


def test_task_projects_must_be_a_list():
    # review finding: task-cli's `projects:` key is a LIST of project entries
    # (tasklib/projects.py:projects_from_config), NOT a mapping like the other three
    # passthrough keys — modeling it as `object` would reject every valid value.
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"projects": {"not": "a-list"}}})


def test_task_projects_accepts_a_list_of_entries():
    validate(
        {
            "version": 1,
            "task": {"projects": [{"repo": "acme/web"}, {"backend": "linear", "team": "HYP"}]},
        }
    )


def test_task_projects_empty_list_is_valid():
    validate({"version": 1, "task": {"projects": []}})


def test_task_projects_non_object_entries_are_accepted_shallowly():
    # deliberately NOT rejected (review finding, 2nd round): task-cli's own
    # projects_from_config SKIPS a malformed (non-mapping) entry rather than erroring, so rig's
    # validator — and the published JSON schema, which must not be STRICTER than this — stays
    # equally shallow: "a list of anything", not "a list of objects".
    validate({"version": 1, "task": {"projects": [1, "x", True]}})


@pytest.mark.parametrize("bad_value", [123, "nope", True])
def test_task_enum_fields_reject_unhashable_and_wrong_type_values_cleanly(bad_value):
    # review finding: the isinstance-before-membership guard — a scalar of the wrong type must
    # still fail closed with ConfigError (not crash), same as the dict/list case below.
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"backend": bad_value}})


@pytest.mark.parametrize("bad_value", [[], {}, ["linear"]])
def test_task_enum_fields_reject_unhashable_values_without_crashing(bad_value):
    # review finding: `backend not in _VALID_TASK_BACKENDS` on a list/dict value raises a raw
    # TypeError (unhashable) without an isinstance guard — must surface as ConfigError instead.
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"backend": bad_value}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"attachment_mode": bad_value}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "task": {"linear": {"attachment_mode": bad_value}}})


# ── schema registry ─────────────────────────────────────────────────────────────────────


def test_task_is_a_registered_top_level_block():
    assert "task" in config_schema.BLOCKS
    assert "task" in config_schema.TOP_LEVEL_KEYS


def test_task_schema_pointer_resolves():
    assert config_schema.schema_pointer_for("task.backend") == "/properties/task/properties/backend"
    assert config_schema.schema_pointer_for("task.linear.team") is not None


def test_task_block_child_keys_include_flat_and_nested_forms():
    keys = config_schema.block_child_keys("task")
    assert keys == {
        "backend",
        "team",
        "project",
        "repo",
        "attachment_mode",
        "projects",
        "enforce",
        "classify",
        "session",
        "github",
        "linear",
    }
