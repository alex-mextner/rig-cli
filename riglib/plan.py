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
from .config import LoadedConfig


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


@dataclass
class Action:
    """A single planned install step. ``kind`` selects the runner in ``actions/``."""

    kind: str  # copy_skill | install_agent_hook | install_dispatcher | install_ci | register_mcp | apply_harness
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
    def _check(category: str, names: set[str], known: set[str], label: str) -> None:
        unknown = names - known
        if unknown:
            raise PlanError(
                f"unknown {label} item(s): {', '.join(sorted(unknown))} "
                f"(known: {', '.join(sorted(known)) or 'none'})"
            )

    # skills — universal group
    sk = config.category("skills")
    uni = sk.get("universal", {}) if isinstance(sk, dict) else {}
    if isinstance(uni, dict):
        uni_known = {i.name for i in catalog.by_category("skills") if i.group == "universal"}
        refs = set(uni.get("enable", []) or []) | set(uni.get("disable", []) or [])
        refs |= set(k for k in uni.get("items", {}) if isinstance(uni.get("items"), dict))
        _check("skills", refs, uni_known, "universal skill")
    # skills — by_type group (fully-qualified item keys; bundle names checked separately)
    bt = sk.get("by_type", {}) if isinstance(sk, dict) else {}
    if isinstance(bt, dict):
        bt_known = {i.name for i in catalog.by_category("skills") if i.group.startswith("by-type/")}
        bt_kinds = {i.meta.get("kind", "") for i in catalog.by_category("skills") if i.group.startswith("by-type/")}
        bt_items = bt.get("items", {})
        if isinstance(bt_items, dict):
            _check("skills", set(bt_items), bt_known, "by-type skill")
        _check("skills", set(bt.get("enable", []) or []), bt_kinds, "by-type bundle")

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
        _check(cat_name, refs, known, cat_name)

    # git_hooks — nested sub-groups (only 'dispatcher' is shipped in v0.1). A typo like
    # 'dispatcherr' must fail closed, not silently build no dispatcher action.
    gh = config.category("git_hooks")
    if isinstance(gh, dict):
        gh_known = catalog.names("git_hooks") | {"templates"}  # templates reserved for v0.2
        unknown_gh = set(gh) - gh_known
        if unknown_gh:
            raise PlanError(
                f"unknown git_hooks key(s): {', '.join(sorted(unknown_gh))} "
                f"(known: {', '.join(sorted(gh_known))})"
            )


def build(config: LoadedConfig, catalog: Catalog, *, project_type: str = "unknown") -> InstallPlan:
    """Build the ordered :class:`InstallPlan` from config + catalog."""
    plan = InstallPlan(on_conflict=str(config.defaults.get("on_conflict", "backup")))
    _validate_item_names(config, catalog)

    # ── skills ───────────────────────────────────────────────────────────────────
    skills_target = _resolve_target(config, "skills")
    for item in _skills_enabled(config, catalog, project_type):
        plan.actions.append(
            Action(
                kind="copy_skill",
                category="skills",
                item=item.name,
                source=item.path,
                target=skills_target / item.path.name,
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
        unknown = referenced - known
        if unknown:
            raise PlanError(
                f"unknown ci item(s): {', '.join(sorted(unknown))} "
                f"(known: {', '.join(sorted(known))})"
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

    return plan


# Default per-machine path for each supported harness's settings file. The harness block
# writes/merges the auto/permission setting HERE. Repo-relative defaults keep the committed
# rig.yaml reproducible (the auto-mode choice travels with the repo).
_HARNESS_SETTINGS = {
    "claude-code": ".claude/settings.json",
}
# The permission-mode value each harness uses for auto-accept / non-interactive mode, keyed
# by (kind, auto_mode). claude-code: `permissions.defaultMode` = bypassPermissions auto-
# accepts every tool call (true non-interactive); `default` restores prompts.
_HARNESS_AUTO_MODE = {
    "claude-code": {True: "bypassPermissions", False: "default"},
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
    settings_path = h.get("settings_path") or _HARNESS_SETTINGS[kind]
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
