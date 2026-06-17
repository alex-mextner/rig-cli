"""SetupState — the in-memory config the wizard edits and (de)serializes to rig.yaml.

This is the single serializer/parser bridging the interactive wizard and the on-disk
``rig.yaml``. The round-trip invariant: ``setup`` → ``to_yaml`` → ``apply --config`` must
produce the identical install plan. ``from_dict`` accepts a cascaded config dict (already
validated by :mod:`riglib.config`).

A v0.1 ``SetupState`` is intentionally a thin wrapper over the config dict — the schema is
already a faithful representation of the choices, so we don't introduce a parallel object
graph that could drift. The value this class adds is a known-good default scaffold and the
``to_yaml`` writer (lazy yaml import).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .github_merge import GITHUB_MERGE_DEFAULTS
from .github_ruleset import GITHUB_RULESET_DEFAULTS


def default_state(
    *,
    agent_tools_source: str | None = None,
    project_type: str = "unknown",
) -> dict[str, Any]:
    """A sensible default config (opt-out skills, security hooks on, security CI gates).

    The committed rig.yaml must be PORTABLE: it is replayed on other machines. So paths use
    ``~`` (expanded per-machine at apply time) and ``agent_tools_source`` is omitted for
    auto-detected sources (the caller only passes it when the user pinned one) — otherwise a
    machine-specific absolute path would disable the env/default fallback elsewhere.
    """
    # The github ruleset scaffold mirrors the action's sensible defaults exactly (one source),
    # plus the plan-gating `enabled` flag — so the committed rig.yaml and the action can never
    # drift apart.
    github_ruleset = {"enabled": True, **GITHUB_RULESET_DEFAULTS}
    # The github merge-button policy scaffold mirrors the action's secure defaults exactly (one
    # source), plus the plan-gating `enabled` flag — so the committed rig.yaml and the action can
    # never drift apart.
    github_merge = {"enabled": True, **GITHUB_MERGE_DEFAULTS}

    by_type_enable = [project_type] if project_type and project_type != "unknown" else []
    # Always the portable ``~/.config/git`` token (no machine-specific path, no env token
    # that goes unresolved elsewhere). At APPLY time, _expand() maps a ``~/.config`` prefix
    # to ``$XDG_CONFIG_HOME`` when that is set, so rig installs where the dispatcher runner
    # actually looks (``${XDG_CONFIG_HOME:-$HOME/.config}``) — matching without pinning.
    git_cfg = "~/.config/git"
    return {
        "version": 1,
        "defaults": {
            "skills_target": "~/.agents/skills",
            "hooks_target": "~/.claude/hooks",
            "ci_target": ".github/workflows",
            "mcp_target": "~/.claude/mcp",
            "on_conflict": "backup",
        },
        **({"agent_tools_source": agent_tools_source} if agent_tools_source else {}),
        "skills": {
            "enabled": True,
            "target": "~/.agents/skills",
            "universal": {"all": True, "disable": []},
            "by_type": {"enable": by_type_enable},
        },
        "agent_hooks": {
            "enabled": True,
            "target": "~/.claude/hooks",
            "target_kind": "claude-code",
            "all": True,
        },
        "git_hooks": {
            "dispatcher": {
                "enabled": True,
                "dir": os.path.join(git_cfg, "global-hooks.d"),
                "runner": os.path.join(git_cfg, "run-global-hooks"),
                "set_global_hooks_path": True,
                "install_local_retrofit_script": True,
                "fragments": {"secret-scan": {"enabled": True}},
            },
        },
        "ci": {
            "enabled": True,
            "target": ".github/workflows",
            "all": False,
            "items": {
                "secret-scan": {"enabled": True, "tier": "block"},
                "codeql": {"enabled": True, "tier": "block", "variant": "selfgate"},
                "dependency-review": {"enabled": True, "tier": "block"},
                "leftover-grep": {"enabled": True, "tier": "block"},
                "review-threads": {"enabled": True, "tier": "block"},
                "ship": {"enabled": True, "install_to": "~/bin", "gh_alias": True},
            },
        },
        "mcp": {
            "enabled": True,
            "target": "~/.claude/mcp",
            "items": {},
        },
        # Auto-mode is recommended ON by default — the agent runs autonomously with minimum
        # babysitting (the harness auto-accepts tool calls). This is only safe BECAUSE the
        # agent-hook guards above are installed (agent_hooks.all: true): block-secrets-write,
        # block-no-verify, enforce-timeout-on-bash, block-raw-process-env, and
        # block-raw-pr-merge intercept the dangerous calls before the side effect. Turn it
        # off with auto_mode: false (writes 'default' → interactive permission prompts).
        "harness": {
            "enabled": True,
            "kind": "claude-code",
            "auto_mode": True,
            # settings_path defaults to .claude/settings.json (repo-local, committed →
            # reproducible). Override to ~/.claude/settings.json for a machine-wide setting.
        },
        # Daily model-freshness checker: rig provisions a once-a-day cron (launchd on macOS,
        # crontab on Linux) that runs the agent-tools checker (lib/checker/model_freshness.py)
        # to propose model-version bumps. On `rig init` AND `rig apply`, rig checks whether the
        # schedule is installed and installs it if missing (idempotent). Default: NOON. Turn it
        # off with enabled: false. The checker path resolves from agent_tools_source unless a
        # checker_path is pinned. (Override the time via schedule.time: "HH:MM".)
        "models": {
            "enabled": True,
            "schedule": {"time": "12:00"},
        },
        # GitHub repository branch ruleset (the modern branch-protection replacement). On `rig
        # init` AND `rig apply`, rig reconciles a ruleset named `rig-managed` on the repo's
        # default branch via `gh api` — a no-op on a repo with no github remote. The SENSIBLE
        # default keeps merges WORKING: a PR is required (zero required reviews), force-push and
        # branch deletion are blocked, and the repo Admin role is a bypass actor so admins are
        # never locked out. rig NEVER emits the `update` ("Restrict updates") rule — a
        # hand-made ruleset with it + zero bypass actors blocks every merge to main. Opt out
        # with ruleset.enabled: false. Add status checks with required_status_checks: [names].
        # The knobs come straight from the action's GITHUB_RULESET_DEFAULTS (one source).
        # The github MERGE-button policy (squash-only, auto-delete head branch, allow auto-merge),
        # reconciled via `PATCH /repos/{o}/{r}` on the same `gh api` backend — a no-op on a repo
        # with no github remote. These are repo SETTINGS, not a ruleset, so they never lock anyone
        # out. Opt out with merge.enabled: false. The knobs come straight from the action's
        # GITHUB_MERGE_DEFAULTS (one source).
        "github": {"ruleset": github_ruleset, "merge": github_merge},
        # NB: `gitignore` (the GLOBAL git-excludes block) is deliberately NOT scaffolded into this
        # generated, COMMITTED repo `rig.yaml`. It is GLOBAL (machine-wide) config — it belongs in
        # the global rig layer (~/.config/rig/config.yaml), with zero per-repo commits — and it is
        # default-ON at PLAN level (an ABSENT `gitignore` key still provisions the block), so `rig
        # init` on a clean machine provisions it WITHOUT baking a global-config block into every
        # repo's committed file. Opt out with `gitignore: { enabled: false }` (in either layer).
    }


@dataclass
class SetupState:
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SetupState":
        return cls(data=dict(data))

    @classmethod
    def default(cls, **kwargs: Any) -> "SetupState":
        return cls(data=default_state(**kwargs))

    def to_yaml(self) -> str:
        import yaml  # lazy

        return yaml.safe_dump(self.data, sort_keys=False, default_flow_style=False)

    def write(self, path: Path) -> Path:
        path = Path(os.path.expanduser(str(path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# rig.yaml — declarative setup for this repo, applied by `rig apply`.\n"
            "# COMMITTED BY DEFAULT: this file is the reproducible source of truth.\n"
            "# Global defaults live at ~/.config/rig/config.yaml; this file overrides them.\n"
            "# See: rig status (drift), rig apply (converge). Schema: docs/config-schema.md\n\n"
        )
        path.write_text(header + self.to_yaml(), encoding="utf-8")
        return path
