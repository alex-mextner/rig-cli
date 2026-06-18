"""Reconciled-area registry — the FULL set of things ``rig`` manages, for the status summary.

``rig status`` historically rendered only the items that WERE drifting, as one flat dump.
Because skills produce the most rows (one per skill, plus a harness-link row each), the output
read as "mostly skills" and every other area — agent-hooks, the git-hook dispatcher, CI gates,
the ship gate, MCP servers, AGENTS/CLAUDE symlinks, repo settings, harness auto-mode, tmux,
the model-freshness cron, the tg-ctl daemon — was either buried under the skill lines or, when
in sync, invisible entirely. A user could not answer "what does rig manage here, and where am
I out of sync?" at a glance.

This module is the SINGLE SOURCE OF TRUTH for the area list: each :class:`Area` names a stable
key, a human label, its owning layer (GLOBAL/REPO, mirroring :mod:`riglib.layers`), and the
drift/plan *categories* that roll up into it. The status renderer enumerates these areas and,
for each, reports in-sync vs drift counts under its heading — so the whole picture shows, not
just the noisy parts. Keep it exhaustive: an area present in the plan but absent here would be
silently uncounted (the summary would under-report what rig manages).

``ci`` is the one category that spans two areas: the CI workflow gates and the ``ship`` merge
gate are distinct user-facing concerns (the ROADMAP lists "CI gates" and "ship/`gh ship`"
separately), so they split by the action's ``slot`` rather than getting one lumped count.

Stdlib-only; no imports beyond the standard library and the sibling layer constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .layers import GLOBAL, REPO


@dataclass(frozen=True)
class Area:
    """One reconciled area for the status summary.

    ``categories`` are the drift/plan category strings (``riglib.plan.Action.category`` /
    ``riglib.drift.DriftItem.category``) that roll up into this area. ``ship_slot`` carries the
    ``ci`` split: ``True`` counts only ``slot == "ship"`` actions/items, ``False`` only the
    non-ship CI slots, ``None`` (the default) counts every action in ``categories`` regardless.
    """

    key: str
    label: str
    layer: str
    categories: tuple[str, ...]
    ship_slot: bool | None = None


# Order is the DISPLAY order within each layer: skills first (the historically-dominant area),
# then the rest of the GLOBAL machine-wide artifacts, then the REPO-local ones. Keep this list
# the authoritative enumeration of what rig reconciles; the status summary iterates it verbatim.
AREAS: tuple[Area, ...] = (
    # ── GLOBAL — machine-wide, from ~/.config/rig/config.yaml ──
    Area("skills", "skills", GLOBAL, ("skills",)),
    Area("agent_hooks", "agent-hooks (v1 descriptors)", GLOBAL, ("agent_hooks",)),
    Area("git_hooks", "git-hooks dispatcher", GLOBAL, ("git_hooks",)),
    Area("gitignore", "global gitignore excludes", GLOBAL, ("gitignore",)),
    Area("mcp", "MCP servers", GLOBAL, ("mcp",)),
    # the harness area spans both the auto/permission-mode write AND the cc-hook-bridge wiring —
    # both emit category="harness" (plan.py: apply_harness + register_hook_bridge), so the label
    # names both rather than undersell it as auto-mode only.
    Area("harness", "harness auto-mode + hook bridge", GLOBAL, ("harness",)),
    Area("permissions", "harness command allowlist", GLOBAL, ("permissions",)),
    Area("tmux", "tmux config", GLOBAL, ("tmux",)),
    Area("models", "model-freshness cron", GLOBAL, ("models",)),
    Area("tg_ctl", "tg-ctl inbound daemon", GLOBAL, ("tg_ctl",)),
    # ── REPO — this repository, from ./rig.yaml ──
    Area("ci", "CI gates", REPO, ("ci",), ship_slot=False),
    Area("ship", "ship / `gh ship` merge gate", REPO, ("ci",), ship_slot=True),
    Area("ship_delegator", "`gh ship` delegator (.claude/scripts/pr-ship.sh)", REPO, ("ship_delegator",)),
    Area("linters", "linter / formatter config files", REPO, ("linters",)),
    Area("agents_md", "AGENTS.md / CLAUDE.md symlinks", REPO, ("agents_md",)),
    Area("github", "repo settings (branch protection / GHAS / merge)", REPO, ("github",)),
)


def areas_for_layer(layer: str) -> tuple[Area, ...]:
    """The areas owned by ``layer`` (``GLOBAL``/``REPO``), in display order."""
    return tuple(a for a in AREAS if a.layer == layer)


def _action_in_ship_slot(action_options: dict[str, Any] | None) -> bool:
    """True when a ``ci`` plan action targets the ``ship`` slot (vs an ordinary workflow gate).

    Tolerates a ``None`` ``options`` (``Action.options`` defaults to a dict, so this is belt-and-
    suspenders for a hand-built action) by falling back to "no slot".
    """
    return (action_options or {}).get("slot") == "ship"


def area_matches_action(area: Area, category: str, options: dict[str, Any] | None) -> bool:
    """Does a plan action (its ``category`` + ``options``) roll up into ``area``?

    Honors the ``ci``/``ship`` split: an ``Area`` with ``ship_slot=True`` matches only ship-slot
    CI actions, ``ship_slot=False`` only non-ship CI actions, ``None`` matches any action in the
    area's categories. Used to bucket the resolved plan's actions per area for the in-sync count.
    """
    if category not in area.categories:
        return False
    if area.ship_slot is None:
        return True
    return _action_in_ship_slot(options) == area.ship_slot


def area_matches_drift(area: Area, category: str, item: str, direction: str = "missing") -> bool:
    """Does a drift item (its ``category`` + ``item`` + ``direction``) roll up into ``area``?

    Mirrors :func:`area_matches_action` for the drift side. The ship merge gate is a ``~/bin``
    script, so its config→disk drift (``missing``/``modified``) carries ``item == "ship"`` and the
    ship/CI split keys off that. A disk→config ``extra`` ci item, however, is a workflow FILENAME
    STEM under ``.github/workflows`` (``_extras_ci``) — an undeclared ``ship.yml`` would have
    ``item == "ship"`` yet is NOT the merge gate. So for the ``extra`` direction every ci item
    routes to the ordinary CI-gates area, never ship (the merge gate can never appear as an
    extra workflow). Drift items carry no ``options``, hence the item/direction keying here.
    """
    if category not in area.categories:
        return False
    if area.ship_slot is None:
        return True
    # disk→config extras are workflow files, never the ~/bin ship script → always the CI area.
    is_ship = direction != "extra" and item == "ship"
    return is_ship == area.ship_slot
