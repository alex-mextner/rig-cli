"""The canonical, COMPLETE JSON Schema for ``rig.yaml`` + the global config — one source.

What this is
------------
A declarative description of EVERY ``rig.yaml`` / ``~/.config/rig/config.yaml`` block: each
top-level key, its fixed sub-keys, their types/enums/defaults, and whether the block is *closed*
(an unknown key is rejected — ``additionalProperties: false``) or carries one named open map such
as ``items:``, ``fragments:``, or ``jobs:``. :func:`json_schema` emits a Draft-07 document from it;
:func:`schema_pointer_for` answers "where is this dotted key in the generated schema?" for error
messages.

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

from .harness_skills import (
    HARNESS_INSTRUCTION_FILES,
    HARNESS_NATIVE_SKILLS,
    HARNESS_SKILL_DIR_KINDS,
)
from .project_tools import HAFT_WORKFLOW_MODES

# Every harness kind rig provisions skill/instruction discovery for, listed skills-dir kinds first
# (claude-code, codex) then native-discovery (opencode) then instruction-file kinds (pi,
# commandcode) — a stable, readable order for the published JSON-schema enum. Sourced from
# :mod:`riglib.harness_skills` so the schema enum can never drift from what ``config.validate``
# accepts.
_HARNESS_KIND_ENUM: tuple[str, ...] = tuple(
    dict.fromkeys((*HARNESS_SKILL_DIR_KINDS, *HARNESS_NATIVE_SKILLS, *HARNESS_INSTRUCTION_FILES))
)

# ── the published-schema identity (referenced by editors via `$schema`/`$id`) ─────────
SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
SCHEMA_ID = "https://github.com/alex-mextner/rig-cli/blob/main/schema/rig.schema.json"
# The relative path the generator writes to and the validator cites in errors. One constant so
# the CLI, the docs, and the sync test name the same file.
SCHEMA_REL_PATH = "schema/rig.schema.json"
PERMISSION_RULE_JSON_PATTERN = r"^(?!.*[\r\n])[A-Za-z0-9_*.-]+(\(.+\))?$"
# Mirrors riglib.tmux's `_pane_titles_format_is_safe` char/substring blocklist EXACTLY (a test —
# `test_pane_titles_format_python_check_matches_json_schema_pattern` — runs a shared corpus
# through both and asserts identical verdicts, so the two can't independently drift): `"`, `\`,
# `$`, `#(` (tmux's shell-exec format token), true Unicode-category-Cc control characters (ASCII
# 0x00-0x1F/0x7F + the C1 range 0x80-0x9F) except a bare tab, and U+2028/U+2029
# (LINE/PARAGRAPH SEPARATOR). Deliberately narrower than "any non-printable character" — that
# would also reject ordinary Powerline/Nerd-Font glyphs (category Co/Cf) that are common in a
# real pane-border-format and can't corrupt a double-quoted tmux value. Kept as a regex here
# (rather than importing tmux.py's Python check) so the published JSON schema can express the
# SAME rule for editors/CI validating rig.yaml offline, without a runtime import.
TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN = r'["\\$]|#\(|[\x00-\x08\x0a-\x1f\x7f-\x9f  ]'


# ── leaf + block descriptors (the declarative registry) ───────────────────────────────
@dataclass(frozen=True)
class Leaf:
    """One scalar/typed key in a block: its JSON type, optional enum, default, and one-line doc.

    ``type`` is a JSON-Schema type name (``boolean``/``string``/``integer``/``array``/``object``),
    or a tuple of type names for nullable/union leaves.
    ``enum`` pins the allowed string values; ``minimum``/``maximum`` are integer bounds (applied
    to the leaf itself, or to each array element when ``type`` is ``array``); ``items_type`` the
    element type of an ``array``; ``items_enum`` pins allowed array entries; ``items_pattern``
    constrains string array entries;
    ``additional_properties_type`` models a string-keyed object map.
    ``not_pattern`` (scalar ``string`` leaves only) rejects any value that MATCHES the given
    regex anywhere — modeled as a ``"not": {"pattern": …}`` sub-schema — for a value whose
    validity is "must not contain X" rather than "must match a shape" (``enum``/``pattern``
    model the latter; a positive pattern can't express a character-blocklist cleanly).
    ``default`` is shown in the schema (editors surface it); ``None`` means "no default
    advertised" (omitted from the emitted node).
    """

    type: str | tuple[str, ...]
    doc: str
    enum: tuple[Any, ...] = ()
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    items_type: str | None = None
    items_enum: tuple[str, ...] = ()
    items_pattern: str | None = None
    additional_properties_type: str | None = None
    not_pattern: str | None = None

    def to_node(self) -> dict[str, Any]:
        node_type: Any = list(self.type) if isinstance(self.type, tuple) else self.type
        node: dict[str, Any] = {"type": node_type, "description": self.doc}
        if self.enum:
            node["enum"] = list(self.enum)
        if self.default is not None:
            node["default"] = self.default
        if self.minimum is not None and self.type != "array":
            node["minimum"] = self.minimum
        if self.maximum is not None and self.type != "array":
            node["maximum"] = self.maximum
        if self.type == "array" and self.items_type:
            items: dict[str, Any] = {"type": self.items_type}
            if self.minimum is not None:
                items["minimum"] = self.minimum
            if self.maximum is not None:
                items["maximum"] = self.maximum
            if self.items_enum:
                items["enum"] = list(self.items_enum)
            if self.items_pattern:
                items["pattern"] = self.items_pattern
            node["items"] = items
        if self.type == "object" and self.additional_properties_type:
            node["additionalProperties"] = {"type": self.additional_properties_type}
        if self.type == "string" and self.not_pattern:
            node["not"] = {"pattern": self.not_pattern}
        return node


@dataclass(frozen=True)
class Block:
    """One config block (a top-level key, or a nested object inside one).

    ``leaves`` are the fixed, named keys. ``nested`` are sub-blocks (each itself a :class:`Block`).
    ``open_map`` names an arbitrary-keyed child map this block permits (``items`` for ci/mcp/
    agent_hooks/skills.by_type, ``fragments`` for git_hooks.dispatcher, ``jobs`` for dev.e2e), so
    the block stays open under that ONE key while every other key is still rejected.
    ``open_map_item`` optionally pins the SHAPE of each map VALUE: when set, the published schema
    models every item with that block's ``required``/``properties``/``enum`` (so an editor flags a
    missing ``content`` or a bad ``role``), matching the Python validator. When ``None`` (ci/mcp/
    agent_hooks — whose item shapes are catalog-defined and open by design) each item stays a
    permissive ``{"type": "object"}``. ``closed`` (default True) emits ``additionalProperties: false``
    so an unknown FIXED key fails; a block with an ``open_map`` carries an explicit
    ``additionalProperties`` allowing only the map. ``additional_properties`` is for a top-level
    dictionary whose own keys are the user-defined entries (``scripts``). ``doc`` is the block's
    one-line description.
    """

    doc: str
    leaves: dict[str, Leaf] = field(default_factory=dict)
    nested: dict[str, Block] = field(default_factory=dict)
    open_map: str | None = None
    open_map_doc: str = ""
    open_map_item: Block | None = None
    open_map_item_required: tuple[str, ...] = ()
    additional_properties: Any | None = None
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
            # the open child map: an object whose keys are arbitrary names. By default we don't
            # model each item's inner shape (catalog-defined, open by design) → a permissive object-
            # of-objects. When `open_map_item` IS set, we model the item shape so the
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
        if self.additional_properties is not None:
            node["additionalProperties"] = self.additional_properties
        else:
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
        "by_stack": Block(
            doc="by-stack skills auto-selected by the repo's declared `stack` (hierarchical "
            "prefix match); tune with disable / per-item overrides.",
            leaves={
                "disable": Leaf(
                    "array",
                    "by-stack skill item names (`by-stack/<l1>/<lang>[/<fw>]/<name>`) to drop "
                    "even though the declared stack would select them",
                    items_type="string",
                ),
            },
            open_map="items",
            open_map_doc="per-skill overrides keyed by `by-stack/<l1>/<lang>[/<fw>]/<name>` "
            "(`enabled: true` force-adds an off-stack skill; `enabled: false` drops one)",
        ),
    },
)

_AGENT_HOOKS_BLOCK = Block(
    doc="programmatic guards that block before a side effect (no-verify, secrets, raw merge).",
    leaves={
        "enabled": Leaf("boolean", "install the agent-hook guards", default=True),
        "target": Leaf("string", "where hook descriptors are written", default="~/.claude/hooks"),
        "target_kind": Leaf(
            "string",
            "legacy ignored scaffold key; accepted for old configs but not used for rendering",
            enum=("claude-code", "generic"),
        ),
        "all": Leaf("boolean", "install all guard hooks", default=True),
        # Two RUNTIME behaviour knobs read BY the installed hooks from this committed rig.yaml
        # (rig apply does not consume them — the hook scripts parse agent_hooks.<key> at fire
        # time). They live here so the strict validator/schema accept them per-repo.
        "worktree_only": Leaf(
            "boolean",
            "enforce the worktree-only workflow: gates TWO hooks. worktree-only-writes DENIES an "
            "Edit/Write while the checkout sits on the default branch (main/master); "
            "pin-primary-worktree DENIES a git checkout/switch that would move the repo's PRIMARY "
            "worktree onto anything but the default branch. Opt-IN, default OFF — a repo that "
            "works directly on main (e.g. 3d-cli) leaves it off and is never blocked. No "
            "self-service env bypass — each hook has its OWN hatch var: a deliberate one-off "
            "Edit/Write on main is requested via "
            "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES=\"<justification>\"; a one-off git checkout/"
            "switch in the primary checkout via "
            "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE=\"<justification>\" (both: tg approval, "
            "deny-by-default; bare 1 rejected). (Alex tg#5742, tg#6462/tg#6477.)",
            default=False,
        ),
        "orchestrator_only": Leaf(
            "boolean",
            "keep the orchestrator thin: the orchestrator-stays-thin hook warns on the first "
            "inline implementation Bash / code Edit by the main thread (delegate to a subagent), "
            "then blocks a repeat within its TTL. Read-only inspection (git status/ls/cat/grep/"
            "find, git worktree list) is never gated; tg/review are sanctioned orchestration, also "
            "never gated. ALL gh is delegated to a subagent too — gh ship, gh pr list/view/checks, "
            "gh run, gh api included. Opt-OUT, default ON — set false to exempt a repo that "
            "legitimately works inline (e.g. 3d-cli). No self-service env bypass; a one-off is "
            "requested via RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN=\"<justification>\" (tg "
            "approval, deny-by-default; bare 1 rejected). (Alex tg#5743, tg#7103.)",
            default=True,
        ),
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

_MCP_ITEM_BLOCK = Block(
    doc="one MCP server registration override.",
    leaves={
        "enabled": Leaf("boolean", "register this MCP server", default=True),
        "server": Leaf("string", "server name written under mcpServers"),
        "command": Leaf("string", "server executable, or legacy command string when args is omitted"),
        "args": Leaf("array", "argv passed to command exactly", items_type="string"),
        "env": Leaf(
            "object",
            "environment variables for this server",
            additional_properties_type="string",
        ),
    },
)

_MCP_BLOCK = Block(
    doc="MCP registrations (review, code-search) — callable from any agent.",
    leaves={
        "enabled": Leaf("boolean", "register MCP servers", default=True),
        "target": Leaf("string", "MCP config dir, file, or `export-only`", default="~/.claude/mcp"),
    },
    open_map="items",
    open_map_doc="per-server overrides (command / args / env / server / enabled) keyed by server name",
    open_map_item=_MCP_ITEM_BLOCK,
)

_HARNESS_BLOCK = Block(
    doc="the agent harness's auto/permission mode — provisioned, not hand-toggled.",
    leaves={
        "enabled": Leaf("boolean", "provision the harness setting", default=True),
        "kind": Leaf(
            "string",
            "which harness to provision (skills-dir: claude-code/codex; native-discovery: opencode; "
            "instruction-file: pi/commandcode; codex is also instruction-file via AGENTS.md). "
            "The auto/permission-MODE write is "
            "claude-code-only today; other kinds still get their skill discovery provisioned.",
            enum=_HARNESS_KIND_ENUM,
            default="claude-code",
        ),
        "kinds": Leaf(
            "array",
            "additional harnesses to provision alongside harness.kind. Use this when one machine "
            "runs multiple harnesses: the primary kind keeps its auto-mode/settings_path behavior, "
            "while additional kinds get skill discovery, agent-hook descriptors, and any supported "
            "hook bridge. If agent_hooks.target pins descriptors to one explicit target, supported "
            "bridges are registered with a descriptor-dir override.",
            items_type="string",
            items_enum=_HARNESS_KIND_ENUM,
        ),
        "auto_mode": Leaf("boolean", "true → auto-accept tool calls; false → interactive", default=True),
        "self_merge": Leaf(
            "boolean",
            "true → add the ship allow rules (Bash(gh ship:*), Bash(*/pr-ship.sh:*), Bash(*/ship.sh:*)) "
            "to permissions.allow so the auto-mode Bash gate stops vetoing gh ship, PLUS the "
            "natural-language carve-out to autoMode.allow (clears the Merge-Without-Review + "
            "Self-Approval soft blocks for the agent's OWN PRs); auto-mode only; the Bash(gh pr merge:*) "
            "deny and every other classifier rule (incl. the anti-exfil hard rule) stay",
            default=True,
        ),
        "mode": Leaf("string", "pin the exact permission value (overrides auto_mode map)"),
        "settings_path": Leaf(
            "string",
            "override the per-harness settings/config/plugin path; absent means rig uses the harness-specific default",
        ),
    },
    nested={
        "hook_bridge": Block(
            doc="wire the agents-hooks/v1 dispatcher into the supported harness config.",
            leaves={
                "enabled": Leaf("boolean", "wire the supported harness hook-bridge dispatcher", default=True),
                "python": Leaf("string", "the interpreter the dispatcher runs under", default="python3"),
            },
        ),
    },
)

_PERMISSIONS_BLOCK = Block(
    doc="reconcile the harness permissions layer: the command allowlist + the deny/ask rule baselines.",
    leaves={
        "enabled": Leaf("boolean", "provision the harness permissions layer", default=True),
        "kind": Leaf(
            ("string", "null"),
            "which harness's permissions to provision; null/absent means fan out to supported "
            "harness.kind plus harness.kinds",
            enum=("claude-code", "opencode", None),
        ),
        "tools": Leaf("array", "command names to pre-allow (replaces the default set)", items_type="string"),
        "extra": Leaf("array", "command names to ADD on top of the set", items_type="string"),
        "disable": Leaf("array", "command names to drop from rig's desired set", items_type="string"),
        "allow": Leaf(
            "array",
            "raw permission-rule entries asserted present in the allow list, on TOP of the tool-derived ones",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "deny": Leaf(
            "array",
            "permission-rule entries asserted present in the deny list (REPLACES the baked baseline)",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "ask": Leaf(
            "array",
            "permission-rule entries asserted present in the ask list (REPLACES the baked baseline)",
            items_type="string",
            items_pattern=PERMISSION_RULE_JSON_PATTERN,
        ),
        "settings_path": Leaf("string", "override the settings file (.json)"),
    },
)

_MODE_BLOCK = Block(
    doc="machine-wide operating mode; `autonomous` provisions review/fix, quorum, escalation, and parallelism policy.",
    leaves={
        "name": Leaf(
            "string",
            "operating mode for agent sessions",
            enum=("standard", "autonomous"),
            default="standard",
        ),
    },
    nested={
        "autonomous": Block(
            doc="policy knobs active when mode.name is autonomous.",
            nested={
                "review_fix": Block(
                    doc="review/fix loop policy.",
                    leaves={
                        "enabled": Leaf("boolean", "run review/fix iterations", default=True),
                        "max_iterations": Leaf(
                            "integer",
                            "maximum review/fix iterations before escalation or budget stop",
                            default=5,
                            minimum=1,
                        ),
                        "until": Leaf(
                            "string",
                            "loop stop condition",
                            enum=("clean", "budget", "manual"),
                            default="clean",
                        ),
                    },
                ),
                "decisions": Block(
                    doc="decision-making policy before asking the user.",
                    nested={
                        "review_quorum": Block(
                            doc="multi-model quorum required for decisions.",
                            leaves={
                                "enabled": Leaf("boolean", "require review quorum for decisions", default=True),
                                "min_iterations": Leaf(
                                    "integer",
                                    "minimum recorded review iterations",
                                    default=2,
                                    minimum=1,
                                ),
                                "min_models": Leaf(
                                    "integer",
                                    "minimum distinct models in the quorum",
                                    default=3,
                                    minimum=2,
                                ),
                            },
                        ),
                    },
                ),
                "escalation": Block(
                    doc="when and how an agent may escalate to the user.",
                    leaves={
                        "framework_skill": Leaf(
                            "string",
                            "skill that defines escalation message behavior",
                            default="decision-request-discipline",
                        ),
                        "require_parallel_worktree_comparison": Leaf(
                            "boolean",
                            "compare independent worktree attempts before escalation",
                            default=True,
                        ),
                    },
                ),
                "parallel_worktree_comparison": Block(
                    doc="parallel worktree comparison before escalation.",
                    leaves={
                        "enabled": Leaf("boolean", "run independent worktree comparisons", default=True),
                        "candidates": Leaf(
                            "integer",
                            "number of independent candidate implementations to compare",
                            default=2,
                            minimum=2,
                        ),
                    },
                ),
                "development_tools": Block(
                    doc="extra allowlisted development-tool permission flows for autonomous mode.",
                    leaves={
                        "allow": Leaf(
                            "array",
                            "raw permission allow rules to add while in autonomous mode",
                            default=["Bash(dev:*)", "Bash(review:*)", "Bash(task:*)"],
                            items_type="string",
                            items_pattern=PERMISSION_RULE_JSON_PATTERN,
                        ),
                    },
                ),
                "parallelism": Block(
                    doc="limit-aware parallelism caps.",
                    leaves={
                        "max_agents": Leaf("integer", "maximum concurrent agents", default=4, minimum=1),
                        "max_worktrees": Leaf("integer", "maximum concurrent worktrees", default=4, minimum=1),
                        "reserve_slots": Leaf("integer", "slots held back for urgent work", default=1, minimum=0),
                        "limit_aware": Leaf("boolean", "respect harness/model/rate limits when dispatching", default=True),
                    },
                ),
            },
        ),
    },
)

_SCRIPTS_BLOCK = Block(
    doc="repo-level commands consumed by the standalone `dev run <name>` CLI (alex-mextner/dev-cli).",
    additional_properties={
        "anyOf": [
            {"type": "string", "pattern": r"\S"},
            {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "command line run by `dev run <name>`",
                    },
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        ],
    },
)

_DEV_E2E_JOB_BLOCK = Block(
    doc="one named e2e job consumed by the standalone `dev` CLI (alex-mextner/dev-cli).",
    leaves={
        "script": Leaf("string", "name of the top-level scripts entry that runs this e2e job"),
        "requires_server": Leaf("boolean", "whether this e2e job expects the dev server", default=True),
        "artifacts_root": Leaf("string", "directory where this e2e job writes artifacts"),
        "logs_root": Leaf("string", "directory where this e2e job writes logs"),
    },
)

_DEV_BLOCK = Block(
    doc="repo-level development server and e2e metadata consumed by the standalone `dev` CLI "
    "(alex-mextner/dev-cli, provisioned like any other ecosystem tool via the `tools:` block).",
    nested={
        "server": Block(
            doc="development server metadata for `dev` lifecycle commands.",
            leaves={
                "script": Leaf("string", "name of the top-level scripts entry that starts the dev server"),
                "url": Leaf("string", "base URL the development server serves"),
                "ready_url": Leaf("string", "URL the dev CLI can poll before running e2e"),
                "port": Leaf(
                    "integer",
                    "a single known TCP port the dev CLI may check (fallback alias for `ports: "
                    "[port]` — dev-cli reads `ports` first, then falls back to this)",
                    minimum=1,
                    maximum=65535,
                ),
                "ports": Leaf(
                    "array",
                    "known TCP ports the dev CLI may check",
                    items_type="integer",
                    minimum=1,
                    maximum=65535,
                ),
                "process_matchers": Leaf(
                    "array",
                    "process command substrings the dev CLI may use to identify owned server processes",
                    items_type="string",
                ),
                "logs_root": Leaf("string", "directory where the dev CLI writes server logs"),
            },
        ),
        "e2e": Block(
            doc="end-to-end test metadata for `dev` lifecycle commands.",
            leaves={
                "script": Leaf("string", "name of the top-level scripts entry that runs e2e tests"),
                "requires_server": Leaf("boolean", "whether e2e expects the dev server to be running", default=True),
                "artifacts_root": Leaf("string", "directory where e2e writes artifacts"),
                "logs_root": Leaf("string", "directory where the dev CLI writes e2e logs"),
            },
            open_map="jobs",
            open_map_doc="named e2e jobs keyed by job name",
            open_map_item=_DEV_E2E_JOB_BLOCK,
        ),
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
        "required_conversation_resolution": Leaf(
            "boolean",
            "require every review thread resolved before merge (required_review_thread_resolution)",
            default=True,
        ),
        "dismiss_stale_reviews": Leaf(
            "boolean", "dismiss stale approvals on a new push (dismiss_stale_reviews_on_push)", default=True
        ),
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
        "autosave": Block(
            doc="an INDEPENDENT launchd saver (decoupled from continuum's status-right hook); when on, continuum's own autosave is disabled so there is one authoritative saver.",
            leaves={
                "enabled": Leaf("boolean", "provision the independent tmux-autosave launchd agent (StartInterval = continuum.save_interval minutes)", default=True),
                "label": Leaf("string", "the autosave launchd agent label", default="ai.hyperide.tmux-autosave"),
                "stale_after": Leaf("integer", "minutes: rig doctor/status flags a mature live server whose newest save is older than this", default=45, minimum=1),
            },
        ),
        "login_shell": Block(
            doc="restored panes are login shells (source ~/.zprofile/PATH).",
            leaves={"enabled": Leaf("boolean", "set a login-shell default-command", default=True), "shell": Leaf("string", "login shell path ('' → resolve $SHELL)", default="")},
        ),
        "pane_titles": Block(
            doc="pane-border-status titles (position + format), and separately dropping tmux's default clock+date from status-right.",
            leaves={
                "enabled": Leaf("boolean", "provision pane-border-status (the pane title itself)", default=True),
                "position": Leaf("string", "pane-border-status placement", enum=("top", "bottom"), default="top"),
                "format": Leaf(
                    "string",
                    "pane-border-format value (no date/time token by default; must not contain "
                    "'\"', '\\\\', '$', '#(' (a shell-exec token), or a non-printable character "
                    "other than a plain tab)",
                    default="#{session_name} #{window_index}:#{window_name}#{window_flags}",
                    not_pattern=TMUX_PANE_TITLES_FORMAT_UNSAFE_PATTERN,
                ),
                "clear_status_right": Leaf("boolean", "when `enabled` is on, also clear tmux's default clock+date status-right; a SEPARATE toggle (nested under `enabled`) so status-right can be left alone while keeping the border title", default=True),
            },
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

_SPOTLIGHT_BLOCK = Block(
    doc="macOS Spotlight-exclude: drop .metadata_never_index into dependency/build dirs + a launchd re-sweep agent.",
    leaves={
        "enabled": Leaf("boolean", "provision the Spotlight-exclude sweep + agent (opt-in, macOS)", default=False),
        "roots": Leaf("array", "dev roots to sweep (default ~/work, ~/xp)", items_type="string"),
        "deny": Leaf("array", "dependency/build dir basenames to exclude (REPLACES the default set)", items_type="string"),
        "extra": Leaf("array", "extra dir basenames to exclude (ADDS to the default set)", items_type="string"),
        "label": Leaf("string", "the launchd agent label", default="ai.hyperide.spotlight-exclude"),
        "max_depth": Leaf("integer", "walk-depth cap below each root", default=8, minimum=1),
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

_PROJECT_TOOLS_BLOCK = Block(
    doc="repo-local integration carriers for Haft, Serena, and Sverklo.",
    leaves={
        "enabled": Leaf("boolean", "provision repo-local project tool integrations", default=True),
    },
    nested={
        "haft": Block(
            doc="Haft FPF/spec carriers plus optional Codex MCP registration.",
            leaves={
                "enabled": Leaf("boolean", "provision Haft project carriers", default=True),
                "project_name": Leaf("string", "Haft project name; defaults to the repo directory name"),
                "project_id": Leaf("string", "stable Haft project id; generated from project_name when omitted"),
                "codex_mcp": Leaf("boolean", "merge the Haft MCP server into .codex/config.toml", default=True),
            },
            nested={
                "workflow": Block(
                    doc="Haft workflow defaults written to .haft/workflow.md.",
                    leaves={
                        "mode": Leaf("string", "Haft workflow mode", enum=HAFT_WORKFLOW_MODES, default="standard"),
                        "require_decision": Leaf("boolean", "require explicit decisions for high-impact work", default=True),
                        "require_verify": Leaf("boolean", "require verification evidence before completion", default=True),
                        "allow_autonomy": Leaf("boolean", "allow autonomous Haft execution by default", default=False),
                    },
                ),
            },
        ),
        "serena": Block(
            doc="Serena project configuration under .serena/.",
            leaves={
                "enabled": Leaf("boolean", "provision Serena project config", default=True),
                "project_name": Leaf("string", "Serena project name; defaults to the repo directory name"),
                "languages": Leaf("array", "Serena language ids; auto-detected when omitted", items_type="string"),
                "read_only": Leaf("boolean", "disable Serena editing tools for this project", default=False),
                "ignored_paths": Leaf("array", "extra Serena ignore patterns", items_type="string"),
            },
        ),
        "sverklo": Block(
            doc="Sverklo global registry/index integration for this repo.",
            leaves={
                "enabled": Leaf("boolean", "provision Sverklo integration", default=True),
                "register": Leaf("boolean", "register this repo in the global Sverklo registry", default=True),
                "reindex": Leaf("boolean", "run sverklo reindex during apply (off by default)", default=False),
            },
        ),
    },
)

_TOOLS_ITEM_BLOCK = Block(
    doc="one declared tool: the repo whose install.sh rig runs, plus optional overrides.",
    leaves={
        "enabled": Leaf("boolean", "install this tool (default true)", default=True),
        "repo": Leaf("string", "the tool's checkout dir (holds install.sh); default ~/xp/<name>-cli"),
        "bin_dir": Leaf("string", "override the managed PATH dir for this one tool"),
    },
)

_TOOLS_BLOCK = Block(
    doc=(
        "the personal CLI tool ecosystem (tg/review/task/draw/…) rig installs + advertises at "
        "apply, by running each tool's own install.sh. rig also keeps a provisioned checkout FRESH: "
        "if a tool repo ships scripts/deploy.sh, apply runs it (ff-only git pull) even when already "
        "installed — opt-in per tool, non-fatal. Default OFF (opt-in): list tools under items. A "
        "per-MACHINE concern — belongs in the GLOBAL layer, not a committed rig.yaml."
    ),
    leaves={
        "enabled": Leaf("boolean", "provision the listed tools (opt-in)", default=False),
        "target": Leaf("string", "managed PATH dir each tool symlinks its bin into", default="~/.local/bin"),
    },
    open_map="items",
    open_map_doc="per-tool spec keyed by command name (repo / bin_dir / enabled)",
    open_map_item=_TOOLS_ITEM_BLOCK,
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
    # The STACK PRESET (distinct from the build-toolchain detect.Environment.stack). Shape is
    # `l1/lang[/framework]` — l1 is a closed six-enum (mobile/frontend/backend/desktop/embedded/
    # system), lang required, framework optional; lang/framework are OPEN vocabulary. Selects the
    # by-stack skills the repo inherits. Global = machine default (optional); per-repo = mandatory
    # by policy (soft-required — a missing value warns, a malformed value fails). The enum is NOT
    # pinned in JSON-schema because lang/framework are open; the six-enum spine is enforced by the
    # Python validator (config.validate).
    "stack": Leaf(
        "string",
        "the repo's stack preset `l1/lang[/framework]` (e.g. mobile/swift/swiftui, "
        "frontend/ts/react, backend/python). l1 in mobile|frontend|backend|desktop|embedded|"
        "system; lang required; framework optional. Selects by-stack skills. Global default is "
        "optional; a per-repo value is expected (soft-required).",
    ),
}

BLOCKS: dict[str, Block] = {
    "defaults": _DEFAULTS_BLOCK,
    "scripts": _SCRIPTS_BLOCK,
    "dev": _DEV_BLOCK,
    "skills": _SKILLS_BLOCK,
    "agent_hooks": _AGENT_HOOKS_BLOCK,
    "git_hooks": _GIT_HOOKS_BLOCK,
    "ci": _CI_BLOCK,
    "mcp": _MCP_BLOCK,
    "mode": _MODE_BLOCK,
    "harness": _HARNESS_BLOCK,
    "permissions": _PERMISSIONS_BLOCK,
    "models": _MODELS_BLOCK,
    "agents_md": _AGENTS_MD_BLOCK,
    "github": _GITHUB_BLOCK,
    "tmux": _TMUX_BLOCK,
    "gitignore": _GITIGNORE_BLOCK,
    "spotlight": _SPOTLIGHT_BLOCK,
    "tools": _TOOLS_BLOCK,
    "tg_ctl": _TG_CTL_BLOCK,
    "ship_delegator": _SHIP_DELEGATOR_BLOCK,
    "linters": _LINTERS_BLOCK,
    "project_tools": _PROJECT_TOOLS_BLOCK,
}

# Every valid TOP-LEVEL key (scalars + blocks). Mirrors config._VALID_TOP_KEYS; the sync test
# asserts the two are identical so the schema and the validator can never disagree.
TOP_LEVEL_KEYS: set[str] = set(_TOP_LEAVES) | set(BLOCKS)


def json_schema() -> dict[str, Any]:
    """Emit the COMPLETE Draft-07 JSON Schema for rig.yaml + the global config (one source).

    Strict by construction: every block is ``additionalProperties: false`` except where it carries
    an open map such as ``items``/``fragments``/``jobs``, so an unknown FIXED key is a schema
    violation an editor flags. ``scope`` is whitelisted at the top level only (a tolerated legacy
    key the loader drops). This is what ``schema/rig.schema.json`` is generated from.
    """
    props: dict[str, Any] = {name: leaf.to_node() for name, leaf in _TOP_LEAVES.items()}
    for name, block in BLOCKS.items():
        props[name] = block.to_node()
    props["mode"]["x-rig-global-only"] = True
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
    nodes. Walking STOPS at an open map — segments past it (item names like ``secret-scan`` or
    ``smoke``) are NOT schema nodes, so the pointer would dangle. We return a
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
        if block.open_map and part == block.open_map and block.open_map_item is not None:
            block = block.open_map_item
            continue
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
