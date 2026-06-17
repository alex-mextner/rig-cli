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
)
from .github_ruleset import GITHUB_RULESET_DEFAULTS


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
_DEFAULTS_KEY = {
    "skills": "skills_target",
    "agent_hooks": "hooks_target",
    "ci": "ci_target",
    "mcp": "mcp_target",
}


# Where each supported harness DISCOVERS Skill-tool skills. A skill installed into
# ``skills_target`` (default ``~/.agents/skills``) is invisible to the harness unless it is
# also present in this dir — claude-code lists/loads skills from ``~/.claude/skills`` (its
# userSettings skill dir; symlinks there resolve to the real skill). So rig maintains an
# idempotent symlink per enabled skill into the harness dir for the configured kind. Other
# harnesses (documented for when they're implemented): opencode discovers skills from its
# own config dir — add the path here when that kind is wired in plan/validation.
_HARNESS_SKILL_DIRS = {
    "claude-code": "~/.claude/skills",
}
_DEFAULT_HARNESS_KIND = "claude-code"


@dataclass
class Action:
    """A single planned install step. ``kind`` selects the runner in ``actions/``."""

    kind: str  # copy_skill | link_skill_harness | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness | register_hook_bridge | provision_schedule | provision_agents_symlink | provision_github_ruleset | provision_tmux | provision_global_excludes
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
    known discovery dir (don't guess a path). An explicit ``skills.harness_skill_dir``
    overrides the per-harness default.
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


def resolve_category_target(config: LoadedConfig, category: str) -> Path | None:
    """Public: the resolved install dir for a category (None when ``export-only``).

    Used by ``rig status`` to scan a category's configured target for disk→config extras
    even when no action lands there.
    """
    raw = config.category(category).get("target")
    if raw == "export-only":
        return None
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
        hooks_target = _resolve_target(config, "agent_hooks")
        target_kind = ah.get("target_kind", "claude-code")
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
                plan.actions.append(
                    Action(
                        kind="register_mcp",
                        category="mcp",
                        item=item.name,
                        source=item.path,
                        target=mcp_target,
                        options={
                            "command": spec.get("command", ""),
                            "server": server_name,
                        },
                    )
                )

    # ── harness (auto-mode / permission provisioning) ─────────────────────────────
    _build_harness(config, plan)

    # ── hook bridge (make agents-hooks/v1 descriptors FIRE in the harness) ─────────
    _build_hook_bridge(config, catalog, plan)

    # ── models (daily model-freshness checker schedule) ───────────────────────────
    _build_models(config, catalog, plan)

    # ── agents_md (AGENTS.md canonical + CLAUDE.md symlink) ────────────────────────
    _build_agents_symlink(config, plan)

    # ── github (repository branch ruleset via gh api) ─────────────────────────────
    _build_github_ruleset(config, plan)

    # ── tmux (rig-managed tmux configuration) ──────────────────────────────────────
    _build_tmux(config, plan)

    # ── gitignore (rig-managed block in the GLOBAL git excludes file) ──────────────
    _build_global_excludes(config, plan)

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
        # validate() already fail-closed on unknown/reserved kinds; defensive guard only.
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


# Harnesses whose settings file CC's hook contract applies to. Only claude-code today —
# other harnesses don't use the settings.json PreToolUse/Stop mechanism this bridge targets.
_HOOK_BRIDGE_HARNESSES = {"claude-code"}


def _build_hook_bridge(config: LoadedConfig, catalog: Catalog, plan: InstallPlan) -> None:
    """Plan the agents-hooks/v1 → harness bridge registration, if applicable.

    Claude Code never runs the ``~/.claude/hooks/*.json`` descriptors rig installs; it only
    runs hooks declared in ``settings.json``. Without this, every agent-hook is INERT in CC
    (agent-tools#18). This emits one ``register_hook_bridge`` action that wires the
    ``cc_hook_bridge`` dispatcher into the SAME settings file the ``harness`` block writes.

    Gated on a harness block being present, enabled, of a supported kind, AND
    ``agent_hooks`` being enabled (a bridge with no installed descriptors is pointless) AND
    ``harness.hook_bridge`` not turned off. Anchored on the resolved agent-tools checkout so
    the dispatcher command imports ``cc_hook_bridge`` from ``<checkout>/lib``.
    """
    h = config.data.get("harness")
    if not isinstance(h, dict) or not h or h.get("enabled") is False:
        return
    kind = str(h.get("kind", _DEFAULT_HARNESS_KIND))
    if kind not in _HOOK_BRIDGE_HARNESSES or kind not in _HARNESS_SETTINGS:
        return
    bridge_cfg = h.get("hook_bridge")
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
    # Fail-CLOSED: never wire a settings.json command that would error at runtime. The
    # catalog only checks for skills/ + agent-hooks/, so an older agent-tools checkout can
    # lack lib/cc_hook_bridge — wiring it anyway means every CC tool call hits a broken hook
    # (which, fail-open, is harmless but noisy). Skip with a clear, actionable note instead.
    if not (lib_dir / "cc_hook_bridge" / "dispatch.py").is_file():
        plan.notes.append(
            f"hook_bridge: skipped — {lib_dir}/cc_hook_bridge not found in this agent-tools "
            "checkout (update agent-tools to a version that ships the dispatcher)"
        )
        return
    settings_path = h.get("settings_path") or _HARNESS_SETTINGS[kind]
    options: dict[str, Any] = {
        "kind": kind,
        "lib_dir": str(lib_dir),
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


def _build_github_ruleset(config: LoadedConfig, plan: InstallPlan) -> None:
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
