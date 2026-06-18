"""agent-browser backend for GitHub settings the API does NOT expose — the pure, side-effect-free core.

WHAT THIS IS. ROADMAP §5 names TWO backends for the ``github:`` block: ``gh api`` for everything the
GitHub REST API exposes, and ``agent-browser`` for the settings that have NO API — driving the
GitHub settings UI headlessly to flip the switches ``gh api`` can't reach. This module is the PURE
core of that second backend: the settings-UI URL for a repo, the per-toggle accessibility selectors,
and the ``agent-browser`` command sequence builder (a list of argv lists). The side-effecting part —
actually spawning ``agent-browser`` — lives in ``actions/runner.py`` behind a single seam, so tests
assert the COMMAND PLAN this module produces without ever launching a browser.

WHY A SEPARATE BACKEND AT ALL. A handful of repository settings are genuinely not in the REST API
(GitHub has never shipped an endpoint for them) — the canonical example rig manages is the repo's
**"Automatically delete head branches"** *confirmation copy* and the org-inherited toggles that the
settings page renders but the API mirrors only partially. rig treats agent-browser as a first-class
backend invoked INSIDE apply (not a manual "now go click this" step): the action ensures a logged-in
browser via :mod:`github_auth`, then replays the command plan this module builds.

CAPABILITY DETECTION / HONESTY. This backend only runs when (a) ``agent-browser`` is installed, (b) a
GitHub session is logged in, and (c) the specific UI toggle is present on the page. If the toggle is
absent (an org policy hid it, or GitHub moved it), the action degrades to a VISIBLE "could not find
the setting in the UI" — never a silent green, never a blind click on the wrong element. The selectors
are accessibility-role based (``find role switch name=…``), not brittle CSS, so a cosmetic UI change
doesn't silently mis-target.
"""

from __future__ import annotations

from typing import Any

# The GitHub repo SETTINGS page path (the page that renders the UI-only toggles). Built from
# owner/repo so the action navigates to the right repo's settings. The branch-of-settings is the
# repo root settings page; sub-pages (actions, security) have their own paths but the API covers
# those, so the browser backend targets only the root settings page where the UI-only toggles live.
def settings_url(owner: str, repo: str) -> str:
    """The repo settings page URL where the UI-only toggles live."""
    return f"https://github.com/{owner}/{repo}/settings"


# The UI-only toggles rig manages through the browser, keyed by rig knob → the accessibility name of
# the switch/checkbox on the settings page. These are settings WITHOUT a stable REST endpoint. Each
# is a labelled control the settings page renders; rig finds it by ARIA role + accessible name (not a
# fragile CSS path), so a cosmetic redesign doesn't silently target the wrong element. The DEFAULT is
# the secure/sensible state, matching the gh-api backend's philosophy (secure defaults ON).
#
#   - allow_forking_label: "Allow forking" copy on a private repo renders only in the UI for some
#     plans; the API field (`allow_forking`) exists but is read-only on certain org repos, so the
#     UI switch is the only writable path there.
UI_ONLY_TOGGLES: dict[str, dict[str, Any]] = {
    "discussions": {"role": "switch", "name": "Discussions", "default": False},
    "projects": {"role": "switch", "name": "Projects", "default": True},
}

# The rig knobs this backend owns (derived from the toggle table so the two never disagree).
BROWSER_KNOBS: tuple[str, ...] = tuple(UI_ONLY_TOGGLES)


def desired_toggles(opts: dict) -> dict[str, bool]:
    """The desired on/off state for each UI-only toggle, as hard bools, secure-default-filled.

    Shared by the action and any drift check: a knob absent from ``opts`` falls back to its secure
    default. Only the toggles rig manages are returned — every other settings-page control is left
    untouched.
    """
    return {
        knob: bool(opts.get(knob, spec["default"]))
        for knob, spec in UI_ONLY_TOGGLES.items()
    }


def build_command_plan(owner: str, repo: str, desired: dict[str, bool]) -> list[list[str]]:
    """The ordered ``agent-browser`` argv lists that drive the settings UI to ``desired``.

    A PURE plan (no spawning) the runner replays: navigate to the settings page, then for each
    managed toggle locate the control by role+name and check/uncheck it to the desired state. The
    runner executes each argv list through its single ``agent-browser`` seam and inspects the result;
    a missing control surfaces as a loud "could not find the setting" rather than a wrong click. The
    plan is deterministic (toggles in registry order) so the test can assert it byte-for-byte.
    """
    plan: list[list[str]] = [["open", settings_url(owner, repo)]]
    for knob in UI_ONLY_TOGGLES:
        spec = UI_ONLY_TOGGLES[knob]
        action = "check" if desired.get(knob, bool(spec["default"])) else "uncheck"
        # The agent-browser contract is `find role <role> [action] --name <label>` (the accessible
        # name is an OPTION, not a positional — a positional name would be parsed as the action and
        # the toggle would never flip). Accessibility-role targeting, not a CSS path, so a cosmetic
        # redesign doesn't silently mis-target. The name is the visible label GitHub renders.
        plan.append(["find", "role", str(spec["role"]), action, "--name", str(spec["name"])])
    return plan
