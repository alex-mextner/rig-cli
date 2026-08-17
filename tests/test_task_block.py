"""The ``task`` block — task-cli's per-repo ticket-tracker routing (tg#11652).

Unlike every other block, rig writes NOTHING to disk for ``task:`` — task-cli reads it
directly at runtime. This module only covers the validator (``riglib.config._validate_task``)
and the schema registry entry (``riglib.config_schema._TASK_BLOCK``); see task-cli's own repo
for the runtime overlay logic (``tasklib.config.rig_task_overlay``).
"""

from __future__ import annotations

import json

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


# ── schema/validator parity for the accepted null values (tg#11652, P2) ────────────────
#
# `_validate_task` accepts an explicit `null` for every leaf under `task:` except the top-level
# `task:` header itself (that one stays rejected — pinned by
# `test_task_block_null_value_is_rejected_not_treated_as_empty`, matching every other block's
# convention). The generated schema must accept the same nulls, or an editor/schema-based CI
# rejects a config `rig status`/`rig apply` happily accepts. `ALL_TASK_NULLABLE_LEAVES` pairs
# each dotted leaf path with its non-null primary JSON type, so the type-parity test can assert
# the FULL expected set (`{primary, "null"}`), not merely "null is somewhere in there" — the
# latter would stay green even if a future edit dropped the primary type entirely.
ALL_TASK_NULLABLE_LEAVES: list[tuple[tuple[str, ...], str]] = [
    (("backend",), "string"),
    (("team",), "string"),
    (("project",), "string"),
    (("repo",), "string"),
    (("attachment_mode",), "string"),
    (("projects",), "array"),
    (("enforce",), "object"),
    (("classify",), "object"),
    (("session",), "object"),
    (("github",), "object"),
    (("linear",), "object"),
    (("github", "repo"), "string"),
    (("linear", "team"), "string"),
    (("linear", "project"), "string"),
    (("linear", "attachment_mode"), "string"),
]
ALL_TASK_NULLABLE_PATHS: list[tuple[str, ...]] = [path for path, _ in ALL_TASK_NULLABLE_LEAVES]


def _task_config_with_null_at(path: tuple[str, ...]) -> dict:
    """Build ``{"version": 1, "task": {...}}`` with ``null`` set at the dotted path under task."""
    node: dict = {}
    cursor = node
    for part in path[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[path[-1]] = None
    return {"version": 1, "task": node}


def _schema_node_for_task_path(schema: dict, path: tuple[str, ...]) -> dict:
    """Walk ``properties.task.properties.<a>[.properties.<b>]`` for a dotted task path."""
    node = schema["properties"]["task"]
    for part in path:
        node = node["properties"][part]
    return node


@pytest.mark.parametrize("path,primary_type", ALL_TASK_NULLABLE_LEAVES, ids=[".".join(p) for p, _ in ALL_TASK_NULLABLE_LEAVES])
def test_task_schema_leaf_type_is_exactly_primary_and_null(path, primary_type):
    node = _schema_node_for_task_path(config_schema.json_schema(), path)
    assert set(node["type"]) == {primary_type, "null"}, (
        f"task.{'.'.join(path)} schema type must be exactly [{primary_type!r}, 'null'], got {node['type']!r}"
    )


# the three enum leaves need null in BOTH the type union (checked above) AND the `enum` list —
# a Draft-07 validator enforces `enum` independently of `type`, so dropping `None` from just the
# enum would still reject `null` even with the type union in place. Non-skippable (review
# finding): the jsonschema-gated file test below also exercises this, but only when jsonschema is
# installed — this assertion must hold regardless.
@pytest.mark.parametrize("path", [("backend",), ("attachment_mode",), ("linear", "attachment_mode")], ids=".".join)
def test_task_schema_enum_leaf_includes_null_in_enum_list(path):
    node = _schema_node_for_task_path(config_schema.json_schema(), path)
    assert None in node["enum"], f"task.{'.'.join(path)} schema enum must include null, got {node['enum']!r}"


@pytest.mark.parametrize("path", ALL_TASK_NULLABLE_PATHS, ids=".".join)
def test_task_runtime_validator_accepts_null_for_every_nullable_leaf(path):
    # kept in its own non-skippable test — the jsonschema-gated test below only runs when
    # jsonschema is installed, but the runtime half of this contract must always be asserted.
    validate(_task_config_with_null_at(path))


@pytest.mark.parametrize("path", ALL_TASK_NULLABLE_PATHS, ids=".".join)
def test_task_published_schema_file_accepts_null_for_every_nullable_leaf(path):
    # validates the checked-in schema/rig.schema.json FILE directly (the artifact editors and
    # schema-based CI actually consume), not just the in-memory generator output.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(config_schema.schema_file_path().read_text(encoding="utf-8"))
    v = jsonschema.Draft7Validator(schema)
    errors = list(v.iter_errors(_task_config_with_null_at(path)))
    assert errors == [], f"published schema must accept task.{'.'.join(path)}: null, got {errors}"


def test_task_schema_top_level_header_stays_non_nullable():
    # the generator-side half of the ONE deliberate exception: a top-level `task: null` must
    # stay REJECTED (matches every other block's convention, pinned on the runtime side by
    # `test_task_block_null_value_is_rejected_not_treated_as_empty` above). Without this test,
    # flipping `_TASK_BLOCK`'s new `nullable` flag to `True` would silently make the schema
    # LAXER than the runtime validator — the same drift class this whole fix closes, just in the
    # opposite direction. Kept in its OWN non-skippable test (review finding: an earlier version
    # mixed this assertion into the jsonschema-gated test below, so on an environment without
    # jsonschema the WHOLE test — including this always-checkable half — reported skipped).
    node = config_schema.json_schema()["properties"]["task"]
    assert node["type"] == "object", f"task's own schema type must stay plain 'object', got {node['type']!r}"


def test_task_published_schema_file_still_rejects_top_level_null():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(config_schema.schema_file_path().read_text(encoding="utf-8"))
    v = jsonschema.Draft7Validator(schema)
    errors = list(v.iter_errors({"version": 1, "task": None}))
    assert errors, "published schema must still reject task: null"
