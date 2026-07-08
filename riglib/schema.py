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

# Dot-path syntax/traversal lives in config.py; schema adapts absent reads to KeyError_.
from .config import ConfigError, canonical_dot_path, get_path as _config_get_path
from .config import set_path as _config_set_path
from .harness_skills import HARNESS_INSTRUCTION_FILES, HARNESS_NATIVE_SKILLS, HARNESS_SKILL_DIR_KINDS
from .layers import GLOBAL, REPO, layer_for_category
from .tmux import DEFAULT_PANE_TITLES_FORMAT

# The value kinds the wizard knows how to prompt for and coerce. Kept tiny on purpose.
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_STR = "str"
KIND_INT = "int"
KIND_LIST = "list"
_KINDS = {KIND_BOOL, KIND_ENUM, KIND_STR, KIND_INT, KIND_LIST}
_HARNESS_KIND_CHOICES: tuple[str, ...] = tuple(
    dict.fromkeys((*HARNESS_SKILL_DIR_KINDS, *HARNESS_NATIVE_SKILLS, *HARNESS_INSTRUCTION_FILES))
)

# WHERE the wizard/`config set` PERSISTS a category's value. This is DISTINCT from
# ``layers.layer_for_category`` (which buckets drift for the `rig status` DISPLAY): several
# categories that status groups under GLOBAL (``harness``, ``models``, ``git_hooks``) are still
# WRITTEN into the committed repo ``rig.yaml`` by the default scaffold (state.default_state), so
# editing their value belongs in the repo file. The GLOBAL-ONLY categories are the machine-wide
# blocks the scaffold deliberately NEVER writes into a committed repo file — ``gitignore`` and
# ``tg_ctl`` (documented global-only in config.py/state.py), ``tmux`` (machine tmux config), and
# ``mode`` (machine-wide agent policy).
# Routing a GLOBAL-only value into a committed repo rig.yaml is the footgun this map prevents.
# A category absent here defaults to REPO (the conservative "it lives in the committed file").
_GLOBAL_ONLY_CATEGORIES = {"gitignore", "spotlight", "tg_ctl", "tmux", "mode"}


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
    items_enum: tuple[str, ...] = field(default_factory=tuple)
    null_tokens: tuple[str, ...] = ("", "null", "none", "~", "unset")

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


def _opt(
    key: str,
    kind: str,
    default: Any,
    hint: str,
    choices: tuple[str, ...] = (),
    items_enum: tuple[str, ...] = (),
    null_tokens: tuple[str, ...] = ("", "null", "none", "~", "unset"),
) -> Option:
    return Option(
        key=key,
        category=key.split(".", 1)[0],
        kind=kind,
        default=default,
        hint=hint,
        choices=choices,
        items_enum=items_enum,
        null_tokens=null_tokens,
    )


# ── the registry — one Area per reconciled `rig status` row, in status order ──────────────
# Hints are the terse why/how from docs/config-schema.md (English only). Defaults mirror the
# generated scaffold in state.default_state / the validators' documented defaults.
AREAS: tuple[Area, ...] = (
    Area(
        "stack", "stack preset", "The repo's stack (l1/lang[/framework]) — selects the by-stack skill set.",
        (
            _opt("stack", KIND_STR, None,
                 "The repo's stack preset as `l1/lang[/framework]` — e.g. mobile/swift/swiftui, "
                 "frontend/ts/react, backend/python. l1 is one of mobile|frontend|backend|desktop|"
                 "embedded|system; lang is required; framework is optional. It selects the by-stack "
                 "skills the repo inherits (hierarchical: mobile/swift/swiftui pulls mobile/swift + "
                 "mobile/swift/swiftui skills). Per-repo it is expected (soft-required); set a global "
                 "default with `rig config set --global stack <value>`."),
        ),
    ),
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
                 "Opt-IN: enforce the worktree-only workflow. Gates TWO hooks: worktree-only-writes "
                 "denies an Edit/Write while the checkout sits on the default branch (main/master); "
                 "pin-primary-worktree denies a git checkout/switch that would move the repo's "
                 "PRIMARY worktree onto anything but the default branch — authoring happens in a "
                 "feature-branch worktree instead. Off by default so a repo that legitimately works "
                 "on main (e.g. 3d-cli) is never blocked. No self-service env bypass — each hook has "
                 "its OWN hatch var: a deliberate one-off Edit/Write on main is requested via "
                 "RIG_HATCH_REQUEST_WORKTREE_ONLY_WRITES=\"<justification>\"; a one-off git checkout/"
                 "switch in the primary checkout via "
                 "RIG_HATCH_REQUEST_PIN_PRIMARY_WORKTREE=\"<justification>\" "
                 "(both: tg approval, deny-by-default; bare 1 rejected)."),
            _opt("agent_hooks.orchestrator_only", KIND_BOOL, True,
                 "Opt-OUT: keep the orchestrator thin. The orchestrator-stays-thin hook warns on "
                 "the first inline implementation (Bash / code Edits) by the main thread, "
                 "delegating to a subagent, then blocks a repeat within its TTL. Read-only "
                 "inspection (git status/ls/cat/grep/find, git worktree list) is never gated; "
                 "tg/review are sanctioned orchestration, also never gated. ALL gh is delegated to "
                 "a subagent too — gh ship, gh pr list/view/checks, gh run, gh api included. On by "
                 "default — set false to exempt a repo that works inline (e.g. 3d-cli). No "
                 "self-service env bypass; a one-off is requested via "
                 "RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN=\"<justification>\" (tg approval, "
                 "deny-by-default; bare 1 rejected)."),
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
            _opt("harness.self_merge", KIND_BOOL, True,
                 "Let the agent self-merge a PR it authored in the session's starting repo via "
                 "`gh ship`. Adds the ship allow rules (`Bash(gh ship:*)`, `Bash(*/pr-ship.sh:*)`, "
                 "`Bash(*/ship.sh:*)`) to `permissions.allow` so the auto-mode Bash gate stops vetoing "
                 "`gh ship`, PLUS a `$defaults`-preserving carve-out to the classifier's `autoMode.allow` "
                 "clearing the Merge-Without-Review + Self-Approval soft blocks for the agent's OWN PRs. "
                 "Auto-mode only. SAFE — the `Bash(gh pr merge:*)` deny, the anti-exfil hard rule, "
                 "block-raw-pr-merge for OTHER PRs, the review-fix loop and the local CI gate all stay "
                 "(`gh ship` remains the only merge path). Off = the agent hits the gate/soft block and asks."),
            _opt("harness.kinds", KIND_LIST, [],
                 "Additional harnesses to provision alongside harness.kind, comma-separated "
                 "(e.g. codex,opencode). Additional harnesses get skill discovery, agent-hook "
                 "descriptors, supported hook bridges, and supported permissions allowlists. If "
                 "agent_hooks.target pins one explicit descriptor target, supported bridges are "
                 "registered with a descriptor-dir override.",
                 items_enum=_HARNESS_KIND_CHOICES),
            _opt("harness.hook_bridge.enabled", KIND_BOOL, True,
                 "Wire the supported harness hook bridge so installed agent-hooks actually FIRE. "
                 "Without it every agent-hook is inert and auto-mode is NOT safe."),
        ),
    ),
    Area(
        "permissions", "harness permissions (allow / deny / ask)",
        "Reconcile the harness permissions layer: pre-allow our CLIs + read-only helpers, and assert "
        "the conservative deny/ask rule baselines (the outer belt under the agent-hooks).",
        (
            _opt("permissions.enabled", KIND_BOOL, True,
                 "Provision the per-harness permissions layer: the command allowlist (tg/review/draw/"
                 "3d/rig/task/dev + read-only rg/jq/gitleaks pre-allowed, no per-call prompts) plus "
                 "the deny/ask rule baselines (claude-code AND opencode; raw PR-merge, force-push, sudo "
                 "rm, screencapture denied; pkill/killall/git reset --hard prompt). codex gets a "
                 "safe-command allow + coarse deny via its execpolicy .rules block. Additive — merges "
                 "into the existing lists, never clobbers or removes the user's own entries. Off = "
                 "leave it alone. The target settings file is per-machine; repo-local config is "
                 "still accepted for compatibility."),
            _opt("permissions.kind", KIND_ENUM, None,
                 "Which harness's permissions to provision. opencode is supported for the ALLOWLIST "
                 "AND deny/ask (its own permission.bash glob dialect) independently of harness.kind. "
                 "Absent permissions.kind, rig provisions supported harness.kind plus harness.kinds "
                 "allowlists; codex has no config allowlist (its allow/coarse-deny go via the "
                 "execpolicy .rules block). The lists (tools/extra/disable, allow/deny/ask) are "
                 "edited directly in the config file.",
                 choices=("claude-code", "opencode"),
                 null_tokens=("", "null", "none", "~", "unset", "fan-out")),
        ),
    ),
    Area(
        "mode", "agent mode", "Machine-wide operating mode for autonomous agent sessions.",
        (
            _opt("mode.name", KIND_ENUM, "standard",
                 "standard = normal rig behavior. autonomous = keep working through review/fix "
                 "loops, quorum decisions, parallel comparisons, and limit-aware dispatch before "
                 "asking for help.",
                 choices=("standard", "autonomous")),
            _opt("mode.autonomous.review_fix.max_iterations", KIND_INT, 5,
                 "Maximum review/fix iterations while autonomous mode is active; the default loops "
                 "until the diff is clean or this cap is reached."),
            _opt("mode.autonomous.decisions.review_quorum.min_models", KIND_INT, 3,
                 "Minimum distinct reviewer models required before a decision is considered settled."),
            _opt("mode.autonomous.parallel_worktree_comparison.candidates", KIND_INT, 2,
                 "Number of independent worktree candidates to compare before escalating a hard choice."),
            _opt("mode.autonomous.parallelism.max_agents", KIND_INT, 4,
                 "Limit-aware cap for concurrent agents; keep below provider and machine capacity."),
            _opt("mode.autonomous.parallelism.max_worktrees", KIND_INT, 4,
                 "Limit-aware cap for concurrent worktrees; prevents a swarm from exhausting local slots."),
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
            _opt("tmux.pane_titles.enabled", KIND_BOOL, True,
                 "Show a compact pane-border title (session/window, no date/time). "
                 "On by default whenever tmux.enabled is on."),
            _opt("tmux.pane_titles.position", KIND_ENUM, "top",
                 "Where pane-border-status renders the title.", choices=("top", "bottom")),
            _opt("tmux.pane_titles.format", KIND_STR, DEFAULT_PANE_TITLES_FORMAT,
                 "pane-border-format value. No date/time token by default; must not contain "
                 "'\"', '\\\\', '$', or control characters (they can corrupt the generated tmux config)."),
            _opt("tmux.pane_titles.clear_status_right", KIND_BOOL, True,
                 "Clear tmux's default clock+date status-right ('%H:%M %d-%b-%y'), separately "
                 "from pane_titles.enabled, so an existing customized status-right can be kept."),
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
        "spotlight", "Spotlight-exclude sweep (macOS)",
        "Drop .metadata_never_index into dependency/build dirs so Spotlight skips node_modules/dist/etc, plus a launchd re-sweep agent.",
        (
            _opt("spotlight.enabled", KIND_BOOL, False,
                 "Sweep the dev roots dropping .metadata_never_index into node_modules/dist-like dirs, "
                 "and install a daily launchd re-sweep for new projects. Opt-in, macOS-only. "
                 "GLOBAL-only — never written to a repo rig.yaml."),
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
    """Read ``dotted`` from a nested dict. Raises KeyError_ if absent, ConfigError if malformed."""
    # canonical_dot_path must stay outside the try: malformed paths propagate as ConfigError,
    # while only absent canonical paths are adapted to KeyError_ for effective_value fallback.
    config_path = canonical_dot_path(dotted)
    try:
        return _config_get_path(data, config_path)
    except ConfigError as exc:
        raise KeyError_(config_path) from exc


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Delegate to :func:`riglib.config.set_path` for wizard edits.

    Raises ConfigError on malformed paths or non-mapping intermediates; the wizard catches that as
    a rejected edit.
    """
    _config_set_path(data, dotted, value)


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
            if option.default is None and raw.lower() in option.null_tokens:
                return None
            raise ValueError(f"{option.key}: expected one of {list(option.choices)}, got {raw!r}")
        return raw
    if option.kind == KIND_LIST:
        if raw in ("", "[]"):
            return []
        if raw.startswith("["):
            try:
                import yaml  # lazy, like the rest of the config stack

                value = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise ValueError(f"{option.key}: expected a comma-separated list, got {raw!r}") from exc
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{option.key}: expected a list of strings, got {raw!r}")
            _validate_list_enum(option, value)
            return value
        value = [part.strip() for part in raw.split(",") if part.strip()]
        _validate_list_enum(option, value)
        return value
    return raw  # str


def _validate_list_enum(option: Option, value: list[str]) -> None:
    if option.items_enum and any(item not in option.items_enum for item in value):
        raise ValueError(f"{option.key}: expected entries from {list(option.items_enum)}, got {value!r}")


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
