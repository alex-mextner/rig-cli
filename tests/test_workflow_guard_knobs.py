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

from riglib import config, config_schema
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


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
