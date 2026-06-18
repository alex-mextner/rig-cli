"""GitHub repository MERGE-button policy desired-state — the pure, side-effect-free core.

WHAT THIS IS. The §5 ``github:`` block provisions a repo's GitHub settings declaratively;
``github.ruleset`` (in ``github_ruleset.py``) owns the branch ruleset, and THIS module owns the
repo MERGE-button policy that the same ``gh api`` backend reconciles via ``PATCH /repos/{o}/{r}``:
the squash-only merge model, auto-delete-head-branch-on-merge, and allow-auto-merge. It is the
single source of truth for the DESIRED merge settings (the secure/sensible default knobs, the
PATCH request body, and the normalized desired-vs-actual comparison). The ``Action`` handler and
the ``gh`` subprocess seams live in ``actions/runner.py`` and import from here; ``plan``,
``state``, and ``drift`` import the constants/builders here at module top.

WHY IT IS ITS OWN MODULE (not in ``actions/runner.py``). ``runner`` imports ``Action`` from
``..plan``, so ``plan`` cannot import ``runner`` at module top without a cycle. Keeping these
PURE pieces here (stdlib-only) lets ``plan`` and ``state`` import them at module top — no
lazy/deferred imports, no cycle. Mirrors ``github_ruleset.py`` exactly.

THE SECURE DEFAULT. Squash-only (merge-commit and rebase OFF) gives a linear, one-commit-per-PR
history; deleting the head branch on merge keeps the branch list clean; allow-auto-merge lets a
PR land the moment its gate goes green without a human re-clicking. These are settings, not a
ruleset, so they never lock anyone out — the worst case of a mis-set merge model is a disabled
button, not a blocked merge. The ``gh api`` backend handles the API-exposed knobs here; settings
the API does NOT expose are a separate ``agent-browser`` backend concern (see ROADMAP §5).
"""

from __future__ import annotations

from typing import Any

# The canonical SECURE/SENSIBLE-DEFAULT merge-button knobs — the single source the plan builder
# merges a sparse config onto and the state.py scaffold writes. `enabled` is a plan-gating
# meta-key, not a body knob, so it is NOT here.
#  - squash_merge: ON, and merge_commit/rebase_merge OFF → squash is the only merge model
#    (linear, one commit per PR). The three are independent GitHub flags; rig manages all three so
#    "squash-only" is actually enforced, not just "squash also allowed".
#  - delete_branch_on_merge: ON → the head branch is auto-deleted on merge (clean branch list).
#  - allow_auto_merge: ON → a PR can be queued to merge the instant its required checks pass.
#  - allow_update_branch: ON → the "Update branch" button is available (keep a PR current with base).
GITHUB_MERGE_DEFAULTS: dict[str, Any] = {
    "squash_merge": True,
    "merge_commit": False,
    "rebase_merge": False,
    "delete_branch_on_merge": True,
    "allow_auto_merge": True,
    "allow_update_branch": True,
}

# Map each rig knob → the GitHub repo-edit API field it sets. rig manages EXACTLY these fields on
# the repo object; every other repo field (name, description, topics, …) is left untouched. One
# table so the body builder and the normalized comparison read the SAME field set — a knob added
# to one but not the other can't silently skip the diff.
_KNOB_TO_API_FIELD: dict[str, str] = {
    "squash_merge": "allow_squash_merge",
    "merge_commit": "allow_merge_commit",
    "rebase_merge": "allow_rebase_merge",
    "delete_branch_on_merge": "delete_branch_on_merge",
    "allow_auto_merge": "allow_auto_merge",
    "allow_update_branch": "allow_update_branch",
}

# The API fields rig manages — derived from the table above so the two never disagree.
MANAGED_MERGE_API_FIELDS: tuple[str, ...] = tuple(_KNOB_TO_API_FIELD.values())


def build_merge_body(opts: dict) -> dict[str, bool]:
    """The desired ``PATCH /repos/{owner}/{repo}`` body — only the merge fields rig manages.

    Shared by apply + drift (one source of truth). Every managed knob is emitted as its API
    field with a hard ``bool`` (so a stray truthy/falsy config value can't send ``1``/``""`` to
    the API), falling back to the secure default when the knob is absent. The body carries ONLY
    the merge-policy fields — never a repo name/description/visibility key — so a PATCH can never
    accidentally rename or expose the repo.
    """
    return {
        _KNOB_TO_API_FIELD[knob]: bool(opts.get(knob, GITHUB_MERGE_DEFAULTS[knob]))
        for knob in GITHUB_MERGE_DEFAULTS
    }


def normalize_merge(repo_obj: dict) -> dict[str, bool]:
    """The comparable shape of the merge policy — only the managed fields, as plain bools.

    Both the desired body and the live repo object (from ``GET /repos/{o}/{r}``) are normalized
    through this, so the desired-vs-actual diff ignores every other repo field GitHub returns
    (id, name, topics, the dozens of unrelated flags) and a missing field reads as ``False``
    (its API default) rather than raising. A semantic match reads as in-sync, not drift.
    """
    return {field: bool(repo_obj.get(field, False)) for field in MANAGED_MERGE_API_FIELDS}
