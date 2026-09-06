"""The action-kind TAG taxonomy — what KIND of change each planned :class:`~riglib.plan.Action`
represents, for config-web's interactive plan-preview panel (rig-cli#310).

What this is
------------
``riglib.actions.runner._HANDLERS`` is the real, authoritative list of action kinds the shared
engine (``plan.build`` + ``actions.run_plan``) can execute — every ``rig apply`` / ``rig init``
action dispatches through exactly one of those handlers. This module maps EVERY handler kind to
one small, real category (:data:`CATEGORIES`) plus a plain-language one-liner naming where on
disk (or which remote setting) it lands and who it affects — a running agent, the human's own
workflow, or both.

The taxonomy is derived from the handlers, never invented: :func:`tag_for_kind` is covered by a
completeness test (``tests/test_action_tags.py``) asserting :data:`ACTION_TAGS`'s key set equals
``riglib.actions.runner._HANDLERS``'s key set, so a newly added action kind can never ship to the
plan-preview UI untagged.

How it is reached at runtime
-----------------------------
config-web's ``/api/plan`` endpoint calls :func:`tag_for_kind` on every planned action's
``action.kind`` to attach a badge to the preview row. Nothing else in rig touches this module —
it is presentation metadata, not part of the plan/execute engine itself.
"""

from __future__ import annotations

from dataclasses import dataclass

# The 9 real categories a planned action can fall into. Each key maps to the short display label
# (the badge text) shown in the config-web plan preview via ``tag.label`` (riglib/config_web.py's
# ``.plan-tag`` badge — currently one fixed style, not per-category colored).
CATEGORIES: dict[str, str] = {
    "creates_file": "creates file",
    "edits_file": "edits file",
    "symlinks": "symlinks",
    "installs_hook": "installs hook",
    "permission_guard": "permission / guard",
    "starts_reloads_service": "starts / reloads service",
    "remote_github_change": "remote GitHub change",
    "policy_only": "policy only (no disk effect)",
    "installs_system_tool": "installs system tool",
    # NOT a real action kind's category — the fallback :func:`tag_for_kind` uses for a kind this
    # registry has no entry for. Deliberately NOT "no disk effect" (that's what "policy_only"
    # falsely implied here before review): a plan builder can emit a kind ``run_plan`` has no
    # handler for (e.g. a typo — kinds are plain strings, nothing validates them at plan-build
    # time), and such an action DOES attempt to run — it just fails with "no handler for action
    # kind" at apply time. Claiming "no disk effect" for that in the PREVIEW would be actively
    # misleading, not just incomplete.
    "unknown": "unrecognized kind",
}

# Who feels the effect of an applied action of this kind. "agent" = a running coding-agent
# harness reads this (skills, hooks, MCP, permission settings); "human" = the developer's own
# workflow/tooling (git hooks, CI, GitHub repo settings, shell env); "both" = it changes something
# both consult (harness auto-mode, agent-hook guards a human could also trip).
AUDIENCE_AGENT = "agent"
AUDIENCE_HUMAN = "human"
AUDIENCE_BOTH = "both"


@dataclass(frozen=True)
class ActionTag:
    """The tag for one action KIND — a badge (category) + audience + a plain-language detail."""

    kind: str
    category: str  # a key into CATEGORIES
    audience: str  # AUDIENCE_AGENT | AUDIENCE_HUMAN | AUDIENCE_BOTH
    detail: str  # one-liner: what this touches on disk (or remotely) and why it matters

    @property
    def label(self) -> str:
        return CATEGORIES[self.category]


def _tag(kind: str, category: str, audience: str, detail: str) -> ActionTag:
    return ActionTag(kind=kind, category=category, audience=audience, detail=detail)


# One entry per riglib.actions.runner._HANDLERS key. Keep this list's keys in lockstep with that
# dict — tests/test_action_tags.py asserts they match exactly.
ACTION_TAGS: dict[str, ActionTag] = {
    "record_mode": _tag(
        "record_mode", "policy_only", AUDIENCE_BOTH,
        "Records that a category was considered disabled; writes nothing to disk.",
    ),
    "copy_skill": _tag(
        "copy_skill", "creates_file", AUDIENCE_AGENT,
        "Copies a skill's files into the skills target dir (default ~/.agents/skills/<name>).",
    ),
    "link_skill_harness": _tag(
        "link_skill_harness", "symlinks", AUDIENCE_AGENT,
        "Symlinks an installed skill into the harness's own discovery dir (e.g. ~/.claude/skills) "
        "so the agent harness actually lists/loads it.",
    ),
    "install_agent_hook": _tag(
        "install_agent_hook", "installs_hook", AUDIENCE_AGENT,
        "Writes an agent-hook descriptor into the harness hooks dir (e.g. ~/.claude/hooks) that "
        "intercepts a tool call before/after it runs.",
    ),
    "install_dispatcher": _tag(
        "install_dispatcher", "creates_file", AUDIENCE_HUMAN,
        "Installs the global git-hook dispatcher + fragments (e.g. secret-scan) and points "
        "core.hooksPath at it — runs on every git operation, not just for the agent.",
    ),
    "install_ci": _tag(
        "install_ci", "creates_file", AUDIENCE_HUMAN,
        "Writes a GitHub Actions workflow file under .github/workflows — runs in CI, not locally.",
    ),
    "register_mcp": _tag(
        "register_mcp", "edits_file", AUDIENCE_AGENT,
        "Merges a server entry into the harness's MCP config file (e.g. ~/.claude/mcp/mcp.json).",
    ),
    "apply_harness": _tag(
        "apply_harness", "permission_guard", AUDIENCE_AGENT,
        "Writes the auto/permission MODE key into the harness settings file — controls whether "
        "the agent auto-accepts tool calls.",
    ),
    "provision_permissions": _tag(
        "provision_permissions", "permission_guard", AUDIENCE_AGENT,
        "Merges allow/deny/ask rules into the harness settings file's permissions block.",
    ),
    "provision_execpolicy": _tag(
        "provision_execpolicy", "permission_guard", AUDIENCE_AGENT,
        "Writes an exec-policy rules block that constrains which shell commands the agent may run.",
    ),
    "install_harness_guard": _tag(
        "install_harness_guard", "installs_hook", AUDIENCE_AGENT,
        "Installs a harness-specific guard extension (e.g. a Codex/opencode policy file) that "
        "blocks a risky action before it executes.",
    ),
    "provision_harness_approval": _tag(
        "provision_harness_approval", "permission_guard", AUDIENCE_AGENT,
        "Writes an approval-policy key into the harness config controlling when it must ask "
        "before acting.",
    ),
    "provision_instruction_policy": _tag(
        "provision_instruction_policy", "permission_guard", AUDIENCE_AGENT,
        "Writes an instruction-following policy key into the harness config.",
    ),
    "register_hook_bridge": _tag(
        "register_hook_bridge", "installs_hook", AUDIENCE_AGENT,
        "Registers the shared agents-hooks/v1 bridge into another harness's own config "
        "(opencode plugin, codex TOML block, or omp extension symlink) so the same installed "
        "hook descriptors fire under that harness too.",
    ),
    "provision_schedule": _tag(
        "provision_schedule", "starts_reloads_service", AUDIENCE_BOTH,
        "Installs a launchd/cron schedule (e.g. the daily model-freshness check) that runs "
        "independently of any interactive session.",
    ),
    "provision_agents_symlink": _tag(
        "provision_agents_symlink", "symlinks", AUDIENCE_AGENT,
        "Symlinks a shared AGENTS.md-equivalent instruction file into a harness's expected "
        "location (e.g. CLAUDE.md).",
    ),
    "provision_ship_delegator": _tag(
        "provision_ship_delegator", "edits_file", AUDIENCE_HUMAN,
        "Writes the ship-delegator env file + wiring that lets `gh ship` reach this repo's "
        "merge gates from anywhere on the machine.",
    ),
    "provision_gh_ship_alias": _tag(
        "provision_gh_ship_alias", "edits_file", AUDIENCE_HUMAN,
        "Installs the `gh ship` alias script + registers it with the gh CLI.",
    ),
    "lint_policy_blocked": _tag(
        "lint_policy_blocked", "policy_only", AUDIENCE_BOTH,
        "Records that a linter policy could not be applied (e.g. no matching carrier); writes "
        "nothing to disk.",
    ),
    "format_policy_blocked": _tag(
        "format_policy_blocked", "policy_only", AUDIENCE_BOTH,
        "Records that a formatter policy could not be applied; writes nothing to disk.",
    ),
    "provision_linter_config": _tag(
        "provision_linter_config", "creates_file", AUDIENCE_HUMAN,
        "Writes a linter/formatter config file into the repo (e.g. a ruff/eslint config).",
    ),
    "provision_linter_bundle": _tag(
        "provision_linter_bundle", "creates_file", AUDIENCE_HUMAN,
        "Writes a bundle of related linter config files into the repo.",
    ),
    "provision_project_tool": _tag(
        "provision_project_tool", "creates_file", AUDIENCE_BOTH,
        "Writes a repo-local project-tool carrier file, or runs a one-shot tool operation "
        "(e.g. a code-index register/reindex) — check the action's detail for which.",
    ),
    "provision_github_ruleset": _tag(
        "provision_github_ruleset", "remote_github_change", AUDIENCE_HUMAN,
        "Creates/updates a branch-protection ruleset on the GitHub repo via the API — a remote "
        "setting, not a local file.",
    ),
    "provision_github_merge": _tag(
        "provision_github_merge", "remote_github_change", AUDIENCE_HUMAN,
        "Updates the GitHub repo's merge-button settings (squash/rebase, auto-delete-branch) "
        "via the API.",
    ),
    "provision_github_ghas": _tag(
        "provision_github_ghas", "remote_github_change", AUDIENCE_HUMAN,
        "Toggles GitHub Advanced Security features (secret scanning, push protection) on the "
        "remote repo via the API.",
    ),
    "provision_github_actions": _tag(
        "provision_github_actions", "remote_github_change", AUDIENCE_HUMAN,
        "Updates the GitHub repo's Actions permissions/settings via the API.",
    ),
    "provision_github_browser": _tag(
        "provision_github_browser", "remote_github_change", AUDIENCE_HUMAN,
        "A GitHub setting handled via browser automation, not the API. Skipped by default; "
        "with RIG_GH_BROWSER=1 it drives a real browser to change it live on github.com.",
    ),
    "provision_tmux": _tag(
        "provision_tmux", "starts_reloads_service", AUDIENCE_HUMAN,
        "Writes rig's tmux config + performs live activation (plugin clones, launchd boot/"
        "autosave agents, first resurrect save, stale-boot cleanup); never reloads a live "
        "tmux server.",
    ),
    "provision_env": _tag(
        "provision_env", "edits_file", AUDIENCE_HUMAN,
        "Writes rig.env.sh and a source line into the shell rc file — affects every new shell, "
        "not just the agent's.",
    ),
    "provision_global_excludes": _tag(
        "provision_global_excludes", "edits_file", AUDIENCE_HUMAN,
        "Writes a managed block into the machine-wide git excludesfile (core.excludesfile).",
    ),
    "provision_spotlight": _tag(
        "provision_spotlight", "starts_reloads_service", AUDIENCE_HUMAN,
        "Drops Spotlight-exclude sentinels under dependency/build dirs and may install a launchd "
        "re-sweep agent — a macOS Finder/Spotlight setting, not agent-facing.",
    ),
    "provision_tools": _tag(
        "provision_tools", "installs_system_tool", AUDIENCE_HUMAN,
        "Runs a personal CLI tool's own install.sh (e.g. tg/review/dev) — installs a real binary "
        "on this machine, outside any repo.",
    ),
    "provision_tg_ctl": _tag(
        "provision_tg_ctl", "starts_reloads_service", AUDIENCE_HUMAN,
        "Installs + (re)loads the tg-ctl inbound Telegram daemon's launchd LaunchAgent — a "
        "live machine-wide service, not scoped to one repo.",
    ),
}

# A defensive fallback for a kind this registry doesn't (yet) know — should never be hit once
# the completeness test passes, but the UI must render *something* rather than crash on a
# handler added without updating this file.
_UNKNOWN_TAG_DETAIL = (
    "Unrecognized action kind — the tag registry needs updating. This action's real effect is "
    "UNKNOWN to the preview; it may fail outright at apply time (\"no handler for action kind\")."
)


def tag_for_kind(kind: str) -> ActionTag:
    """The :class:`ActionTag` for a planned action's ``kind``. Falls back to a generic tag."""
    return ACTION_TAGS.get(
        kind,
        _tag(kind, "unknown", AUDIENCE_BOTH, _UNKNOWN_TAG_DETAIL),
    )
