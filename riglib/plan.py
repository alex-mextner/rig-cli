"""Plan builder — resolves (config + catalog) into an ordered list of Actions.

This is the headless engine ``rig apply`` and the wizard's Apply screen both call. It
turns the declarative ``rig.yaml`` decisions into concrete, idempotent install actions:

- resolves which catalog items are enabled (category ``enabled``, ``all``/``enable``/
  ``disable`` deltas, per-item ``items.<name>.enabled``, and the detected project type
  pulling in by-type skill bundles),
- resolves each item's install target (item → category → ``defaults`` → built-in),
- produces a stable, ordered ``InstallPlan`` of :class:`Action` objects.

An :class:`Action` is a small dataclass describing the change; the *execution* lives in
``actions/`` (stdlib-only). The plan never touches disk — ``--dry-run`` prints it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import Catalog, Item
from .config import (
    GITIGNORE_DEFAULT_ENTRIES,
    GITIGNORE_DEFAULT_EXCLUDESFILE,
    LoadedConfig,
    OPENCODE_HOOK_BRIDGE_PLUGIN_NAME,
)
from .github_actions import GITHUB_ACTIONS_DEFAULTS
from .github_browser import UI_ONLY_TOGGLES
from .github_ghas import GITHUB_GHAS_DEFAULTS
from .github_merge import GITHUB_MERGE_DEFAULTS
from .github_ruleset import CI_GATE_CHECK_CONTEXTS, GITHUB_RULESET_DEFAULTS
from .harness_skills import HARNESS_SKILL_DIRS as _HARNESS_SKILL_DIRS
from .harness_skills import instruction_file_for as _instruction_file_for
from .harness_skills import native_skills_dir_for as _native_skills_dir_for
from . import project_tools


class PlanError(ValueError):
    """Raised when the plan cannot be built (fail-closed before any write).

    Notably: an unknown item name in the config. The schema promises fail-closed
    validation for unknown items; catching it here (where the catalog is available)
    keeps ``apply`` from silently exiting 0 having installed nothing.
    """


# Built-in default targets per category (used when config + defaults don't pin one).
_BUILTIN_TARGETS = {
    "skills": "~/.agents/skills",
    "agent_hooks": "~/.claude/hooks",
    "ci": ".github/workflows",
    "mcp": "~/.claude/mcp",
}
_HARNESS_AGENT_HOOK_TARGETS = {
    "codex": "~/.codex/hooks",
    "opencode": "~/.config/opencode/hooks",
}
_DEFAULTS_KEY = {
    "skills": "skills_target",
    "agent_hooks": "hooks_target",
    "ci": "ci_target",
    "mcp": "mcp_target",
}


# Per-harness skill/instruction discovery lives in ONE registry, :mod:`riglib.harness_skills`.
# ``_HARNESS_SKILL_DIRS`` (imported above) is the skills-DIRECTORY map: a skill copied into
# ``skills_target`` (default ``~/.agents/skills``) is invisible to a skills-dir harness unless it
# is also present in that harness's discovery dir (claude-code → ``~/.claude/skills``, opencode →
# ``~/.config/opencode/skill``), so rig maintains an idempotent symlink per enabled skill into the
# harness dir for the configured kind. INSTRUCTION-FILE harnesses (codex/gemini/pi/commandcode)
# have no skills dir — they surface guidance via a global AGENTS.md/GEMINI.md (the ``agents_md``
# area), so :func:`_resolve_harness_skill_dir` returns None for them and records a status note.
# (The module-level alias name is preserved so ``plan._HARNESS_SKILL_DIRS`` keeps resolving.)
_DEFAULT_HARNESS_KIND = "claude-code"


@dataclass
class Action:
    """A single planned install step. ``kind`` selects the runner in ``actions/``."""

    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | provision_permissions | register_hook_bridge | provision_schedule | provision_agents_symlink | provision_project_tool | provision_github_ruleset | provision_github_merge | provision_github_ghas | provision_github_actions | provision_github_browser | provision_tmux | provision_global_excludes
    category: str
    item: str
    source: Path  # carrier path in the agent-tools checkout
    target: Path  # resolved install destination (expanded, absolute where applicable)
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.category}/{self.item} → {self.target}"


@dataclass
class InstallPlan:
    actions: list[Action] = field(default_factory=list)
    on_conflict: str = "backup"
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.actions)


def _expand(path_str: str, repo_root: Path) -> Path:
    # Per-machine expansion at apply time keeps a committed rig.yaml portable.
    #  - a portable ``~/.config/...`` prefix maps to ``$XDG_CONFIG_HOME`` when set, so rig
    #    installs where XDG-aware tools (the dispatcher runner) actually look;
    #  - ``$VAR``/``${VAR}`` and ``~`` are expanded;
    #  - a relative remainder is anchored at the repo root.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and (path_str == "~/.config" or path_str.startswith("~/.config/")):
        path_str = xdg + path_str[len("~/.config"):]
    p = Path(os.path.expanduser(os.path.expandvars(path_str)))
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return p


def _resolve_target(config: LoadedConfig, category: str) -> Path:
    cat = config.category(category)
    target = cat.get("target")
    if not target:
        target = config.defaults.get(_DEFAULTS_KEY[category])
    if not target:
        target = _BUILTIN_TARGETS[category]
    return _expand(str(target), config.repo_root)


def _resolve_agent_hooks_target(config: LoadedConfig) -> Path:
    """Resolve the agent-hooks descriptor dir, defaulting to the active harness when needed."""
    ah = config.category("agent_hooks")
    target = ah.get("target")
    defaults_target = config.defaults.get(_DEFAULTS_KEY["agent_hooks"])
    kind = _harness_kind_for_skills(config)
    harness_default = _HARNESS_AGENT_HOOK_TARGETS.get(kind)
    legacy_default = _BUILTIN_TARGETS["agent_hooks"]
    if harness_default and (not target or target == legacy_default) and (
        not defaults_target or defaults_target == legacy_default
    ):
        return _expand(harness_default, config.repo_root)
    return _resolve_target(config, "agent_hooks")


def _same_dir(a: Path, b: Path) -> bool:
    """True when two paths denote the same directory, resolving symlinks/`..` where possible.

    Falls back to a lexical compare if ``resolve()`` raises (e.g. a path under a non-existent
    parent on a platform that resolves strictly) — never raises.
    """
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _harness_kind_for_skills(config: LoadedConfig) -> str:
    """The harness kind whose skill-discovery dir rig links into.

    Follows the ``harness.kind`` if a harness block pins one (so the skill links target the
    same harness the auto-mode write does); else the built-in default (claude-code).
    """
    h = config.data.get("harness")
    if isinstance(h, dict) and h.get("kind"):
        return str(h["kind"])
    return _DEFAULT_HARNESS_KIND


def _resolve_harness_skill_dir(config: LoadedConfig) -> Path | None:
    """Resolve the harness skill-discovery dir to symlink installed skills into.

    Returns ``None`` when ``skills.harness_link`` is disabled or the harness kind has no
    known discovery dir — either an INSTRUCTION-FILE harness (codex/gemini/pi/commandcode,
    which surface skills via AGENTS.md/GEMINI.md, not a symlinked dir) or an unknown kind. We
    never guess a path. An explicit ``skills.harness_skill_dir`` overrides the per-harness
    default (and forces the link even for an instruction-file harness — the user pointed at a
    real dir on purpose).
    """
    sk = config.category("skills")
    if sk.get("harness_link") is False:
        return None
    raw = sk.get("harness_skill_dir")
    if not raw:
        kind = _harness_kind_for_skills(config)
        raw = _HARNESS_SKILL_DIRS.get(kind)
        if not raw:
            return None
    return _expand(str(raw), config.repo_root)


def _skill_discovery_note(config: LoadedConfig) -> str | None:
    """A status note explaining why no harness skill-link is emitted for an instruction-file
    harness, or ``None`` when one is (skills-dir harness) or linking is disabled / overridden.

    Keeps ``rig status`` honest: a codex/gemini/pi/commandcode config that links no skills isn't a
    silent gap — the note says the kind reads a global AGENTS.md/GEMINI.md instead, so the skill
    content reaches it through the ``agents_md`` area, not a per-skill symlink.
    """
    sk = config.category("skills")
    if sk.get("enabled") is False:
        return None  # no skills installed at all → nothing to say about their discovery
    if sk.get("harness_link") is False or sk.get("harness_skill_dir"):
        return None  # linking off, or an explicit dir override forces a real link → no note
    kind = _harness_kind_for_skills(config)
    if kind in _HARNESS_SKILL_DIRS:
        return None  # skills-dir harness → a link IS emitted, no note needed
    native = _native_skills_dir_for(kind)
    if native is not None:
        return (
            f"skills: harness '{kind}' auto-loads {native} natively — no per-skill symlink "
            "needed (skills install to the default skills_target, which it already scans)"
        )
    instr = _instruction_file_for(kind)
    if instr is None:
        return None  # unknown kind → handled by validation; nothing to say here
    return (
        f"skills: harness '{kind}' has no skill-discovery dir — it reads a global "
        f"instruction file ({instr}); skills reach it via the agents_md area, not a per-skill "
        "symlink (set skills.harness_skill_dir to force a directory link)"
    )


def resolve_category_target(config: LoadedConfig, category: str) -> Path | None:
    """Public: the resolved install dir for a category (None when ``export-only``).

    Used by ``rig status`` to scan a category's configured target for disk→config extras
    even when no action lands there.
    """
    raw = config.category(category).get("target")
    if raw == "export-only":
        return None
    if category == "agent_hooks":
        return _resolve_agent_hooks_target(config)
    return _resolve_target(config, category)


def _item_enabled(cat_cfg: dict[str, Any], item: Item, *, type_enabled: bool) -> bool:
    """Resolve a single item's enabled state from category-level deltas + per-item override.

    Precedence (highest first):
      1. explicit per-item ``items.<name>.enabled``
      2. ``enable:`` / ``disable:`` delta lists
      3. ``all: true|false``
      4. project-type pull-in (by-type skills) / catalog ``default_enabled``
    """
    items = cat_cfg.get("items", {})
    if isinstance(items, dict) and item.name in items:
        spec = items[item.name]
        if isinstance(spec, dict) and "enabled" in spec:
            return bool(spec["enabled"])

    enable = set(cat_cfg.get("enable", []) or [])
    disable = set(cat_cfg.get("disable", []) or [])
    if item.name in disable:
        return False
    if item.name in enable:
        return True

    all_flag = cat_cfg.get("all")
    if all_flag is True:
        return True
    if all_flag is False:
        return False

    return type_enabled or item.default_enabled


def _skills_enabled(config: LoadedConfig, catalog: Catalog, project_type: str) -> list[Item]:
    """Skills get special handling: nested universal{} / by_type{} groups + type pull-in."""
    sk = config.category("skills")
    if sk.get("enabled") is False:
        return []
    out: list[Item] = []

    uni_cfg = sk.get("universal", {})
    if not isinstance(uni_cfg, dict):
        uni_cfg = {}
    # default for universal is all-on (opt-out model)
    uni_cfg.setdefault("all", True)

    bt_cfg = sk.get("by_type", {})
    if not isinstance(bt_cfg, dict):
        bt_cfg = {}
    enabled_kinds = set(bt_cfg.get("enable", []) or [])
    # auto-pull the detected project type's bundle unless config explicitly lists kinds
    if not enabled_kinds and project_type and project_type != "unknown":
        enabled_kinds = {project_type}
    bt_items_cfg = bt_cfg.get("items", {})
    if not isinstance(bt_items_cfg, dict):
        bt_items_cfg = {}

    for item in catalog.by_category("skills"):
        if item.group == "universal":
            if _item_enabled(uni_cfg, item, type_enabled=True):
                out.append(item)
        elif item.group.startswith("by-type/"):
            kind = item.meta.get("kind", "")
            type_on = kind in enabled_kinds
            # per-item override under by_type.items.<by-type/kind/name>
            if item.name in bt_items_cfg:
                spec = bt_items_cfg[item.name]
                if isinstance(spec, dict) and "enabled" in spec:
                    if bool(spec["enabled"]):
                        out.append(item)
                    continue
            if type_on:
                out.append(item)
    return out


def _validate_item_names(config: LoadedConfig, catalog: Catalog) -> None:
    """Fail-closed on item names that don't exist in the catalog (typo guard).

    Checks every place a config can reference a catalog item by name: ``enable``/
    ``disable`` lists and ``items.<name>`` keys, across all categories. Skills are special:
    universal/by_type are separate groups, and by_type uses fully-qualified
    ``by-type/<kind>/<name>`` keys plus bare ``<kind>`` bundle names in ``by_type.enable``.
    """
    from . import errors

    def _check(category: str, names: set[str], known: set[str], key_prefix: str) -> None:
        """Raise a structured :class:`errors.UnknownItemError` for the first unknown name.

        ``category`` is the catalog category used for the removed-slot lookup + did-you-mean;
        ``key_prefix`` is the dotted config-key path (e.g. ``mcp.items``) the bad name hangs
        off, so the error names the EXACT offending key (``mcp.items.review``) + its file. The
        file is resolved by the key's PROVENANCE (``source_for_key``): a stale entry that came
        solely from the global config is reported against the global file, not the repo's.
        """
        unknown = sorted(names - known)
        if unknown:
            bad = unknown[0]
            key = f"{key_prefix}.{bad}"
            raise errors.unknown_item_error(
                category=category,
                key=key,
                bad=bad,
                known=known,
                config_path=str(config.source_for_key(key)),
            )

    # skills — universal group
    sk = config.category("skills")
    uni = sk.get("universal", {}) if isinstance(sk, dict) else {}
    if isinstance(uni, dict):
        uni_known = {i.name for i in catalog.by_category("skills") if i.group == "universal"}
        refs = set(uni.get("enable", []) or []) | set(uni.get("disable", []) or [])
        refs |= set(k for k in uni.get("items", {}) if isinstance(uni.get("items"), dict))
        _check("skills", refs, uni_known, "skills.universal")
    # skills — by_type group (fully-qualified item keys; bundle names checked separately)
    bt = sk.get("by_type", {}) if isinstance(sk, dict) else {}
    if isinstance(bt, dict):
        bt_known = {i.name for i in catalog.by_category("skills") if i.group.startswith("by-type/")}
        bt_kinds = {i.meta.get("kind", "") for i in catalog.by_category("skills") if i.group.startswith("by-type/")}
        bt_items = bt.get("items", {})
        if isinstance(bt_items, dict):
            _check("skills", set(bt_items), bt_known, "skills.by_type.items")
        _check("skills", set(bt.get("enable", []) or []), bt_kinds, "skills.by_type.enable")

    # agent_hooks + mcp — flat items/enable/disable
    for cat_name in ("agent_hooks", "mcp"):
        cfg = config.category(cat_name)
        if not isinstance(cfg, dict):
            continue
        known = catalog.names(cat_name)
        refs = set(cfg.get("enable", []) or []) | set(cfg.get("disable", []) or [])
        items = cfg.get("items", {})
        if isinstance(items, dict):
            refs |= set(items)
        _check(cat_name, refs, known, f"{cat_name}.items")

    # git_hooks — nested sub-groups (only 'dispatcher' is shipped in v0.1). A typo like
    # 'dispatcherr' must fail closed, not silently build no dispatcher action.
    gh = config.category("git_hooks")
    if isinstance(gh, dict):
        gh_known = catalog.names("git_hooks") | {"templates"}  # templates reserved for v0.2
        _check("git_hooks", set(gh), gh_known, "git_hooks")


def build(config: LoadedConfig, catalog: Catalog, *, project_type: str = "unknown") -> InstallPlan:
    """Build the ordered :class:`InstallPlan` from config + catalog."""
    plan = InstallPlan(on_conflict=str(config.defaults.get("on_conflict", "backup")))
    _validate_item_names(config, catalog)

    # ── skills ───────────────────────────────────────────────────────────────────
    skills_target = _resolve_target(config, "skills")
    harness_link_dir = _resolve_harness_skill_dir(config)
    # Whether to emit harness-discovery symlinks: only when a dir is configured AND it is not
    # the install dir itself (no self-link). Compare resolved paths so a HOME symlink or a
    # ``..`` segment that makes the two dirs textually differ but point at the same place is
    # still recognized as the same dir (avoids a spurious self-link → real-dir warning).
    link_into_harness = harness_link_dir is not None and not _same_dir(harness_link_dir, skills_target)
    # Instruction-file harness (codex/gemini/pi/commandcode) → no skill-link dir; record WHY so
    # ``rig status`` shows "uses <AGENTS.md/GEMINI.md>" instead of a silent empty skill-link area.
    skill_note = _skill_discovery_note(config)
    if skill_note is not None:
        plan.notes.append(skill_note)
    for item in _skills_enabled(config, catalog, project_type):
        installed = skills_target / item.path.name
        plan.actions.append(
            Action(
                kind="copy_skill",
                category="skills",
                item=item.name,
                source=item.path,
                target=installed,
            )
        )
        # Make the installed skill discoverable by the harness: symlink it into the harness's
        # skill dir (claude-code: ~/.claude/skills). Without this the skill sits in
        # skills_target but the harness never lists/loads it.
        if link_into_harness:
            plan.actions.append(
                Action(
                    kind="link_skill_harness",
                    category="skills",
                    item=item.name,
                    source=installed,  # the installed skill the symlink points at
                    target=harness_link_dir / item.path.name,
                )
            )

    # ── agent_hooks ──────────────────────────────────────────────────────────────
    ah = config.category("agent_hooks")
    if ah.get("enabled") is not False:
        ah.setdefault("all", True)
        hooks_target = _resolve_agent_hooks_target(config)
        target_kind = ah.get("target_kind") or "claude-code"
        for item in catalog.by_category("agent_hooks"):
            if _item_enabled(ah, item, type_enabled=False):
                spec = ah.get("items", {}).get(item.name, {})
                plan.actions.append(
                    Action(
                        kind="install_agent_hook",
                        category="agent_hooks",
                        item=item.name,
                        source=item.path,
                        target=hooks_target,
                        options={
                            "descriptor": item.meta.get("descriptor", ""),
                            "target_kind": target_kind,
                            "on_error": spec.get("on_error") if isinstance(spec, dict) else None,
                            "agent_tools_source": str(catalog.source),
                        },
                    )
                )

    # ── git_hooks (dispatcher) ───────────────────────────────────────────────────
    gh = config.category("git_hooks")
    disp_cfg = gh.get("dispatcher", {}) if isinstance(gh, dict) else {}
    if isinstance(disp_cfg, dict) and disp_cfg.get("enabled"):
        item = catalog.get("git_hooks", "dispatcher")
        if item is not None:
            runner = disp_cfg.get("runner", "~/.config/git/run-global-hooks")
            plan.actions.append(
                Action(
                    kind="install_dispatcher",
                    category="git_hooks",
                    item="dispatcher",
                    source=item.path,
                    target=_expand(str(disp_cfg.get("dir", "~/.config/git/global-hooks.d")), config.repo_root),
                    options={
                        "runner": str(_expand(str(runner), config.repo_root)),
                        "set_global_hooks_path": bool(disp_cfg.get("set_global_hooks_path", True)),
                        "install_local_retrofit_script": bool(
                            disp_cfg.get("install_local_retrofit_script", True)
                        ),
                        "fragments": disp_cfg.get("fragments", {}),
                    },
                )
            )

    # ── ci ───────────────────────────────────────────────────────────────────────
    ci = config.category("ci")
    if ci.get("enabled") is not False:
        ci_target_raw = ci.get("target", config.defaults.get("ci_target", ".github/workflows"))
        export_only = ci_target_raw == "export-only"
        ci_target = None if export_only else _resolve_target(config, "ci")
        ci_items = ci.get("items", {})
        if not isinstance(ci_items, dict):
            ci_items = {}

        # fail-closed: an unknown item name (in items:, enable:, or disable:) is a config
        # typo, not a silent skip. 'ship' is a real catalog item (ci/ship/) — it is in
        # catalog.names("ci") when present and ABSENT when the checkout lacks it, so a
        # config enabling ship against a minimal checkout fails closed instead of dropping.
        known = catalog.names("ci")
        referenced = set(ci_items) | set(ci.get("enable", []) or []) | set(ci.get("disable", []) or [])
        unknown = sorted(referenced - known)
        if unknown:
            from . import errors

            bad = unknown[0]
            key = f"ci.items.{bad}"
            raise errors.unknown_item_error(
                category="ci",
                key=key,
                bad=bad,
                known=known,
                config_path=str(config.source_for_key(key)),
            )

        # resolve which slots are enabled: per-item override > enable/disable > all > off.
        for item in catalog.by_category("ci"):
            name = item.name
            spec = ci_items.get(name, {})
            if not isinstance(spec, dict):
                spec = {}
            if not _item_enabled(ci, item, type_enabled=False):
                continue
            if export_only:
                plan.notes.append(f"ci/{name} export-only (recorded, not written)")
                continue
            if name == "ship":
                plan.actions.append(
                    Action(
                        kind="install_ci",
                        category="ci",
                        item="ship",
                        source=item.path,
                        target=_expand(str(spec.get("install_to", "~/bin")), config.repo_root),
                        options={"slot": "ship", "gh_alias": bool(spec.get("gh_alias", False))},
                    )
                )
                continue
            plan.actions.append(
                Action(
                    kind="install_ci",
                    category="ci",
                    item=name,
                    source=item.path,
                    target=ci_target,  # type: ignore[arg-type]
                    options={
                        "slot": name,
                        "tier": spec.get("tier", "block"),
                        "variant": spec.get("variant"),
                        # companions install relative to the CHECKOUT ROOT (the workflow
                        # runs `bash ci/<slot>/x.sh` from there), independent of ci.target.
                        "repo_root": str(config.repo_root),
                    },
                )
            )

    # ── mcp ──────────────────────────────────────────────────────────────────────
    mcp = config.category("mcp")
    if mcp.get("enabled") is not False:
        mcp_target_raw = mcp.get("target", config.defaults.get("mcp_target", "~/.claude/mcp"))
        if mcp_target_raw != "export-only":
            mcp_target = _expand(str(mcp_target_raw), config.repo_root)
            mcp_items = mcp.get("items", {})
            if not isinstance(mcp_items, dict):
                mcp_items = {}
            for item in catalog.by_category("mcp"):
                # honor category all/enable/disable + per-item enabled (same as other cats)
                if not _item_enabled(mcp, item, type_enabled=False):
                    continue
                spec = mcp_items.get(item.name, {})
                if not isinstance(spec, dict):
                    spec = {}
                # the registration KEY is the configured server name if set, else the item
                # name — so `server: serena` registers under "serena", not the catalog key.
                server_name = str(spec.get("server") or item.name)
                options = {
                    "command": spec.get("command", ""),
                    "server": server_name,
                }
                if "args" in spec:
                    options["args"] = spec.get("args", [])
                if "env" in spec:
                    options["env"] = spec.get("env", {})
                plan.actions.append(
                    Action(
                        kind="register_mcp",
                        category="mcp",
                        item=item.name,
                        source=item.path,
                        target=mcp_target,
                        options=options,
                    )
                )

    # ── harness (auto-mode / permission provisioning) ─────────────────────────────
    _build_harness(config, plan)

    # ── permissions (per-harness command allowlist) ───────────────────────────────
    _build_permissions(config, plan)

    # ── hook bridge (make agents-hooks/v1 descriptors FIRE in the harness) ─────────
    _build_hook_bridge(config, catalog, plan)

    # ── models (daily model-freshness checker schedule) ───────────────────────────
    _build_models(config, catalog, plan)

    # ── agents_md (AGENTS.md canonical + CLAUDE.md symlink) ────────────────────────
    _build_agents_symlink(config, plan)

    # ── ship_delegator (per-repo .claude/scripts/pr-ship.sh so `gh ship` works here) ─
    _build_ship_delegator(config, catalog, plan)

    # ── linters (per-repo linter/formatter config files) ──────────────────────────
    _build_linters(config, plan)

    # ── project_tools (Haft / Serena / Sverklo repo integrations) ─────────────────
    _build_project_tools(config, plan)

    # ── github (repository settings via gh api + agent-browser) ───────────────────
    # ORDER MATTERS — actions run in this build order (runner.run_plan iterates plan.actions
    # in sequence, no sort). Enable GitHub Actions BEFORE GHAS: CodeQL default-setup
    # (provisioned by _build_github_ghas) requires Actions to be enabled — GitHub rejects
    # default-setup with "Actions must be enabled for default setup" otherwise. Building
    # actions first makes a brand-new repo converge in ONE apply instead of needing a second.
    _build_github_ruleset(config, catalog, plan)
    _build_github_merge(config, plan)
    _build_github_actions(config, plan)
    _build_github_ghas(config, plan)
    _build_github_browser(config, plan)

    # ── tmux (rig-managed tmux configuration) ──────────────────────────────────────
    _build_tmux(config, plan)

    # ── gitignore (rig-managed block in the GLOBAL git excludes file) ──────────────
    _build_global_excludes(config, plan)

    # ── tools (the personal CLI ecosystem: tg/review/task/draw/…) ──────────────────
    _build_tools(config, plan)

    # ── tg_ctl (rig-managed tg-ctl inbound daemon LaunchAgent) ─────────────────────
    _build_tg_ctl(config, plan)

    return plan


# Per-harness settings file. NON-auto modes are written to the repo's PROJECT settings
# (committed, travels with the repo). `auto` is special: Claude Code IGNORES
# `permissions.defaultMode: auto` from project/local settings (v2.1.142+) and honors it ONLY
# from the user's machine settings — so auto-mode is provisioned per-MACHINE, not per-repo.
_HARNESS_SETTINGS = {
    "claude-code": ".claude/settings.json",
}
_HARNESS_AUTO_USER_SETTINGS = {
    "claude-code": "~/.claude/settings.json",
}
# The permission-mode value each harness uses for auto-accept, keyed by (kind, auto_mode).
# claude-code: `auto` (research preview) auto-approves WITH a safety classifier — preferred
# over `bypassPermissions` (which skips every check; container/VM only). `default` restores
# prompts. Pin `harness.mode: bypassPermissions` to opt back into full bypass (non-qualifying
# account / container) — that value IS committable at project scope.
_HARNESS_AUTO_MODE = {
    "claude-code": {True: "auto", False: "default"},
}


def _build_harness(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the harness auto/permission write, if a ``harness`` block is present.

    No harness block → no action (rig leaves the harness config untouched). With a block,
    one ``apply_harness`` action carries the resolved settings file + the permission-mode
    key/value to merge. The plan stays pure; the merge happens in ``actions/``.
    """
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h:
        return
    if h.get("enabled") is False:
        return
    kind = str(h.get("kind", "claude-code"))
    if kind not in _HARNESS_SETTINGS:
        # The config schema now ACCEPTS opencode/codex/gemini/pi/commandcode (rig provisions their
        # SKILL discovery), but the auto/permission-MODE write is only implemented for the kinds in
        # ``_HARNESS_SETTINGS`` (claude-code today). Skip the auto-mode write for the others — but
        # say so, so a config that set ``auto_mode``/``mode`` on such a kind isn't a silent no-op.
        if h.get("auto_mode") is not None or h.get("mode"):
            plan.notes.append(
                f"harness: auto-mode write skipped — kind '{kind}' has no rig auto/permission-mode "
                "writer yet (its skills are still provisioned; set the mode in the harness's own "
                "config for now)"
            )
        return
    auto_mode = bool(h.get("auto_mode", False))
    # an explicit `mode:` override wins over the auto_mode → mode mapping (lets a config pin
    # e.g. `acceptEdits` instead of full bypass while staying non-interactive for edits).
    mode_value = h.get("mode") or _HARNESS_AUTO_MODE[kind][auto_mode]
    # `auto` is honored only from the user's machine settings (CC strips it from project/local
    # scope); every other mode writes to the repo's project settings. Explicit settings_path wins.
    if h.get("settings_path"):
        settings_path = h["settings_path"]
    elif mode_value == "auto":
        settings_path = _HARNESS_AUTO_USER_SETTINGS[kind]
    else:
        settings_path = _HARNESS_SETTINGS[kind]
    plan.actions.append(
        Action(
            kind="apply_harness",
            category="harness",
            item=kind,
            source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
            target=_expand(str(settings_path), config.repo_root),
            options={
                "kind": kind,
                "auto_mode": auto_mode,
                "mode_value": str(mode_value),
            },
        )
    )


def _build_permissions(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the per-harness permissions provisioning (allow + deny/ask), unless ``enabled: false``.

    Default **ON** (like ``agents_md``/``github``/``tg_ctl``): an ABSENT or empty ``permissions:``
    block still provisions the allowlist with the default tool set AND the conservative deny/ask
    rule baselines (rig-cli#100 — the outer belt), so ``rig init`` on a clean machine gets both
    with no config at all.

    Everything is CONFIG-DRIVEN — ``permissions.tools`` (a list) REPLACES the default set;
    ``permissions.extra`` adds; ``permissions.disable`` removes; ``permissions.allow`` adds RAW
    rule entries on top of the tool-derived allowlist; ``permissions.deny``/``ask`` REPLACE the
    baked rule baselines. The action carries the RESOLVED lists (so the runner stays config-pure)
    and is keyed off ``harness.kind`` (exactly like the auto-mode write), targeting the SAME
    per-harness user-scope settings file. A harness whose kind has no additively-mergeable
    allowlist (codex, gemini/pi — see :mod:`riglib.permissions`) emits NO action and is recorded
    N/A; a note explains why so ``rig status`` isn't silently empty.
    """
    from .permissions import (
        HARNESS_ALLOWLISTS,
        HARNESS_ALLOWLIST_NA,
        harness_supported,
        resolve_tools,
    )

    p = config.data.get("permissions")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return  # validate() already fail-closed on a non-mapping block
    if p.get("enabled") is False:
        return

    # The harness kind to provision for: an explicit `permissions.kind` wins (so opencode can be
    # targeted INDEPENDENTLY of the auto-mode write, whose `harness.kind` validator rejects opencode
    # as not-yet-implemented — the allowlist provisioning DOES support it); else follow `harness.kind`
    # if pinned; else the built-in default (claude-code). One allowlist per harness, since each
    # harness's settings file is distinct.
    h = config.data.get("harness")
    if p.get("kind"):
        kind = str(p["kind"])
    elif isinstance(h, dict) and h.get("kind"):
        kind = str(h["kind"])
    else:
        kind = _DEFAULT_HARNESS_KIND

    if not harness_supported(kind):
        reason = HARNESS_ALLOWLIST_NA.get(kind, "no command-allowlist mechanism")
        plan.notes.append(f"permissions: skipped — harness '{kind}' has no allowlist to provision ({reason})")
        return

    tools_cfg = p.get("tools")
    tools = resolve_tools(
        list(tools_cfg) if isinstance(tools_cfg, list) else None,
        list(p.get("extra", []) or []) if isinstance(p.get("extra"), list) else [],
        list(p.get("disable", []) or []) if isinstance(p.get("disable"), list) else [],
    )
    allow_rules, deny_rules, ask_rules = _resolve_permission_rules(p, kind, plan)
    spec = HARNESS_ALLOWLISTS[kind]
    # An explicit settings_path wins (lets a test/odd setup point elsewhere); else the harness's
    # documented per-machine settings file (the SAME file the auto-mode write targets for CC).
    settings_path = p.get("settings_path") or spec.settings_path
    plan.actions.append(
        Action(
            kind="provision_permissions",
            category="permissions",
            item=kind,
            source=config.repo_root,  # no carrier in agent-tools; anchor on the repo
            target=_expand(str(settings_path), config.repo_root),
            options={"kind": kind, "tools": tools, "allow_rules": allow_rules,
                     "deny_rules": deny_rules, "ask_rules": ask_rules},
        )
    )


def _resolve_permission_rules(
    p: dict[str, Any], kind: str, plan: InstallPlan
) -> tuple[list[str], list[str], list[str]]:
    """Resolve the raw allow entries + the deny/ask rule lists for the permissions action.

    ``allow`` is ADDITIVE raw entries on top of the tool-derived allowlist (this is how the
    hand-grown machine allowlist is adopted as declared config); ``deny``/``ask`` REPLACE the
    baked baseline (see :mod:`riglib.permissions` — an explicit ``[]`` disables it). All three
    are RAW rule strings in claude-code's dialect, so a harness kind with no VERIFIED rule
    dialect (opencode: its ``permission.bash`` glob keys are a DIFFERENT syntax) gets none of
    them — with a plan note when the config explicitly asked, so the drop is visible in
    ``rig plan``, never silent (a claude-shaped rule written as an opencode glob key would be a
    bogus entry that never matches).
    """
    from .permissions import HARNESS_RULE_CONTAINERS, HARNESS_RULES_NA, resolve_rules

    if kind not in HARNESS_RULE_CONTAINERS:
        dropped = [k for k in ("allow", "deny", "ask") if isinstance(p.get(k), list)]
        if dropped:
            reason = HARNESS_RULES_NA.get(kind, "no verified rule dialect")
            plan.notes.append(
                f"permissions: raw {'/'.join(dropped)} entries dropped — harness '{kind}' ({reason})"
            )
        return [], [], []
    allow_cfg = p.get("allow")
    allow_rules: list[str] = []
    seen: set[str] = set()
    for entry in (allow_cfg if isinstance(allow_cfg, list) else []):
        if str(entry) not in seen:
            seen.add(str(entry))
            allow_rules.append(str(entry))
    deny = resolve_rules(kind, "deny", list(p["deny"]) if isinstance(p.get("deny"), list) else None)
    ask = resolve_rules(kind, "ask", list(p["ask"]) if isinstance(p.get("ask"), list) else None)
    return allow_rules, deny, ask


_HOOK_BRIDGE_HARNESSES = {
    "claude-code": {
        "module": "cc_hook_bridge",
        "settings": ".claude/settings.json",
        "format": "json",
    },
    "codex": {
        "module": "codex_hook_bridge",
        "settings": "~/.codex/config.toml",
        "format": "toml",
    },
    "opencode": {
        "module": "opencode_hook_bridge",
        "settings": f".opencode/plugins/{OPENCODE_HOOK_BRIDGE_PLUGIN_NAME}",
        "format": "opencode-plugin",
    },
}


def _build_hook_bridge(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the agents-hooks/v1 → harness bridge registration, if applicable.

    Harnesses do not run the ``agent_hooks`` descriptors directly; they need a bridge registered
    in their own config. This emits one ``register_hook_bridge`` action that wires the relevant
    dispatcher from ``agent-tools/lib`` into that harness config.

    Gated on a harness block being present, enabled, of a supported kind, AND
    ``agent_hooks`` being enabled (a bridge with no installed descriptors is pointless) AND
    ``harness.hook_bridge`` not turned off. Anchored on the resolved agent-tools checkout so
    the dispatcher command imports the harness bridge from ``<checkout>/lib``.
    """
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h or h.get("enabled") is False:
        return
    kind = str(h.get("kind", _DEFAULT_HARNESS_KIND))
    bridge_cfg = h.get("hook_bridge")
    bridge_spec = _HOOK_BRIDGE_HARNESSES.get(kind)
    if bridge_spec is None:
        # Reaching this branch means the harness has skill/instruction discovery but no known
        # hook bridge surface yet (currently gemini/pi/commandcode). If a config EXPLICITLY
        # asked for the bridge on such a kind, say it is not wired; the default-on case stays
        # quiet.
        if isinstance(bridge_cfg, dict) and bridge_cfg.get("enabled") is True:
            plan.notes.append(
                f"hook_bridge: skipped — kind '{kind}' has no supported agents-hooks bridge yet"
            )
        return
    if isinstance(bridge_cfg, dict) and bridge_cfg.get("enabled") is False:
        return
    # No installed descriptors → the bridge would be a no-op carrier. Skip rather than wire
    # a dispatcher that has nothing to dispatch (and surface why in a note).
    ah = config.category("agent_hooks")
    if ah.get("enabled") is False:
        plan.notes.append(
            "hook_bridge: skipped — agent_hooks disabled, so no descriptors to dispatch"
        )
        return
    lib_dir = catalog.source / "lib"
    # Fail-CLOSED: never wire a harness-config command that would error at runtime. The
    # catalog only checks for skills/ + agent-hooks/, so an older agent-tools checkout can
    # lack a runnable bridge package — wiring it anyway means every tool call hits a broken
    # hook (which, fail-open, is harmless but noisy). Skip with a clear,
    # actionable note instead.
    module = str(bridge_spec["module"])
    bridge_format = str(bridge_spec["format"])
    bridge_dir = lib_dir / module
    bridge_required = [bridge_dir / "dispatch.py", bridge_dir / "__main__.py"]
    if bridge_format == "opencode-plugin":
        bridge_required.append(bridge_dir / "plugin.js")
    bridge_missing = [p.name for p in bridge_required if not p.is_file()]
    if bridge_missing:
        plan.notes.append(
            f"hook_bridge: skipped — {bridge_dir} is incomplete in this agent-tools checkout "
            f"(missing {', '.join(bridge_missing)}; update agent-tools to a version that ships "
            "the runnable dispatcher)"
        )
        return
    explicit_settings_path = h.get("settings_path")
    settings_path = explicit_settings_path or bridge_spec["settings"]
    expected_suffix = {
        "toml": ".toml",
        "opencode-plugin": ".js",
    }.get(bridge_format, ".json")
    actual_suffix = Path(str(settings_path)).suffix
    if (
        bridge_format in {"toml", "opencode-plugin"}
        and explicit_settings_path
        and actual_suffix
        and actual_suffix != expected_suffix
    ):
        plan.notes.append(
            f"hook_bridge: skipped — kind '{kind}' expects a {expected_suffix} settings_path, "
            f"got {settings_path}"
        )
        return
    options: dict[str, Any] = {
        "kind": kind,
        "lib_dir": str(lib_dir),
        "module": module,
        "format": bridge_format,
    }
    if isinstance(bridge_cfg, dict) and bridge_cfg.get("python"):
        options["python"] = str(bridge_cfg["python"])
    plan.actions.append(
        Action(
            kind="register_hook_bridge",
            category="harness",
            item="hook-bridge",
            source=catalog.source,
            target=_expand(str(settings_path), config.repo_root),
            options=options,
        )
    )


def _build_agents_symlink(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the AGENTS.md (canonical) + CLAUDE.md (symlink) provisioning for the repo.

    Default **ON**: every repo should expose one agent-guide file under both names so every
    harness reads the same instructions. Opt out with ``agents_md: { enabled: false }`` (or
    ``{ symlink: false }``). The classify-and-converge logic lives in ``actions/`` (it depends
    on what is already on disk), so the plan emits one idempotent action anchored at the repo
    root; no carrier in agent-tools.
    """
    am = config.data.get("agents_md")
    if am is None:
        am = {}
    if not isinstance(am, dict):
        return  # validate() already fail-closed on a non-mapping block
    if am.get("enabled") is False or am.get("symlink") is False:
        return
    plan.actions.append(
        Action(
            kind="provision_agents_symlink",
            category="agents_md",
            item="symlink",
            source=config.repo_root,
            target=config.repo_root,
            options={},
        )
    )


def _build_ship_delegator(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the per-repo ``.claude/scripts/pr-ship.sh`` (``gh ship`` delegator) provisioning.

    Default **ON**: every managed repo should expose the delegator the global ``gh ship`` alias
    runs, so ``gh ship`` works there on a clean machine — not only in agent-tools (the only repo that
    historically carried it). Opt out with ``ship_delegator: { enabled: false }``.

    Fail-CLOSED on a checkout lacking the canonical ``ci/ship/ship.sh``: rig never provisions a
    delegator that would exec a non-existent script. The canonical path is resolved from the
    agent-tools checkout NOW and carried in the action's ``canonical_ship`` option — apply writes
    it (as the agent-tools root) into the MACHINE-level ``$XDG_CONFIG_HOME/agent-tools/env`` file,
    NOT into the delegator, whose rendered content is a portable constant (a re-apply / drift
    compare is a byte-for-byte no-op even for a repo that commits it). The classify-and-converge
    + the ``.git/info/exclude`` ignore handling live in ``actions/`` (they depend on what is on
    disk); the plan emits one idempotent action anchored at the repo root. No carrier in agent-tools.
    """
    sd = config.data.get("ship_delegator")
    if sd is None:
        sd = {}
    if not isinstance(sd, dict):
        return  # validate() already fail-closed on a non-mapping block
    if sd.get("enabled") is False:
        return
    canonical = catalog.source / "ci" / "ship" / "ship.sh"
    if not canonical.is_file():
        plan.notes.append(
            "ship_delegator: skipped — no ci/ship/ship.sh in this agent-tools checkout "
            f"({catalog.source}); `gh ship` cannot delegate (update agent-tools to a version "
            "that ships the ship gate)"
        )
        return
    plan.actions.append(
        Action(
            kind="provision_ship_delegator",
            category="ship_delegator",
            item="delegator",
            source=catalog.source,
            target=config.repo_root,
            options={"canonical_ship": str(canonical)},
        )
    )


def _build_linters(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the per-repo linter/formatter config-file provisioning (the ``linters`` block).

    Default **ON**: every declared, enabled item becomes one idempotent action that writes its
    config file at the repo-relative ``path`` with the exact ``content`` from config (CTO decision
    #4136.2 — linter settings are provisioned by rig like every other reconciled area). The tool +
    path + content are PER-REPO config; the plan hardcodes no specific linter. Opt out of the whole
    area with ``linters: { enabled: false }`` or a single item with ``items.<name>.enabled: false``.

    One action per item (``item`` is the config label), anchored at the repo root. The
    create/repair/never-clobber-without-backup write + the byte-compare drift live in ``actions/``
    and ``drift.py`` (they depend on what is on disk), so this only resolves the desired files.
    ``validate()`` already fail-closed on a malformed block, so the structural re-checks here are
    belt-and-suspenders for a hand-built plan.
    """
    li = config.data.get("linters")
    if li is None:
        li = {}
    if not isinstance(li, dict):
        return  # validate() already fail-closed on a non-mapping block
    if li.get("enabled") is False:
        return
    items = li.get("items", {})
    if not isinstance(items, dict):
        return
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled") is False:
            continue
        rel_path = spec.get("path")
        content = spec.get("content")
        if not isinstance(rel_path, str) or not isinstance(content, str) or not rel_path or not content:
            continue  # validate() rejects this; skip rather than emit a broken action
        tool = str(spec.get("tool") or "")
        role = str(spec.get("role") or "linter")
        plan.actions.append(
            Action(
                kind="provision_linter_config",
                category="linters",
                item=str(name),
                source=config.repo_root,
                target=config.repo_root,
                options={"tool": tool, "role": role, "rel_path": rel_path, "content": content},
            )
        )


def _build_global_excludes(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed block in the GLOBAL git excludes file (``core.excludesfile``).

    This is GLOBAL (machine-wide) config, wired like the git-hooks ``dispatcher``: rig owns ONE
    marker-delimited block in git's global ``core.excludesfile`` so harness artifacts — chiefly
    Claude Code's throwaway ``**/.claude/worktrees/`` — are ignored in EVERY repo on the machine,
    with zero per-repo commits and no per-repo ``rig apply``. Opt out with
    ``gitignore: { enabled: false }``. Default **ON** (like the dispatcher), so on a clean machine
    ``rig init``/``rig apply`` provisions it without any per-repo config.

    Target resolution is deferred to apply time (it depends on whether ``core.excludesfile`` is
    already set on this machine — a thing the plan cannot read purely), so the plan emits ONE
    idempotent action carrying the resolved ``entries`` and the XDG fallback path; the runner
    reads ``core.excludesfile`` and EITHER reconciles the block in the user's existing excludes
    file OR sets ``core.excludesfile`` to the XDG default and writes the block there. The
    placeholder ``target`` is the XDG default for display; the runner re-resolves it. No carrier
    in agent-tools.

    The ignored ``entries`` are configurable with a sensible default (``GITIGNORE_DEFAULT_ENTRIES``);
    an empty/absent list uses that default. The ``excludesfile`` override (rare) forces a specific
    file rather than honoring ``core.excludesfile``.
    """
    gi = config.data.get("gitignore")
    if gi is None:
        gi = {}
    if not isinstance(gi, dict):
        return  # validate() already fail-closed on a non-mapping block
    if gi.get("enabled") is False:
        return
    raw_entries = gi.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        entries = list(GITIGNORE_DEFAULT_ENTRIES)
    else:
        entries = [str(e) for e in raw_entries]
    override = gi.get("excludesfile")
    options: dict[str, Any] = {
        "entries": entries,
        "xdg_default": GITIGNORE_DEFAULT_EXCLUDESFILE,
    }
    if isinstance(override, str) and override:
        options["excludesfile"] = override
    plan.actions.append(
        Action(
            kind="provision_global_excludes",
            category="gitignore",
            item="block",
            source=config.repo_root,
            # Placeholder for display only — the runner re-resolves the real target from
            # core.excludesfile (or the XDG default) at apply time. Expanded for portability.
            target=_expand(GITIGNORE_DEFAULT_EXCLUDESFILE, config.repo_root),
            options=options,
        )
    )


def _enabled_ci_check_contexts(config: LoadedConfig, catalog: Catalog) -> list[str]:
    """The required-status-check contexts for the merge-gating CI gates this repo actually has.

    ROADMAP §5 names the PR-checklist and unresolved-review-threads gates as the required checks
    rig adds to the ruleset. But requiring a check whose workflow ISN'T in the repo wedges every PR
    (GitHub waits forever for a check-run that can never report — the lockout guard in
    ``github_ruleset.CI_GATE_CHECK_CONTEXTS``). So we require a context ONLY when its CI gate is
    enabled AND being written to ``.github/workflows`` (not ``export-only``, where nothing lands).
    The returned list is in ``CI_GATE_CHECK_CONTEXTS`` order so the plan is deterministic.
    """
    ci = config.category("ci")
    if ci.get("enabled") is False:
        return []
    # WHITELIST the one target GitHub actually runs workflows from: `.github/workflows`. GitHub only
    # executes workflow files in that exact directory — `export-only` (write nowhere) OR any other
    # custom target means the check-run never appears, so requiring its context would wedge every PR
    # (the lockout footgun). A blacklist of just `export-only` would miss every other non-standard
    # target; the whitelist is the safe form.
    target = ci.get("target", config.defaults.get("ci_target", ".github/workflows"))
    if target != ".github/workflows":
        return []
    known = catalog.names("ci")
    contexts: list[str] = []
    for slot, context in CI_GATE_CHECK_CONTEXTS.items():
        if slot not in known:
            continue  # the checkout doesn't carry this gate — can't be provisioned, so don't require
        item = catalog.get("ci", slot)
        # `type_enabled=False` is DELIBERATE: a gate becomes a required check only when the config
        # actually enables it (items.<>.enabled / enable: / all:), matching the CI builder's own
        # resolution exactly — so a gate that is WRITTEN as a workflow is the same set that is
        # REQUIRED, and the two can't drift. The scaffold (riglib/state.py) enables both merge gates
        # explicitly, so a fresh repo gets both required. (A bare project-type default does not flip a
        # CI gate on here — CI items are `default_enabled=False` in the catalog — so there is no
        # "type pulls in a gate but it isn't required" gap; the gate isn't pulled in at all without
        # explicit config.)
        if item is not None and _item_enabled(ci, item, type_enabled=False):
            contexts.append(context)
    return contexts


def _build_github_ruleset(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the GitHub repository branch-ruleset provisioning for the repo.

    Default **ON** (like ``agents_md``): rig reconciles a branch ruleset named
    ``github.ruleset.name`` (default ``rig-managed``) on the repo's DEFAULT branch via
    ``gh api``. Opt out with ``github: { ruleset: { enabled: false } }``. The whole
    GitHub-vs-desired classification lives in ``actions/`` (it depends on the live API), so the
    plan emits ONE idempotent action carrying the resolved knobs; no carrier in agent-tools,
    and the repo root is the target the action resolves ``owner/repo`` from.

    The action itself returns ``skipped`` when the repo has no github remote — so "default ON
    when the repo has a github remote" needs no detection here; a non-github repo is a no-op.
    The resolved options merge the documented defaults with any ``ruleset`` overrides, so a
    sparse config still produces the safe default ruleset and the footgun ``update`` rule is
    never reachable.

    REQUIRED STATUS CHECKS (ROADMAP §5). When the config does NOT pin ``required_status_checks``,
    rig defaults them to the merge-gating CI gates this repo actually provisions (PR Checklist +
    review-threads, via :func:`_enabled_ci_check_contexts`) — so a PR can't merge until those gates
    are green, WITHOUT requiring a check whose workflow isn't present (which would wedge every PR).
    An explicit ``required_status_checks`` in the config wins verbatim (including ``[]`` to require
    none), so the auto-default never overrides a deliberate choice.
    """
    gh = config.data.get("github")
    if gh is None:
        gh = {}
    if not isinstance(gh, dict):
        return  # validate() already fail-closed on a non-mapping block
    ruleset = gh.get("ruleset", {})
    if not isinstance(ruleset, dict):
        return
    if ruleset.get("enabled") is False:
        return
    # `enabled` is a plan-gating meta-key, not a ruleset-body knob — strip it before merging the
    # overrides onto the canonical defaults so it never leaks into the action options. Also drop
    # explicit `null` values: a `required_reviews: null` (which validate() tolerates) would
    # otherwise overlay the `0` default with None and crash `int(None)` in build_ruleset_rules —
    # and a `null` bool knob would silently disable a guard instead of using its default. A
    # missing key already falls back to the default; an explicit null must do the same.
    overrides = {k: v for k, v in ruleset.items() if k != "enabled" and v is not None}
    options = {**GITHUB_RULESET_DEFAULTS, **overrides}
    # Default the required checks to the repo's actual merge-gating CI gates when not pinned. The
    # presence test is on the ORIGINAL ruleset mapping (an explicit `[]` is a deliberate "require
    # none" and must survive the `v is not None` filter above — which it does, so check `ruleset`).
    if "required_status_checks" not in ruleset or ruleset.get("required_status_checks") is None:
        options["required_status_checks"] = _enabled_ci_check_contexts(config, catalog)
    plan.actions.append(
        Action(
            kind="provision_github_ruleset",
            category="github",
            item="ruleset",
            source=config.repo_root,
            target=config.repo_root,
            options=options,
        )
    )


def _github_subblock(config: LoadedConfig, name: str) -> dict | None:
    """Resolve the ``github.<name>`` sub-block for a plan builder, or None to SKIP it.

    Returns None when the github block is absent/non-mapping, the sub-block is non-mapping, or the
    sub-block is explicitly disabled (``enabled: false``) — the four callers each then return early.
    Otherwise returns the sub-block dict (possibly empty → all defaults). One helper so every
    github plan builder reads the block the same way, with the same default-ON + opt-out semantics
    as ``_build_github_ruleset``.
    """
    gh = config.data.get("github")
    if gh is None:
        gh = {}
    if not isinstance(gh, dict):
        return None
    block = gh.get(name, {})
    if not isinstance(block, dict):
        return None
    if block.get("enabled") is False:
        return None
    return block


def _github_options(block: dict, defaults: dict) -> dict:
    """Merge a sub-block's overrides onto ``defaults``, dropping ``enabled`` and explicit nulls.

    ``enabled`` is a plan-gating meta-key (handled by :func:`_github_subblock`), and an explicit
    ``null`` must fall back to the default (never overlay it with None — which would disable a
    secure-default guard or crash a coercion). Mirrors the ruleset builder's override discipline.
    """
    overrides = {k: v for k, v in block.items() if k != "enabled" and v is not None}
    return {**defaults, **overrides}


def _build_github_merge(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub repo merge-button-policy provisioning (``github.merge``).

    Default **ON** (like ``github.ruleset``): rig reconciles the squash-only merge model +
    auto-delete-head-branch + allow-auto-merge via ``PATCH /repos/{o}/{r}``. Opt out with
    ``github: { merge: { enabled: false } }``. The classification lives in ``actions/`` (it depends
    on the live API); the plan emits ONE idempotent action carrying the resolved knobs. The action
    returns ``skipped`` on a repo with no github remote, so default-ON needs no detection here.
    """
    block = _github_subblock(config, "merge")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_merge",
            category="github",
            item="merge",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_MERGE_DEFAULTS),
        )
    )


def _build_github_ghas(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub Advanced Security provisioning (``github.ghas``).

    Default **ON**: rig reconciles dependency graph + vuln-alerts + Dependabot security updates +
    secret-scanning (+ push protection) + CodeQL default-setup, each via the right ``gh api``
    endpoint. Opt out with ``github: { ghas: { enabled: false } }``. The action degrades loudly on a
    repo whose plan does not include a GHAS-licensed scanner (private repo without GHAS), and is a
    no-op on a repo with no github remote.
    """
    block = _github_subblock(config, "ghas")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_ghas",
            category="github",
            item="ghas",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_GHAS_DEFAULTS),
        )
    )


def _build_github_actions(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the GitHub Actions permissions provisioning (``github.actions``).

    Default **ON**: rig reconciles Actions-enabled + allowed_actions and the default GITHUB_TOKEN
    scope (READ-only by default, least privilege) + whether workflows may approve PRs. Opt out with
    ``github: { actions: { enabled: false } }``. A no-op on a repo with no github remote.
    """
    block = _github_subblock(config, "actions")
    if block is None:
        return
    plan.actions.append(
        Action(
            kind="provision_github_actions",
            category="github",
            item="actions",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, GITHUB_ACTIONS_DEFAULTS),
        )
    )


def _build_github_browser(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the API-unreachable GitHub settings provisioning via agent-browser (``github.browser``).

    Default **ON** in the plan (so ``rig status`` shows it), but the action itself is gated OFF at
    apply time unless ``RIG_GH_BROWSER=1`` — driving a real browser is a heavier path than gh api,
    so it runs only when explicitly enabled. Opt out of even planning it with
    ``github: { browser: { enabled: false } }``. The toggle defaults come from the browser backend's
    ``UI_ONLY_TOGGLES`` table (one source).
    """
    block = _github_subblock(config, "browser")
    if block is None:
        return
    defaults = {knob: spec["default"] for knob, spec in UI_ONLY_TOGGLES.items()}
    plan.actions.append(
        Action(
            kind="provision_github_browser",
            category="github",
            item="browser",
            source=config.repo_root,
            target=config.repo_root,
            options=_github_options(block, defaults),
        )
    )


def _build_models(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the daily model-freshness checker schedule, if a ``models`` block enables it.

    The CTO's #3685 direction: on ``rig init`` AND ``rig apply``, check whether the daily
    checker cron is installed and install it if missing. The action carries the
    platform-resolved desired schedule (launchd plist on macOS / crontab line on Linux); the
    install-if-missing + idempotency live in ``actions/runner.py``, so the plan stays pure.

    No ``models`` block (or ``enabled: false``) → no action (rig leaves the system cron
    alone). The checker command is anchored on the resolved ``agent_tools_source`` (rig runs
    agent-tools content read-only from the checkout) unless ``checker_path`` pins one.
    """
    from .config import _DEFAULT_SCHEDULE_TIME, parse_hhmm
    from .schedule import DEFAULT_LABEL, build_schedule, default_checker_path

    m = config.data.get("models")
    if not isinstance(m, dict) or not m:
        return
    if m.get("enabled") is False:
        return

    schedule_cfg = m.get("schedule", {}) if isinstance(m.get("schedule"), dict) else {}
    hour, minute = parse_hhmm(schedule_cfg.get("time", _DEFAULT_SCHEDULE_TIME))
    label = str(schedule_cfg.get("label") or DEFAULT_LABEL)

    # checker path: an explicit ``checker_path`` wins; else the model_freshness.py inside the
    # resolved agent-tools checkout. If neither resolves, plan a note (not a crash) — the
    # schedule can't run a checker that doesn't exist.
    checker_raw = m.get("checker_path")
    if checker_raw:
        checker_path = _expand(str(checker_raw), config.repo_root)
    else:
        checker_path = default_checker_path(catalog.source)
    if checker_path is None:
        plan.notes.append(
            "models: schedule skipped — no checker_path and agent_tools_source did not resolve "
            "a checker (set models.checker_path or agent_tools_source)"
        )
        return
    # Don't provision a schedule that runs a checker that isn't on disk — that would install a
    # cron firing a missing script daily. Skip with a note so the operator fixes the path.
    if not checker_path.is_file():
        plan.notes.append(
            f"models: schedule skipped — checker not found at {checker_path} "
            "(set models.checker_path, or point agent_tools_source at a checkout that ships "
            "lib/checker/model_freshness.py)"
        )
        return

    sched = build_schedule(checker_path=checker_path, hour=hour, minute=minute, label=label)
    # the action target is the artifact path the install/drift code keys off: the plist on
    # macOS, or the user's crontab (a sentinel-conceptual target) on Linux.
    target = sched.plist_path if sched.platform == "launchd" else config.repo_root
    plan.actions.append(
        Action(
            kind="provision_schedule",
            category="models",
            item="model-freshness",
            source=config.repo_root,  # no carrier; anchor on the repo
            target=target,  # type: ignore[arg-type]
            options={
                "platform": sched.platform,
                "label": label,
                "hour": hour,
                "minute": minute,
                "checker_path": str(checker_path),
            },
        )
    )


def _build_tmux(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tmux configuration provisioning, if a ``tmux`` block enables it.

    NOT default-on: tmux config is opt-in (a ``tmux:`` block with ``enabled`` not false). The
    whole generate-and-migrate logic depends on what is already on disk (an existing hand-
    written ``~/.tmux.conf``), so the plan emits ONE idempotent ``provision_tmux`` action
    carrying the validated config sub-blocks; the runner resolves the :class:`TmuxPlan` and
    writes the artifacts. No carrier in agent-tools — rig generates the config itself.

    The conf path is resolved at apply time (HOME-relative ``~/.tmux.conf`` stays portable in a
    committed rig.yaml), so the action target is the conf path expanded against the repo root.
    """
    t = config.data.get("tmux")
    # A PRESENT `tmux:` block opts in (the docs: a block with `enabled` not false provisions) —
    # including an explicit empty mapping `tmux: {}` (which yields all the safe defaults). Only an
    # ABSENT key (None) or `enabled: false` is a no-op. `t == {}` must NOT be treated as absent.
    if t is None or not isinstance(t, dict):
        return
    if t.get("enabled") is False:
        return

    from .tmux import DEFAULT_APPLY_MODE

    # Resolve conf_path / generated_dir HERE against the repo root, so a relative path is
    # anchored to the `-C` repo (never the process CWD) and ~/ stays home-anchored. The runner
    # gets ABSOLUTE paths and never re-resolves against CWD. (Mirrors how every other action's
    # target is plan-resolved.)
    conf_path = _expand(str(t.get("conf_path", "~/.tmux.conf")), config.repo_root)
    generated_dir = _expand(str(t.get("generated_dir", "~/.config/rig/tmux")), config.repo_root)

    # RESOLVE the login shell to a CONCRETE path ONCE here (plan time), not per render. An empty
    # `login_shell.shell` means "use the user's $SHELL"; resolving it at render would make
    # rig.tmux.conf depend on $SHELL/FS at the moment of EACH render — so `rig apply` (one $SHELL)
    # and `rig status` (launchd/cron/CI, different/empty $SHELL) would render DIFFERENT
    # default-command lines → permanent flapping drift apply "fixes" every run (review Medium). By
    # baking the path into the action options, render/drift are deterministic and idempotent.
    login_shell = dict(t.get("login_shell", {}) or {})
    if login_shell.get("enabled", True) is not False and not login_shell.get("shell"):
        from .tmux import resolve_login_shell

        login_shell["shell"] = resolve_login_shell()

    plan.actions.append(
        Action(
            kind="provision_tmux",
            category="tmux",
            item="config",
            source=config.repo_root,  # no carrier; rig generates the config
            target=conf_path,
            options={
                "apply_mode": str(t.get("apply", DEFAULT_APPLY_MODE)),
                "conf_path": str(conf_path),
                "generated_dir": str(generated_dir),
                "resurrect": dict(t.get("resurrect", {}) or {}),
                "continuum": dict(t.get("continuum", {}) or {}),
                "moshi": dict(t.get("moshi", {}) or {}),
                "cc_restore": dict(t.get("cc_restore", {}) or {}),
                "anti_sprawl": dict(t.get("anti_sprawl", {}) or {}),
                "boot": dict(t.get("boot", {}) or {}),
                "login_shell": login_shell,
            },
        )
        )


def _build_project_tools(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan repo-local integrations for Haft, Serena, and Sverklo.

    The pure renderer in :mod:`riglib.project_tools` owns the desired carriers. The plan emits one
    action per file/operation so dry-run/status can name the exact drifting integration.
    """
    for entry in project_tools.desired_entries(config.repo_root, config.data.get("project_tools")):
        plan.actions.append(
            Action(
                kind="provision_project_tool",
                category="project_tools",
                item=entry.item,
                source=config.repo_root,
                target=config.repo_root,
                options=entry.to_options(),
            )
        )


def _build_tools(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the personal CLI ecosystem install (tg/review/task/draw/…), if ``tools:`` opts in.

    Default **OFF** (opt-in, unlike ``tg_ctl``/``models``): an ABSENT, empty, or ``enabled: false``
    ``tools:`` block emits NO action, so a clean ``rig init`` never clones four repos a user may not
    have and the e2e suite never shells out to a real ``install.sh``. A machine opts in by listing
    tools under ``tools.items``. This is a per-MACHINE concern (the tool ecosystem on this dev box),
    so the block lives in the GLOBAL layer (``~/.config/rig/config.yaml``).

    Emits ONE idempotent ``provision_tools`` action carrying every resolved :class:`ToolSpec` (as a
    small JSON-ish dict each). The runner runs each tool's OWN ``install.sh`` only when the tool is
    not already current; drift diffs declared-vs-on-disk. There is no agent-tools carrier — each
    tool installs FROM its own checkout, so ``source`` is just the repo root for describe().
    """
    from . import tools as toolsmod

    specs = toolsmod.resolve_tool_specs(config.data.get("tools"))
    if not specs:
        return

    plan.actions.append(
        Action(
            kind="provision_tools",
            category="tools",
            item="ecosystem",
            source=config.repo_root,  # no carrier; each tool installs from its own checkout
            target=specs[0].bin_dir,  # the managed PATH dir (display/anchor only)
            options={"specs": [toolsmod.spec_to_option(s) for s in specs]},
        )
    )


def _build_tg_ctl(config: LoadedConfig, plan: InstallPlan) -> None:
    """Plan the rig-managed tg-ctl inbound-daemon LaunchAgent, unless ``enabled: false``.

    Default **ON** (like ``agents_md``/``github``): an ABSENT or empty ``tg_ctl:`` block still
    provisions the daemon, so ``rig init`` on a clean machine sets it up with no config at all.
    Only ``enabled: false`` opts out. This is a per-MACHINE concern (one inbound Telegram
    control daemon per machine), so the block belongs in the GLOBAL layer
    (``~/.config/rig/config.yaml``) — but it cascades into the merged config the same way.

    Unlike tmux, the tg-ctl artifact paths are HOME-anchored per-machine (not repo-relative):
    the runner resolves them against ``Path.home()`` at apply time (so a committed rig.yaml stays
    portable and never anchors ~/.files/bin to a repo root). The action just carries the raw
    config knobs; the bun path is discovered at apply time.
    """
    from .tg_ctl import DEFAULT_BOOT_LABEL

    # An ABSENT key (None) defaults to provisioning (default-on). A PRESENT block (validate() has
    # already guaranteed it is a mapping) opts in unless `enabled: false`.
    t = config.data.get("tg_ctl") or {}
    if t.get("enabled") is False:
        return

    plan.actions.append(
        Action(
            kind="provision_tg_ctl",
            category="tg_ctl",
            item="boot",
            source=config.repo_root,  # no carrier; rig generates the plist
            # target is the launchd LABEL (not a filesystem path) — the runner resolves the real
            # ~/Library/LaunchAgents/<label>.plist against HOME at apply time. `label or DEFAULT`
            # (not `get(key, default)`) so a YAML `label:` with no value (None) can't become the
            # literal string "None".
            target=Path(t.get("label") or DEFAULT_BOOT_LABEL),
            options={
                "boot": t.get("boot", True),
                "label": t.get("label"),
                "bun_path": t.get("bun_path"),
                "tg_ctl_path": t.get("tg_ctl_path"),
                "config_dir": t.get("config_dir"),
            },
        )
    )
