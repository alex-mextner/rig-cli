"""Claude Code config-dir discovery — the ONE place that knows the claude-rotate layout.

Claude Code reads its user-scope ``settings.json`` from ``$CLAUDE_CONFIG_DIR`` when that
variable is set, and from ``~/.claude`` otherwise. ``claude-rotate`` (the multi-account
launcher) starts EVERY interactive session with ``CLAUDE_CONFIG_DIR=~/.claude-accounts/account-N``,
so a rig that provisions hooks/permissions/auto-mode into ``~/.claude/settings.json`` alone
leaves those sessions with ZERO rig-managed hooks — no guard, no Stop gate, no tg-ctl inbox —
while ``rig status`` stays green (rig-cli#368). This module resolves the full set of
user-scope settings files a claude-code write must fan out to:

* the primary target (``~/.claude/settings.json``, or ``harness.settings_path``),
* every explicit ``harness.settings_paths`` entry,
* ``<dir>/settings.json`` for every discovered ``~/.claude-accounts/account-*`` DIRECTORY
  (``harness.discover_config_dirs: false`` opts out).

Fan-out applies ONLY when the primary target IS the user-scope file under ``~/.claude``: a
repo-local ``.claude/settings.json`` is PROJECT scope, which Claude Code reads regardless of
``CLAUDE_CONFIG_DIR``, so duplicating it into account dirs would be wrong. Discovery is
filesystem-only (deterministic plans, stable config-web fingerprints); the ambient
``CLAUDE_CONFIG_DIR`` is consulted by ``rig doctor`` alone (:func:`doctor_config_dirs`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACCOUNTS_DIRNAME = ".claude-accounts"
ACCOUNT_DIR_GLOB = "account-*"
SETTINGS_FILENAME = "settings.json"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def account_sort_key(account_dir: Path) -> tuple[int, str]:
    """Natural-sort ``account-N`` by the numeric suffix, not lexically — a plain
    ``sorted()`` on directory names would order ``account-10`` before ``account-2`` once a
    machine accumulates a 10th rotated account. A non-numeric or missing suffix sorts after
    every numeric one, so this can't raise on an unexpected directory name."""
    suffix = account_dir.name.removeprefix("account-")
    # ASCII digits only: ``str.isdigit`` is true for superscripts/other Unicode digits that
    # ``int()`` rejects (``"²".isdigit()`` → True, ``int("²")`` → ValueError), and one such
    # directory name would crash every discovery caller (review finding on rig-cli#368).
    numeric = suffix.isascii() and suffix.isdigit()
    return (0, f"{int(suffix):020d}") if numeric else (1, account_dir.name)


def _home(home: Path | None) -> Path:
    return home if home is not None else Path(os.path.expanduser("~"))


def primary_config_dir(home: Path | None = None) -> Path:
    """``~/.claude`` — the config dir a bare ``claude`` (no ``CLAUDE_CONFIG_DIR``) uses."""
    return _home(home) / ".claude"


def discover_claude_config_dirs(home: Path | None = None) -> list[Path]:
    """Every ``~/.claude-accounts/account-*`` DIRECTORY on disk, natural-sorted.

    Directories only — the launcher keeps ``current``/``rotate.log`` FILES beside them. A
    missing ``~/.claude-accounts`` is a normal empty result, not an error. Never creates
    anything: a dir that does not exist is not a session that could ever load settings.
    """
    accounts_dir = _home(home) / ACCOUNTS_DIRNAME
    if not accounts_dir.is_dir():
        return []
    dirs = [d for d in accounts_dir.glob(ACCOUNT_DIR_GLOB) if d.is_dir()]
    return sorted(dirs, key=account_sort_key)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def is_user_scope_settings(path: Path, home: Path | None = None) -> bool:
    """Whether ``path`` is the user-scope claude-code settings file (``~/.claude/settings.json``)."""
    return _same_file(path, primary_config_dir(home) / SETTINGS_FILENAME)


ORIGIN_PRIMARY = "primary"
ORIGIN_EXPLICIT = "explicit"  # a ``harness.settings_paths`` entry
ORIGIN_DISCOVERED = "discovered"  # a ``~/.claude-accounts/account-*`` dir


@dataclass(frozen=True)
class SettingsTarget:
    """One settings file a claude-code write fans out to, with a human label for the log/status.

    ``label`` is ``None`` for the primary target (its plan item stays the bare kind, unchanged
    for every existing consumer) and the config-dir name (``account-2``) or the file's parent
    name for a fan-out target — that is what ``rig status`` prints so drift names WHICH file.
    Labels are unique within one fan-out (a collision falls back to the full path), so two
    targets never share a plan ``item``. ``origin`` says where the target came from.
    """

    path: Path
    label: str | None
    origin: str = ORIGIN_PRIMARY


def _explicit_settings_paths(harness: dict[str, Any] | None) -> list[Path]:
    raw = (harness or {}).get("settings_paths")
    if not isinstance(raw, list):
        return []
    return [Path(os.path.expanduser(str(p))) for p in raw if isinstance(p, str) and p]


def discovery_enabled(harness: dict[str, Any] | None) -> bool:
    """``harness.discover_config_dirs`` — default on; only an explicit ``false`` opts out."""
    return (harness or {}).get("discover_config_dirs", True) is not False


def _candidate_targets(harness: dict[str, Any] | None, home: Path | None) -> list[SettingsTarget]:
    """Explicit ``settings_paths`` first, then the discovered account dirs — before dedup."""
    extra = [
        SettingsTarget(p, p.parent.name or str(p), ORIGIN_EXPLICIT) for p in _explicit_settings_paths(harness)
    ]
    if discovery_enabled(harness):
        for config_dir in discover_claude_config_dirs(home):
            extra.append(SettingsTarget(config_dir / SETTINGS_FILENAME, config_dir.name, ORIGIN_DISCOVERED))
    return extra


def fan_out_settings(
    primary: Path, harness: dict[str, Any] | None, home: Path | None = None
) -> list[SettingsTarget]:
    """The settings files a claude-code user-scope write targets: primary + explicit + discovered.

    Deduped by resolved path, primary first, labels unique (a label another target already
    carries — two ``settings_paths`` entries under same-named parents, or a parent literally
    named ``account-2`` — becomes the full path, deterministically, so plan items and the
    config-web fingerprint never alias two files). A non-user-scope ``primary`` (repo-local
    project settings, a custom path) is returned alone — see the module docstring for why.
    """
    targets = [SettingsTarget(primary, None)]
    if not is_user_scope_settings(primary, home):
        return targets
    seen = [primary]
    labels: set[str] = set()
    for candidate in _candidate_targets(harness, home):
        if any(_same_file(candidate.path, s) for s in seen):
            continue
        seen.append(candidate.path)
        label = candidate.label if candidate.label not in labels else str(candidate.path)
        labels.add(label)
        targets.append(SettingsTarget(candidate.path, label, candidate.origin))
    return targets


def managed_settings_files(home: Path | None = None) -> list[Path]:
    """``~/.claude/settings.json`` + every discovered account dir's — the config-less default.

    The ``rig doctor`` missing-target scan loads no rig config, so it scans the files the
    DEFAULT fan-out would manage. Filesystem-only: never consults ``CLAUDE_CONFIG_DIR``, so the
    result does not vary with the caller's shell (the env dir is doctor's separate inventory).
    """
    primary = primary_config_dir(home) / SETTINGS_FILENAME
    return [t.path for t in fan_out_settings(primary, None, home)]


def fan_out_item(kind: str, target: SettingsTarget) -> str:
    """The plan ``item`` for a fan-out action: ``claude-code@account-2`` (primary keeps ``kind``)."""
    return kind if target.label is None else f"{kind}@{target.label}"


# ── rig doctor: what a live `claude` on this machine would actually load ────────────────────
ROLE_DEFAULT = "default"  # ~/.claude — what a bare `claude` (no CLAUDE_CONFIG_DIR) loads
ROLE_ENV = "env"  # $CLAUDE_CONFIG_DIR of the shell running `rig doctor`
ROLE_ACCOUNT = "account"  # a discovered ~/.claude-accounts/account-* dir (managed by the fan-out)
ROLE_CONFIGURED = "configured"  # a harness.settings_paths entry (managed by the fan-out)
ROLE_UNMANAGED_ACCOUNT = "unmanaged-account"  # an account dir with discover_config_dirs: false


@dataclass(frozen=True)
class ConfigDirStatus:
    """The managed-key inventory of one claude-code config dir's ``settings.json``."""

    config_dir: Path
    settings: Path
    role: str
    exists: bool
    hook_events: int  # number of ``hooks`` event keys with at least one block
    bridge_hooks: int  # blocks whose command mentions ``cc_hook_bridge``
    allow_entries: int
    default_mode: str | None
    malformed: bool = False


@dataclass(frozen=True)
class ConfigDirGap:
    """A config dir a live ``claude`` could load that lacks the rig hook bridge, with a fix
    that actually converges it — ``rig apply commit`` reaches only the fan-out targets, so an
    ad-hoc ``CLAUDE_CONFIG_DIR`` dir needs a ``harness.settings_paths`` entry instead."""

    row: ConfigDirStatus
    what: str
    fix: str


def _count_bridge_hooks(hooks: Any) -> int:
    if not isinstance(hooks, dict):
        return 0
    count = 0
    for blocks in hooks.values():
        for block in blocks if isinstance(blocks, list) else []:
            inner = block.get("hooks") if isinstance(block, dict) else None
            for hook in inner if isinstance(inner, list) else []:
                if isinstance(hook, dict) and "cc_hook_bridge" in str(hook.get("command", "")):
                    count += 1
    return count


def inspect_config_dir(config_dir: Path, role: str) -> ConfigDirStatus:
    """Read ``<config_dir>/settings.json`` and count the rig-managed keys it carries."""
    settings = config_dir / SETTINGS_FILENAME
    empty = ConfigDirStatus(config_dir, settings, role, False, 0, 0, 0, None)
    if not settings.is_file():
        return empty
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ConfigDirStatus(config_dir, settings, role, True, 0, 0, 0, None, malformed=True)
    if not isinstance(data, dict):
        return ConfigDirStatus(config_dir, settings, role, True, 0, 0, 0, None, malformed=True)
    hooks = data.get("hooks")
    events = sum(1 for v in hooks.values() if isinstance(v, list) and v) if isinstance(hooks, dict) else 0
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    allow = perms.get("allow")
    mode = perms.get("defaultMode")
    return ConfigDirStatus(
        config_dir, settings, role, True, events, _count_bridge_hooks(hooks),
        len(allow) if isinstance(allow, list) else 0, str(mode) if mode is not None else None,
    )


def _doctor_candidates(
    harness: dict[str, Any] | None, home: Path | None, env: dict[str, str]
) -> list[tuple[Path, str]]:
    """``$CLAUDE_CONFIG_DIR`` first, then the SAME managed targets the plan fans out to, then
    the account dirs a ``discover_config_dirs: false`` opted OUT of (listed as unmanaged)."""
    candidates: list[tuple[Path, str]] = []
    env_dir = env.get(CONFIG_DIR_ENV)
    if env_dir:
        candidates.append((Path(os.path.expanduser(env_dir)), ROLE_ENV))
    primary = primary_config_dir(home) / SETTINGS_FILENAME
    for target in fan_out_settings(primary, harness, home)[1:]:
        role = ROLE_ACCOUNT if target.origin == ORIGIN_DISCOVERED else ROLE_CONFIGURED
        candidates.append((target.path.parent, role))
    if not discovery_enabled(harness):
        candidates.extend((d, ROLE_UNMANAGED_ACCOUNT) for d in discover_claude_config_dirs(home))
    return candidates


def doctor_config_dirs(
    home: Path | None = None,
    env: dict[str, str] | None = None,
    harness: dict[str, Any] | None = None,
) -> list[ConfigDirStatus]:
    """Inventory ``~/.claude``, ``$CLAUDE_CONFIG_DIR`` and every managed fan-out target.

    ``harness`` is the effective ``harness:`` block when the doctor could load one (it honors
    ``settings_paths`` + ``discover_config_dirs`` exactly as the plan does); ``None`` means
    the built-in defaults. Deduped by resolved dir, the default dir first.
    """
    env = os.environ if env is None else env
    primary = primary_config_dir(home)
    rows = [inspect_config_dir(primary, ROLE_DEFAULT)]
    seen = [primary]
    for config_dir, role in _doctor_candidates(harness, home, env):
        if any(_same_file(config_dir, s) for s in seen):
            continue
        seen.append(config_dir)
        rows.append(inspect_config_dir(config_dir, role))
    return rows


_GAP_FIX = {
    ROLE_ENV: "add {settings} to harness.settings_paths (rig apply commit reaches only its managed targets)",
    ROLE_UNMANAGED_ACCOUNT: "set harness.discover_config_dirs: true (or add {settings} to harness.settings_paths), then rig apply commit",
}
_GAP_FIX_MANAGED = "run `rig apply commit` in a rig-managed repo to fan the hooks out"
_GAP_FIX_MALFORMED = (
    "repair the JSON (or move the file aside), then run `rig apply commit` — apply merges into "
    "the file, it cannot rewrite one it cannot parse"
)


def _malformed_what(row: ConfigDirStatus) -> str:
    return f"{row.settings} is malformed JSON — a session there loads no hooks"


def config_dir_gaps(rows: list[ConfigDirStatus]) -> list[ConfigDirGap]:
    """Dirs a live ``claude`` could load that lack the rig hook bridge the default dir carries.

    Without a loaded config the default dir (``~/.claude``) is the reference for "does rig
    manage this machine": ONLY a ``cc_hook_bridge`` hook there proves it (a ``defaultMode``
    or an allowlist can be hand-set by anyone, so they never count as the rig signature). A
    malformed ``settings.json`` — the default dir's included — is always a gap: that session
    loads no hooks at all.
    """
    if not rows:
        return []
    ref = rows[0]
    gaps: list[ConfigDirGap] = []
    if ref.malformed:
        # the default dir is the reference for the bridge check, but a malformed file there is a
        # gap in its own right: a bare `claude` loads no hooks and `rig apply` cannot merge into it
        gaps.append(ConfigDirGap(ref, _malformed_what(ref), _GAP_FIX_MALFORMED))
    for row in rows[1:]:
        fix = _GAP_FIX.get(row.role, _GAP_FIX_MANAGED).format(settings=row.settings)
        if row.malformed:
            gaps.append(ConfigDirGap(row, _malformed_what(row), _GAP_FIX_MALFORMED))
        elif ref.bridge_hooks and not row.bridge_hooks:
            what = (
                f"{row.settings} has 0 rig hook-bridge hooks ({ref.settings} has {ref.bridge_hooks}) "
                "— a `claude` started from this config dir runs NO rig-provisioned guard"
            )
            gaps.append(ConfigDirGap(row, what, fix))
    return gaps
