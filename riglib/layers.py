"""Config-layer classification — which drift category belongs to which layer.

`rig status` mixes two kinds of state:

- **GLOBAL** — machine-wide artifacts a developer carries across every repo, declared in
  ``~/.config/rig/config.yaml``: the installed skills (``~/.agents/skills`` + the harness
  discovery symlinks), agent-hooks, the global git-hook dispatcher, the harness auto/permission
  mode, the model-freshness schedule, and rig-managed tmux config.
- **REPO** — this specific repository's artifacts, declared in its committed ``./rig.yaml``:
  the CI workflows under ``.github/``, the AGENTS.md/CLAUDE.md symlinks, and the GitHub ruleset.

Grouping drift by layer (and naming WHICH config file declares each item) is what makes
``rig status`` legible: a global-skills drift is not the repo's problem, and a non-git dir has
no repo layer at all. This module is the single source of truth for that classification, so the
status renderer and any future consumer agree.

Stdlib-only; no imports beyond the standard library.
"""

from __future__ import annotations

GLOBAL = "GLOBAL"
REPO = "REPO"

# Each drift/action category → the config layer that OWNS it. Keep this exhaustive: a category
# missing here would silently render under the wrong/no heading. The tmux agent owns the tmux
# rows in drift.py; the classification of "tmux" as GLOBAL lives here (a single dict entry) so
# this file does not collide with the tmux provisioning work.
_CATEGORY_LAYER = {
    # GLOBAL — machine-wide, from ~/.config/rig/config.yaml
    "skills": GLOBAL,
    "agent_hooks": GLOBAL,
    "mcp": GLOBAL,
    "harness": GLOBAL,
    "models": GLOBAL,
    "git_hooks": GLOBAL,
    "tmux": GLOBAL,
    # REPO — this repo, from ./rig.yaml
    "ci": REPO,
    "agents_md": REPO,
    "github": REPO,
}


def layer_for_category(category: str) -> str:
    """The owning layer (``GLOBAL``/``REPO``) for a drift/action category.

    Unknown categories default to ``GLOBAL`` (the conservative choice — a machine-wide artifact
    is the safer assumption than claiming a repo declares something it may not), but every known
    category is mapped explicitly above so this default is only a forward-compat guard.
    """
    return _CATEGORY_LAYER.get(category, GLOBAL)
