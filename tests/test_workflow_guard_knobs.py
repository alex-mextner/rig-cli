"""Contract tests for the per-repo harness workflow-guard knobs (Alex tg#5742 / tg#5743).

`agent_hooks.worktree_only` (opt-IN, default off) and `agent_hooks.orchestrator_only`
(opt-OUT, default on) are RUNTIME behaviour knobs read by the agent-hooks from the committed
rig.yaml at fire time. rig-cli's job is only to (1) let the strict validator + published schema
ACCEPT them per-repo, and (2) keep the managed PreToolUse matcher set that carries bash,
write, and subagent-dispatch hooks.

Run from the repo root::

    python -m pytest tests/test_workflow_guard_knobs.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config, config_schema, schema
from riglib.actions.runner import hook_bridge_entries
from riglib.plan import Action


def _agent_hooks_schema_props() -> dict:
    return config_schema.json_schema()["properties"]["agent_hooks"]["properties"]


def test_schema_exposes_both_knobs_with_correct_defaults():
    props = _agent_hooks_schema_props()
    assert props["worktree_only"]["type"] == "boolean"
    assert props["worktree_only"]["default"] is False  # opt-IN — a repo is never blocked by default
    assert props["orchestrator_only"]["type"] == "boolean"
    assert props["orchestrator_only"]["default"] is True  # opt-OUT — no behaviour change by default


def test_validator_accepts_both_knobs():
    # both knobs, both polarities, must pass the strict validator
    config.validate(
        {"version": 1, "agent_hooks": {"worktree_only": True, "orchestrator_only": False}}
    )
    config.validate(
        {"version": 1, "agent_hooks": {"worktree_only": False, "orchestrator_only": True}}
    )


@pytest.mark.parametrize("key", ["worktree_only", "orchestrator_only"])
@pytest.mark.parametrize("bad", ["false", "true", 1, 0])
def test_validator_rejects_non_bool_knob_values(key: str, bad: object):
    """The published schema types both knobs as ``boolean``; the strict validator must reject a
    non-bool (a string or an int) or it would silently accept configs the schema forbids — the
    validator↔schema type invariant. Ints count: ``bool`` is an int subclass, but ``1``/``0``
    are not bools, so a YAML ``worktree_only: 1`` is a type error, not a truthy toggle."""
    with pytest.raises(config.ConfigError, match=f"agent_hooks.{key} must be a bool"):
        config.validate({"version": 1, "agent_hooks": {key: bad}})


def test_block_child_keys_includes_knobs():
    """The validator sources its accepted keys from config_schema — both must be there, or the
    published schema and the validator would disagree (the one-source invariant)."""
    keys = config_schema.block_child_keys("agent_hooks")
    assert keys is not None
    assert {"worktree_only", "orchestrator_only"} <= keys


def test_strictness_preserved_a_typo_is_still_rejected():
    """Adding the knobs must NOT loosen the block — a typo'd key still fails closed."""
    with pytest.raises(config.ConfigError, match="unknown agent_hooks key"):
        config.validate({"version": 1, "agent_hooks": {"worktree_onlyy": True}})


def test_pretooluse_matchers_cover_the_full_managed_set():
    """The bridge keeps every managed PreToolUse matcher, including subagent dispatch."""
    action = Action(
        kind="register_hook_bridge",
        category="agent_hooks",
        item="bridge",
        source=Path("/x"),
        target=Path("/y"),
        options={"lib_dir": "/agent-tools/lib", "python": "python3"},
    )
    matchers = {m for m, _cmd in hook_bridge_entries(action)["PreToolUse"]}
    assert matchers == {
        "Bash",
        "Edit|Write|MultiEdit|NotebookEdit",
        "Agent|Task",
    }


def test_wizard_registry_exposes_both_knobs_with_correct_defaults():
    """`rig setup`/`config-web` enumerate `riglib.schema.AREAS`, a SEPARATE registry from the
    strict validator/JSON-schema (`config_schema.py`) exercised above. Adding a key only to
    config_schema makes hand edits and `config set` valid, but leaves it undiscoverable/
    untoggleable in the interactive surfaces — a repo could not opt in to `worktree_only` or opt
    out of `orchestrator_only` before activation. Regression for the codex P2 review finding on
    PR #104 (`riglib/config_schema.py:221`)."""
    worktree_opt = schema.option_for_key("agent_hooks.worktree_only")
    orchestrator_opt = schema.option_for_key("agent_hooks.orchestrator_only")
    assert worktree_opt is not None, "worktree_only missing from the wizard/config-web registry"
    assert orchestrator_opt is not None, "orchestrator_only missing from the wizard/config-web registry"
    assert worktree_opt.kind == schema.KIND_BOOL
    assert orchestrator_opt.kind == schema.KIND_BOOL
    # defaults must match config_schema's Leaf defaults exactly (the two-source invariant). Read
    # the OTHER source's live value rather than a second hardcoded literal, or the two registries
    # could drift to different values and this test would still pass.
    config_schema_props = _agent_hooks_schema_props()
    assert worktree_opt.default == config_schema_props["worktree_only"]["default"]
    assert worktree_opt.default is False  # opt-IN
    assert orchestrator_opt.default == config_schema_props["orchestrator_only"]["default"]
    assert orchestrator_opt.default is True  # opt-OUT
    assert worktree_opt.hint and orchestrator_opt.hint


def test_both_registries_document_the_tg_hatch_token_not_the_dead_env_bypass():
    """Both the wizard hints (`schema.py`) and the JSON-schema descriptions (`config_schema.py`)
    must name the CURRENT deny-by-default tg-hatch env var and must NOT resurrect the removed
    self-service `=1` bypass. The two registries are hand-copied prose, so this locks the
    load-bearing contract token against drift (the agent-tools hooks were converted from
    RIG_ALLOW_MAIN_EDIT / ALLOW_ORCHESTRATOR_WORK to the RIG_HATCH_REQUEST_* form)."""
    props = _agent_hooks_schema_props()
    for key, tokens in (
        # worktree_only gates TWO hooks with TWO separate hatch vars — both must be named, not
        # just worktree-only-writes's (a single shared var would misdescribe the actual per-hook
        # contract, the same drift class the AGENTS.md/docs doc-lock test below guards).
        ("worktree_only", ("RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES", "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE")),
        ("orchestrator_only", ("RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN",)),
    ):
        opt = schema.option_for_key(f"agent_hooks.{key}")
        assert opt is not None
        for token in tokens:
            assert token in opt.hint, f"wizard hint for {key} must name {token}"
            assert token in props[key]["description"], f"schema description for {key} must name {token}"
    # The dead self-service bypasses must appear in NEITHER registry.
    for dead in ("RIG_ALLOW_MAIN_EDIT", "ALLOW_ORCHESTRATOR_WORK"):
        for key in ("worktree_only", "orchestrator_only"):
            assert dead not in schema.option_for_key(f"agent_hooks.{key}").hint
            assert dead not in props[key]["description"]


def test_hand_copied_docs_document_the_tg_hatch_tokens_not_the_dead_env_bypass():
    """AGENTS.md and docs/config-schema.md are the SAME hand-copied prose as the wizard/schema
    registries (they restate the `worktree_only`/`orchestrator_only` contract for a human/agent
    reader), so they drift independently of `schema.py`/`config_schema.py`. AGENTS.md in
    particular is the surface an agent actually reads at runtime — precisely where a resurrected
    dead bypass name would do damage. This locks BOTH docs against the same drift the registry
    test above locks, and additionally requires the worktree_only doc to name BOTH of its two
    hooks' hatch vars (`pin-primary-worktree` has its OWN `RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE`,
    distinct from `worktree-only-writes`'s `RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES` — a single
    shared var would misdescribe the actual per-hook hatch contract)."""
    repo_root = Path(__file__).resolve().parent.parent
    agents_md = (repo_root / "AGENTS.md").read_text()
    config_schema_md = (repo_root / "docs" / "config-schema.md").read_text()
    live_tokens = (
        "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES",
        "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE",
        "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN",
    )
    dead_tokens = ("RIG_ALLOW_MAIN_EDIT", "ALLOW_ORCHESTRATOR_WORK")
    for doc_name, text in (("AGENTS.md", agents_md), ("docs/config-schema.md", config_schema_md)):
        for token in live_tokens:
            assert token in text, f"{doc_name} must name {token}"
        for dead in dead_tokens:
            assert dead not in text, f"{doc_name} must not resurrect the dead bypass {dead}"


def test_orchestrator_only_gh_delegation_documented_consistently():
    """The (Alex tg#7103) contract — orchestrator-stays-thin delegates ALL `gh` (incl. `gh ship`,
    `gh pr`) to a subagent, and warns on a first offense before blocking a repeat — must be stated
    the same way across all four hand-copied prose surfaces (a Fable review finding: AGENTS.md
    once said 'warned-then-blocked' while the other three just said 'blocks', understating the
    real behavior; someone reverting one surface to the old wording would pass every other check
    in this file)."""
    repo_root = Path(__file__).resolve().parent.parent
    agents_md = (repo_root / "AGENTS.md").read_text()
    config_schema_md = (repo_root / "docs" / "config-schema.md").read_text()
    props = _agent_hooks_schema_props()
    surfaces = {
        "AGENTS.md": agents_md,
        "docs/config-schema.md": config_schema_md,
        "riglib/schema.py (wizard hint)": schema.option_for_key("agent_hooks.orchestrator_only").hint,
        "riglib/config_schema.py (schema description)": props["orchestrator_only"]["description"],
    }
    for doc_name, raw_text in surfaces.items():
        # Markdown line-wraps a long sentence, so "delegated to a subagent" can land split across
        # a newline in the checked-in prose (AGENTS.md's 90-col wrapping does exactly this) —
        # collapse whitespace before the substring check so wrapping doesn't produce a false fail.
        text = " ".join(raw_text.split())
        # "gh ship"/"gh pr" alone would also match the OLD (pre-tg#7103) wording, where they meant
        # the OPPOSITE thing (allowed as inline orchestration) — assert the phrase that only holds
        # under the NEW contract, so a revert to the old wording actually fails this test.
        assert "delegated to a subagent" in text and "gh ship" in text and "gh pr" in text, (
            f"{doc_name} must say gh ship/gh pr are delegated to a subagent, not just mention them"
        )
        assert "warn" in text.lower(), f"{doc_name} must describe the warn-first-then-block behavior"


def test_wizard_can_toggle_both_knobs():
    """The wizard/`config set` coercion + effective-value path must round-trip both knobs, i.e.
    they are actually settable, not just visible."""
    worktree_opt = schema.option_for_key("agent_hooks.worktree_only")
    orchestrator_opt = schema.option_for_key("agent_hooks.orchestrator_only")
    assert schema.coerce(worktree_opt, "yes") is True
    assert schema.coerce(orchestrator_opt, "no") is False
    # absent from a config -> falls back to the registry default (agent_hooks is not a
    # block-presence-gated category, so this is the documented default, not a forced False).
    assert schema.effective_value(worktree_opt, {}) is False
    assert schema.effective_value(orchestrator_opt, {}) is True
    assert schema.effective_value(worktree_opt, {"agent_hooks": {"worktree_only": True}}) is True
    assert (
        schema.effective_value(orchestrator_opt, {"agent_hooks": {"orchestrator_only": False}})
        is False
    )
    # both write into the committed repo rig.yaml, not the global config (repo-runtime knobs).
    assert worktree_opt.layer == schema.REPO
    assert orchestrator_opt.layer == schema.REPO


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
