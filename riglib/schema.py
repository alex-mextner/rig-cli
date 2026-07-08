"""The rig.yaml option REGISTRY — the single source of truth for the setup wizard.

What this is
------------
A flat list of every wizard-exposable rig.yaml option, each carrying its dotted config key,
its owning layer (GLOBAL vs REPO), its value kind (bool/enum/str/int), default, and an inline
HINT (how the option works + why it is needed). ``rig setup`` reads THIS registry to render
"what is enabled across all reconciled areas" and to drive the change-and-apply loop; the
hints are the "why next to the toggle, not buried in docs" the roadmap asked for.

Why a code registry and not a JSON schema file
----------------------------------------------
The roadmap pairs this with a separate "rig.yaml JSON schema" item that does not exist on disk
yet. Rather than block, the option list + hints live here as the authoritative source, and
:func:`json_schema` EMITS a JSON-Schema document from it — so when the schema file lands it is
GENERATED from this registry (one source), never hand-maintained in parallel. The hint text is
the human-facing distillation of ``docs/config-schema.md`` (kept English, kept terse).

How options reach the two config layers
---------------------------------------
Each option names a top-level category (``skills``, ``ci``, ``harness`` …). The OWNING LAYER is
resolved through :func:`riglib.layers.layer_for_category` — GLOBAL options are written to
``~/.config/rig/config.yaml`` and REPO options to the repo's ``rig.yaml``. The wizard NEVER
writes a GLOBAL-only block (``gitignore``, ``tg_ctl``, the dispatcher, the harness, …) into the
committed repo file — that is the documented footgun this routing prevents.

Stdlib-only (the repo import rule): no yaml/textual here; the wizard imports yaml lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .layers import GLOBAL, REPO, layer_for_category

# The value kinds the wizard knows how to prompt for and coerce. Kept tiny on purpose — a
# wizard option is a single scalar toggle/value; nested lists/maps are edited in the file.
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_STR = "str"
KIND_INT = "int"
_KINDS = {KIND_BOOL, KIND_ENUM, KIND_STR, KIND_INT}

# WHERE the wizard/`config set` PERSISTS a category's value. This is DISTINCT from
# ``layers.layer_for_category`` (which buckets drift for the `rig status` DISPLAY): several
# categories that status groups under GLOBAL (``harness``, ``models``, ``git_hooks``) are still
# WRITTEN into the committed repo ``rig.yaml`` by the default scaffold (state.default_state), so
# editing their value belongs in the repo file. The GLOBAL-ONLY categories are the machine-wide
# blocks the scaffold deliberately NEVER writes into a committed repo file — ``gitignore`` and
# ``tg_ctl`` (documented global-only in config.py/state.py) and ``tmux`` (machine tmux config).
# Routing a GLOBAL-only value into a committed repo rig.yaml is the footgun this map prevents.
# A category absent here defaults to REPO (the conservative "it lives in the committed file").
_GLOBAL_ONLY_CATEGORIES = {"gitignore", "tg_ctl", "tmux", "permissions"}


def writable_layer_for_category(category: str) -> str:
    """The layer the wizard/`config set` writes a category's value to (REPO unless global-only)."""
    return GLOBAL if category in _GLOBAL_ONLY_CATEGORIES else REPO


@dataclass(frozen=True)
class Option:
    """One wizard-exposable rig.yaml option.

    ``key`` is the dotted path into the config dict (e.g. ``harness.auto_mode``); ``category``
    is its top-level block, used to resolve the owning layer. ``kind`` selects how the value is
    prompted/coerced; ``choices`` is required for ``enum``. ``hint`` is the inline why/how shown
    next to the option in the wizard.
    """

    key: str
    category: str
    kind: str
    default: Any
    hint: str
    choices: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"Option {self.key!r}: unknown kind {self.kind!r}")
        if self.kind == KIND_ENUM and not self.choices:
            raise ValueError(f"Option {self.key!r}: enum kind requires choices")
        if self.key.split(".", 1)[0] != self.category:
            raise ValueError(
                f"Option {self.key!r}: key must start with its category {self.category!r}"
            )

    @property
    def layer(self) -> str:
        """The config file the wizard/`config set` WRITES this option to (REPO or GLOBAL).

        This is the WRITABLE layer (where the edit is persisted), not the status-display layer —
        see :func:`writable_layer_for_category`. The wizard shows this tag so the user knows which
        file an edit lands in, and routes the write by it (keeping global-only blocks out of the
        committed repo rig.yaml).
        """
        return writable_layer_for_category(self.category)

    @property
    def status_layer(self) -> str:
        """The layer ``rig status`` GROUPS this category under (display only; may differ from layer)."""
        return layer_for_category(self.category)


@dataclass(frozen=True)
class Area:
    """A reconciled area (one row in ``rig status``) grouping the options under one category."""

    category: str
    title: str
    blurb: str
    options: tuple[Option, ...]

    @property
    def layer(self) -> str:
        """The WRITABLE layer for this area's options (where the wizard persists an edit)."""
        return writable_layer_for_category(self.category)

    @property
    def status_layer(self) -> str:
        return layer_for_category(self.category)


def _opt(key: str, kind: str, default: Any, hint: str, choices: tuple[str, ...] = ()) -> Option:
    return Option(key=key, category=key.split(".", 1)[0], kind=kind, default=default, hint=hint, choices=choices)


# ── the registry — one Area per reconciled `rig status` row, in status order ──────────────
# Hints are the terse why/how from docs/config-schema.md (English only). Defaults mirror the
# generated scaffold in state.default_state / the validators' documented defaults.
AREAS: tuple[Area, ...] = (
    Area(
        "skills", "skills", "Advisory markdown rules copied into the agent skills dir (opt-out model).",
        (
            _opt("skills.enabled", KIND_BOOL, True,
                 "Install skills at all. Off = leave the skills dir untouched."),
            _opt("skills.universal.all", KIND_BOOL, True,
                 "Enable every universal skill (opt-out). The portable engineering-discipline baseline."),
            _opt("skills.harness_link", KIND_BOOL, True,
                 "Symlink each installed skill into the harness discovery dir (~/.claude/skills) "
                 "so the Skill tool actually lists it — a skill in ~/.agents/skills alone is invisible."),
        ),
    ),
    Area(
        "agent_hooks", "agent-hooks", "Programmatic guards that block before a side effect (no-verify, secrets, raw merge).",
        (
            _opt("agent_hooks.enabled", KIND_BOOL, True,
                 "Install the agent-hook guards. These are what make auto-mode safe — they "
                 "intercept dangerous tool calls (block-no-verify, block-secrets-write, …) before they land."),
            _opt("agent_hooks.all", KIND_BOOL, True,
                 "Install ALL guard hooks (vs cherry-picking via items). Recommended on."),
            _opt("agent_hooks.worktree_only", KIND_BOOL, False,
                 "Opt-IN: enforce the worktree-only workflow. The worktree-only-writes hook "
                 "denies an Edit/Write while the checkout sits on the default branch (main/"
                 "master) — authoring happens in a feature-branch worktree instead. Off by "
                 "default so a repo that legitimately works on main (e.g. 3d-cli) is never "
                 "blocked. Escape hatch: RIG_ALLOW_MAIN_EDIT=1."),
            _opt("agent_hooks.orchestrator_only", KIND_BOOL, True,
                 "Opt-OUT: keep the orchestrator thin. The orchestrator-stays-thin hook blocks "
                 "inline implementation (Bash / code Edits) by the main thread, delegating to a "
                 "subagent, while still allowing read-only inspection and orchestration (gh pr "
                 "list/view/checks, gh ship, tg, review, git worktree list). On by default — set "
                 "false to exempt a repo that works inline (e.g. 3d-cli). Escape hatch: "
                 "ALLOW_ORCHESTRATOR_WORK=1 + reason."),
        ),
    ),
    Area(
        "git_hooks", "git-hooks (dispatcher)", "The global-hook dispatcher: your hooks run in EVERY repo, even ones that hijack core.hooksPath.",
        (
            _opt("git_hooks.dispatcher.enabled", KIND_BOOL, True,
                 "Install the global-hook dispatcher (machine-wide). Your hooks then fire in every "
                 "repo via a drop-in dir, surviving a repo that overrides core.hooksPath."),
            _opt("git_hooks.dispatcher.set_global_hooks_path", KIND_BOOL, True,
                 "Wire the dispatcher as git's global core.hooksPath (the prior value is recorded). "
                 "Off = install the runner but do not claim the global hooks path."),
        ),
    ),
    Area(
        "ci", "CI gates", "Vendor-neutral CI gates (secret-scan, codeql, dependency-review, ship, …).",
        (
            _opt("ci.enabled", KIND_BOOL, True,
                 "Install CI gate workflows under .github/workflows. Off = write no CI files."),
        ),
    ),
    Area(
        "mcp", "MCP servers", "MCP registrations (review, code-search) — callable from any agent.",
        (
            _opt("mcp.enabled", KIND_BOOL, True,
                 "Register MCP servers into the harness MCP config (idempotent merge by server name)."),
        ),
    ),
    Area(
        "harness", "harness / auto-mode", "The agent harness's auto/permission mode — provisioned, not hand-toggled.",
        (
            _opt("harness.enabled", KIND_BOOL, True,
                 "Provision the harness permission setting. Off = leave the harness config alone."),
            _opt("harness.auto_mode", KIND_BOOL, True,
                 "Auto-accept tool calls (the agent runs autonomously). SAFE because the agent-hook "
                 "guards above catch the dangerous calls first. Off = interactive permission prompts."),
            _opt("harness.hook_bridge.enabled", KIND_BOOL, True,
                 "Wire the supported harness hook bridge so installed agent-hooks actually FIRE. "
                 "Without it every agent-hook is inert and auto-mode is NOT safe."),
        ),
    ),
    Area(
        "permissions", "harness permissions (allow / deny / ask)",
        "Reconcile the harness permissions layer: pre-allow our CLIs + safe dev tools, and assert "
        "the conservative deny/ask rule baselines (the outer belt under the agent-hooks).",
        (
            _opt("permissions.enabled", KIND_BOOL, True,
                 "Provision the per-harness permissions layer: the command allowlist (tg/review/draw/"
                 "3d/rig/task + gh/git/rg/uv/bun/jq/gitleaks pre-allowed, no per-call prompts) plus "
                 "the deny/ask rule baselines (claude-code only; raw PR-merge, force-push, sudo rm, "
                 "screencapture denied; pkill/killall/git reset --hard prompt). Additive — merges "
                 "into the existing lists, never clobbers or removes the user's own entries. Off = "
                 "leave it alone. GLOBAL-only (the settings file is per-machine) — never written to "
                 "a repo rig.yaml."),
            _opt("permissions.kind", KIND_ENUM, "claude-code",
                 "Which harness's permissions to provision. opencode is supported for the ALLOWLIST "
                 "independently of harness.kind (its deny/ask dialect is unverified → N/A); "
                 "codex/gemini have no additively-mergeable allowlist (N/A). The lists "
                 "(tools/extra/disable, allow/deny/ask) are edited directly in the config file.",
                 choices=("claude-code", "opencode")),
        ),
    ),
    Area(
        "models", "model-freshness schedule", "A daily cron that runs the model-freshness checker and proposes version bumps.",
        (
            _opt("models.enabled", KIND_BOOL, True,
                 "Provision the daily checker schedule (launchd on macOS, crontab on Linux). "
                 "Off = leave the system cron alone."),
            _opt("models.schedule.time", KIND_STR, "12:00",
                 "Daily run time, HH:MM 24-hour (default noon). Fail-closed on a malformed/out-of-range value."),
        ),
    ),
    Area(
        "agents_md", "AGENTS.md / CLAUDE.md", "One canonical agent-guide file (AGENTS.md) + a CLAUDE.md symlink, so every harness reads the same instructions.",
        (
            _opt("agents_md.enabled", KIND_BOOL, True,
                 "Provision the canonical AGENTS.md + CLAUDE.md symlink pair. Never clobbers a real "
                 "file — a conflict is left untouched and surfaced as drift."),
        ),
    ),
    Area(
        "github", "GitHub ruleset", "A branch ruleset on the default branch (the modern branch-protection replacement).",
        (
            _opt("github.ruleset.enabled", KIND_BOOL, True,
                 "Reconcile a 'rig-managed' ruleset via gh api (a no-op with no github remote). "
                 "Requires a PR to merge; blocks force-push and branch deletion; admins always bypass."),
            _opt("github.ruleset.require_pull_request", KIND_BOOL, True,
                 "Emit the pull_request rule — require a PR to merge to the default branch."),
            _opt("github.ruleset.required_reviews", KIND_INT, 0,
                 "required_approving_review_count on the pull_request rule (0 = a PR but no required approval)."),
            _opt("github.ruleset.required_conversation_resolution", KIND_BOOL, True,
                 "Require every review thread to be resolved before merge (secure default ON). "
                 "Does not require a reviewer or an approval — but blocks any merge until all threads "
                 "are resolved. Inert if require_pull_request: false."),
            _opt("github.ruleset.dismiss_stale_reviews", KIND_BOOL, True,
                 "Dismiss stale pull-request approvals when new commits are pushed (secure default ON). "
                 "Applies to any approval, including voluntary ones when required_reviews is 0. "
                 "Inert only if require_pull_request: false."),
        ),
    ),
    Area(
        "tmux", "tmux config", "rig-managed tmux configuration (persistence across reboots, plugin-init ordering).",
        (
            _opt("tmux.enabled", KIND_BOOL, False,
                 "Manage tmux config declaratively (generate + migrate an existing ~/.tmux.conf). "
                 "Off (default) = leave tmux alone."),
        ),
    ),
    Area(
        "gitignore", "global git-excludes block", "A rig-managed block in git's global core.excludesfile (ignores harness worktrees machine-wide).",
        (
            _opt("gitignore.enabled", KIND_BOOL, True,
                 "Maintain the managed block in the GLOBAL excludes file so **/.claude/worktrees/ is "
                 "ignored in every repo, with zero per-repo commits. GLOBAL-only — never written to a repo rig.yaml."),
        ),
    ),
    Area(
        "tg_ctl", "tg-ctl inbound daemon", "The Telegram control daemon auto-started as a per-machine boot LaunchAgent (macOS).",
        (
            _opt("tg_ctl.enabled", KIND_BOOL, True,
                 "Provision the tg-ctl inbound daemon. GLOBAL-only (one per machine) — never written to a repo rig.yaml."),
            _opt("tg_ctl.boot", KIND_BOOL, True,
                 "Auto-start the daemon at login via a launchd boot agent (macOS). Off = install but do not boot."),
        ),
    ),
    Area(
        "linters", "linter / formatter config files", "Per-repo linter + formatter config files rig writes/reconciles (tool + content per repo).",
        (
            _opt("linters.enabled", KIND_BOOL, True,
                 "Provision the declared linter/formatter config files (the `linters.items` map). "
                 "Per-item tool + path + content live in rig.yaml; this toggle gates the whole area. "
                 "Never clobbers a hand-written config — a conflict is backed up before overwrite."),
        ),
    ),
    Area(
        "project_tools", "project tools (Haft / Serena / Sverklo)",
        "Repo-local carriers and registrations for code-intelligence/governance tools.",
        (
            _opt("project_tools.enabled", KIND_BOOL, True,
                 "Provision repo-local project-tool integrations. Off = leave .haft/.serena/.codex "
                 "and live Sverklo registration untouched."),
            _opt("project_tools.haft.enabled", KIND_BOOL, True,
                 "Provision Haft FPF/spec carriers and workflow defaults under .haft/."),
            _opt("project_tools.haft.codex_mcp", KIND_BOOL, True,
                 "Merge the Haft MCP server into .codex/config.toml for Codex."),
            _opt("project_tools.serena.enabled", KIND_BOOL, True,
                 "Provision .serena/project.yml and the Serena local gitignore."),
            _opt("project_tools.sverklo.enabled", KIND_BOOL, True,
                 "Provision Sverklo integration for this repo."),
            _opt("project_tools.sverklo.register", KIND_BOOL, True,
                 "Register this repo in the global Sverklo registry during apply (idempotent)."),
            _opt("project_tools.sverklo.reindex", KIND_BOOL, False,
                 "Run `sverklo reindex` during apply. Off by default because indexing can be slow."),
        ),
    ),
)


def all_options() -> list[Option]:
    """Every registered option, flattened, in status/area order."""
    return [o for area in AREAS for o in area.options]


def area_for_category(category: str) -> Area | None:
    for area in AREAS:
        if area.category == category:
            return area
    return None


def option_for_key(key: str) -> Option | None:
    for o in all_options():
        if o.key == key:
            return o
    return None


# ── dotted-path get/set on a nested config dict (shared by the wizard + `rig config get|set`) ──
class KeyError_(KeyError):
    """A get on a key that is absent from the config (distinct from a registry-unknown key)."""


def get_path(data: dict[str, Any], dotted: str) -> Any:
    """Read ``dotted`` (e.g. ``harness.auto_mode``) from a nested dict. Raises KeyError_ if absent."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError_(dotted)
        cur = cur[part]
    return cur


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``dotted`` to ``value`` in a nested dict, creating intermediate mappings as needed.

    Mutates ``data`` in place. A MISSING intermediate (``None`` / absent) is created as an empty
    mapping. But an intermediate that exists as a NON-mapping (a scalar/list the user authored,
    e.g. ``harness: "TODO"``) is NOT silently clobbered — that would destroy their content before
    validation ever runs. We raise :class:`ValueError` instead, so the wizard surfaces it as a
    "rejected" message and the CLI as a config error, leaving the file untouched.
    """
    parts = dotted.split(".")
    cur = data
    walked: list[str] = []
    for part in parts[:-1]:
        walked.append(part)
        nxt = cur.get(part)
        if nxt is None:
            nxt = {}
            cur[part] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(
                f"cannot set {dotted!r}: existing value at {'.'.join(walked)!r} is "
                f"{type(nxt).__name__}, not a mapping — fix it in the config file first"
            )
        cur = nxt
    cur[parts[-1]] = value


# Categories whose plan builder treats an ABSENT top-level block as INACTIVE (it returns early
# when the block is missing/empty, or requires `enabled` to be truthy), NOT as enabled-by-default:
# `_build_harness`/`_build_models` bail on a missing block, and the git-hooks dispatcher only runs
# when `dispatcher.enabled` is truthy. The other default-ON categories (skills, agent_hooks, ci,
# mcp, agents_md, github, gitignore, tg_ctl) gate on `enabled is False`, so an absent block IS
# active for them and the registry default is the correct live value. Keeping this in lockstep
# with plan.py is what stops `rig setup`/`config get` from claiming auto-mode, the schedule, or the
# dispatcher are on when `rig apply` would skip them. (tmux defaults OFF, so it needs no entry.)
_BLOCK_PRESENCE_GATED_CATEGORIES = frozenset({"harness", "models", "git_hooks"})


def effective_value(option: Option, merged: dict[str, Any]) -> Any:
    """The option's CURRENT value in a (cascaded) config dict — the live state the wizard shows.

    ``merged`` is the loaded+cascaded config (global then repo). An absent key normally falls back
    to the documented default. The exception is a block-presence-gated category (see
    :data:`_BLOCK_PRESENCE_GATED_CATEGORIES`): when its ENTIRE top-level block is absent, the plan
    builder skips it, so its bool activation keys are reported OFF (``False``) rather than at the
    enabled-by-default value — otherwise the wizard would advertise a feature `rig apply` ignores.
    """
    try:
        return get_path(merged, option.key)
    except KeyError_:
        if (
            option.kind == KIND_BOOL
            and option.category in _BLOCK_PRESENCE_GATED_CATEGORIES
            and option.category not in merged
        ):
            # the block is entirely absent → apply treats it as inactive → show OFF, not the default
            return False
        return option.default


def coerce(option: Option, raw: str) -> Any:
    """Coerce a wizard/CLI string into the option's typed value. Raises ValueError on a bad value."""
    raw = raw.strip()
    if option.kind == KIND_BOOL:
        low = raw.lower()
        if low in ("y", "yes", "true", "on", "1"):
            return True
        if low in ("n", "no", "false", "off", "0"):
            return False
        raise ValueError(f"{option.key}: expected yes/no, got {raw!r}")
    if option.kind == KIND_INT:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{option.key}: expected an integer, got {raw!r}") from None
    if option.kind == KIND_ENUM:
        if raw not in option.choices:
            raise ValueError(f"{option.key}: expected one of {list(option.choices)}, got {raw!r}")
        return raw
    return raw  # str


def json_schema() -> dict[str, Any]:
    """Emit the COMPLETE JSON Schema for rig.yaml — delegated to ``config_schema`` (one emitter).

    This wizard module owns a CURATED option subset (toggles + hints); the EXHAUSTIVE schema (every
    block + key, ``additionalProperties: false``, the editor-facing artifact) lives in
    :mod:`riglib.config_schema`, and is what ``schema/rig.schema.json`` is generated from. Kept as a
    re-export here so the historical call site (``schema.json_schema()``) and its test stay valid
    while there is still ONE generator. The wizard's own registry is reconciled against this schema
    by the sync test (every wizard option key must be a node in the emitted schema).
    """
    from . import config_schema

    return config_schema.json_schema()


__all__ = [
    "GLOBAL",
    "REPO",
    "Area",
    "Option",
    "AREAS",
    "all_options",
    "area_for_category",
    "option_for_key",
    "writable_layer_for_category",
    "get_path",
    "set_path",
    "effective_value",
    "coerce",
    "json_schema",
    "KeyError_",
]
