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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .harness_skills import KNOWN_HARNESS_KINDS as _KNOWN_HARNESS_KINDS
from .project_tools import (
    HAFT_KEYS,
    HAFT_WORKFLOW_KEYS,
    HAFT_WORKFLOW_MODES,
    PROJECT_TOOLS_KEYS as PROJECT_TOOLS_KEYS,
    SERENA_KEYS,
    SVERKLO_KEYS,
)

CONFIG_FILENAME = "rig.yaml"

_VALID_TOP_KEYS = {
    "version",
    "defaults",
    "agent_tools_source",
    "stack",
    "skills",
    "agent_hooks",
    "git_hooks",
    "ci",
    "mcp",
    "mode",
    "harness",
    "permissions",
    "models",
    "agents_md",
    "github",
    "tmux",
    "gitignore",
    "spotlight",
    "tools",
    "tg_ctl",
    "ship_delegator",
    "linters",
    "project_tools",
    "scripts",
    "dev",
}
_VALID_CATEGORIES = {"skills", "agent_hooks", "git_hooks", "ci", "mcp"}
_VALID_ON_CONFLICT = {"skip", "overwrite", "backup"}
_VALID_TIERS = {"block", "warn"}
_VALID_ON_ERROR = {"open", "closed"}
_VALID_LEGACY_AGENT_HOOK_TARGET_KINDS = {"claude-code", "generic"}
_MCP_ITEM_KEYS = {"enabled", "server", "command", "args", "env"}
_VALID_TMUX_APPLY = {"import", "block"}
_VALID_MODE_NAMES = {"standard", "autonomous"}
_VALID_AUTONOMOUS_UNTIL = {"clean", "budget", "manual"}
# Harness kinds rig knows a skill/instruction discovery convention for — the union of the
# skills-DIRECTORY harnesses (claude-code, opencode) and the INSTRUCTION-FILE harnesses
# (codex, pi, commandcode). A ``harness.kind`` in this set is ACCEPTED: rig can
# provision skill discovery (and, for the supported kinds, the auto-mode write / allowlist)
# for it. The single source of truth is :mod:`riglib.harness_skills`. The narrower
# auto-mode-write capability is gated separately in plan.py (``_HARNESS_SETTINGS``) — a kind
# accepted here but not auto-mode-capable self-skips that write with a plan note, not a crash.
_VALID_HARNESS_KINDS = set(_KNOWN_HARNESS_KINDS)
# No kind is "reserved + rejected" any longer — every documented kind is now provisionable for
# skills. Kept as an (empty) set so the validator's reserved-kind branch stays well-defined.
_RESERVED_HARNESS_KINDS: set[str] = set()
# DEPRECATED harness kinds — removed everywhere (CTO 2026-07). Still recognized ONLY so a config
# that names one fails with a helpful "no longer supported (deprecated)" message instead of the
# generic typo error. gemini was an instruction-file harness (~/.gemini/GEMINI.md); it is gone.
_DEPRECATED_HARNESS_KINDS: dict[str, str] = {
    "gemini": "Gemini is deprecated and no longer provisioned by rig.",
}


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any write).

    A 3-part, renderable error (WHAT / WHY / FIX) that also names the SCHEMA PATH of the
    offending key. The roadmap (§5 "ENFORCED JSON schema") asks every rejection to say what is
    wrong, where (the dotted schema path, e.g. ``harness.auto_mode``), and how to fix it — so a
    malformed config fails LOUDLY, not silently.

    Subclass of :class:`ValueError` for backward-compat: every existing ``except ConfigError``
    (and ``except (ConfigError, …)``) keeps catching it. ``str(err)`` stays the one-line WHAT, so
    legacy ``error: {exc}`` renderers are unchanged; the richer renderer (:func:`render_config_error`)
    shows the why/fix/schema path when present.

    ``schema_path`` is the dotted path into the config tree the JSON schema indexes
    (``defaults.on_conflict``, ``github.ruleset.required_reviews``) — the SAME path an editor would
    show from ``schema/rig.schema.json``. ``why``/``fix`` carry the root cause and a concrete remedy.
    A plain ``ConfigError("msg")`` (no kwargs) still works — why/fix/schema_path default empty.
    """

    def __init__(
        self,
        what: str,
        *,
        why: str = "",
        fix: str = "",
        schema_path: str = "",
    ) -> None:
        super().__init__(what)
        self.what = what
        self.why = why
        self.fix = fix
        self.schema_path = schema_path

    def __str__(self) -> str:
        return self.what


def render_config_error(err: ConfigError, *, color: bool = True) -> str:
    """Render a :class:`ConfigError` as the consistent 3-part block (what / why / fix + path).

    Always shows the WHAT (prefixed ``error:``). Shows the SCHEMA PATH on the ``why`` line and the
    FIX line only when populated, so a terse error doesn't print empty labels. Mirrors
    :func:`riglib.errors.render` (the structured-error renderer) so config errors and catalog/plan
    errors read identically. The path is shown as ``schema/rig.schema.json#/<json-pointer>`` so the
    user can jump straight to the offending node in the published schema.
    """

    def _col(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    lines = [_col("31", f"error: {err.what}")]
    why = err.why
    if err.schema_path:
        from . import config_schema

        # The pointer is resolved through the registry so it stops at an open `items`/`fragments`
        # map (item names aren't schema nodes) and never dangles. A path that can't resolve at all
        # shows the dotted key alone — better than a pointer to a node that doesn't exist.
        pointer = config_schema.schema_pointer_for(err.schema_path)
        if pointer is not None:
            loc = f"schema path: {err.schema_path}  ({config_schema.SCHEMA_REL_PATH}#{pointer})"
        else:
            loc = f"schema path: {err.schema_path}"
        why = f"{why}; {loc}" if why else loc
    if why:
        lines.append(_col("2", "  why: ") + why)
    if err.fix:
        lines.append(_col("32", "  fix: ") + err.fix)
    return "\n".join(lines)


def _reject_unknown_keys(block: dict[str, Any], block_path: str) -> None:
    """Reject any key in ``block`` that the schema does not declare for ``block_path``.

    The single unknown-key gate, sourced from :mod:`riglib.config_schema` (the SAME registry the
    published JSON schema is emitted from), so the validator and ``schema/rig.schema.json`` can
    never disagree on what is a typo. Raises a 3-part :class:`ConfigError` whose schema path points
    at the FIRST offending key and whose fix lists the keys that block DOES accept. ``block_path``
    is the dotted path of the parent block (``""`` for the document root, ``github.ruleset`` for a
    nested block).

    A block carrying an OPEN ``items``/``fragments`` map (ci, mcp, agent_hooks, skills.by_type,
    git_hooks.dispatcher) keeps that map key as valid — only OTHER unknown keys are rejected.
    """
    from . import config_schema

    if block_path:
        valid = config_schema.block_child_keys(block_path)
    else:
        valid = set(config_schema.TOP_LEVEL_KEYS) | {"scope"}  # tolerate the legacy top key
    if valid is None:
        return  # not a registry-known block (defensive — caller passes a known path)
    unknown = sorted(set(block) - valid)
    if not unknown:
        return
    bad = unknown[0]
    # "top-level" at the document root, else the dotted block path ("github.ruleset").
    where = block_path or "top-level"
    key_path = f"{block_path}.{bad}" if block_path else bad
    plural = "key" if len(unknown) == 1 else "keys"
    accepted = ", ".join(sorted(valid - {"scope"}))
    raise ConfigError(
        f"unknown {where} {plural}: {', '.join(unknown)}",
        why=f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not a known "
        f"{where} key — likely a typo (validated against {config_schema.SCHEMA_REL_PATH})",
        fix=f"remove it, or use one of: {accepted}",
        schema_path=key_path,
    )


def _check_bool(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a bool, naming the schema ``path``.

    A tiny shared guard so every block's bool knob (``enabled``/``all``/…) rejects a typo'd value
    with the schema path attached — the runtime and the JSON schema then agree on the type. NB:
    ``bool`` is an int subclass, so this also rejects an int posing as a bool only implicitly; use
    a dedicated check where an int and a bool must be told apart (e.g. ``required_reviews``).
    """
    value = block.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def _check_str(block: dict[str, Any], key: str, path: str) -> None:
    """Fail-closed if ``block[key]`` is present and not a string, naming the schema ``path``."""
    value = block.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


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
    """Split and normalize a dot path into trimmed segments, rejecting empty segments.

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


def canonical_dot_path(dotted: str) -> str:
    """Return the trimmed, single-dot-joined canonical form of a config dot path."""
    return ".".join(split_path(dotted))


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

    @property
    def stack(self) -> str | None:
        """The declared stack preset (``l1/lang[/framework]``), or ``None`` when unset.

        This is the STACK PRESET (the by-stack curation axis), distinct from the build-toolchain
        ``detect.Environment.stack``. Cascaded like every value: a repo ``stack`` overrides the
        global default. Shape is validated in :func:`validate`; here we only surface the raw value.
        """
        v = self.data.get("stack")
        return str(v).strip() if isinstance(v, str) and v.strip() else None

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
    include_repo: bool = True,
) -> LoadedConfig:
    """Cascade-load config for ``repo_root``.

    - ``explicit_config`` (from ``--config P``) replaces the per-repo layer with ``P``.
    - The global layer is always the base unless ``include_global=False``.
    - ``include_repo=False`` skips the repo/config layer entirely, including ``explicit_config``.
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
        if label == "repo" and "mode" in data:
            raise ConfigError(
                "mode is a global-only config block",
                why=f"{path} is loaded as the repo layer, but mode is machine-wide policy",
                fix=f"move mode to {global_config_path()} or run `rig config set mode.name ... --global`",
                schema_path="mode",
            )
        merged = _deep_merge(merged, data)
        for k in data:
            key_sources[k] = path
        layers.append(f"{label}:{path}")

    if include_global:
        gpath = global_config_path()
        if gpath.is_file():
            _merge_layer(gpath, "global")

    if include_repo:
        if explicit_config is not None:
            rpath = explicit_config.resolve()
            if not rpath.is_file():
                raise ConfigError(f"--config file not found: {rpath}")
            label = "repo" if rpath == repo_config_path(repo_root).resolve() else "config"
            _merge_layer(rpath, label)
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
    """Fail-closed schema validation. Raises :class:`ConfigError` on any violation.

    The accepted key set + the schema PATH on each error come from :mod:`riglib.config_schema`
    (the SAME registry ``schema/rig.schema.json`` is generated from), so a hand-edit, a
    ``rig config set``, and the editor all agree on what is valid. The roadmap's "reject an
    unknown key or bad value with a clear error + the schema path" is implemented here.
    """
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping", schema_path="")

    # `scope` was removed (the two layers cascade by LOCATION — a repo rig.yaml is repo-scoped,
    # the global config is global; see the module docstring). It is tolerated (whitelisted in the
    # gate) so existing committed rig.yaml files don't break before they're cleaned up.
    _reject_unknown_keys(data, "")

    version = data.get("version", 1)
    # bool is an int subclass and True == 1, so guard it explicitly — `version: true` is a typo,
    # not v1 (this also blocks `rig config set version true` from coercing past the check).
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError(
            f"version must be an int, got {version!r}",
            why="version is the config schema version (only 1 is supported)",
            fix="set `version: 1`",
            schema_path="version",
        )
    if version != 1:
        raise ConfigError(
            f"unsupported config version {version} (this rig supports v1)",
            fix="set `version: 1`",
            schema_path="version",
        )

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping", schema_path="defaults")
    _reject_unknown_keys(defaults, "defaults")
    on_conflict = defaults.get("on_conflict", "backup")
    if on_conflict not in _VALID_ON_CONFLICT:
        raise ConfigError(
            f"defaults.on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}, "
            f"got {on_conflict!r}",
            fix=f"use one of: {', '.join(sorted(_VALID_ON_CONFLICT))}",
            schema_path="defaults.on_conflict",
        )

    for cat in _VALID_CATEGORIES:
        if cat in data and not isinstance(data[cat], dict):
            raise ConfigError(f"category '{cat}' must be a mapping", schema_path=cat)
    for project_block in ("scripts", "dev"):
        if project_block in data and not isinstance(data[project_block], dict):
            raise ConfigError(f"{project_block} must be a mapping", schema_path=project_block)

    _validate_stack(data)
    _validate_ci(data.get("ci", {}))
    _validate_agent_hooks(data.get("agent_hooks", {}))
    _validate_mcp(data.get("mcp", {}))
    _validate_git_hooks(data.get("git_hooks", {}))
    _validate_skills(data.get("skills", {}))
    _validate_mode(data.get("mode", {}))
    _validate_harness(data.get("harness", {}))
    _validate_permissions(data.get("permissions", {}))
    _validate_models(data.get("models", {}))
    _validate_agents_md(data.get("agents_md", {}))
    _validate_github(data.get("github", {}))
    _validate_tmux(data.get("tmux", {}))
    _validate_gitignore(data.get("gitignore", {}))
    _validate_spotlight(data.get("spotlight", {}))
    _validate_tools(data.get("tools", {}))
    _validate_tg_ctl(data.get("tg_ctl", {}))
    _validate_ship_delegator(data.get("ship_delegator", {}))
    _validate_linters(data.get("linters", {}))
    _validate_project_tools(data.get("project_tools", {}))


def _validate_stack(data: dict[str, Any]) -> None:
    """Validate the top-level ``stack`` preset value's SHAPE, if present.

    A MISSING stack is NOT an error here — per-repo mandatoriness is a SOFT requirement surfaced
    as a warning by init/apply/status (see :func:`stack_requirement_warning`), not a hard
    validation failure (which would break every existing committed rig.yaml on the next apply).
    Only a PRESENT, malformed value fails: wrong shape or an l1 outside the six-enum. lang and
    framework are open vocabulary. The parse/enum logic lives in :mod:`riglib.stack` (one source).
    """
    if "stack" not in data:
        return
    value = data["stack"]
    if not isinstance(value, str):
        raise ConfigError(
            f"stack must be a string like 'l1/lang[/framework]', got {value!r}",
            schema_path="stack",
        )
    from .stack import STACK_L1, StackError, parse_stack

    try:
        parse_stack(value)
    except StackError as exc:
        raise ConfigError(
            str(exc),
            why="stack is the repo's stack preset (l1/lang[/framework]); it selects by-stack skills",
            fix=f"use l1/lang[/framework] with l1 in {list(STACK_L1)} "
            "(e.g. mobile/swift/swiftui, frontend/ts/react, backend/python)",
            schema_path="stack",
        ) from exc


def resolve_init_stack(
    repo_root: Path, *, explicit: str | None = None, global_stack: str | None = None
) -> str | None:
    """Resolve the stack preset a fresh ``rig init`` writes into the committed rig.yaml.

    Cascade (mirrors the headless init resolver so the interactive wizard and the headless
    path agree): an explicit ``--stack`` wins, else the global-config default, else a
    best-guess from the repo files, else ``None`` (unset → :func:`stack_requirement_warning`).
    Both the headless `_resolve_init_plan` and the interactive TUI go through here so the
    two front-ends can never seed a different stack."""
    from .detect import detect_stack_preset

    return explicit or global_stack or detect_stack_preset(repo_root)


def stack_requirement_warning(config: "LoadedConfig") -> str | None:
    """A soft-require warning when a repo config carries no ``stack``, else ``None``.

    Per-repo ``stack`` is mandatory by POLICY but enforced softly during the migration phase: a
    missing value warns (and points at the detected guess) rather than failing. Callers (init /
    apply / status) print the returned line. A present stack → ``None`` (no warning)."""
    if config.stack:
        return None
    from .detect import detect_stack_preset

    guess = detect_stack_preset(config.repo_root)
    hint = f"; rig detected '{guess}'" if guess else ""
    return (
        "stack: not set — declare it in rig.yaml as l1/lang[/framework] "
        f"(e.g. mobile/swift/swiftui, frontend/ts/react, backend/python){hint}. "
        "Run `rig init` to set it, or `rig config set stack <value>`."
    )


def _validate_ci(ci: dict[str, Any]) -> None:
    # Reject an unknown FIXED knob (enabled/target/all); the open `items` map keeps arbitrary
    # gate names valid (a bad gate NAME is a catalog error caught later, not a schema typo).
    _reject_unknown_keys(ci, "ci")
    _check_bool(ci, "enabled", "ci.enabled")
    _check_str(ci, "target", "ci.target")
    _check_bool(ci, "all", "ci.all")
    items = ci.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("ci.items must be a mapping", schema_path="ci.items")
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        tier = spec.get("tier")
        if tier is not None and tier not in _VALID_TIERS:
            raise ConfigError(
                f"ci.items.{name}.tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}",
                fix=f"use one of: {', '.join(sorted(_VALID_TIERS))}",
                schema_path=f"ci.items.{name}.tier",
            )


def _validate_agent_hooks(ah: dict[str, Any]) -> None:
    _reject_unknown_keys(ah, "agent_hooks")
    _check_bool(ah, "enabled", "agent_hooks.enabled")
    _check_str(ah, "target", "agent_hooks.target")
    target_kind = ah.get("target_kind")
    if target_kind is not None:
        if not isinstance(target_kind, str):
            raise ConfigError(
                f"agent_hooks.target_kind must be a string, got {target_kind!r}",
                schema_path="agent_hooks.target_kind",
            )
        if target_kind not in _VALID_LEGACY_AGENT_HOOK_TARGET_KINDS:
            raise ConfigError(
                "agent_hooks.target_kind is a legacy ignored key and must be one of "
                f"{sorted(_VALID_LEGACY_AGENT_HOOK_TARGET_KINDS)}, got {target_kind!r}",
                fix="remove agent_hooks.target_kind; rig now derives hook targets from harness.kind/kinds",
                schema_path="agent_hooks.target_kind",
            )
    _check_bool(ah, "all", "agent_hooks.all")
    # The two per-repo workflow knobs are booleans in the published schema; type-check them
    # here so the strict validator and schema/rig.schema.json agree (else `worktree_only: "no"`
    # would pass the validator but violate the schema). See tests/test_workflow_guard_knobs.py.
    _check_bool(ah, "worktree_only", "agent_hooks.worktree_only")
    _check_bool(ah, "orchestrator_only", "agent_hooks.orchestrator_only")
    items = ah.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("agent_hooks.items must be a mapping", schema_path="agent_hooks.items")
    for name, spec in items.items():
        if not isinstance(spec, dict):
            continue
        on_error = spec.get("on_error")
        if on_error is not None and on_error not in _VALID_ON_ERROR:
            raise ConfigError(
                f"agent_hooks.items.{name}.on_error must be one of "
                f"{sorted(_VALID_ON_ERROR)}, got {on_error!r}",
                fix=f"use one of: {', '.join(sorted(_VALID_ON_ERROR))}",
                schema_path=f"agent_hooks.items.{name}.on_error",
            )


def _validate_mcp(mcp: dict[str, Any]) -> None:
    """Validate the ``mcp`` block — MCP server registrations.

    Until now ``mcp`` had no dedicated validator (only the generic "must be a mapping" check), so
    a typo'd FIXED knob (``enabled``/``target``) was silently ignored. Now fail-closed on an
    unknown fixed key — the open ``items`` map (arbitrary server names) stays valid; a bad SERVER
    name is a catalog error caught later, not a schema typo.
    """
    if not isinstance(mcp, dict):
        raise ConfigError("mcp must be a mapping", schema_path="mcp")
    _reject_unknown_keys(mcp, "mcp")
    enabled = mcp.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"mcp.enabled must be a bool, got {enabled!r}", schema_path="mcp.enabled")
    target = mcp.get("target")
    if target is not None and not isinstance(target, str):
        raise ConfigError(f"mcp.target must be a string, got {target!r}", schema_path="mcp.target")
    items = mcp.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("mcp.items must be a mapping", schema_path="mcp.items")
    for name, spec in items.items():
        path = f"mcp.items.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{path} must be a mapping", schema_path=path)
        unknown = sorted(set(spec) - _MCP_ITEM_KEYS)
        if unknown:
            bad = unknown[0]
            raise ConfigError(
                f"unknown {path} key: {bad}",
                why=f"{bad} is not a known MCP item key",
                fix=f"use one of: {', '.join(sorted(_MCP_ITEM_KEYS))}",
                schema_path=f"{path}.{bad}",
            )
        _check_bool(spec, "enabled", f"{path}.enabled")
        _check_str(spec, "server", f"{path}.server")
        _check_str(spec, "command", f"{path}.command")
        if "args" in spec:
            _validate_string_list(spec["args"], f"{path}.args")
        env = spec.get("env")
        if env is not None:
            if not isinstance(env, dict):
                raise ConfigError(f"{path}.env must be a mapping", schema_path=f"{path}.env")
            for env_key, env_value in env.items():
                if not isinstance(env_key, str):
                    raise ConfigError(f"{path}.env keys must be strings", schema_path=f"{path}.env")
                if not isinstance(env_value, str):
                    raise ConfigError(
                        f"{path}.env.{env_key} must be a string, got {env_value!r}",
                        schema_path=f"{path}.env.{env_key}",
                    )


def _validate_git_hooks(gh: dict[str, Any]) -> None:
    """Validate the ``git_hooks`` block — the global-hook dispatcher.

    Until now ``git_hooks`` had no dedicated validator (only the generic "must be a mapping"
    check), so a typo'd dispatcher knob was silently ignored. Fail-closed, consistent with every
    other block, on an unknown ``git_hooks`` / ``git_hooks.dispatcher`` key (the dispatcher keeps
    its open ``fragments`` map) and a non-bool dispatcher bool knob. An EMPTY/absent block is fine.
    """
    if not isinstance(gh, dict):
        raise ConfigError("git_hooks must be a mapping", schema_path="git_hooks")
    if not gh:
        return
    _reject_unknown_keys(gh, "git_hooks")
    disp = gh.get("dispatcher")
    if disp is None:
        return
    if not isinstance(disp, dict):
        raise ConfigError(
            f"git_hooks.dispatcher must be a mapping, got {disp!r}",
            schema_path="git_hooks.dispatcher",
        )
    _reject_unknown_keys(disp, "git_hooks.dispatcher")
    for boolkey in ("enabled", "set_global_hooks_path", "install_local_retrofit_script"):
        value = disp.get(boolkey)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"git_hooks.dispatcher.{boolkey} must be a bool, got {value!r}",
                schema_path=f"git_hooks.dispatcher.{boolkey}",
            )
    for strkey in ("dir", "runner"):
        value = disp.get(strkey)
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"git_hooks.dispatcher.{strkey} must be a string, got {value!r}",
                schema_path=f"git_hooks.dispatcher.{strkey}",
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
    _reject_unknown_keys(sk, "skills")
    _check_bool(sk, "enabled", "skills.enabled")
    _check_str(sk, "target", "skills.target")
    _check_bool(sk, "all", "skills.all")
    harness_link = sk.get("harness_link")
    if harness_link is not None and not isinstance(harness_link, bool):
        raise ConfigError(
            f"skills.harness_link must be a bool, got {harness_link!r}",
            schema_path="skills.harness_link",
        )
    harness_skill_dir = sk.get("harness_skill_dir")
    if harness_skill_dir is not None and not isinstance(harness_skill_dir, str):
        raise ConfigError(
            f"skills.harness_skill_dir must be a string, got {harness_skill_dir!r}",
            schema_path="skills.harness_skill_dir",
        )
    # Reject a typo'd FIXED key in the universal / by_type sub-blocks too (the schema closes them).
    # The catalog-keyed `by_type.items` map stays open; only OTHER unknown keys are rejected.
    for sub in ("universal", "by_type", "by_stack"):
        block = sk.get(sub)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"skills.{sub} must be a mapping, got {block!r}", schema_path=f"skills.{sub}")
        _reject_unknown_keys(block, f"skills.{sub}")
    _validate_by_stack_items(sk.get("by_stack"))


def _validate_by_stack_items(bs: Any) -> None:
    """Fail-closed on a malformed ``skills.by_stack.items.<name>`` spec.

    Mirrors the per-item shape checks in ``_validate_mcp`` / ``_validate_agent_hooks`` so by_stack
    is not the one lax corner: each item value must be a mapping, and a present ``enabled`` must be
    a real bool (not a truthy string that ``bool()`` would silently coerce). Without this a typo'd
    ``items.foo: "yes"`` or ``enabled: "false"`` would be accepted then silently ignored/misread."""
    if not isinstance(bs, dict):
        return
    items = bs.get("items")
    if items is None:
        return
    if not isinstance(items, dict):
        raise ConfigError("skills.by_stack.items must be a mapping", schema_path="skills.by_stack.items")
    for name, spec in items.items():
        path = f"skills.by_stack.items.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{path} must be a mapping, got {spec!r}", schema_path=path)
        enabled = spec.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(
                f"{path}.enabled must be a bool, got {enabled!r}", schema_path=f"{path}.enabled"
            )


def _validate_harness(h: dict[str, Any]) -> None:
    """Validate the ``harness`` block — the agent harness's skill + auto/permission provisioning.

    Fail-closed on an unknown ``kind`` (typo guard), a DEPRECATED ``kind`` (``gemini`` — helpful
    "no longer supported" message), and a non-bool ``auto_mode``. Every harness
    rig knows a skill/instruction discovery convention for (claude-code, opencode, codex,
    pi, commandcode — :data:`_VALID_HARNESS_KINDS`) is ACCEPTED here: rig provisions its SKILL
    discovery. The narrower auto/permission-MODE write is only implemented for some of them
    (claude-code today) and self-skips the rest with a plan note (see plan.py ``_build_harness``)
    rather than being rejected at validation — so a config can target a harness for skills even
    where the auto-mode write isn't wired yet.
    """
    if not isinstance(h, dict):
        raise ConfigError("harness must be a mapping", schema_path="harness")
    if not h:
        return
    _reject_unknown_keys(h, "harness")
    kind = h.get("kind", "claude-code")
    if not isinstance(kind, str):
        raise ConfigError(f"harness.kind must be a string, got {kind!r}", schema_path="harness.kind")
    if kind in _DEPRECATED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind '{kind}' is no longer supported (deprecated). "
            f"{_DEPRECATED_HARNESS_KINDS[kind]}",
            fix=f"remove the harness block or use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}",
            schema_path="harness.kind",
        )
    if kind in _RESERVED_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind '{kind}' is documented but not implemented in this rig "
            f"(supported: {sorted(_VALID_HARNESS_KINDS)}). Remove the harness block or "
            f"use a supported kind.",
            fix=f"use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}, or remove the harness block",
            schema_path="harness.kind",
        )
    if kind not in _VALID_HARNESS_KINDS:
        raise ConfigError(
            f"harness.kind must be one of {sorted(_VALID_HARNESS_KINDS)}, got {kind!r}",
            fix=f"use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}",
            schema_path="harness.kind",
        )
    kinds = h.get("kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not all(isinstance(v, str) for v in kinds):
            raise ConfigError(
                f"harness.kinds must be a list of strings, got {kinds!r}",
                schema_path="harness.kinds",
            )
        for extra_kind in kinds:
            if extra_kind in _DEPRECATED_HARNESS_KINDS:
                raise ConfigError(
                    f"harness.kinds entry '{extra_kind}' is no longer supported (deprecated). "
                    f"{_DEPRECATED_HARNESS_KINDS[extra_kind]}",
                    fix=f"use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}",
                    schema_path="harness.kinds",
                )
            if extra_kind in _RESERVED_HARNESS_KINDS:
                raise ConfigError(
                    f"harness.kinds entry '{extra_kind}' is documented but not implemented in this rig "
                    f"(supported: {sorted(_VALID_HARNESS_KINDS)}).",
                    fix=f"use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}",
                    schema_path="harness.kinds",
                )
            if extra_kind not in _VALID_HARNESS_KINDS:
                raise ConfigError(
                    f"harness.kinds entries must be one of {sorted(_VALID_HARNESS_KINDS)}, got {extra_kind!r}",
                    fix=f"use one of: {', '.join(sorted(_VALID_HARNESS_KINDS))}",
                    schema_path="harness.kinds",
                )
    auto_mode = h.get("auto_mode")
    if auto_mode is not None and not isinstance(auto_mode, bool):
        raise ConfigError(
            f"harness.auto_mode must be a bool, got {auto_mode!r}",
            schema_path="harness.auto_mode",
        )
    # self_merge gates a security-sensitive global carve-out; plan.py coerces it via bool(...),
    # so a non-bool like the string "false" would read truthy and ENABLE what the user meant to
    # disable. Fail closed, mirroring auto_mode.
    self_merge = h.get("self_merge")
    if self_merge is not None and not isinstance(self_merge, bool):
        raise ConfigError(
            f"harness.self_merge must be a bool, got {self_merge!r}",
            schema_path="harness.self_merge",
        )
    mode = h.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise ConfigError(f"harness.mode must be a string, got {mode!r}", schema_path="harness.mode")
    # harness.hook_bridge — wires the agents-hooks/v1 → harness dispatcher into the harness's
    # native hook surface so installed agent-hooks actually FIRE (agent-tools#18). enabled
    # defaults true; a custom python interpreter is optional. Fail-closed on wrong types.
    bridge = h.get("hook_bridge")
    if bridge is not None:
        if not isinstance(bridge, dict):
            raise ConfigError(
                f"harness.hook_bridge must be a mapping, got {bridge!r}",
                schema_path="harness.hook_bridge",
            )
        _reject_unknown_keys(bridge, "harness.hook_bridge")
        enabled = bridge.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigError(
                f"harness.hook_bridge.enabled must be a bool, got {enabled!r}",
                schema_path="harness.hook_bridge.enabled",
            )
        py = bridge.get("python")
        if py is not None and not isinstance(py, str):
            raise ConfigError(
                f"harness.hook_bridge.python must be a string, got {py!r}",
                schema_path="harness.hook_bridge.python",
            )


def _check_int_min(block: dict[str, Any], key: str, path: str, minimum: int) -> None:
    value = block.get(key)
    if key in block and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
        raise ConfigError(f"{path} must be an int >= {minimum}, got {value!r}", schema_path=path)


def _check_mode_bool(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if key in block and not isinstance(value, bool):
        raise ConfigError(f"{path} must be a bool, got {value!r}", schema_path=path)


def _check_mode_str(block: dict[str, Any], key: str, path: str) -> None:
    value = block.get(key)
    if key in block and not isinstance(value, str):
        raise ConfigError(f"{path} must be a string, got {value!r}", schema_path=path)


def _mode_mapping(parent: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    if key not in parent:
        return {}
    value = parent[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    return value


def _validate_mode(m: dict[str, Any]) -> None:
    """Validate the global ``mode`` block.

    ``mode.name: autonomous`` is a machine/global policy declaration. It does not replace the
    existing harness or permissions reconcilers; instead it feeds them with extra notes/rules while
    keeping the policy visible in the same config schema as every other rig setting.
    """
    if not isinstance(m, dict):
        raise ConfigError("mode must be a mapping", schema_path="mode")
    if not m:
        return
    _reject_unknown_keys(m, "mode")
    name = m.get("name", "standard")
    if not isinstance(name, str) or name not in _VALID_MODE_NAMES:
        raise ConfigError(
            f"mode.name must be one of {sorted(_VALID_MODE_NAMES)}, got {name!r}",
            fix=f"use one of: {', '.join(sorted(_VALID_MODE_NAMES))}",
            schema_path="mode.name",
        )
    auto = _mode_mapping(m, "autonomous", "mode.autonomous")
    _reject_unknown_keys(auto, "mode.autonomous")

    review_fix = _mode_mapping(auto, "review_fix", "mode.autonomous.review_fix")
    _reject_unknown_keys(review_fix, "mode.autonomous.review_fix")
    _check_mode_bool(review_fix, "enabled", "mode.autonomous.review_fix.enabled")
    _check_int_min(review_fix, "max_iterations", "mode.autonomous.review_fix.max_iterations", 1)
    until = review_fix.get("until")
    if "until" in review_fix and (not isinstance(until, str) or until not in _VALID_AUTONOMOUS_UNTIL):
        raise ConfigError(
            f"mode.autonomous.review_fix.until must be one of {sorted(_VALID_AUTONOMOUS_UNTIL)}, got {until!r}",
            fix=f"use one of: {', '.join(sorted(_VALID_AUTONOMOUS_UNTIL))}",
            schema_path="mode.autonomous.review_fix.until",
        )

    decisions = _mode_mapping(auto, "decisions", "mode.autonomous.decisions")
    _reject_unknown_keys(decisions, "mode.autonomous.decisions")
    quorum = _mode_mapping(
        decisions,
        "review_quorum",
        "mode.autonomous.decisions.review_quorum",
    )
    _reject_unknown_keys(quorum, "mode.autonomous.decisions.review_quorum")
    _check_mode_bool(quorum, "enabled", "mode.autonomous.decisions.review_quorum.enabled")
    _check_int_min(quorum, "min_iterations", "mode.autonomous.decisions.review_quorum.min_iterations", 1)
    _check_int_min(quorum, "min_models", "mode.autonomous.decisions.review_quorum.min_models", 2)

    escalation = _mode_mapping(auto, "escalation", "mode.autonomous.escalation")
    _reject_unknown_keys(escalation, "mode.autonomous.escalation")
    _check_mode_str(escalation, "framework_skill", "mode.autonomous.escalation.framework_skill")
    _check_mode_bool(
        escalation,
        "require_parallel_worktree_comparison",
        "mode.autonomous.escalation.require_parallel_worktree_comparison",
    )

    comparison = _mode_mapping(
        auto,
        "parallel_worktree_comparison",
        "mode.autonomous.parallel_worktree_comparison",
    )
    _reject_unknown_keys(comparison, "mode.autonomous.parallel_worktree_comparison")
    _check_mode_bool(comparison, "enabled", "mode.autonomous.parallel_worktree_comparison.enabled")
    _check_int_min(comparison, "candidates", "mode.autonomous.parallel_worktree_comparison.candidates", 2)

    devtools = _mode_mapping(auto, "development_tools", "mode.autonomous.development_tools")
    _reject_unknown_keys(devtools, "mode.autonomous.development_tools")
    if "allow" in devtools:
        _validate_string_list(devtools["allow"], "mode.autonomous.development_tools.allow")
        for entry in devtools["allow"]:
            if not _PERMISSION_RULE_RE.match(entry):
                raise ConfigError(
                    f"mode.autonomous.development_tools.allow entry {entry!r} is not a permission rule",
                    schema_path="mode.autonomous.development_tools.allow",
                )

    parallel = _mode_mapping(auto, "parallelism", "mode.autonomous.parallelism")
    _reject_unknown_keys(parallel, "mode.autonomous.parallelism")
    _check_int_min(parallel, "max_agents", "mode.autonomous.parallelism.max_agents", 1)
    _check_int_min(parallel, "max_worktrees", "mode.autonomous.parallelism.max_worktrees", 1)
    _check_int_min(parallel, "reserve_slots", "mode.autonomous.parallelism.reserve_slots", 0)
    _check_mode_bool(parallel, "limit_aware", "mode.autonomous.parallelism.limit_aware")


# The keys the permissions block accepts. Listed once so the validator rejects a typo (fail-closed,
# consistent with every other block).
_PERMISSIONS_KEYS = {"enabled", "kind", "tools", "extra", "disable", "settings_path",
                     "allow", "deny", "ask"}
# Harness kinds the ALLOWLIST provisioning supports — broader than the auto-mode write
# (_VALID_HARNESS_KINDS), since opencode HAS an additively-mergeable allowlist even though its
# auto-mode write is not yet implemented. codex/pi have no such mechanism → recorded N/A
# (rejected here with a clear message rather than silently writing nothing / breaking the harness).
# claude-code/opencode provision permissions via a config allowlist; pi provisions them via the
# `permission-guard` extension + a rig-written policy file (no config allowlist, but the same
# deny/ask effect) — all three are VALID targets for permissions.kind.
_VALID_PERMISSIONS_KINDS = {"claude-code", "opencode", "pi"}
_NA_PERMISSIONS_KINDS = {"codex"}
# A pre-allowed tool is a single command token: it must START with an alphanumeric or ``/`` (an
# absolute path) — never a dash (a leading-dash entry like ``-rf`` / ``--flag`` would render a
# nonsensical/surprising allowlist entry) — and otherwise contain only letters, digits, and the
# chars that legitimately appear in a command name or path (``.``/``_``/``-``/``/``). No spaces, no
# shell metachars; ``..`` is rejected separately (path traversal).
_PERMISSION_TOOL_RE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9._/-]*$")
# A RAW permission-rule entry (``allow``/``deny``/``ask``) is ``Tool`` or ``Tool(specifier)`` in
# the harness's own syntax: ``WebFetch``, ``mcp__pencil``, ``Bash(git push * --force *)``,
# ``Read(//tmp/**)``. Shape-checked only — the tool-name part is a bare token (glob ``*`` allowed:
# deny rules accept tool-name globs like ``mcp__*``), the specifier is any non-empty
# parenthesized string. Semantics belong to the harness; rig never interprets the pattern.
# Multi-line entries are rejected as typos; keep this aligned with the JSON Schema pattern.
_PERMISSION_RULE_RE = re.compile(r"^(?!.*[\r\n])[A-Za-z0-9_*.-]+(\(.+\))?\Z")


def _validate_permissions(p: dict[str, Any]) -> None:
    """Validate the ``permissions`` block — the per-harness command allowlist rig provisions.

    rig pre-allows our ecosystem CLIs (tg/review/draw/3d/rig/task/dev) + the safe-to-allow
    external dev tools (gh/git/rg/uv/bun/jq/gitleaks) in the harness's permission allowlist so the
    agent never stops to ask for a known-safe command. Default **ON**: an EMPTY/absent block still
    provisions the DEFAULT tool set (a present block with ``enabled`` not false opts in). The list
    is config-driven — ``tools`` (a list of command names) REPLACES the default set, ``extra``
    adds, ``disable`` removes. Fail-closed, consistent with every other block, on: a non-mapping
    block, an unknown key (typo guard), a non-bool ``enabled``, a non-string-list
    ``tools``/``extra``/``disable``, and a non-string ``settings_path``.
    """
    if not isinstance(p, dict):
        raise ConfigError("permissions must be a mapping", schema_path="permissions")
    if not p:
        return
    _reject_unknown_keys(p, "permissions")
    _check_bool(p, "enabled", "permissions.enabled")
    kind = p.get("kind")
    if kind is not None:
        if not isinstance(kind, str):
            raise ConfigError(f"permissions.kind must be a string, got {kind!r}", schema_path="permissions.kind")
        if kind in _DEPRECATED_HARNESS_KINDS:
            raise ConfigError(
                f"permissions.kind '{kind}' is no longer supported (deprecated). "
                f"{_DEPRECATED_HARNESS_KINDS[kind]}",
                fix=f"remove permissions.kind or use one of: {', '.join(sorted(_VALID_PERMISSIONS_KINDS))}",
                schema_path="permissions.kind",
            )
        if kind in _NA_PERMISSIONS_KINDS:
            raise ConfigError(
                f"permissions.kind '{kind}' has no additively-mergeable command allowlist "
                f"(supported: {sorted(_VALID_PERMISSIONS_KINDS)}). Remove permissions.kind or use a "
                f"supported harness.",
                fix=f"use one of: {', '.join(sorted(_VALID_PERMISSIONS_KINDS))}, or remove permissions.kind",
                schema_path="permissions.kind",
            )
        if kind not in _VALID_PERMISSIONS_KINDS:
            raise ConfigError(
                f"permissions.kind must be one of {sorted(_VALID_PERMISSIONS_KINDS)}, got {kind!r}",
                fix=f"use one of: {', '.join(sorted(_VALID_PERMISSIONS_KINDS))}",
                schema_path="permissions.kind",
            )
    for listkey in ("tools", "extra", "disable"):
        value = p.get(listkey)
        if value is not None:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(
                    f"permissions.{listkey} must be a list of strings, got {value!r}",
                    schema_path=f"permissions.{listkey}",
                )
            # A tool name becomes a Bash(<name>:*) / "<name> *" allowlist ENTRY — a space or shell
            # metachar would render a broken/surprising entry (Bash(git status:*), Bash(:*)). Restrict
            # to a plain command token so the rendered entry always means what the author intends.
            for v in value:
                if not _PERMISSION_TOOL_RE.match(v) or ".." in v:
                    raise ConfigError(
                        f"permissions.{listkey} entry {v!r} is not a plain command name or "
                        "absolute path (allowed: letters, digits, '.', '_', '-', '/'; no spaces, "
                        "metachars, or '..')",
                        schema_path=f"permissions.{listkey}",
                    )
    _validate_permission_rule_lists(p)
    settings_path = p.get("settings_path")
    if settings_path is not None:
        if not isinstance(settings_path, str):
            raise ConfigError(
                f"permissions.settings_path must be a string, got {settings_path!r}",
                schema_path="permissions.settings_path",
            )
        # both supported harnesses store the allowlist in a JSON file; a non-.json override would be
        # silently nested as <path>/settings.json by the runner. Require .json so the write lands
        # exactly where the user pointed.
        if not settings_path.endswith(".json"):
            raise ConfigError(
                f"permissions.settings_path must end in .json (the harness allowlist is a JSON file), "
                f"got {settings_path!r}",
                schema_path="permissions.settings_path",
            )


def _validate_permission_rule_lists(p: dict[str, Any]) -> None:
    """Validate ``allow``/``deny``/``ask`` — RAW harness permission-rule entries (rig-cli#100).

    Unlike ``tools`` (bare command names rig RENDERS into entries), these are full rule strings
    in the harness's own syntax: ``allow`` adds raw entries on top of the tool-derived allowlist
    (the adopted hand-grown machine allowlist), ``deny``/``ask`` REPLACE the baked baseline in
    :mod:`riglib.permissions`. Shape-checked only — ``Tool`` or ``Tool(specifier)`` with a
    non-empty specifier — never semantics (the harness owns matcher semantics). Rejects the
    classic typo shapes fail-closed: a non-list, non-string items, whitespace outside the
    parens (``Bash rm``), an empty ``Tool()``, newlines.
    """
    for listkey in ("allow", "deny", "ask"):
        value = p.get(listkey)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(
                f"permissions.{listkey} must be a list of strings, got {value!r}",
                schema_path=f"permissions.{listkey}",
            )
        for v in value:
            if not _PERMISSION_RULE_RE.match(v):
                raise ConfigError(
                    f"permissions.{listkey} entry {v!r} is not a permission rule "
                    "(expected Tool or Tool(specifier), e.g. WebFetch or Bash(git status:*))",
                    schema_path=f"permissions.{listkey}",
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
        raise ConfigError(
            f"models.schedule.time must be 'HH:MM', got {value!r}",
            fix="use a 24-hour time like '12:00'",
            schema_path="models.schedule.time",
        )
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(
            f"models.schedule.time out of range (00:00–23:59), got {value!r}",
            schema_path="models.schedule.time",
        )
    return hour, minute


def _validate_models(m: dict[str, Any]) -> None:
    """Validate the ``models`` block — the model-freshness checker's daily schedule.

    The block provisions a daily cron (launchd on macOS, crontab on Linux) that runs the
    agent-tools checker. Fail-closed on: a non-mapping block, a non-bool ``enabled``, a
    malformed ``schedule.time`` (must be ``HH:MM``), and unknown keys (typo guard). An
    EMPTY/absent block means "no schedule provisioned" — rig leaves the system cron alone.
    """
    if not isinstance(m, dict):
        raise ConfigError("models must be a mapping", schema_path="models")
    if not m:
        return
    _reject_unknown_keys(m, "models")
    enabled = m.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"models.enabled must be a bool, got {enabled!r}", schema_path="models.enabled")
    checker_path = m.get("checker_path")
    if checker_path is not None and not isinstance(checker_path, str):
        raise ConfigError(
            f"models.checker_path must be a string, got {checker_path!r}",
            schema_path="models.checker_path",
        )
    schedule = m.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ConfigError("models.schedule must be a mapping", schema_path="models.schedule")
    _reject_unknown_keys(schedule, "models.schedule")
    if "time" in schedule:
        parse_hhmm(schedule["time"])  # fail-closed on a bad time
    label = schedule.get("label")
    if label is not None and not isinstance(label, str):
        raise ConfigError(
            f"models.schedule.label must be a string, got {label!r}",
            schema_path="models.schedule.label",
        )


def _validate_agents_md(am: dict[str, Any]) -> None:
    """Validate the ``agents_md`` block — AGENTS.md/CLAUDE.md canonical+symlink provisioning.

    Default **ON**: every repo gets one canonical agent-guide file (``AGENTS.md``) plus a
    ``CLAUDE.md`` symlink to it, so every harness reads the same instructions. Opt out with
    ``agents_md: { enabled: false }`` (or the equivalent ``{ symlink: false }``). An absent
    block means "provision it" — there is nothing to validate then. Fail-closed on non-bool
    knobs and unknown keys (typo guard), consistent with every other block.
    """
    if not isinstance(am, dict):
        raise ConfigError("agents_md must be a mapping", schema_path="agents_md")
    if not am:
        return
    _reject_unknown_keys(am, "agents_md")
    for knob in ("enabled", "symlink"):
        value = am.get(knob)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"agents_md.{knob} must be a bool, got {value!r}",
                schema_path=f"agents_md.{knob}",
            )


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


# ── ship_delegator ─────────────────────────────────────────────────────────────────
# rig provisions a per-repo ``.claude/scripts/pr-ship.sh`` thin delegator so the repo-keyed
# ``gh ship`` alias (``gh ship`` → ``<repo>/.claude/scripts/pr-ship.sh``) works in EVERY managed
# repo on a clean machine — not only in agent-tools, which is the only repo that historically
# carried it. The delegator execs the canonical ``ci/ship/ship.sh`` (agent-tools' real ship
# implementation): repo-local first (so agent-tools self-hosts), else via ``$AGENT_TOOLS_ROOT``
# — from the environment or from the machine-level ``$XDG_CONFIG_HOME/agent-tools/env`` file rig
# apply writes (the delegator itself is a portable constant, no baked path). The file is IGNORED in
# the repo's ``.git/info/exclude`` (a per-repo, never-committed exclude) so it does not dirty the
# worktree — ship refuses a dirty tree, so an un-ignored provisioned file would break the very
# command it enables. Defined here (the schema layer, stdlib-only) so the plan builder, runner,
# and drift reference the SAME relative path + marker; the runner imports them so the two never
# drift.
SHIP_DELEGATOR_REL_PATH = ".claude/scripts/pr-ship.sh"

# The markers that fence rig's managed entry in the per-repo ``.git/info/exclude``. Distinct text
# from the GLOBAL excludes markers so the two managed blocks never collide if a future change put
# both in one file (they don't today — global vs per-repo). Same begin/end shape the global block
# uses, reconciled by the same marker-block machinery.
SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER = "# >>> rig-managed ship delegator (do not edit) >>>"
SHIP_DELEGATOR_EXCLUDE_END_MARKER = "# <<< rig-managed ship delegator (do not edit) <<<"
SHIP_DELEGATOR_EXCLUDE_COMMENT = (
    "# rig provisions .claude/scripts/pr-ship.sh (the `gh ship` delegator); ignored so it "
    "does not dirty the worktree."
)

# rig provisions the opencode hook bridge as a repo-local symlink so it can run after project
# plugins. The symlink target is machine-local, so it must be ignored per repo rather than
# committed.
OPENCODE_HOOK_BRIDGE_EXCLUDE_BEGIN_MARKER = "# >>> rig-managed opencode hook bridge (do not edit) >>>"
OPENCODE_HOOK_BRIDGE_EXCLUDE_END_MARKER = "# <<< rig-managed opencode hook bridge (do not edit) <<<"
OPENCODE_HOOK_BRIDGE_EXCLUDE_COMMENT = (
    "# rig provisions the opencode hook bridge plugin symlink; ignored so it does not dirty "
    "the worktree."
)
OPENCODE_HOOK_BRIDGE_PLUGIN_NAME = "zz-agent-tools-hook-bridge.js"


def _validate_ship_delegator(sd: dict[str, Any]) -> None:
    """Validate the ``ship_delegator`` block — rig's per-repo ``gh ship`` delegator.

    Default **ON**: every managed repo gets ``.claude/scripts/pr-ship.sh`` so ``gh ship`` works
    there on a clean machine. The only knob is ``enabled`` (opt out with ``{ enabled: false }``).
    Fail-closed, consistent with every other block, on a non-mapping block, a non-bool ``enabled``,
    or an unknown key (typo guard).
    """
    if not isinstance(sd, dict):
        raise ConfigError("ship_delegator must be a mapping", schema_path="ship_delegator")
    if not sd:
        return
    _reject_unknown_keys(sd, "ship_delegator")
    # `enabled` must be a real bool when PRESENT — and we reject an explicit `null` (`enabled: ~`)
    # too: the published JSON Schema declares `boolean` (null is invalid there), so accepting null in
    # the validator would let rig pass a config an editor / CI schema-lint flags. To take the default,
    # OMIT the key; don't set it to null. (`"enabled" in sd`, not `.get()`, so an explicit null is
    # caught rather than silently read as "absent".)
    if "enabled" in sd and not isinstance(sd["enabled"], bool):
        raise ConfigError(
            f"ship_delegator.enabled must be a bool, got {sd['enabled']!r}",
            schema_path="ship_delegator.enabled",
        )


# ── linters ──────────────────────────────────────────────────────────────────────
# rig provisions per-repo LINTER + FORMATTER config files the same way it provisions skills/hooks/
# CI/ship: a config-driven block declares, per repo, WHICH config file each tool needs and the
# EXACT bytes it should hold (e.g. an `oxfmt` formatter writing `.oxfmtrc.jsonc`, a `ruff` linter
# writing `ruff.toml`, an `eslint`/`prettier` pair, …). rig init/apply reconciles each file
# (create when absent, repair when drifted, never clobber a hand-written file without an
# on_conflict-honoring backup); rig status reports drift. The tool + path + content are PER-REPO
# config — rig hardcodes NO specific linter. The keys below are the fixed per-item knobs, listed
# once so the validator, the plan builder, the runner, and the schema registry reference the SAME
# set and never disagree (the sync test asserts it).
#
# CTO decision #4136.2 ("linter settings must also be provisioned by rig"): linter config is now a
# first-class reconciled area, not a thing each repo hand-maintains and silently lets drift.
LINTER_ITEM_KEYS = {"tool", "role", "path", "content", "enabled"}
# `role` is DESCRIPTIVE — it is rendered in the apply/drift label (`<role> <tool>:<item>`, see
# runner._linter_label) so a formatter and a linter read distinctly; both roles reconcile identically
# (a config file written + drift-compared). A foreign role is rejected so a typo (`role: format`)
# fails loudly rather than rendering an odd label.
_VALID_LINTER_ROLES = {"linter", "formatter"}


def linter_path_escapes_repo(rel: str) -> bool:
    """True when a ``linters.items.<n>.path`` would write OUTSIDE the repo (reject it).

    A provisioned config file must stay inside the repo: a committed rig.yaml that could write
    anywhere on disk (``../../etc/x``, ``/etc/x``, a Windows ``C:\\...`` / ``C:/...`` drive-absolute,
    or a backslash path that escapes once interpreted on Windows) is a footgun. This is the SINGLE
    predicate the validator (fail-closed at load) AND the runner/drift (defense-in-depth against a
    hand-built or replayed Action) both call, so the containment rule lives in exactly one place.
    Conservative by design — it rejects on ANY of: a POSIX, native, OR Windows-drive absolute (so a
    config authored on one OS can't silently escape on another), a ``..`` component, a backslash, a
    ``.git`` component (a path into the git dir would let a committed rig.yaml rewrite repo metadata
    or install a hook on apply — a privilege-escalation footgun, never a real linter config location),
    or a ``.``-only path (``.``/``./.``) that names the repo ROOT and so can never name a file.
    """
    if not isinstance(rel, str) or not rel:
        return True
    if "\\" in rel:
        return True  # a Windows separator: ambiguous/unsafe across platforms — refuse it.
    # Absolute under ANY interpretation: POSIX (`/x`), the running OS, OR a Windows DRIVE — the last
    # catches `C:/tmp/x` (forward-slashed) on POSIX, where PurePosixPath sees a plain relative name
    # but Windows would treat it as drive-absolute. PureWindowsPath.drive is non-empty for `C:/...`.
    if PurePosixPath(rel).is_absolute() or os.path.isabs(rel):
        return True
    if PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        return True
    # `..` as a path COMPONENT (not a substring — `..foo` is a legal filename). Check both the POSIX
    # split and the OS-native split so `foo/../../bar` is caught regardless of the running platform.
    parts = set(PurePosixPath(rel).parts) | set(Path(rel).parts)
    if ".." in parts:
        return True
    # `.git` as ANY component: a provisioned file inside the git dir (`.git/config`,
    # `.git/hooks/pre-commit`) would let a committed rig.yaml rewrite repo metadata or install a hook
    # on `rig apply` — a privilege-escalation footgun. A linter config NEVER legitimately lives in
    # `.git`, so reject it (case-insensitive: `.GIT` is the same dir on a case-insensitive FS).
    if any(p.lower() == ".git" for p in parts):
        return True
    # A path that is ONLY `.` components (`.`, `./`, `./.`) names the repo ROOT, never a file — it
    # has no `..`/abs so it would otherwise validate clean, yet can never converge (`repo_root / "."
    # == repo_root`, a directory → io_error forever). Reject it at load too, consistent with the
    # fail-closed-at-validation principle: never accept a "valid config that can never name a file".
    return parts <= {"."}


def _validate_linters(li: dict[str, Any]) -> None:
    """Validate the ``linters`` block — rig's per-repo linter/formatter config provisioning.

    Default **ON**: every declared item is provisioned on ``rig init`` AND ``rig apply``. The block
    carries a fixed ``enabled`` flag plus an open ``items`` map keyed by an arbitrary LABEL; each
    item declares ``{ tool, role?, path, content, enabled? }`` — the tool name, an optional role
    (``linter``/``formatter``, default ``linter``, status-label only), the repo-relative file
    ``path``, and the exact file ``content``. Fail-closed, consistent with every other block, on: a
    non-mapping block, a non-bool ``enabled``, a non-mapping ``items``, a non-mapping item, an
    unknown per-item key (typo guard), a missing/empty/non-string ``tool`` / ``path`` / ``content``,
    an absolute or parent-escaping ``path`` (a provisioned file must stay inside the repo), a
    non-bool item ``enabled``, a ``role`` outside the allowed set, and TWO enabled items targeting
    the SAME normalized ``path`` (which would make apply/status churn forever).
    """
    if not isinstance(li, dict):
        raise ConfigError("linters must be a mapping", schema_path="linters")
    if not li:
        return
    # Only `enabled` and `items` are fixed keys; `items` is the open map. Reject any other top key.
    for key in li:
        if key not in {"enabled", "items"}:
            raise ConfigError(
                f"unknown linters key {key!r} (expected one of: enabled, items)",
                schema_path="linters",
            )
    if "enabled" in li and not isinstance(li["enabled"], bool):
        raise ConfigError(
            f"linters.enabled must be a bool, got {li['enabled']!r}", schema_path="linters.enabled"
        )
    items = li.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("linters.items must be a mapping", schema_path="linters.items")
    # Track the target path of each ENABLED item so two items can't both provision the same file:
    # with different content `rig apply` would write one then the other and `rig status` would churn
    # forever (each apply re-flags + re-backs-up the loser). One file = one item.
    seen_paths: dict[str, str] = {}
    for name, spec in items.items():
        path = f"linters.items.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"{path} must be a mapping", schema_path=path)
        for key in spec:
            if key not in LINTER_ITEM_KEYS:
                raise ConfigError(
                    f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(LINTER_ITEM_KEYS))})",
                    schema_path=path,
                )
        # tool / path / content are REQUIRED non-empty strings — a config file with no path or no
        # bytes is meaningless; failing here beats writing a 0-byte file or crashing in the runner.
        for req in ("tool", "path", "content"):
            val = spec.get(req)
            if not isinstance(val, str) or not val:
                raise ConfigError(
                    f"{path}.{req} must be a non-empty string, got {val!r}",
                    schema_path=f"{path}.{req}",
                )
        rel = spec["path"]
        # Reject leading/trailing whitespace in `path`: the runner/drift do NOT strip it (they operate
        # on the literal bytes), so ` ../escape` would validate as one filename here yet escape at
        # apply time, and ` config/x ` would validate as a quoted name but write to `config/x`. Fail
        # closed on the ambiguity — a config path is a literal filename, never whitespace-padded.
        if rel != rel.strip():
            raise ConfigError(
                f"{path}.path must not have leading/trailing whitespace, got {rel!r}",
                schema_path=f"{path}.path",
            )
        # The provisioned file MUST stay inside the repo — see `linter_path_escapes_repo` (the one
        # containment predicate the runner/drift also call, so the rule lives in a single place).
        if linter_path_escapes_repo(rel):
            raise ConfigError(
                f"{path}.path must be a repo-relative path inside the repo (no leading '/', '..', or '\\'), got {rel!r}",
                schema_path=f"{path}.path",
            )
        role = spec.get("role")
        # `isinstance(role, str)` BEFORE the membership test: a non-string role (a YAML list/map, e.g.
        # `role: [linter]`) is UNHASHABLE, so `role not in <set>` would raise a raw TypeError instead
        # of a structured ConfigError. Guard the type first so every bad role fails the same clean way.
        if role is not None and (not isinstance(role, str) or role not in _VALID_LINTER_ROLES):
            raise ConfigError(
                f"{path}.role must be one of {sorted(_VALID_LINTER_ROLES)}, got {role!r}",
                fix=f"use one of: {', '.join(sorted(_VALID_LINTER_ROLES))}",
                schema_path=f"{path}.role",
            )
        if "enabled" in spec and not isinstance(spec["enabled"], bool):
            raise ConfigError(
                f"{path}.enabled must be a bool, got {spec['enabled']!r}",
                schema_path=f"{path}.enabled",
            )
        # Duplicate-target guard (enabled items only — a disabled item provisions nothing, so it can't
        # collide). Normalize the path (`./a` vs `a`) so `a/b` and `./a/b` are recognized as the same
        # file. Two items on one file is always a config smell; reject it rather than let apply/status
        # churn. A disabled item is skipped, so toggling one off resolves a collision without an edit.
        if spec.get("enabled") is not False:
            norm = PurePosixPath(rel).as_posix()
            if norm in seen_paths:
                raise ConfigError(
                    f"{path}.path {rel!r} is already provisioned by linters.items.{seen_paths[norm]} "
                    "— two items must not target the same file",
                    fix="give each item a distinct path, or disable one with `enabled: false`",
                    schema_path=f"{path}.path",
                )
            seen_paths[norm] = name


def _validate_string_list(value: Any, path: str) -> None:
    """Fail-closed unless ``value`` is a list of strings."""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{path} must be a list of strings", schema_path=path)


def _validate_project_tools(pt: dict[str, Any]) -> None:
    """Validate repo-local project-tool provisioning (Haft, Serena, Sverklo).

    This is distinct from the GLOBAL ``tools`` block: ``tools`` installs personal command-line
    programs, while ``project_tools`` writes committed repo carriers and safe live registrations so
    the project is usable by those tools. The block is default-off unless present in a scaffolded
    ``rig.yaml``. Fail closed on unknown keys and bad scalar/list types.
    """
    if not isinstance(pt, dict):
        raise ConfigError("project_tools must be a mapping", schema_path="project_tools")
    if not pt:
        return
    _reject_unknown_keys(pt, "project_tools")
    _check_bool(pt, "enabled", "project_tools.enabled")

    haft = pt.get("haft", {})
    if haft is not None:
        if not isinstance(haft, dict):
            raise ConfigError("project_tools.haft must be a mapping", schema_path="project_tools.haft")
        for key in haft:
            if key not in HAFT_KEYS:
                raise ConfigError(
                    f"unknown project_tools.haft key {key!r} (expected one of: {', '.join(sorted(HAFT_KEYS))})",
                    schema_path="project_tools.haft",
                )
        _check_bool(haft, "enabled", "project_tools.haft.enabled")
        _check_bool(haft, "codex_mcp", "project_tools.haft.codex_mcp")
        _check_str(haft, "project_name", "project_tools.haft.project_name")
        _check_str(haft, "project_id", "project_tools.haft.project_id")
        workflow = haft.get("workflow", {})
        if workflow is not None:
            if not isinstance(workflow, dict):
                raise ConfigError(
                    "project_tools.haft.workflow must be a mapping",
                    schema_path="project_tools.haft.workflow",
                )
            for key in workflow:
                if key not in HAFT_WORKFLOW_KEYS:
                    raise ConfigError(
                        f"unknown project_tools.haft.workflow key {key!r} "
                        f"(expected one of: {', '.join(sorted(HAFT_WORKFLOW_KEYS))})",
                        schema_path="project_tools.haft.workflow",
                    )
            mode = workflow.get("mode")
            if mode is not None and mode not in HAFT_WORKFLOW_MODES:
                raise ConfigError(
                    f"project_tools.haft.workflow.mode must be one of {list(HAFT_WORKFLOW_MODES)}, got {mode!r}",
                    fix=f"use one of: {', '.join(HAFT_WORKFLOW_MODES)}",
                    schema_path="project_tools.haft.workflow.mode",
                )
            for key in ("require_decision", "require_verify", "allow_autonomy"):
                _check_bool(workflow, key, f"project_tools.haft.workflow.{key}")

    serena = pt.get("serena", {})
    if serena is not None:
        if not isinstance(serena, dict):
            raise ConfigError("project_tools.serena must be a mapping", schema_path="project_tools.serena")
        for key in serena:
            if key not in SERENA_KEYS:
                raise ConfigError(
                    f"unknown project_tools.serena key {key!r} (expected one of: {', '.join(sorted(SERENA_KEYS))})",
                    schema_path="project_tools.serena",
                )
        _check_bool(serena, "enabled", "project_tools.serena.enabled")
        _check_bool(serena, "read_only", "project_tools.serena.read_only")
        _check_str(serena, "project_name", "project_tools.serena.project_name")
        if "languages" in serena:
            _validate_string_list(serena["languages"], "project_tools.serena.languages")
        if "ignored_paths" in serena:
            _validate_string_list(serena["ignored_paths"], "project_tools.serena.ignored_paths")

    sverklo = pt.get("sverklo", {})
    if sverklo is not None:
        if not isinstance(sverklo, dict):
            raise ConfigError("project_tools.sverklo must be a mapping", schema_path="project_tools.sverklo")
        for key in sverklo:
            if key not in SVERKLO_KEYS:
                raise ConfigError(
                    f"unknown project_tools.sverklo key {key!r} (expected one of: {', '.join(sorted(SVERKLO_KEYS))})",
                    schema_path="project_tools.sverklo",
                )
        for key in ("enabled", "register", "reindex"):
            _check_bool(sverklo, key, f"project_tools.sverklo.{key}")


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
        raise ConfigError("gitignore must be a mapping", schema_path="gitignore")
    if not gi:
        return
    _reject_unknown_keys(gi, "gitignore")
    enabled = gi.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"gitignore.enabled must be a bool, got {enabled!r}", schema_path="gitignore.enabled")
    excludesfile = gi.get("excludesfile")
    if excludesfile is not None and not isinstance(excludesfile, str):
        raise ConfigError(
            f"gitignore.excludesfile must be a string, got {excludesfile!r}",
            schema_path="gitignore.excludesfile",
        )
    entries = gi.get("entries")
    if entries is not None:
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ConfigError(
                f"gitignore.entries must be a list of strings, got {entries!r}",
                schema_path="gitignore.entries",
            )
        # Reject an entry that carries one of rig's block markers: writing it inside the managed
        # block would make every later resolve see a duplicated marker and classify the file as a
        # permanent conflict (apply could never re-converge). Fail closed on the footgun.
        for e in entries:
            if GITIGNORE_BEGIN_MARKER in e or GITIGNORE_END_MARKER in e:
                raise ConfigError(
                    f"gitignore.entries may not contain a rig-managed marker line, got {e!r}",
                    schema_path="gitignore.entries",
                )


def _validate_spotlight(s: dict[str, Any]) -> None:
    """Validate the ``spotlight`` block — the macOS Spotlight-exclude sweep + periodic agent.

    GLOBAL (machine-wide) config: rig drops ``.metadata_never_index`` into dependency/build dirs
    under the configured dev roots and installs a launchd re-sweep agent. Default **OFF** (opt-in,
    macOS-specific): an absent/empty block is a no-op. Fail-closed on a non-mapping block, a
    non-bool ``enabled``, an unknown key, non-string-list ``roots``/``deny``/``extra``, a
    non-string ``label``, and a non-positive-int ``max_depth``.
    """
    if not isinstance(s, dict):
        raise ConfigError("spotlight must be a mapping", schema_path="spotlight")
    if not s:
        return
    _reject_unknown_keys(s, "spotlight")
    enabled = s.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"spotlight.enabled must be a bool, got {enabled!r}", schema_path="spotlight.enabled")
    for key in ("roots", "deny", "extra"):
        val = s.get(key)
        if val is not None and (not isinstance(val, list) or not all(isinstance(e, str) for e in val)):
            raise ConfigError(
                f"spotlight.{key} must be a list of strings, got {val!r}",
                schema_path=f"spotlight.{key}",
            )
    label = s.get("label")
    if label is not None and not isinstance(label, str):
        raise ConfigError(f"spotlight.label must be a string, got {label!r}", schema_path="spotlight.label")
    max_depth = s.get("max_depth")
    if max_depth is not None and (isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1):
        raise ConfigError(
            f"spotlight.max_depth must be a positive int, got {max_depth!r}",
            schema_path="spotlight.max_depth",
        )


# The ruleset knobs that are plain booleans (typo + type guard). Listed once so the
# validator and the action builder reference the SAME knob set.
# The bool knobs of each github sub-block — every one validated as a bool (fail-closed) so a typo'd
# value (`enabled: yes`, `squash_merge: 1`) is rejected with the schema path. Listed once here so
# the validator and the schema registry never disagree on the type of a knob. The allowed-key sets
# themselves come from config_schema (the single source `_reject_unknown_keys` reads).
_GITHUB_MERGE_BOOL_KNOBS = (
    "enabled",
    "squash_merge",
    "merge_commit",
    "rebase_merge",
    "delete_branch_on_merge",
    "allow_auto_merge",
    "allow_update_branch",
)
_GITHUB_GHAS_BOOL_KNOBS = (
    "enabled",
    "vulnerability_alerts",
    "automated_security_fixes",
    "secret_scanning",
    "secret_scanning_push_protection",
    "code_scanning_default_setup",
)
_GITHUB_ACTIONS_BOOL_KNOBS = ("enabled", "actions_enabled", "can_approve_pull_request_reviews")
_GITHUB_BROWSER_BOOL_KNOBS = ("enabled", "discussions", "projects")
# Enum knobs of github.actions → their allowed values (fail-closed on anything else).
_GITHUB_ACTIONS_ENUMS = {
    "allowed_actions": ("all", "local_only", "selected"),
    "default_workflow_permissions": ("read", "write"),
}
_GITHUB_RULESET_BOOL_KNOBS = (
    "enabled",
    "require_pull_request",
    "required_conversation_resolution",
    "dismiss_stale_reviews",
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
        raise ConfigError("github must be a mapping", schema_path="github")
    if not gh:
        return
    _reject_unknown_keys(gh, "github")
    ruleset = gh.get("ruleset", {})
    if not isinstance(ruleset, dict):
        raise ConfigError("github.ruleset must be a mapping", schema_path="github.ruleset")
    _reject_unknown_keys(ruleset, "github.ruleset")
    for knob in _GITHUB_RULESET_BOOL_KNOBS:
        value = ruleset.get(knob)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"github.ruleset.{knob} must be a bool, got {value!r}",
                schema_path=f"github.ruleset.{knob}",
            )
    name = ruleset.get("name")
    if name is not None and not isinstance(name, str):
        raise ConfigError(
            f"github.ruleset.name must be a string, got {name!r}",
            schema_path="github.ruleset.name",
        )
    reviews = ruleset.get("required_reviews")
    # NB: bool is an int subclass in Python — reject it explicitly so `true` can't masquerade
    # as a review count.
    if reviews is not None and (isinstance(reviews, bool) or not isinstance(reviews, int) or reviews < 0):
        raise ConfigError(
            f"github.ruleset.required_reviews must be an int >= 0, got {reviews!r}",
            schema_path="github.ruleset.required_reviews",
        )
    checks = ruleset.get("required_status_checks")
    if checks is not None:
        if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
            raise ConfigError(
                f"github.ruleset.required_status_checks must be a list of strings, got {checks!r}",
                schema_path="github.ruleset.required_status_checks",
            )
    # The other github sub-blocks (merge / ghas / actions / browser) — each a mapping, each with its
    # allowed keys gated by _reject_unknown_keys (sourced from the schema registry), each bool knob
    # validated as a bool, and github.actions' two enum knobs pinned to their allowed values.
    merge = _validate_github_subblock(gh, "merge", _GITHUB_MERGE_BOOL_KNOBS)
    # Skip the "at least one merge model" check on a DISABLED block: `_build_github_merge` doesn't
    # provision it (enabled:false → no PATCH), so rejecting an all-off-but-disabled block would
    # forbid a harmless combination the plan never acts on — keep validation aligned with the plan.
    if merge is not None and merge.get("enabled") is not False:
        # GitHub rejects (HTTP 422) a repo that allows NO merge model. Catch it at config time —
        # a `rig.yaml` that leaves squash, merge-commit, AND rebase all off would otherwise fail only
        # on the live PATCH. Each omitted knob falls back to its default (squash ON, the other two
        # OFF), so "all off" is reachable by setting squash_merge:false alone (merge_commit and
        # rebase_merge already default off). An explicit `null` resolves to the DEFAULT too — matching
        # the plan builder, which drops `null` overrides (`v is not None`) so a `squash_merge: null`
        # is squash-ON, not off. We resolve null→default here so the validator can't reject a config
        # the plan would happily run.
        _MERGE_MODEL_DEFAULTS = {"squash_merge": True, "merge_commit": False, "rebase_merge": False}

        def _resolved(k: str, default: bool) -> bool:
            v = merge.get(k, default)
            return default if v is None else bool(v)

        if all(not _resolved(k, default) for k, default in _MERGE_MODEL_DEFAULTS.items()):
            raise ConfigError(
                "github.merge disables every merge model (squash_merge, merge_commit, rebase_merge "
                "all false) — GitHub requires at least one; enable one (squash_merge is the default)",
                schema_path="github.merge",
            )
    _validate_github_subblock(gh, "ghas", _GITHUB_GHAS_BOOL_KNOBS)
    actions = _validate_github_subblock(gh, "actions", _GITHUB_ACTIONS_BOOL_KNOBS)
    if actions is not None:
        for knob, allowed in _GITHUB_ACTIONS_ENUMS.items():
            value = actions.get(knob)
            if value is not None and value not in allowed:
                raise ConfigError(
                    f"github.actions.{knob} must be one of {', '.join(allowed)}, got {value!r}",
                    schema_path=f"github.actions.{knob}",
                )
    _validate_github_subblock(gh, "browser", _GITHUB_BROWSER_BOOL_KNOBS)


def _validate_github_subblock(
    gh: dict[str, Any], name: str, bool_knobs: tuple[str, ...]
) -> dict[str, Any] | None:
    """Validate one ``github.<name>`` sub-block; return it (for further checks) or None if absent.

    Fail-closed, consistent with every other block: a non-mapping sub-block, an unknown key (typo
    guard, via the schema-sourced :func:`_reject_unknown_keys`), or a non-bool bool knob each raise
    a 3-part ConfigError with the schema path. A sub-block ABSENT from the github block → returns
    None (the caller skips its extra cross-knob checks — nothing was configured, defaults apply); an
    empty sub-block (``merge: {}``) is present-but-default and returns ``{}``, so its cross-knob
    checks still run on the resolved defaults.
    """
    if name not in gh:
        return None  # absent → no sub-block to validate; caller's extra checks are skipped
    block = gh.get(name, {})
    if not isinstance(block, dict):
        raise ConfigError(f"github.{name} must be a mapping", schema_path=f"github.{name}")
    _reject_unknown_keys(block, f"github.{name}")
    for knob in bool_knobs:
        _check_bool(block, knob, f"github.{name}.{knob}")
    return block


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
    "autosave",
    "pane_titles",
}
_TMUX_SUBKEYS = {
    "resurrect": {"processes", "capture_pane_contents"},
    "continuum": {"restore", "boot", "save_interval"},
    "moshi": {"enabled"},
    "cc_restore": {"enabled"},
    "anti_sprawl": {"enabled", "session"},
    "boot": {"enabled", "label"},
    "login_shell": {"enabled", "shell"},
    "autosave": {"enabled", "label", "stale_after"},
    "pane_titles": {"enabled", "position", "format", "clear_status_right"},
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
        raise ConfigError("tmux must be a mapping", schema_path="tmux")
    if not t:
        return
    _reject_unknown_keys(t, "tmux")

    enabled = t.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"tmux.enabled must be a bool, got {enabled!r}", schema_path="tmux.enabled")
    # `apply` PRESENT must be a valid enum string. A present null would `str(None)` into a bogus
    # "None" apply_mode, and an unhashable value (list/dict) would raise a raw TypeError from the
    # `in` check — so require a string in the enum. Only an ABSENT key falls back to the default.
    if "apply" in t and (
        not isinstance(t["apply"], str) or t["apply"] not in _VALID_TMUX_APPLY
    ):
        raise ConfigError(
            f"tmux.apply must be one of {sorted(_VALID_TMUX_APPLY)}, got {t['apply']!r}",
            fix=f"use one of: {', '.join(sorted(_VALID_TMUX_APPLY))}",
            schema_path="tmux.apply",
        )
    for pathkey in ("conf_path", "generated_dir"):
        # A key PRESENT with no value (YAML `conf_path:` → None) must fail closed: it can't fall
        # back to the default the way a truly-absent key does (the plan would `str(None)` it into
        # a literal "None" path). Only an absent key is allowed; a present non-string is rejected.
        if pathkey in t and not isinstance(t[pathkey], str):
            raise ConfigError(
                f"tmux.{pathkey} must be a string, got {t[pathkey]!r}",
                schema_path=f"tmux.{pathkey}",
            )

    for sub in _TMUX_SUBKEYS:
        block = t.get(sub)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"tmux.{sub} must be a mapping, got {block!r}", schema_path=f"tmux.{sub}")
        _reject_unknown_keys(block, f"tmux.{sub}")

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

    for sub in ("moshi", "cc_restore", "anti_sprawl", "boot", "login_shell", "autosave", "pane_titles"):
        block = t.get(sub, {})
        if isinstance(block, dict):
            value = block.get("enabled")
            if value is not None and not isinstance(value, bool):
                raise ConfigError(f"tmux.{sub}.enabled must be a bool, got {value!r}")
    pane_titles = t.get("pane_titles", {})
    if isinstance(pane_titles, dict):
        # Import here (not at module top) only to sidestep import-order churn in this large
        # module; tmux.py itself has zero internal riglib imports, so there is no cycle risk —
        # this is the ONE source for both the valid-position set and the format-safety check,
        # shared with tmux.build_tmux's own defense-in-depth clamp (review: the two must never
        # independently drift).
        from .tmux import VALID_PANE_TITLES_POSITIONS, _pane_titles_format_is_safe

        position = pane_titles.get("position")
        if position is not None and (
            not isinstance(position, str) or position not in VALID_PANE_TITLES_POSITIONS
        ):
            raise ConfigError(
                f"tmux.pane_titles.position must be one of "
                f"{sorted(VALID_PANE_TITLES_POSITIONS)}, got {position!r}",
                fix=f"use one of: {', '.join(sorted(VALID_PANE_TITLES_POSITIONS))}",
                schema_path="tmux.pane_titles.position",
            )
        fmt = pane_titles.get("format")
        if fmt is not None:
            if not isinstance(fmt, str):
                raise ConfigError(f"tmux.pane_titles.format must be a string, got {fmt!r}")
            if not _pane_titles_format_is_safe(fmt):
                raise ConfigError(
                    r"""tmux.pane_titles.format must not contain '"', '\', '$', '#(' (a """
                    "shell-exec token), or a non-printable character other than a plain tab "
                    "(they can corrupt, inject into, or execute code from the generated tmux "
                    f"config), got {fmt!r}",
                    fix=(
                        "drop the offending character/sequence — tmux format variables use "
                        r"""'#{...}', never '$' or '#('"""
                    ),
                    schema_path="tmux.pane_titles.format",
                )
        clear_sr = pane_titles.get("clear_status_right")
        if clear_sr is not None and not isinstance(clear_sr, bool):
            raise ConfigError(
                f"tmux.pane_titles.clear_status_right must be a bool, got {clear_sr!r}"
            )
    autosave = t.get("autosave", {})
    if isinstance(autosave, dict):
        label = autosave.get("label")
        if label is not None and not isinstance(label, str):
            raise ConfigError(f"tmux.autosave.label must be a string, got {label!r}")
        stale = autosave.get("stale_after")
        # bool is an int subclass — reject it so `true` can't pose as a minute count.
        if stale is not None and (
            isinstance(stale, bool) or not isinstance(stale, int) or stale < 1
        ):
            raise ConfigError(
                f"tmux.autosave.stale_after must be an int >= 1, got {stale!r}"
            )
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


def _validate_tools(t: dict[str, Any]) -> None:
    """Validate the ``tools`` block — the personal CLI ecosystem rig installs at apply.

    A per-MACHINE concern (the tool ecosystem on this dev box), so it belongs in the GLOBAL layer
    (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/``tg_ctl`` — NOT a committed repo
    ``rig.yaml``. Default **OFF** (opt-in): an absent/empty block provisions nothing; a machine
    opts in by listing tools under ``items``. Fail-closed, consistent with every other block, on:
    a non-mapping block, an unknown FIXED key (typo guard — the open ``items`` map keeps arbitrary
    tool NAMES valid), a non-bool ``enabled``, a non-string ``target``, a non-mapping ``items`` or
    item, and a bad per-item ``enabled``/``repo``/``bin_dir`` type.
    """
    if not isinstance(t, dict):
        raise ConfigError("tools must be a mapping", schema_path="tools")
    if not t:
        return
    _reject_unknown_keys(t, "tools")
    _check_bool(t, "enabled", "tools.enabled")
    _check_str(t, "target", "tools.target")
    items = t.get("items", {})
    if not isinstance(items, dict):
        raise ConfigError("tools.items must be a mapping", schema_path="tools.items")
    for name, spec in items.items():
        _validate_tools_item(name, spec)


# The keys a tools.items.<name> entry accepts. Explicit set (mirrors LINTER_ITEM_KEYS) so the
# validator rejects a per-item typo — _reject_unknown_keys can't, since block_child_keys only
# walks .nested, never an open_map_item block.
_TOOLS_ITEM_KEYS = {"enabled", "repo", "bin_dir"}


def _validate_tools_item(name: str, spec: Any) -> None:
    """Validate one ``tools.items.<name>`` entry — its shape and per-item knob types."""
    path = f"tools.items.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{path} must be a mapping", schema_path=path)
    for key in spec:
        if key not in _TOOLS_ITEM_KEYS:
            raise ConfigError(
                f"unknown {path} key {key!r} (expected one of: {', '.join(sorted(_TOOLS_ITEM_KEYS))})",
                schema_path=path,
            )
    _check_bool(spec, "enabled", f"{path}.enabled")
    _check_str(spec, "repo", f"{path}.repo")
    _check_str(spec, "bin_dir", f"{path}.bin_dir")


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
        raise ConfigError("tg_ctl must be a mapping", schema_path="tg_ctl")
    if not t:
        return
    _reject_unknown_keys(t, "tg_ctl")
    for boolkey in ("enabled", "boot"):
        value = t.get(boolkey)
        if value is not None and not isinstance(value, bool):
            raise ConfigError(
                f"tg_ctl.{boolkey} must be a bool, got {value!r}",
                schema_path=f"tg_ctl.{boolkey}",
            )
    for strkey in ("label", "bun_path", "tg_ctl_path", "config_dir"):
        value = t.get(strkey)
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"tg_ctl.{strkey} must be a string, got {value!r}",
                schema_path=f"tg_ctl.{strkey}",
            )
