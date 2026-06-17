"""The rig.yaml JSON Schema: it is COMPLETE, ENFORCED, and stays in sync (roadmap §5).

Covers the three things "it should work" means for the schema layer:
1. The published ``schema/rig.schema.json`` is a valid Draft-07 doc and equals the in-code
   registry output (one source of truth — a registry change without a regen fails here).
2. The schema's accepted key set matches ``config.validate``'s, block by block (the validator
   and the editor can never disagree on what is a typo).
3. A real JSON-Schema validator (when installed) flags an unknown key / bad enum the SAME way
   the runtime validator does — so a config a human writes fails loudly in both places.
"""

from __future__ import annotations

import json

import pytest

from riglib import config, config_schema


# ── the published file is valid + in sync with the registry ───────────────────────────
def test_schema_file_exists_and_in_sync():
    # the committed file editors read MUST equal the generated text; a drift means someone
    # edited the registry without `rig schema --write` (or hand-edited the json).
    assert config_schema.schema_file_path().is_file(), "schema/rig.schema.json is missing"
    assert config_schema.schema_file_in_sync(), (
        "schema/rig.schema.json is stale — regenerate with `rig schema --write`"
    )


def test_schema_is_valid_draft7():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft7Validator.check_schema(config_schema.json_schema())


def test_schema_top_level_is_strict_and_complete():
    doc = config_schema.json_schema()
    assert doc["additionalProperties"] is False  # an unknown top-level key is a violation
    props = set(doc["properties"])
    # every top-level key the validator accepts is a schema property (plus the tolerated `scope`)
    assert config_schema.TOP_LEVEL_KEYS <= props
    assert "scope" in props


def test_blocks_without_open_map_are_closed():
    # a fixed-knob block (harness, defaults, github.ruleset, …) must reject an unknown key.
    doc = config_schema.json_schema()
    for name in ("defaults", "skills", "harness", "permissions", "models", "agents_md", "tmux", "tg_ctl"):
        assert doc["properties"][name]["additionalProperties"] is False, f"{name} must be closed"


def test_blocks_with_open_map_whitelist_only_that_map():
    # ci/mcp/agent_hooks keep `items` (arbitrary gate/server names) but reject OTHER unknown keys.
    doc = config_schema.json_schema()
    for name, mapkey in (("ci", "items"), ("mcp", "items"), ("agent_hooks", "items")):
        block = doc["properties"][name]
        assert block["additionalProperties"] is False
        assert mapkey in block["properties"]
        # the open map itself permits arbitrary item objects
        assert block["properties"][mapkey]["additionalProperties"] == {"type": "object"}


# ── registry ↔ validator agreement (no drift between the two key sets) ─────────────────
@pytest.mark.parametrize(
    "block_path, config_keys",
    [
        ("permissions", config._PERMISSIONS_KEYS),
        ("tg_ctl", config._TG_CTL_KEYS),
        ("github.ruleset", config._GITHUB_RULESET_KEYS),
        ("tmux", config._TMUX_TOP_KEYS),
        ("models", {"enabled", "schedule", "checker_path"}),
        ("models.schedule", {"time", "label"}),
        ("agents_md", {"enabled", "symlink"}),
        ("gitignore", {"enabled", "entries", "excludesfile"}),
    ],
)
def test_registry_block_keys_match_validator(block_path, config_keys):
    assert config_schema.block_child_keys(block_path) == set(config_keys)


def test_registry_tmux_subblock_keys_match_validator():
    for sub, allowed in config._TMUX_SUBKEYS.items():
        assert config_schema.block_child_keys(f"tmux.{sub}") == set(allowed)


def test_top_level_keys_match_validator():
    assert config_schema.TOP_LEVEL_KEYS == config._VALID_TOP_KEYS


# ── a real validator flags the same things the runtime does ───────────────────────────
def test_real_validator_flags_unknown_key_and_bad_enum():
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft7Validator(config_schema.json_schema())
    assert list(v.iter_errors({"version": 1, "harness": {"auto_mode": True}})) == []
    assert list(v.iter_errors({"version": 1, "harness": {"aut_mode": True}})), "typo must be flagged"
    assert list(v.iter_errors({"version": 1, "defaults": {"on_conflict": "nuke"}})), "bad enum must be flagged"
    assert list(v.iter_errors({"version": 1, "bogus": 1})), "unknown top-level key must be flagged"


def test_schema_json_roundtrips():
    # the rendered text parses back to the same object (no serialization surprise).
    text = config_schema.render_schema_json()
    assert json.loads(text) == config_schema.json_schema()


@pytest.mark.parametrize(
    "dotted, pointer",
    [
        ("version", "/properties/version"),
        ("harness.auto_mode", "/properties/harness/properties/auto_mode"),
        ("harness.aut_mode", "/properties/harness"),  # a typo → the parent block (which exists)
        ("github.ruleset.required_reviews", "/properties/github/properties/ruleset/properties/required_reviews"),
        ("ci.items.secret-scan.tier", "/properties/ci/properties/items"),  # stops at the open map
        ("git_hooks.dispatcher.fragments.x.enabled", "/properties/git_hooks/properties/dispatcher/properties/fragments"),
    ],
)
def test_schema_pointer_resolves_in_published_file(dotted, pointer):
    assert config_schema.schema_pointer_for(dotted) == pointer
    # and the pointer actually addresses a node in the emitted schema (never dangles)
    node = config_schema.json_schema()
    for seg in pointer.strip("/").split("/"):
        assert seg in node
        node = node[seg]


def test_schema_pointer_none_for_unknown_top_block():
    assert config_schema.schema_pointer_for("bogus.key") is None


# ── docs/config-schema.md is the human reference and stays in sync (every key documented) ──
def _docs_text() -> str:
    from pathlib import Path

    docs = Path(__file__).resolve().parent.parent / "docs" / "config-schema.md"
    return docs.read_text(encoding="utf-8")


def test_every_top_level_block_has_a_doc_section():
    # one ## section per block, so a new block can't land undocumented.
    text = _docs_text()
    for name in config_schema.BLOCKS:
        assert f"## `{name}`" in text or f"## {name}" in text, f"docs missing a section for `{name}`"


def _all_registry_keys() -> set[str]:
    """Every leaf key name across the whole registry (flattened, deduped)."""
    keys: set[str] = set()

    def walk(block: config_schema.Block) -> None:
        keys.update(block.leaves)
        for sub in block.nested.values():
            walk(sub)

    for block in config_schema.BLOCKS.values():
        walk(block)
    return keys


def test_every_registry_key_is_documented():
    # each leaf key must appear somewhere in the human reference (one source, every key documented).
    text = _docs_text()
    missing = sorted(k for k in _all_registry_keys() if k not in text)
    assert not missing, f"docs/config-schema.md does not mention: {missing}"


def test_docs_cite_the_schema_file():
    text = _docs_text()
    assert config_schema.SCHEMA_REL_PATH in text  # the doc points editors at the JSON Schema file
    assert "rig schema" in text  # and documents the command
