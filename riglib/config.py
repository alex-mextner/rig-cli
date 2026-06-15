"""Config cascade loader + schema validation for ``rig.yaml``.

Two layers, cascaded by **location** (no scope flag):

1. **Global** — ``~/.config/rig/config.yaml`` (or ``$XDG_CONFIG_HOME/rig/config.yaml``).
   Machine-wide defaults a developer carries across repos.
2. **Per-repo** — ``rig.yaml`` at the repo root. Committed by default; it is the
   reproducible source of truth and **overrides** the global layer.

The merge is a deep dict merge: per-repo keys win, dicts merge recursively, scalars and
lists replace wholesale (a list in rig.yaml fully replaces the global list — lists are
treated as atomic decisions, not appended, to keep the result predictable).

``yaml`` is imported lazily so ``rig --help``/``doctor`` work even if PyYAML is missing.
Validation is **fail-closed**: unknown top-level keys, unknown categories, and invalid
enum values abort before anything touches disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "rig.yaml"

_VALID_TOP_KEYS = {
    "version",
    "defaults",
    "agent_tools_source",
    "skills",
    "agent_hooks",
    "git_hooks",
    "ci",
    "mcp",
    "harness",
    "models",
}
_VALID_CATEGORIES = {"skills", "agent_hooks", "git_hooks", "ci", "mcp"}
_VALID_ON_CONFLICT = {"skip", "overwrite", "backup"}
_VALID_TIERS = {"block", "warn"}
_VALID_ON_ERROR = {"open", "closed"}
# Harness kinds rig can provision an auto/permission setting for. claude-code is the only
# one IMPLEMENTED in v0.1; opencode is reserved (documented in docs/config-schema.md) so a
# config naming it fails closed with a clear message rather than silently doing nothing.
_VALID_HARNESS_KINDS = {"claude-code"}
_RESERVED_HARNESS_KINDS = {"opencode"}


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any write)."""


def global_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "rig" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file to a dict. Lazy yaml import; empty file → {}."""
    import yaml  # lazy: keeps `rig --help` dependency-free

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # fail closed with the usual error message, not a PyYAML traceback
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; over wins. Lists/scalars replace wholesale."""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class LoadedConfig:
    """A cascaded, validated config plus provenance of where each layer came from."""

    data: dict[str, Any]
    repo_root: Path
    global_path: Path | None = None
    repo_path: Path | None = None
    layers: list[str] = field(default_factory=list)

    @property
    def agent_tools_source(self) -> str | None:
        v = self.data.get("agent_tools_source")
        return str(v) if v else None

    def category(self, name: str) -> dict[str, Any]:
        cat = self.data.get(name)
        return cat if isinstance(cat, dict) else {}

    @property
    def defaults(self) -> dict[str, Any]:
        d = self.data.get("defaults")
        return d if isinstance(d, dict) else {}


def load(
    repo_root: Path,
    *,
    explicit_config: Path | None = None,
    include_global: bool = True,
) -> LoadedConfig:
    """Cascade-load config for ``repo_root``.

    - ``explicit_config`` (from ``--config P``) replaces the per-repo layer with ``P``.
    - The global layer is always the base unless ``include_global=False``.
    - The result is validated (fail-closed) before return.
    """
    repo_root = repo_root.resolve()
    merged: dict[str, Any] = {}
    layers: list[str] = []
    gpath: Path | None = None
    rpath: Path | None = None

    if include_global:
        gpath = global_config_path()
        if gpath.is_file():
            merged = _deep_merge(merged, _load_yaml(gpath))
            layers.append(f"global:{gpath}")

    if explicit_config is not None:
        rpath = explicit_config.resolve()
        if not rpath.is_file():
            raise ConfigError(f"--config file not found: {rpath}")
        merged = _deep_merge(merged, _load_yaml(rpath))
        layers.append(f"config:{rpath}")
    else:
        rpath = repo_config_path(repo_root)
        if rpath.is_file():
            merged = _deep_merge(merged, _load_yaml(rpath))
            layers.append(f"repo:{rpath}")

    validate(merged)
    merged.pop("scope", None)  # `scope` is a removed legacy key — drop it so it never
    # lingers in loaded.data, gets re-serialized, or is mistaken for a live setting.
    return LoadedConfig(
        data=merged,
        repo_root=repo_root,
        global_path=gpath if gpath and gpath.is_file() else None,
        repo_path=rpath if rpath and rpath.is_file() else None,
        layers=layers,
    )


def validate(data: dict[str, Any]) -> None:
    """Fail-closed schema validation. Raises :class:`ConfigError` on any violation."""
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    # `scope` was removed (the two layers cascade by LOCATION — a repo rig.yaml is repo-scoped,
    # the global config is global; see the module docstring). Tolerate a legacy `scope` key so
    # existing committed rig.yaml files don't break before they're cleaned up — it is ignored.
    unknown = set(data) - _VALID_TOP_KEYS - {"scope"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    version = data.get("version", 1)
    if not isinstance(version, int):
        raise ConfigError(f"version must be an int, got {version!r}")
    if version != 1:
        raise ConfigError(f"unsupported config version {version} (this rig supports v1)")

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping")
    on_conflict = defaults.get("on_conflict", "backup")
    if on_conflict not in _VALID_ON_CONFLICT:
        raise ConfigError(
            f"defaults.on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}, "
            f"got {on_conflict!r}"
        )

    for cat in _VALID_CATEGORIES:
        if cat in data and not isinstance(data[cat], dict):
            raise ConfigError(f"category '{cat}' must be a mapping")

    _validate_ci(data.get("ci", {}))
    _validate_agent_hooks(data.get("agent_hooks", {}))
    _validate_skills(data.get("skills", {}))
    _validate_harness(data.get("harness", {}))
    _validate_models(data.get("models", {}))


def _validate_ci(ci: dict[str, Any]) -> None:
    items = ci.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("ci.items must be a mapping")
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        tier = spec.get("tier")
        if tier is not None and tier not in _VALID_TIERS:
            raise ConfigError(
                f"ci.items.{name}.tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}"
            )


def _validate_agent_hooks(ah: dict[str, Any]) -> None:
    items = ah.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("agent_hooks.items must be a mapping")
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        on_error = spec.get("on_error")
        if on_error is not None and on_error not in _VALID_ON_ERROR:
            raise ConfigError(
                f"agent_hooks.items.{name}.on_error must be one of "
                f"{sorted(_VALID_ON_ERROR)}, got {on_error!r}"
            )


def _validate_skills(sk: dict[str, Any]) -> None:
    """Validate the skill-discovery knobs (``harness_link`` / ``harness_skill_dir``).

    Skills land in ``skills_target`` (default ``~/.agents/skills``), but the agent harness
    discovers Skill-tool skills from its OWN dir (claude-code: ``~/.claude/skills``). Unless
    each installed skill is symlinked into that dir, the harness never lists/loads it. So
    ``harness_link`` (default true) maintains an idempotent symlink per enabled skill, and
    ``harness_skill_dir`` overrides the per-harness default discovery dir. Fail-closed on a
    non-bool ``harness_link`` and a non-string ``harness_skill_dir`` (typo guard).
    """
    # ``validate()`` runs the "category must be a mapping" check before this, so ``sk`` is a
    # dict here (a bare ``skills:`` → None is rejected there). Guard defensively anyway, then
    # validate the knobs.
    if not isinstance(sk, dict):
        return
    harness_link = sk.get("harness_link")
    if harness_link is not None and not isinstance(harness_link, bool):
        raise ConfigError(f"skills.harness_link must be a bool, got {harness_link!r}")
    harness_skill_dir = sk.get("harness_skill_dir")
    if harness_skill_dir is not None and not isinstance(harness_skill_dir, str):
        raise ConfigError(
            f"skills.harness_skill_dir must be a string, got {harness_skill_dir!r}"
        )


def _validate_harness(h: dict[str, Any]) -> None:
    """Validate the ``harness`` block — the agent harness's auto/permission provisioning.

    Fail-closed on an unknown ``kind`` (typo guard) and a non-bool ``auto_mode``. A
    *reserved* kind (opencode) is rejected with an explicit "not implemented yet" message
    so the config author isn't left thinking rig wrote a setting it didn't.
    """
    if not isinstance(h, dict):
        raise ConfigError("harness must be a mapping")
    if not h:
        return
    kind = h.get("kind", "claude-code")
    if kind in _RESERVED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind '{kind}' is documented but not implemented in this rig "
            f"(supported: {sorted(_VALID_HARNESS_KINDS)}). Remove the harness block or "
            f"use a supported kind."
        )
    if kind not in _VALID_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind must be one of {sorted(_VALID_HARNESS_KINDS)}, got {kind!r}"
        )
    auto_mode = h.get("auto_mode")
    if auto_mode is not None and not isinstance(auto_mode, bool):
        raise ConfigError(f"harness.auto_mode must be a bool, got {auto_mode!r}")
    mode = h.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise ConfigError(f"harness.mode must be a string, got {mode!r}")
    # harness.hook_bridge — wires the agents-hooks/v1 → CC dispatcher into settings.json so
    # installed agent-hooks actually FIRE (agent-tools#18). enabled defaults true; a custom
    # python interpreter is optional. Fail-closed on the wrong types (typo guard).
    bridge = h.get("hook_bridge")
    if bridge is not None:
        if not isinstance(bridge, dict):
            raise ConfigError(f"harness.hook_bridge must be a mapping, got {bridge!r}")
        enabled = bridge.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(f"harness.hook_bridge.enabled must be a bool, got {enabled!r}")
        py = bridge.get("python")
        if py is not None and not isinstance(py, str):
            raise ConfigError(f"harness.hook_bridge.python must be a string, got {py!r}")


# The model-freshness schedule defaults to NOON (run once a day, at noon). A `time:` override
# is "HH:MM" 24h. Keeping the parse here (not just in plan/actions) makes a malformed time a
# fail-closed config error, consistent with every other block.
_DEFAULT_SCHEDULE_TIME = "12:00"


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse an "HH:MM" 24-hour time → (hour, minute). Raises ConfigError on a bad value.

    Shared by config validation and the plan builder so the accepted format never drifts.
    """
    parts = str(value).split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(f"models.schedule.time must be 'HH:MM', got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"models.schedule.time out of range (00:00–23:59), got {value!r}")
    return hour, minute


def _validate_models(m: dict[str, Any]) -> None:
    """Validate the ``models`` block — the model-freshness checker's daily schedule.

    The block provisions a daily cron (launchd on macOS, crontab on Linux) that runs the
    agent-tools checker. Fail-closed on: a non-mapping block, a non-bool ``enabled``, a
    malformed ``schedule.time`` (must be ``HH:MM``), and unknown keys (typo guard). An
    EMPTY/absent block means "no schedule provisioned" — rig leaves the system cron alone.
    """
    if not isinstance(m, dict):
        raise ConfigError("models must be a mapping")
    if not m:
        return
    unknown = set(m) - {"enabled", "schedule", "checker_path"}
    if unknown:
        raise ConfigError(f"unknown models key(s): {', '.join(sorted(unknown))}")
    enabled = m.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"models.enabled must be a bool, got {enabled!r}")
    checker_path = m.get("checker_path")
    if checker_path is not None and not isinstance(checker_path, str):
        raise ConfigError(f"models.checker_path must be a string, got {checker_path!r}")
    schedule = m.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ConfigError("models.schedule must be a mapping")
    unknown_sched = set(schedule) - {"time", "label"}
    if unknown_sched:
        raise ConfigError(f"unknown models.schedule key(s): {', '.join(sorted(unknown_sched))}")
    if "time" in schedule:
        parse_hhmm(schedule["time"])  # fail-closed on a bad time
    label = schedule.get("label")
    if label is not None and not isinstance(label, str):
        raise ConfigError(f"models.schedule.label must be a string, got {label!r}")
