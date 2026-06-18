"""The canonical, COMPLETE JSON Schema for ``rig.yaml`` + the global config — one source.

What this is
------------
A declarative description of EVERY ``rig.yaml`` / ``~/.config/rig/config.yaml`` block: each
top-level key, its fixed sub-keys, their types/enums/defaults, and whether the block is *closed*
(an unknown key is rejected — ``additionalProperties: false``) or carries an *open* ``items:`` /
``fragments:`` map keyed by arbitrary catalog item names. :func:`json_schema` emits a Draft-07
document from it; :func:`schema_path_exists` answers "is this dotted key a known schema node" for
error messages.

Why a SEPARATE module from ``config.py`` and ``schema.py``
---------------------------------------------------------
- ``config.py`` is the runtime VALIDATOR (deep semantic checks: marker collisions, HH:MM ranges,
  catalog cross-refs). It stays the enforcement engine and now consults THIS module for the
  unknown-key check + the schema path on each error, so the two can never disagree on the key set.
- ``schema.py`` is the WIZARD option registry (a flat list of toggles + hints). It exposes only a
  curated subset (what a wizard can prompt for); this module is the EXHAUSTIVE shape (every key an
  editor should complete/validate). ``schema.json_schema()`` delegates here so there is ONE emitter.

The published artifact is ``schema/rig.schema.json`` (committed, referenced by editors). A sync
test (``tests/test_config_schema.py``) asserts the file equals :func:`json_schema` output, so the
generated schema, the on-disk file editors read, and ``config.validate``'s key set never drift.

Stdlib-only at import time (the AGENTS.md hard rule): no yaml/jsonschema here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── the published-schema identity (referenced by editors via `$schema`/`$id`) ─────────
SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
SCHEMA_ID = "https://github.com/alex-mextner/rig-cli/blob/main/schema/rig.schema.json"
# The relative path the generator writes to and the validator cites in errors. One constant so
# the CLI, the docs, and the sync test name the same file.
SCHEMA_REL_PATH = "schema/rig.schema.json"


# ── leaf + block descriptors (the declarative registry) ───────────────────────────────
@dataclass(frozen=True)
class Leaf:
    """One scalar/typed key in a block: its JSON type, optional enum, default, and one-line doc.

    ``type`` is a JSON-Schema type name (``boolean``/``string``/``integer``/``array``/``object``).
    ``enum`` pins the allowed string values; ``minimum`` an integer floor; ``items_type`` the
    element type of an ``array``. ``default`` is shown in the schema (editors surface it); ``None``
    means "no default advertised" (omitted from the emitted node).
    """

    type: str
    doc: str
    enum: tuple[str, ...] = ()
    default: Any = None
    minimum: int | None = None
    items_type: str | None = None

    def to_node(self) -> dict[str, Any]:
        node: dict[str, Any] = {"type": self.type, "description": self.doc}
        if self.enum:
            node["enum"] = list(self.enum)
        if self.default is not None:
            node["default"] = self.default
        if self.minimum is not None:
            node["minimum"] = self.minimum
        if self.type == "array" and self.items_type:
            node["items"] = {"type": self.items_type}
        return node


@dataclass(frozen=True)
class Block:
    """One config block (a top-level key, or a nested object inside one).

    ``leaves`` are the fixed, named keys. ``nested`` are sub-blocks (each itself a :class:`Block`).
    ``open_map`` names an arbitrary-keyed child map this block permits (``items`` for ci/mcp/
    agent_hooks/skills.by_type, ``fragments`` for git_hooks.dispatcher) — its keys are catalog item
    names, so the block stays open under that ONE key while every other key is still rejected.
    ``open_map_item`` optionally pins the SHAPE of each map VALUE: when set, the published schema
    models every item with that block's ``required``/``properties``/``enum`` (so an editor flags a
    missing ``content`` or a bad ``role``), matching the Python validator. When ``None`` (ci/mcp/
    agent_hooks — whose item shapes are catalog-defined and open by design) each item stays a
    permissive ``{"type": "object"}``. ``closed`` (default True) emits ``additionalProperties: false``
    so an unknown FIXED key fails; a block with an ``open_map`` carries an explicit
    ``additionalProperties`` allowing only the map. ``doc`` is the block's one-line description.
    """

    doc: str
    leaves: dict[str, Leaf] = field(default_factory=dict)
    nested: dict[str, Block] = field(default_factory=dict)
    open_map: str | None = None
    open_map_doc: str = ""
    open_map_item: Block | None = None
    open_map_item_required: tuple[str, ...] = ()
    closed: bool = True

    def child_keys(self) -> set[str]:
        keys = set(self.leaves) | set(self.nested)
        if self.open_map:
            keys.add(self.open_map)
        return keys

    def to_node(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for name, leaf in self.leaves.items():
            props[name] = leaf.to_node()
        for name, blk in self.nested.items():
            props[name] = blk.to_node()
        node: dict[str, Any] = {"type": "object", "description": self.doc, "properties": props}
        if self.open_map:
            # the open child map: an object whose keys are arbitrary item names. By default we don't
            # model each item's inner shape (catalog-defined, open by design) → a permissive object-
            # of-objects. When `open_map_item` IS set (linters), we model the item shape so the
            # published schema enforces required keys / enums exactly like the Python validator.
            if self.open_map_item is not None:
                item_node = self.open_map_item.to_node()
                if self.open_map_item_required:
                    item_node["required"] = list(self.open_map_item_required)
                additional: Any = item_node
            else:
                additional = {"type": "object"}
            props[self.open_map] = {
                "type": "object",
                "description": self.open_map_doc or "per-item overrides keyed by item name",
                "additionalProperties": additional,
            }
        node["additionalProperties"] = False if self.closed else True
        return node


# ── the registry — every rig.yaml block, mirroring config.validate's accepted key set ──
# Kept in lockstep with riglib/config.py's validators by tests/test_config_schema.py (which
# round-trips a config through both). Defaults mirror docs/config-schema.md.

_DEFAULTS_BLOCK = Block(
    doc="cross-category fallback targets + the on-conflict policy.",
    leaves={
        "skills_target": Leaf("string", "default skills install dir", default="~/.agents/skills"),
        "hooks_target": Leaf("string", "default agent-hooks dir", default="~/.claude/hooks"),
        "ci_target": Leaf("string", "default CI workflows dir", default=".github/workflows"),
        "mcp_target": Leaf("string", "default MCP config dir", default="~/.claude/mcp"),
        "on_conflict": Leaf(
            "string",
            "what apply does when a target already exists",
            enum=("skip", "overwrite", "backup"),
            default="backup",
        ),
    },
)

_SKILLS_BLOCK = Block(
    doc="advisory markdown rules copied into the agent skills dir (opt-out model).",
    leaves={
        "enabled": Leaf("boolean", "install skills at all", default=True),
        "target": Leaf("string", "where SKILL.md dirs are copied", default="~/.agents/skills"),
        "all": Leaf("boolean", "enable all skills (opt-out)", default=True),
        "harness_link": Leaf(
            "boolean",
            "also symlink each installed skill into the harness discovery dir",
            default=True,
        ),
        "harness_skill_dir": Leaf("string", "override the harness skill-discovery dir"),
    },
    nested={
        "universal": Block(
            doc="the universal skill set (opt-out via disable / opt-in via enable).",
            leaves={
                "all": Leaf("boolean", "enable all universal skills", default=True),
                "disable": Leaf("array", "universal skills to drop", items_type="string"),
                "enable": Leaf("array", "universal skills to add (with all:false)", items_type="string"),
            },
        ),
        "by_type": Block(
            doc="by-type skill bundles for the detected project type.",
            leaves={
                "enable": Leaf("array", "which by-type bundles to install whole", items_type="string"),
                "disable": Leaf("array", "by-type bundles to drop", items_type="string"),
            },
            open_map="items",
            open_map_doc="per-skill overrides keyed by `by-type/<kind>/<name>`",
        ),
    },
)

_AGENT_HOOKS_BLOCK = Block(
    doc="programmatic guards that block before a side effect (no-verify, secrets, raw merge).",
    leaves={
        "enabled": Leaf("boolean", "install the agent-hook guards", default=True),
        "target": Leaf("string", "where hook descriptors are written", default="~/.claude/hooks"),
        "target_kind": Leaf(
            "string", "logical-point → harness-event mapping", enum=("claude-code", "generic")
        ),
        "all": Leaf("boolean", "install all guard hooks", default=True),
    },
    open_map="items",
    open_map_doc="per-hook overrides (enabled / on_error) keyed by hook name",
)

_GIT_HOOKS_BLOCK = Block(
    doc="the global-hook dispatcher: your hooks run in every repo, even ones that hijack core.hooksPath.",
    nested={
        "dispatcher": Block(
            doc="the machine-wide global-hook dispatcher.",
            leaves={
                "enabled": Leaf("boolean", "install the global-hook dispatcher", default=False),
                "dir": Leaf("string", "drop-in fragments dir", default="~/.config/git/global-hooks.d"),
                "runner": Leaf("string", "the dispatcher script", default="~/.config/git/run-global-hooks"),
                "set_global_hooks_path": Leaf(
                    "boolean", "wire it as git's global core.hooksPath", default=True
                ),
                "install_local_retrofit_script": Leaf(
                    "boolean", "put install-local-hooks.sh on PATH", default=True
                ),
            },
            open_map="fragments",
            open_map_doc="per-fragment overrides keyed by fragment name (secret-scan, …)",
        ),
    },
)

_CI_BLOCK = Block(
    doc="vendor-neutral CI gates (secret-scan, codeql, dependency-review, ship, …).",
    leaves={
        "enabled": Leaf("boolean", "install CI gate workflows", default=True),
        "target": Leaf("string", "workflows dir, or `export-only`", default=".github/workflows"),
        "all": Leaf("boolean", "install every gate (vs per-item)", default=False),
    },
    open_map="items",
    open_map_doc="per-gate overrides (enabled / tier / variant / …) keyed by gate name",
)

_MCP_BLOCK = Block(
    doc="MCP registrations (review, code-search) — callable from any agent.",
    leaves={
        "enabled": Leaf("boolean", "register MCP servers", default=True),
        "target": Leaf("string", "MCP config dir, file, or `export-only`", default="~/.claude/mcp"),
    },
    open_map="items",
    open_map_doc="per-server overrides (command / server / enabled) keyed by server name",
)

_HARNESS_BLOCK = Block(
    doc="the agent harness's auto/permission mode — provisioned, not hand-toggled.",
    leaves={
        "enabled": Leaf("boolean", "provision the harness setting", default=True),
        "kind": Leaf(
            "string",
            "which harness to write (claude-code; opencode reserved)",
            enum=("claude-code", "opencode"),
            default="claude-code",
        ),
        "auto_mode": Leaf("boolean", "true → auto-accept tool calls; false → interactive", default=True),
        "mode": Leaf("string", "pin the exact permission value (overrides auto_mode map)"),
        "settings_path": Leaf("string", "the settings file to merge into", default=".claude/settings.json"),
    },
    nested={
        "hook_bridge": Block(
            doc="wire the agents-hooks/v1 → CC dispatcher into settings.json.",
            leaves={
                "enabled": Leaf("boolean", "wire the cc_hook_bridge dispatcher", default=True),
                "python": Leaf("string", "the interpreter the dispatcher runs under", default="python3"),
            },
        ),
    },
)

_PERMISSIONS_BLOCK = Block(
    doc="pre-allow our ecosystem CLIs + safe external dev tools in the harness allowlist.",
    leaves={
        "enabled": Leaf("boolean", "provision the command allowlist", default=True),
        "kind": Leaf(
            "string",
            "which harness's allowlist to provision",
            enum=("claude-code", "opencode"),
        ),
        "tools": Leaf("array", "command names to pre-allow (replaces the default set)", items_type="string"),
        "extra": Leaf("array", "command names to ADD on top of the set", items_type="string"),
        "disable": Leaf("array", "command names to drop from rig's desired set", items_type="string"),
        "settings_path": Leaf("string", "override the settings file (.json)"),
    },
)

_MODELS_BLOCK = Block(
    doc="a daily cron that runs the model-freshness checker and proposes version bumps.",
    leaves={
        "enabled": Leaf("boolean", "provision the daily checker schedule", default=True),
        "checker_path": Leaf("string", "the model_freshness.py the schedule runs"),
    },
    nested={
        "schedule": Block(
            doc="when the daily checker runs.",
            leaves={
                "time": Leaf("string", "daily run time, HH:MM 24-hour", default="12:00"),
                "label": Leaf("string", "the launchd Label / crontab sentinel identity"),
            },
        ),
    },
)

_AGENTS_MD_BLOCK = Block(
    doc="one canonical AGENTS.md + a CLAUDE.md symlink, so every harness reads the same instructions.",
    leaves={
        "enabled": Leaf("boolean", "provision the canonical + symlink pair", default=True),
        "symlink": Leaf("boolean", "alias opt-out — false equals enabled:false", default=True),
    },
)

_GITHUB_RULESET_BLOCK = Block(
    doc="a branch ruleset on the default branch (the modern branch-protection replacement).",
    leaves={
        "enabled": Leaf("boolean", "provision the ruleset", default=True),
        "name": Leaf("string", "the ruleset rig owns/reconciles", default="rig-managed"),
        "require_pull_request": Leaf("boolean", "require a PR to merge", default=True),
        "required_reviews": Leaf("integer", "required_approving_review_count", default=0, minimum=0),
        "block_force_push": Leaf("boolean", "emit the non_fast_forward rule", default=True),
        "restrict_deletion": Leaf("boolean", "emit the deletion rule", default=True),
        "require_linear_history": Leaf("boolean", "emit the required_linear_history rule", default=False),
        "require_signatures": Leaf("boolean", "emit the required_signatures rule", default=False),
        "required_status_checks": Leaf(
            "array", "contexts to require; empty emits no rule", items_type="string"
        ),
        "admin_bypass": Leaf("boolean", "add the repo Admin role to bypass_actors", default=True),
    },
)

_GITHUB_MERGE_BLOCK = Block(
    doc="the repo merge-button policy (squash-only, auto-delete head branch, allow-auto-merge) via PATCH /repos.",
    leaves={
        "enabled": Leaf("boolean", "provision the merge policy", default=True),
        "squash_merge": Leaf("boolean", "allow_squash_merge (the only merge model by default)", default=True),
        "merge_commit": Leaf("boolean", "allow_merge_commit", default=False),
        "rebase_merge": Leaf("boolean", "allow_rebase_merge", default=False),
        "delete_branch_on_merge": Leaf("boolean", "auto-delete the head branch on merge", default=True),
        "allow_auto_merge": Leaf("boolean", "allow a PR to auto-merge when its gate is green", default=True),
        "allow_update_branch": Leaf("boolean", "offer the 'Update branch' button", default=True),
    },
)

_GITHUB_GHAS_BLOCK = Block(
    doc="GitHub Advanced Security: dependency graph + vuln-alerts + Dependabot + secret-scanning + CodeQL.",
    leaves={
        "enabled": Leaf("boolean", "provision GHAS settings", default=True),
        "vulnerability_alerts": Leaf("boolean", "the vulnerability-alerts sub-resource (Dependabot alerts)", default=True),
        "automated_security_fixes": Leaf("boolean", "Dependabot security updates (automated-security-fixes)", default=True),
        "secret_scanning": Leaf("boolean", "security_and_analysis.secret_scanning", default=True),
        "secret_scanning_push_protection": Leaf("boolean", "secret-scanning push protection", default=True),
        "code_scanning_default_setup": Leaf("boolean", "CodeQL default-setup (configured)", default=True),
    },
)

_GITHUB_ACTIONS_BLOCK = Block(
    doc="GitHub Actions permissions: enabled + allowed_actions + the default GITHUB_TOKEN scope.",
    leaves={
        "enabled": Leaf("boolean", "provision Actions permissions", default=True),
        "actions_enabled": Leaf("boolean", "whether Actions runs at all", default=True),
        "allowed_actions": Leaf("string", "which actions are allowed", enum=("all", "local_only", "selected"), default="all"),
        "default_workflow_permissions": Leaf("string", "default GITHUB_TOKEN scope", enum=("read", "write"), default="read"),
        "can_approve_pull_request_reviews": Leaf("boolean", "may a workflow approve/create PRs", default=False),
    },
)

_GITHUB_BROWSER_BLOCK = Block(
    doc="settings the REST API does NOT expose, driven via agent-browser (gated off at apply unless RIG_GH_BROWSER=1).",
    leaves={
        "enabled": Leaf("boolean", "plan the agent-browser backend (status lists it)", default=True),
        "discussions": Leaf("boolean", "the Discussions UI-only toggle", default=False),
        "projects": Leaf("boolean", "the Projects UI-only toggle", default=True),
    },
)

_GITHUB_BLOCK = Block(
    doc="GitHub repository settings rig reconciles via gh api + agent-browser.",
    nested={
        "ruleset": _GITHUB_RULESET_BLOCK,
        "merge": _GITHUB_MERGE_BLOCK,
        "ghas": _GITHUB_GHAS_BLOCK,
        "actions": _GITHUB_ACTIONS_BLOCK,
        "browser": _GITHUB_BROWSER_BLOCK,
    },
)

_TMUX_BLOCK = Block(
    doc="rig-managed tmux configuration (persistence across reboots, plugin-init ordering).",
    leaves={
        "enabled": Leaf("boolean", "provision the rig-managed tmux config (opt-in)", default=False),
        "apply": Leaf("string", "apply mechanism", enum=("import", "block"), default="import"),
        "conf_path": Leaf("string", "the user's tmux config rig migrates", default="~/.tmux.conf"),
        "generated_dir": Leaf("string", "where rig writes its files", default="~/.config/rig/tmux"),
    },
    nested={
        "resurrect": Block(
            doc="tmux-resurrect knobs.",
            leaves={
                "processes": Leaf("array", "@resurrect-processes", items_type="string"),
                "capture_pane_contents": Leaf("boolean", "@resurrect-capture-pane-contents", default=True),
            },
        ),
        "continuum": Block(
            doc="tmux-continuum knobs.",
            leaves={
                "restore": Leaf("boolean", "@continuum-restore on", default=True),
                "boot": Leaf("boolean", "no-op (rig owns boot via launchd)", default=True),
                "save_interval": Leaf("integer", "@continuum-save-interval (minutes)", default=15, minimum=1),
            },
        ),
        "moshi": Block(doc="opt-in Moshi status-line tweaks.", leaves={"enabled": Leaf("boolean", "enable Moshi tweaks", default=False)}),
        "cc_restore": Block(doc="per-window Claude Code resume by session id.", leaves={"enabled": Leaf("boolean", "wire cc-save/cc-restore", default=True)}),
        "anti_sprawl": Block(
            doc="one canonical session (attach-or-create).",
            leaves={"enabled": Leaf("boolean", "install the attach-or-create entry", default=True), "session": Leaf("string", "the canonical session name", default="main")},
        ),
        "boot": Block(
            doc="a launchd agent that brings tmux up after a reboot.",
            leaves={"enabled": Leaf("boolean", "write + load the boot agent", default=True), "label": Leaf("string", "the launchd agent label", default="ai.hyperide.tmux-boot")},
        ),
        "login_shell": Block(
            doc="restored panes are login shells (source ~/.zprofile/PATH).",
            leaves={"enabled": Leaf("boolean", "set a login-shell default-command", default=True), "shell": Leaf("string", "login shell path ('' → resolve $SHELL)", default="")},
        ),
    },
)

_GITIGNORE_BLOCK = Block(
    doc="a rig-managed block in git's global core.excludesfile (ignores harness worktrees machine-wide).",
    leaves={
        "enabled": Leaf("boolean", "provision the managed block", default=True),
        "entries": Leaf("array", "the ignored paths inside the managed block", items_type="string"),
        "excludesfile": Leaf("string", "force a specific file instead of honoring core.excludesfile"),
    },
)

_SHIP_DELEGATOR_BLOCK = Block(
    doc="a per-repo .claude/scripts/pr-ship.sh delegator so `gh ship` works in this repo (ignored in .git/info/exclude).",
    leaves={
        "enabled": Leaf("boolean", "provision the per-repo gh-ship delegator", default=True),
    },
)

# The SHAPE of one `linters.items.<label>` entry, modeled so the published schema enforces the same
# required keys + `role` enum the Python validator does (an editor flags a missing `content` / a bad
# `role` before `rig apply` ever runs). `closed=True` → an unknown per-item key is rejected too.
_LINTERS_ITEM_BLOCK = Block(
    doc="one linter/formatter config file rig writes/reconciles.",
    leaves={
        "tool": Leaf("string", "the tool name (informational; drives the status/log label)"),
        "role": Leaf("string", "linter | formatter (status label only)", enum=("linter", "formatter"), default="linter"),
        "path": Leaf("string", "repo-relative path of the config file (no leading '/' or '..')"),
        "content": Leaf("string", "the exact bytes rig writes/reconciles"),
        "enabled": Leaf("boolean", "provision this one file", default=True),
    },
)

_LINTERS_BLOCK = Block(
    doc="per-repo linter + formatter config files rig provisions/reconciles (tool + content per repo).",
    leaves={
        "enabled": Leaf("boolean", "provision the declared linter/formatter config files", default=True),
    },
    open_map="items",
    open_map_doc=(
        "the config files keyed by a label; each is `{ tool, role, path, content, enabled }` "
        "(e.g. an `oxfmt` formatter writing `.oxfmtrc.jsonc`, a `ruff` linter writing `ruff.toml`)."
    ),
    open_map_item=_LINTERS_ITEM_BLOCK,
    open_map_item_required=("tool", "path", "content"),
)

_TG_CTL_BLOCK = Block(
    doc="the tg-ctl inbound daemon auto-started as a per-machine boot LaunchAgent (macOS).",
    leaves={
        "enabled": Leaf("boolean", "provision the tg-ctl LaunchAgent", default=True),
        "boot": Leaf("boolean", "write + load the boot agent", default=True),
        "label": Leaf("string", "launchd Label / plist filename stem", default="ai.hyperide.tg-ctl"),
        "bun_path": Leaf("string", "the bun binary"),
        "tg_ctl_path": Leaf("string", "the tg-ctl Bun script", default="~/.files/bin/tg-ctl"),
        "config_dir": Leaf("string", "tg-cli config + launchd logs dir", default="~/.config/tg-cli"),
    },
)


# The top-level shape: the scalar top keys + every block, in the order config.py validates them.
_TOP_LEAVES: dict[str, Leaf] = {
    "version": Leaf("integer", "schema version (only 1 supported)", default=1, minimum=1),
    "agent_tools_source": Leaf("string", "the agent-tools checkout to apply FROM (default: auto-detect)"),
}

BLOCKS: dict[str, Block] = {
    "defaults": _DEFAULTS_BLOCK,
    "skills": _SKILLS_BLOCK,
    "agent_hooks": _AGENT_HOOKS_BLOCK,
    "git_hooks": _GIT_HOOKS_BLOCK,
    "ci": _CI_BLOCK,
    "mcp": _MCP_BLOCK,
    "harness": _HARNESS_BLOCK,
    "permissions": _PERMISSIONS_BLOCK,
    "models": _MODELS_BLOCK,
    "agents_md": _AGENTS_MD_BLOCK,
    "github": _GITHUB_BLOCK,
    "tmux": _TMUX_BLOCK,
    "gitignore": _GITIGNORE_BLOCK,
    "tg_ctl": _TG_CTL_BLOCK,
    "ship_delegator": _SHIP_DELEGATOR_BLOCK,
    "linters": _LINTERS_BLOCK,
}

# Every valid TOP-LEVEL key (scalars + blocks). Mirrors config._VALID_TOP_KEYS; the sync test
# asserts the two are identical so the schema and the validator can never disagree.
TOP_LEVEL_KEYS: set[str] = set(_TOP_LEAVES) | set(BLOCKS)


def json_schema() -> dict[str, Any]:
    """Emit the COMPLETE Draft-07 JSON Schema for rig.yaml + the global config (one source).

    Strict by construction: every block is ``additionalProperties: false`` except where it carries
    an open ``items``/``fragments`` map (catalog item names), so an unknown FIXED key is a schema
    violation an editor flags. ``scope`` is whitelisted at the top level only (a tolerated legacy
    key the loader drops). This is what ``schema/rig.schema.json`` is generated from.
    """
    props: dict[str, Any] = {name: leaf.to_node() for name, leaf in _TOP_LEAVES.items()}
    for name, block in BLOCKS.items():
        props[name] = block.to_node()
    # `scope` is a removed legacy key the loader still TOLERATES (drops it) — whitelist it so a
    # not-yet-cleaned committed file doesn't trip an editor, but it is intentionally undocumented.
    props["scope"] = {"type": "string", "description": "removed legacy key (tolerated, ignored)", "deprecated": True}
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": "rig.yaml — rig-cli declarative config",
        "description": "The committed source of truth for a repo's setup (and ~/.config/rig/config.yaml). "
        "See docs/config-schema.md for the human-readable reference.",
        "type": "object",
        "additionalProperties": False,
        "properties": props,
    }


def schema_pointer_for(dotted: str) -> str | None:
    """A JSON pointer into ``schema/rig.schema.json`` for ``dotted``, or ``None`` if it can't resolve.

    The path is walked through the block registry, prefixing ``/properties/`` between resolvable
    nodes. Walking STOPS at an open ``items``/``fragments`` map — segments past it (catalog item
    names like ``secret-scan``) are NOT schema nodes, so the pointer would dangle. We return a
    pointer to the open-map node itself in that case (the deepest node that actually exists), which
    is honest: the editor jumps to ``…/items``, not a non-existent ``…/items/secret-scan``. An
    unknown top block → ``None`` (the caller then shows the dotted path without a dangling pointer).
    """
    parts = dotted.split(".")
    top = parts[0]
    if top in _TOP_LEAVES:
        return f"/properties/{top}"  # a top-level scalar leaf
    block = BLOCKS.get(top)
    if block is None:
        return None
    pointer = [top]
    for part in parts[1:]:
        if part in block.leaves:
            pointer.append(part)
            return "/properties/" + "/properties/".join(pointer)  # a leaf — done
        if block.open_map and part == block.open_map:
            pointer.append(part)
            return "/properties/" + "/properties/".join(pointer)  # the open map node itself
        if part in block.nested:
            pointer.append(part)
            block = block.nested[part]
            continue
        if block.open_map:
            # we're already inside an open map (a prior segment was the map key) — the deepest
            # resolvable node is the map; stop here rather than emit item-name segments.
            pointer.append(block.open_map)
            return "/properties/" + "/properties/".join(pointer)
        # an unknown fixed segment (the typo itself): point at the PARENT block, which exists.
        return "/properties/" + "/properties/".join(pointer) if len(pointer) > 1 else f"/properties/{top}"
    return "/properties/" + "/properties/".join(pointer)


def block_child_keys(block_path: str) -> set[str] | None:
    """The valid child keys of a block addressed by dotted path (``github.ruleset`` → its keys).

    Returns ``None`` when the path does not name a known block (so the caller can distinguish
    "unknown block" from "known block, here are its keys"). Used by the validator to build the
    unknown-key error's "expected one of …" hint from the SAME registry the schema is emitted from.
    """
    if not block_path:
        return None
    parts = block_path.split(".")
    top = parts[0]
    block = BLOCKS.get(top)
    if block is None:
        return None
    for part in parts[1:]:
        block = block.nested.get(part)
        if block is None:
            return None
    return block.child_keys()


def schema_file_path() -> Path:
    """Absolute path to the committed ``schema/rig.schema.json`` (a checked-in repo artifact).

    Resolved from THIS module's location (``riglib/config_schema.py`` → repo root → ``schema/``).
    The canonical install is a SYMLINK to the checked-out repo (install.sh), where ``schema/`` always
    sits next to ``riglib/`` — so the file is present and `--check`/`--write` work. NOT a per-repo
    path: the schema is rig's own, generated once and committed in the rig-cli repo. (Runtime
    ``config.validate`` is registry-based and never reads this file, so a packaging gap on a bare
    ``pip install`` only affects ``rig schema --check/--write``, never validation/apply.)
    """
    return Path(__file__).resolve().parent.parent / SCHEMA_REL_PATH


def render_schema_json() -> str:
    """The canonical on-disk serialization of the schema: 2-space indent, trailing newline.

    One renderer so :func:`write_schema_file`, ``rig schema``, and the sync test all produce/compare
    byte-identical text (a mismatch is what the sync test catches). ``sort_keys=False`` keeps the
    blocks in registry order (readable), ``ensure_ascii=False`` keeps the unicode ellipses intact.
    """
    return json.dumps(json_schema(), indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def write_schema_file() -> Path:
    """(Re)generate ``schema/rig.schema.json`` from the registry. Returns the written path."""
    path = schema_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema_json(), encoding="utf-8")
    return path


def schema_file_in_sync() -> bool:
    """True iff the committed schema file exists and equals the freshly generated text.

    The drift guard: a registry change that forgets to regenerate the file (or a hand-edit to the
    file) makes this False, so ``rig schema --check`` and the sync test fail loudly.
    """
    path = schema_file_path()
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == render_schema_json()


__all__ = [
    "SCHEMA_DIALECT",
    "SCHEMA_ID",
    "SCHEMA_REL_PATH",
    "Leaf",
    "Block",
    "BLOCKS",
    "TOP_LEVEL_KEYS",
    "json_schema",
    "block_child_keys",
    "schema_pointer_for",
    "schema_file_path",
    "render_schema_json",
    "write_schema_file",
    "schema_file_in_sync",
]
