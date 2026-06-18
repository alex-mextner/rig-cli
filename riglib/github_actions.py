"""GitHub Actions permissions desired-state — the pure, side-effect-free core.

WHAT THIS IS. The §5 ``github:`` block provisions a repo's GitHub settings declaratively;
``github.actions`` owns the repo's GitHub-Actions permissions, which live on TWO endpoints:

  - ``PUT /repos/{o}/{r}/actions/permissions`` — whether Actions runs at all, and which actions
    are allowed (``all`` / ``local_only`` / ``selected``). Secure default: enabled, but only
    ``local_only`` + verified/marketplace + the explicit allow-list is conservative; rig's default
    is ``enabled=true, allowed_actions="all"`` (don't break existing workflows) but the knob
    exists so a security-conscious repo can lock it to ``local_only``.
  - ``PUT /repos/{o}/{r}/actions/permissions/workflow`` — the DEFAULT GITHUB_TOKEN permissions
    (``read``/``write``) and whether a workflow may approve/create PRs. Secure default: the token
    is READ-only by default (least privilege; a workflow that needs write declares it explicitly),
    and workflows may NOT approve PRs.

This module is the single source of truth for the DESIRED Actions settings: the secure-default
knobs, the two PUT request bodies, and the normalized desired-vs-actual comparison. The ``Action``
handler and the ``gh`` subprocess seams live in ``actions/runner.py`` and import from here;
``plan``/``state``/``drift`` import the constants here at module top.

WHY ITS OWN MODULE. ``runner`` imports ``Action`` from ``..plan``; keeping these PURE pieces here
(stdlib-only) lets ``plan``/``state`` import them at module top with no cycle. Mirrors
``github_ruleset.py``.

CAPABILITY DEGRADE. Setting the default-workflow-token permission to READ-only is harmless and
always allowed for a repo admin. A non-admin token gets HTTP 403 → a VISIBLE error/"could not
verify", never a silent green. These are repo settings, not a ruleset — a mis-set value at worst
restricts a workflow token, it can never lock a human out of merging.
"""

from __future__ import annotations

from typing import Any

# The canonical SECURE/SENSIBLE-DEFAULT Actions knobs. Secure defaults: Actions ENABLED (don't
# silently break a repo that relies on CI), but the GITHUB_TOKEN is READ-only by default (least
# privilege) and workflows may NOT approve PRs. `enabled`/`allowed_actions` govern the first
# endpoint; the other two govern the workflow-permissions endpoint. `enabled` here is the
# plan-gating meta-key handled by the plan builder — NOT in this table (which is body knobs only).
GITHUB_ACTIONS_DEFAULTS: dict[str, Any] = {
    "actions_enabled": True,
    "allowed_actions": "all",
    "default_workflow_permissions": "read",
    "can_approve_pull_request_reviews": False,
}

# The allowed values for the two enum knobs, so the body builder and the validator agree. A value
# outside these is rejected at config-validation time (fail-closed), so the body builder can trust
# its input.
ALLOWED_ACTIONS_VALUES: tuple[str, ...] = ("all", "local_only", "selected")
WORKFLOW_PERMISSION_VALUES: tuple[str, ...] = ("read", "write")


def build_permissions_body(opts: dict) -> dict[str, Any]:
    """The desired ``PUT .../actions/permissions`` body (enabled + allowed_actions).

    Shared by apply + drift. ``enabled`` is a hard bool; ``allowed_actions`` is only included when
    Actions are enabled (the API rejects ``allowed_actions`` alongside ``enabled=false``).
    """
    enabled = bool(opts.get("actions_enabled", GITHUB_ACTIONS_DEFAULTS["actions_enabled"]))
    body: dict[str, Any] = {"enabled": enabled}
    if enabled:
        body["allowed_actions"] = str(
            opts.get("allowed_actions", GITHUB_ACTIONS_DEFAULTS["allowed_actions"])
        )
    return body


def build_workflow_permissions_body(opts: dict) -> dict[str, Any]:
    """The desired ``PUT .../actions/permissions/workflow`` body (token perms + PR approval).

    Shared by apply + drift. ``default_workflow_permissions`` is the GITHUB_TOKEN's default scope
    (``read``/``write``); ``can_approve_pull_request_reviews`` is whether a workflow may approve a
    PR. Both default to the least-privilege value.
    """
    return {
        "default_workflow_permissions": str(
            opts.get(
                "default_workflow_permissions",
                GITHUB_ACTIONS_DEFAULTS["default_workflow_permissions"],
            )
        ),
        "can_approve_pull_request_reviews": bool(
            opts.get(
                "can_approve_pull_request_reviews",
                GITHUB_ACTIONS_DEFAULTS["can_approve_pull_request_reviews"],
            )
        ),
    }


def normalize_permissions(live: dict) -> dict[str, Any]:
    """The comparable shape of the live ``.../actions/permissions`` — only the managed fields.

    A repo with Actions disabled has no ``allowed_actions`` field; we read it as absent (compared
    only when enabled). A semantic match reads as in-sync, not drift.
    """
    enabled = bool(live.get("enabled", False))
    out: dict[str, Any] = {"enabled": enabled}
    if enabled:
        out["allowed_actions"] = str(live.get("allowed_actions", "all"))
    return out


def normalize_workflow_permissions(live: dict) -> dict[str, Any]:
    """The comparable shape of the live ``.../actions/permissions/workflow`` — managed fields only."""
    return {
        "default_workflow_permissions": str(live.get("default_workflow_permissions", "read")),
        "can_approve_pull_request_reviews": bool(
            live.get("can_approve_pull_request_reviews", False)
        ),
    }
