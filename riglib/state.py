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

from .github_actions import GITHUB_ACTIONS_DEFAULTS
from .github_browser import UI_ONLY_TOGGLES
from .github_ghas import GITHUB_GHAS_DEFAULTS
from .github_merge import GITHUB_MERGE_DEFAULTS
from .github_ruleset import GITHUB_RULESET_DEFAULTS


def default_state(
    *,
    agent_tools_source: str | None = None,
    project_type: str = "unknown",
    stack: str | None = None,
) -> dict[str, Any]:
    """A sensible default config (opt-out skills, security hooks on, security CI gates).

    The committed rig.yaml must be PORTABLE: it is replayed on other machines. So paths use
    ``~`` (expanded per-machine at apply time) and ``agent_tools_source`` is omitted for
    auto-detected sources (the caller only passes it when the user pinned one) — otherwise a
    machine-specific absolute path would disable the env/default fallback elsewhere.
    """
    # The github scaffolds mirror each action's sensible defaults exactly (one source), plus the
    # plan-gating `enabled` flag — so the committed rig.yaml and the actions can never drift apart.
    # ruleset = branch protection; merge = merge-button policy; ghas = Advanced Security; actions =
    # Actions permissions; browser = the API-unreachable toggles driven via agent-browser.
    # Omit `required_status_checks` from the scaffold so the plan builder's §5 auto-default applies
    # (it requires the merge-gating CI gates the repo actually provisions — PR Checklist +
    # review-threads). Pinning an explicit `[]` here would read as a deliberate "require none" and
    # suppress that default; leaving the key out lets rig derive the right checks per-repo. A user
    # who wants a fixed set still adds `required_status_checks: [...]` by hand.
    github_ruleset = {
        "enabled": True,
        **{k: v for k, v in GITHUB_RULESET_DEFAULTS.items() if k != "required_status_checks"},
    }
    github_merge = {"enabled": True, **GITHUB_MERGE_DEFAULTS}
    github_ghas = {"enabled": True, **GITHUB_GHAS_DEFAULTS}
    github_actions = {"enabled": True, **GITHUB_ACTIONS_DEFAULTS}
    github_browser = {"enabled": True, **{k: v["default"] for k, v in UI_ONLY_TOGGLES.items()}}

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
        # The stack preset (l1/lang[/framework]) — written when init detected/confirmed one. Absent
        # when undetected (a non-interactive init on an unrecognized repo), which triggers the soft-
        # require warning rather than inventing a wrong stack. Selects the by-stack skills.
        **({"stack": stack} if stack else {}),
        "skills": {
            "enabled": True,
            "target": "~/.agents/skills",
            "universal": {"all": True, "disable": []},
            "by_type": {"enable": by_type_enable},
        },
        "agent_hooks": {
            "enabled": True,
            "target": "~/.claude/hooks",
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
                # PR-checklist gate (verifies every task-list checkbox is checked). Enabled so it
                # both lands as a workflow AND becomes a required status check on the ruleset
                # (ROADMAP §5 names "PR Checklist" + review-threads as the required merge gates).
                "pr-checklist": {"enabled": True, "tier": "block"},
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
        #
        # The full github area is reconciled here, all default-ON, opt-out per sub-block:
        #   merge   — squash-only merge model + auto-delete head branch + allow-auto-merge (PATCH).
        #   ghas    — dependency graph + vuln-alerts + Dependabot + secret-scanning + CodeQL.
        #   actions — Actions enabled + allowed_actions + READ-only GITHUB_TOKEN (least privilege).
        #   browser — settings the REST API does NOT expose, driven via agent-browser (gated OFF at
        #             apply unless RIG_GH_BROWSER=1; planned so `rig status` lists it).
        # Every sub-block is a no-op on a repo with no github remote, and every gh-api mutation
        # passes the #4136.1 auth gate (notify-and-wait for `gh auth login`) before it touches a
        # live setting.
        "github": {
            "ruleset": github_ruleset,
            "merge": github_merge,
            "ghas": github_ghas,
            "actions": github_actions,
            "browser": github_browser,
        },
        "project_tools": {
            "enabled": True,
            "haft": {
                "enabled": True,
                "codex_mcp": True,
                "workflow": {
                    "mode": "standard",
                    "require_decision": True,
                    "require_verify": True,
                    "allow_autonomy": False,
                },
            },
            "serena": {
                "enabled": True,
                "read_only": False,
                "ignored_paths": [],
            },
            "sverklo": {
                "enabled": True,
                "register": True,
                "reindex": False,
            },
        },
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
        # The `yaml-language-server` modeline makes editors (VS Code YAML, neovim) load the
        # committed JSON Schema → live key completion + unknown-key/bad-value squiggles, the same
        # rules `rig apply` enforces. `rig validate`/`rig schema` keep the file in sync.
        header = (
            "# yaml-language-server: $schema=schema/rig.schema.json\n"
            "# rig.yaml — declarative setup for this repo, applied by `rig apply`.\n"
            "# COMMITTED BY DEFAULT: this file is the reproducible source of truth.\n"
            "# Global defaults live at ~/.config/rig/config.yaml; this file overrides them.\n"
            "# See: rig status (drift), rig apply (converge). Schema: docs/config-schema.md\n\n"
        )
        path.write_text(header + self.to_yaml(), encoding="utf-8")
        return path
