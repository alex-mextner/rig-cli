"""Contract tests for the per-repo harness workflow-guard knobs (Alex tg#5742 / tg#5743).

`agent_hooks.worktree_only` (opt-IN, default off) and `agent_hooks.orchestrator_only`
(opt-OUT, default on) are RUNTIME behaviour knobs read by the agent-hooks from the committed
rig.yaml at fire time. rig-cli's job is only to (1) let the strict validator + published schema
ACCEPT them per-repo, and (2) carry the two hooks via the ALREADY-registered PreToolUse
matchers (so no new matcher/collision with the open lint-on-write bridge PRs).

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


def test_pretooluse_matchers_already_carry_both_guards():
    """No NEW matcher is required: worktree-only-writes (pre-write) rides the existing
    Edit|Write|MultiEdit|NotebookEdit matcher; orchestrator-stays-thin (pre-bash) rides Bash.
    This is why the change merges cleanly alongside the open bridge PRs (#103)."""
    action = Action(
        kind="register_hook_bridge",
        category="agent_hooks",
        item="bridge",
        source=Path("/x"),
        target=Path("/y"),
        options={"lib_dir": "/agent-tools/lib", "python": "python3"},
    )
    matchers = {m for m, _cmd in hook_bridge_entries(action)["PreToolUse"]}
    assert "Edit|Write|MultiEdit|NotebookEdit" in matchers  # carries the pre-write guards
    assert "Bash" in matchers  # carries the pre-bash orchestrator guard


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
