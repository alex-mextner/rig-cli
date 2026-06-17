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
import re
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
    "permissions",
    "models",
    "agents_md",
    "github",
    "tmux",
    "gitignore",
    "tg_ctl",
}
_VALID_CATEGORIES = {"skills", "agent_hooks", "git_hooks", "ci", "mcp"}
_VALID_ON_CONFLICT = {"skip", "overwrite", "backup"}
_VALID_TIERS = {"block", "warn"}
_VALID_ON_ERROR = {"open", "closed"}
_VALID_TMUX_APPLY = {"import", "block"}
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


# ── dot-path get/set + scalar coercion (the `rig config get|set` engine) ────────────
# A dot path indexes the YAML mapping tree: ``harness.mode`` → ``data["harness"]["mode"]``.
# Used ONLY for the targeted config edit/read commands — not for the cascade loader, which
# always merges whole layers. Kept here (next to the loader/validator) so the path syntax
# and the schema it indexes never drift into a separate module.


def split_path(dotted: str) -> list[str]:
    """Split a dot path into segments, rejecting empties (``a..b``, leading/trailing dot).

    Fail-closed: a malformed path is a :class:`ConfigError`, not a silent no-op — a typo in
    ``rig config set`` must abort before it writes a key the schema can't reach.
    """
    if not dotted or not dotted.strip():
        raise ConfigError("empty config path")
    parts = [p.strip() for p in dotted.split(".")]
    if any(p == "" for p in parts):
        # empty segment ("a..b", a leading/trailing dot, or a whitespace-only segment "a. .b")
        raise ConfigError(f"invalid config path {dotted!r}: empty segment")
    return parts


def get_path(data: dict[str, Any], dotted: str) -> Any:
    """Read a nested value by dot path. Raises :class:`ConfigError` if the path is absent.

    Traversal fails closed when an intermediate segment is missing OR is a non-mapping
    (``a.b`` where ``a`` is a scalar/list) — both are "the path does not exist", reported
    with the exact segment that broke so the caller can fix the typo.
    """
    node: Any = data
    walked: list[str] = []
    for seg in split_path(dotted):
        if not isinstance(node, dict) or seg not in node:
            where = ".".join(walked) or "<root>"
            raise ConfigError(f"config path {dotted!r} not found (no {seg!r} under {where})")
        node = node[seg]
        walked.append(seg)
    return node


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested value by dot path, creating intermediate mappings in place.

    Fail-closed if an existing intermediate segment is a non-mapping: overwriting
    ``ci.items`` (a dict) by setting ``ci.items.x`` is fine, but setting ``a.b`` when ``a``
    is already a scalar would silently clobber it — refuse instead, naming the segment.
    """
    parts = split_path(dotted)
    node = data
    for seg in parts[:-1]:
        if seg not in node:
            node[seg] = {}
        elif not isinstance(node[seg], dict):
            raise ConfigError(
                f"cannot set {dotted!r}: {seg!r} is a {type(node[seg]).__name__}, not a mapping"
            )
        node = node[seg]
    node[parts[-1]] = value


def coerce_scalar(raw: str) -> Any:
    """Coerce a CLI string to the obvious scalar type: bool, int, float, null, else string.

    The shell hands every value to us as text; ``tier=block`` must stay a string while
    ``auto_mode=true`` must become a real bool (the schema validators reject a string there).
    Quote-wrap (``'"true"'`` → the string ``true``) forces a literal string for the rare case
    a stringly-typed value collides with a keyword.

    Coercion is deliberately CONSERVATIVE — only the unambiguous forms convert, everything
    else stays a string. So ``int``/``float`` accept a plain optionally-signed number but
    NOT Python's surprising extras (``1_000``, ``nan``, ``inf``, whitespace), which a config
    author would never mean to type and which would otherwise smuggle a NaN/underscore-int
    into the tree.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]  # explicit string escape: "true" / '12' stay strings
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~"):
        return None
    # require ASCII so str.isdigit()'s Unicode digits (superscripts like '²') — which int()/
    # float() then choke on — can't sneak past as a number; they stay strings. The try/except
    # is a belt-and-suspenders guard so coercion NEVER raises (fail-closed → fall back to str).
    if raw.isascii():
        body = raw[1:] if raw[:1] in ("+", "-") else raw  # allow a single leading sign
        # plain integer, but a leading zero ("0644", "007") stays a string — it's almost
        # always a file mode / zero-padded id / version the author means literally, and int()
        # would silently drop the zero.
        if body.isdigit() and not (len(body) > 1 and body[0] == "0"):
            try:
                return int(raw)
            except ValueError:
                pass
        # plain decimal float: digits, exactly one dot, optional sign — rejects nan/inf/1e3/_.
        elif body.count(".") == 1 and body.replace(".", "", 1).isdigit():
            try:
                return float(raw)
            except ValueError:
                pass
    return raw


def read_yaml_file(path: Path) -> dict[str, Any]:
    """Parse a single YAML config file to a dict (fail-closed). Lazy yaml import; empty → {}.

    Public so the targeted `config get|set` commands can read ONE file (not the cascade) and
    inherit the same error handling: YAML syntax errors and a non-mapping top level both raise
    :class:`ConfigError` rather than leaking a PyYAML traceback or a list/scalar.
    """
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
    # Provenance: which config FILE last declared each top-level key. Built during the cascade
    # (global first, then repo/explicit overwrites), so a key present only in the global config
    # maps to the global path even when a repo rig.yaml is also loaded. Used to name the CORRECT
    # source file in an unknown-item error instead of always blaming the repo file.
    key_sources: dict[str, Path] = field(default_factory=dict)

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

    @property
    def primary_config_path(self) -> Path:
        """The most-specific config file backing this config, for naming in error messages.

        The repo ``rig.yaml`` OVERRIDES the global config, so an invalid key is most likely
        the repo one — name it first; fall back to the global path, then to a notional repo
        path so an error always points somewhere concrete to edit.
        """
        if self.repo_path is not None:
            return self.repo_path
        if self.global_path is not None:
            return self.global_path
        return repo_config_path(self.repo_root)

    def source_for_key(self, dotted_key: str) -> Path:
        """The config FILE that declared ``dotted_key`` (e.g. ``mcp.items.review``).

        Resolves provenance by the TOP-LEVEL key (``mcp``): a key that came solely from the
        global ``~/.config/rig/config.yaml`` names the global file, while a key the repo
        ``rig.yaml`` set (or overrode) names the repo file. This is what lets an unknown-item
        error point at the file actually carrying the stale entry — e.g. a removed MCP slot
        left in the global config is reported against the global file, not the repo's.

        Falls back to :attr:`primary_config_path` when the key has no tracked provenance (a
        config built directly in tests, or a key that is not top-level).
        """
        top = dotted_key.split(".", 1)[0]
        src = self.key_sources.get(top)
        return src if src is not None else self.primary_config_path


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
    # Track which file last declared each top-level key, in cascade order (global, then
    # repo/explicit). A key the repo layer sets overwrites the global provenance — so the final
    # mapping names the file the user must edit to remove the offending key.
    key_sources: dict[str, Path] = {}

    def _merge_layer(path: Path, label: str) -> None:
        """Merge one config file into the accumulator and record its key provenance + layer.

        Single seam so a new layer can't forget to update ``key_sources`` (the bug the
        provenance feature exists to avoid). ``merged`` is rebound via ``nonlocal`` because
        ``_deep_merge`` returns a fresh dict rather than mutating in place.
        """
        nonlocal merged
        data = read_yaml_file(path)
        merged = _deep_merge(merged, data)
        for k in data:
            key_sources[k] = path
        layers.append(f"{label}:{path}")

    if include_global:
        gpath = global_config_path()
        if gpath.is_file():
            _merge_layer(gpath, "global")

    if explicit_config is not None:
        rpath = explicit_config.resolve()
        if not rpath.is_file():
            raise ConfigError(f"--config file not found: {rpath}")
        _merge_layer(rpath, "config")
    else:
        rpath = repo_config_path(repo_root)
        if rpath.is_file():
            _merge_layer(rpath, "repo")

    validate(merged)
    merged.pop("scope", None)  # `scope` is a removed legacy key — drop it so it never
    # lingers in loaded.data, gets re-serialized, or is mistaken for a live setting.
    key_sources.pop("scope", None)
    return LoadedConfig(
        data=merged,
        repo_root=repo_root,
        global_path=gpath if gpath and gpath.is_file() else None,
        repo_path=rpath if rpath and rpath.is_file() else None,
        layers=layers,
        key_sources=key_sources,
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
    # bool is an int subclass and True == 1, so guard it explicitly — `version: true` is a typo,
    # not v1 (this also blocks `rig config set version true` from coercing past the check).
    if isinstance(version, bool) or not isinstance(version, int):
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
    _validate_permissions(data.get("permissions", {}))
    _validate_models(data.get("models", {}))
    _validate_agents_md(data.get("agents_md", {}))
    _validate_github(data.get("github", {}))
    _validate_tmux(data.get("tmux", {}))
    _validate_gitignore(data.get("gitignore", {}))
    _validate_tg_ctl(data.get("tg_ctl", {}))


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


# The keys the permissions block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
_PERMISSIONS_KEYS = {"enabled", "kind", "tools", "extra", "disable", "settings_path"}
# Harness kinds the ALLOWLIST provisioning supports — broader than the auto-mode write
# (_VALID_HARNESS_KINDS), since opencode HAS an additively-mergeable allowlist even though its
# auto-mode write is not yet implemented. codex/gemini/pi have no such mechanism → recorded N/A
# (rejected here with a clear message rather than silently writing nothing / breaking the harness).
_VALID_PERMISSIONS_KINDS = {"claude-code", "opencode"}
_NA_PERMISSIONS_KINDS = {"codex", "gemini", "pi"}
# A pre-allowed tool is a single command token: it must START with an alphanumeric or ``/`` (an
# absolute path) — never a dash (a leading-dash entry like ``-rf`` / ``--flag`` would render a
# nonsensical/surprising allowlist entry) — and otherwise contain only letters, digits, and the
# chars that legitimately appear in a command name or path (``.``/``_``/``-``/``/``). No spaces, no
# shell metachars; ``..`` is rejected separately (path traversal).
_PERMISSION_TOOL_RE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9._/-]*$")


def _validate_permissions(p: dict[str, Any]) -> None:
    """Validate the ``permissions`` block — the per-harness command allowlist rig provisions.

    rig pre-allows our ecosystem CLIs (tg/review/draw/3d/rig/task) + the safe-to-allow external
    dev tools (gh/git/rg/uv/bun/jq/gitleaks) in the harness's permission allowlist so the agent
    never stops to ask for a known-safe command. Default **ON**: an EMPTY/absent block still
    provisions the DEFAULT tool set (a present block with ``enabled`` not false opts in). The list
    is config-driven — ``tools`` (a list of command names) REPLACES the default set, ``extra``
    adds, ``disable`` removes. Fail-closed, consistent with every other block, on: a non-mapping
    block, an unknown key (typo guard), a non-bool ``enabled``, a non-string-list
    ``tools``/``extra``/``disable``, and a non-string ``settings_path``.
    """
    if not isinstance(p, dict):
        raise ConfigError("permissions must be a mapping")
    if not p:
        return
    unknown = set(p) - _PERMISSIONS_KEYS
    if unknown:
        raise ConfigError(f"unknown permissions key(s): {', '.join(sorted(unknown))}")
    enabled = p.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"permissions.enabled must be a bool, got {enabled!r}")
    kind = p.get("kind")
    if kind is not None:
        if not isinstance(kind, str):
            raise ConfigError(f"permissions.kind must be a string, got {kind!r}")
        if kind in _NA_PERMISSIONS_KINDS:
            raise ConfigError(
                f"permissions.kind '{kind}' has no additively-mergeable command allowlist "
                f"(supported: {sorted(_VALID_PERMISSIONS_KINDS)}). Remove permissions.kind or use a "
                f"supported harness."
            )
        if kind not in _VALID_PERMISSIONS_KINDS:
            raise ConfigError(
                f"permissions.kind must be one of {sorted(_VALID_PERMISSIONS_KINDS)}, got {kind!r}"
            )
    for listkey in ("tools", "extra", "disable"):
        value = p.get(listkey)
        if value is not None:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"permissions.{listkey} must be a list of strings, got {value!r}")
            # A tool name becomes a Bash(<name>:*) / "<name> *" allowlist ENTRY — a space or shell
            # metachar would render a broken/surprising entry (Bash(git status:*), Bash(:*)). Restrict
            # to a plain command token so the rendered entry always means what the author intends.
            for v in value:
                if not _PERMISSION_TOOL_RE.match(v) or ".." in v:
                    raise ConfigError(
                        f"permissions.{listkey} entry {v!r} is not a plain command name or "
                        "absolute path (allowed: letters, digits, '.', '_', '-', '/'; no spaces, "
                        "metachars, or '..')"
                    )
    settings_path = p.get("settings_path")
    if settings_path is not None:
        if not isinstance(settings_path, str):
            raise ConfigError(f"permissions.settings_path must be a string, got {settings_path!r}")
        # both supported harnesses store the allowlist in a JSON file; a non-.json override would be
        # silently nested as <path>/settings.json by the runner. Require .json so the write lands
        # exactly where the user pointed.
        if not settings_path.endswith(".json"):
            raise ConfigError(
                f"permissions.settings_path must end in .json (the harness allowlist is a JSON file), "
                f"got {settings_path!r}"
            )


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


def _validate_agents_md(am: dict[str, Any]) -> None:
    """Validate the ``agents_md`` block — AGENTS.md/CLAUDE.md canonical+symlink provisioning.

    Default **ON**: every repo gets one canonical agent-guide file (``AGENTS.md``) plus a
    ``CLAUDE.md`` symlink to it, so every harness reads the same instructions. Opt out with
    ``agents_md: { enabled: false }`` (or the equivalent ``{ symlink: false }``). An absent
    block means "provision it" — there is nothing to validate then. Fail-closed on non-bool
    knobs and unknown keys (typo guard), consistent with every other block.
    """
    if not isinstance(am, dict):
        raise ConfigError("agents_md must be a mapping")
    if not am:
        return
    unknown = set(am) - {"enabled", "symlink"}
    if unknown:
        raise ConfigError(f"unknown agents_md key(s): {', '.join(sorted(unknown))}")
    for knob in ("enabled", "symlink"):
        value = am.get(knob)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(f"agents_md.{knob} must be a bool, got {value!r}")


# The default entries rig's managed block puts in the GLOBAL git excludes file. The harness
# (Claude Code) creates throwaway worktrees under each repo's ``.claude/worktrees/``; those must
# be gitignored MACHINE-WIDE (every repo, no per-repo commit) via git's global ``core.excludesfile``
# — not by a per-repo committed ``.gitignore`` and not by a hand-edited global ignore. Listed once
# so the validator (default-fill), the plan builder, and the runner reference the SAME default —
# NB: ``.serena/`` is deliberately NOT here (Serena state is COMMITTED shared project memory).
GITIGNORE_DEFAULT_ENTRIES = ("**/.claude/worktrees/",)

# The XDG default rig points ``core.excludesfile`` at when the user has NOT already set it. Git's
# documented global-ignore location is ``$XDG_CONFIG_HOME/git/ignore`` (``~/.config/git/ignore``).
# Defined here so the plan builder and runner agree on the fallback path; the runner expands ``~``
# and ``$XDG_CONFIG_HOME`` at apply time so a committed config stays portable.
GITIGNORE_DEFAULT_EXCLUDESFILE = "~/.config/git/ignore"

# The markers that fence rig's managed block in the global excludes file. Defined here (the schema
# layer, stdlib-only) so config validation can reject an entry that collides with a marker WITHOUT
# importing the actions runner (which would form a config→plan→config import cycle); the runner
# imports these from config so the two never drift.
GITIGNORE_BEGIN_MARKER = "# >>> rig-managed (do not edit) >>>"
GITIGNORE_END_MARKER = "# <<< rig-managed (do not edit) <<<"

# A fixed explanatory comment rig writes as the FIRST line INSIDE the managed block, right after the
# begin marker, so a human reading the global excludes file knows what the block is and why it is
# there. It is part of the canonical block text (rendered byte-for-byte), so it must match what is
# ALREADY on a provisioned machine for a re-apply to be a true zero-churn no-op. Do not reword
# casually — a change here makes the next apply rewrite every provisioned machine's block.
GITIGNORE_BLOCK_COMMENT = (
    "# Claude Code creates throwaway worktrees under each repo's .claude/worktrees/; "
    "rig ignores them globally."
)


def _validate_gitignore(gi: dict[str, Any]) -> None:
    """Validate the ``gitignore`` block — rig's managed block in the GLOBAL git excludes file.

    This is GLOBAL (machine-wide) config: rig maintains a marker-delimited block in git's
    ``core.excludesfile`` so harness artifacts (chiefly ``**/.claude/worktrees/``) are ignored in
    EVERY repo on the machine, with zero per-repo commits — not by a per-repo committed
    ``.gitignore`` and not by a hand-edited global ignore. Default **ON**: an absent/empty block
    means "provision the default entries (and set core.excludesfile if it is unset)". Fail-closed,
    consistent with every other block, on: a non-mapping block, a non-bool ``enabled``, an unknown
    key (typo guard), a non-string ``excludesfile`` override, and a non-string-list ``entries``.
    """
    if not isinstance(gi, dict):
        raise ConfigError("gitignore must be a mapping")
    if not gi:
        return
    unknown = set(gi) - {"enabled", "entries", "excludesfile"}
    if unknown:
        raise ConfigError(f"unknown gitignore key(s): {', '.join(sorted(unknown))}")
    enabled = gi.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"gitignore.enabled must be a bool, got {enabled!r}")
    excludesfile = gi.get("excludesfile")
    if excludesfile is not None and not isinstance(excludesfile, str):
        raise ConfigError(f"gitignore.excludesfile must be a string, got {excludesfile!r}")
    entries = gi.get("entries")
    if entries is not None:
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(f"gitignore.entries must be a list of strings, got {entries!r}")
        # Reject an entry that carries one of rig's block markers: writing it inside the managed
        # block would make every later resolve see a duplicated marker and classify the file as a
        # permanent conflict (apply could never re-converge). Fail closed on the footgun.
        for e in entries:
            if GITIGNORE_BEGIN_MARKER in e or GITIGNORE_END_MARKER in e:
                raise ConfigError(
                    f"gitignore.entries may not contain a rig-managed marker line, got {e!r}"
                )


# The ruleset knobs that are plain booleans (typo + type guard). Listed once so the
# validator and the action builder reference the SAME knob set.
_GITHUB_RULESET_BOOL_KNOBS = (
    "enabled",
    "require_pull_request",
    "block_force_push",
    "restrict_deletion",
    "require_linear_history",
    "require_signatures",
    "admin_bypass",
)
_GITHUB_RULESET_KEYS = {
    *_GITHUB_RULESET_BOOL_KNOBS,
    "name",
    "required_reviews",
    "required_status_checks",
}


def _validate_github(gh: dict[str, Any]) -> None:
    """Validate the ``github`` block — the GitHub repository ruleset rig provisions.

    rig reconciles a branch ruleset (the modern replacement for branch protection) on the
    repo's default branch via ``gh api``, named by ``ruleset.name`` (rig owns rulesets with
    that name). Default **ON** when the repo has a github remote; a repo without one is a
    no-op (the action skips, never errors). Fail-closed, consistent with every other block,
    on: a non-mapping block, an unknown ``github`` / ``github.ruleset`` key (typo guard), a
    non-bool boolean knob, a ``required_reviews`` that is not an int >= 0, and a
    ``required_status_checks`` that is not a list of strings.

    The footgun guard is structural, not a config knob: rig NEVER emits the ``update``
    ("Restrict updates") rule (it locks out every merge to a protected default branch), and
    never emits a ``required_deployments`` rule with an empty environment list — so neither is
    expressible here at all.
    """
    if not isinstance(gh, dict):
        raise ConfigError("github must be a mapping")
    if not gh:
        return
    unknown = set(gh) - {"ruleset"}
    if unknown:
        raise ConfigError(f"unknown github key(s): {', '.join(sorted(unknown))}")
    ruleset = gh.get("ruleset", {})
    if not isinstance(ruleset, dict):
        raise ConfigError("github.ruleset must be a mapping")
    unknown_rs = set(ruleset) - _GITHUB_RULESET_KEYS
    if unknown_rs:
        raise ConfigError(f"unknown github.ruleset key(s): {', '.join(sorted(unknown_rs))}")
    for knob in _GITHUB_RULESET_BOOL_KNOBS:
        value = ruleset.get(knob)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(f"github.ruleset.{knob} must be a bool, got {value!r}")
    name = ruleset.get("name")
    if name is not None and not isinstance(name, str):
        raise ConfigError(f"github.ruleset.name must be a string, got {name!r}")
    reviews = ruleset.get("required_reviews")
    # NB: bool is an int subclass in Python — reject it explicitly so `true` can't masquerade
    # as a review count.
    if reviews is not None and (isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0):
        raise ConfigError(
            f"github.ruleset.required_reviews must be an int >= 0, got {reviews!r}"
        )
    checks = ruleset.get("required_status_checks")
    if checks is not None:
        if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
            raise ConfigError(
                f"github.ruleset.required_status_checks must be a list of strings, got {checks!r}"
            )


# The nested sub-blocks the tmux block accepts, each with its own allowed keys. Listed once so
# the validator rejects a typo in any of them (fail-closed, consistent with every other block).
_TMUX_TOP_KEYS = {
    "enabled",
    "apply",
    "conf_path",
    "generated_dir",
    "resurrect",
    "continuum",
    "moshi",
    "cc_restore",
    "anti_sprawl",
    "boot",
    "login_shell",
}
_TMUX_SUBKEYS = {
    "resurrect": {"processes", "capture_pane_contents"},
    "continuum": {"restore", "boot", "save_interval"},
    "moshi": {"enabled"},
    "cc_restore": {"enabled"},
    "anti_sprawl": {"enabled", "session"},
    "boot": {"enabled", "label"},
    "login_shell": {"enabled", "shell"},
}


def _validate_tmux(t: dict[str, Any]) -> None:
    """Validate the ``tmux`` block — rig-managed tmux configuration provisioning.

    rig GENERATES a tmux config from this block (guaranteeing plugin-init ordering so the
    Moshi status-right tweak can't wipe continuum's autosave hook) and MIGRATES an existing
    hand-written ``~/.tmux.conf`` into an import (or a sentinel-fenced managed block). An
    EMPTY/absent block means "leave tmux alone". Fail-closed, consistent with every other
    block, on: a non-mapping block, an unknown top-level/nested key (typo guard), a non-bool
    bool knob, a bad ``apply`` enum, a non-list-of-strings ``resurrect.processes``, and a
    ``continuum.save_interval`` that is not an int >= 1.
    """
    if not isinstance(t, dict):
        raise ConfigError("tmux must be a mapping")
    if not t:
        return
    unknown = set(t) - _TMUX_TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown tmux key(s): {', '.join(sorted(unknown))}")

    enabled = t.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"tmux.enabled must be a bool, got {enabled!r}")
    # `apply` PRESENT must be a valid enum string. A present null would `str(None)` into a bogus
    # "None" apply_mode, and an unhashable value (list/dict) would raise a raw TypeError from the
    # `in` check — so require a string in the enum. Only an ABSENT key falls back to the default.
    if "apply" in t and (
        not isinstance(t["apply"], str) or t["apply"] not in _VALID_TMUX_APPLY
    ):
        raise ConfigError(
            f"tmux.apply must be one of {sorted(_VALID_TMUX_APPLY)}, got {t['apply']!r}"
        )
    for pathkey in ("conf_path", "generated_dir"):
        # A key PRESENT with no value (YAML `conf_path:` → None) must fail closed: it can't fall
        # back to the default the way a truly-absent key does (the plan would `str(None)` it into
        # a literal "None" path). Only an absent key is allowed; a present non-string is rejected.
        if pathkey in t and not isinstance(t[pathkey], str):
            raise ConfigError(f"tmux.{pathkey} must be a string, got {t[pathkey]!r}")

    for sub, allowed in _TMUX_SUBKEYS.items():
        block = t.get(sub)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"tmux.{sub} must be a mapping, got {block!r}")
        unknown_sub = set(block) - allowed
        if unknown_sub:
            raise ConfigError(
                f"unknown tmux.{sub} key(s): {', '.join(sorted(unknown_sub))}"
            )

    res = t.get("resurrect", {})
    if isinstance(res, dict):
        procs = res.get("processes")
        if procs is not None and (
            not isinstance(procs, list) or not all(isinstance(p, str) for p in procs)
        ):
            raise ConfigError(
                f"tmux.resurrect.processes must be a list of strings, got {procs!r}"
            )
        cap = res.get("capture_pane_contents")
        if cap is not None and not isinstance(cap, bool):
            raise ConfigError(
                f"tmux.resurrect.capture_pane_contents must be a bool, got {cap!r}"
            )

    cont = t.get("continuum", {})
    if isinstance(cont, dict):
        for boolkey in ("restore", "boot"):
            value = cont.get(boolkey)
            if value is not None and not isinstance(value, bool):
                raise ConfigError(f"tmux.continuum.{boolkey} must be a bool, got {value!r}")
        interval = cont.get("save_interval")
        # NB: bool is an int subclass — reject it explicitly so `true` can't pose as a count.
        if interval is not None and (
            isinstance(interval, bool) or not isinstance(interval, int) or interval < 1
        ):
            raise ConfigError(
                f"tmux.continuum.save_interval must be an int >= 1, got {interval!r}"
            )

    for sub in ("moshi", "cc_restore", "anti_sprawl", "boot", "login_shell"):
        block = t.get(sub, {})
        if isinstance(block, dict):
            value = block.get("enabled")
            if value is not None and not isinstance(value, bool):
                raise ConfigError(f"tmux.{sub}.enabled must be a bool, got {value!r}")
    anti = t.get("anti_sprawl", {})
    if isinstance(anti, dict):
        session = anti.get("session")
        if session is not None and not isinstance(session, str):
            raise ConfigError(f"tmux.anti_sprawl.session must be a string, got {session!r}")
    boot = t.get("boot", {})
    if isinstance(boot, dict):
        label = boot.get("label")
        if label is not None and not isinstance(label, str):
            raise ConfigError(f"tmux.boot.label must be a string, got {label!r}")
    login = t.get("login_shell", {})
    if isinstance(login, dict):
        shell = login.get("shell")
        if shell is not None:
            if not isinstance(shell, str):
                raise ConfigError(f"tmux.login_shell.shell must be a string, got {shell!r}")
            # Empty string → "resolve $SHELL at apply". A NON-empty override must be an ABSOLUTE
            # path to the shell BINARY ONLY — no relative name, and NO arguments (whitespace). rig
            # appends ` -l` itself; a value like `/bin/zsh -l` would render `'/bin/zsh -l' -l`,
            # making tmux try to exec a binary literally named "/bin/zsh -l" (review P2).
            if shell and (not shell.startswith("/") or any(c.isspace() for c in shell)):
                raise ConfigError(
                    "tmux.login_shell.shell must be an absolute path to the shell BINARY with no "
                    f"arguments (rig adds `-l`), or empty to use $SHELL — got {shell!r}"
                )


# The keys the tg_ctl block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
_TG_CTL_KEYS = {
    "enabled",
    "boot",
    "label",
    "bun_path",
    "tg_ctl_path",
    "config_dir",
}


def _validate_tg_ctl(t: dict[str, Any]) -> None:
    """Validate the ``tg_ctl`` block — rig-managed tg-ctl inbound-daemon LaunchAgent.

    This is a per-MACHINE concern (one inbound Telegram control daemon per machine), so it
    belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/
    ``git_hooks`` — NOT a committed repo ``rig.yaml``. Default **ON**: an EMPTY/absent block
    still provisions the daemon (a present block with ``enabled`` not false opts in). Fail-closed,
    consistent with every other block, on: a non-mapping block, an unknown key (typo guard), a
    non-bool ``enabled``/``boot``, and a non-string ``label``/``bun_path``/``tg_ctl_path``/
    ``config_dir``.
    """
    if not isinstance(t, dict):
        raise ConfigError("tg_ctl must be a mapping")
    if not t:
        return
    unknown = set(t) - _TG_CTL_KEYS
    if unknown:
        raise ConfigError(f"unknown tg_ctl key(s): {', '.join(sorted(unknown))}")
    for boolkey in ("enabled", "boot"):
        value = t.get(boolkey)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(f"tg_ctl.{boolkey} must be a bool, got {value!r}")
    for strkey in ("label", "bun_path", "tg_ctl_path", "config_dir"):
        value = t.get(strkey)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"tg_ctl.{strkey} must be a string, got {value!r}")
