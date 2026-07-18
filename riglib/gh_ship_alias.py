"""Provision the machine-global ``gh ship`` alias so ``gh ship <PR>`` reaches the ship gate.

``gh ship`` is a ``gh`` alias (stored in gh's config, ``aliases.ship``) whose expansion is a POSIX
``sh`` dispatcher: it runs the per-repo delegator ``<repo>/.claude/scripts/pr-ship.sh`` (rig
provisions that too — see :mod:`riglib` ship_delegator) and, outside a managed repo, falls back to
the canonical ``ci/ship/ship.sh`` resolved via the machine-level env file
(``$XDG_CONFIG_HOME/agent-tools/env`` — ``AGENT_TOOLS_ROOT``). Historically this alias was
HAND-SET, so on a clean machine (or after a gh-config reset) ``gh ship`` silently became "unknown
command". rig now provisions it, idempotently, so it is reproducible and drift-surfaced.

The expansion is a PORTABLE CONSTANT — no machine-specific absolute path is ever baked in (the
delegator and the env file carry the machine specifics) — so a re-apply byte-compares equal and
``rig status`` is a no-op when in sync. This mirrors the delegator's own portability guarantee
(see :func:`riglib.actions.runner.ship_delegator_content`).

Ownership split (mirrors the shared ``resolve_*`` predicates elsewhere): this module owns the
DESIRED alias body and the READ/RESOLVE side (shared by apply and drift so they can never disagree
on whether the alias is in sync); the WRITE (a live ``gh alias set``) lives in the runner behind a
dry-run guard, like every other live mutation rig performs.

Stdlib-only at import (``subprocess`` / ``shutil`` / ``os`` / ``dataclasses`` are stdlib); no
third-party imports here or at call time — gh's config is read by shelling out to ``gh``, never by
parsing YAML ourselves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The alias name and the drift/area category. One string, referenced by the plan builder, the
# runner, the drift check, and the area/layer registries so they can never disagree.
GH_SHIP_ALIAS_NAME = "ship"
GH_SHIP_ALIAS_CATEGORY = "gh_ship_alias"


def gh_ship_alias_expansion() -> str:
    """The exact gh-alias expansion for ``gh ship`` — a pure, portable CONSTANT.

    A ``gh`` shell alias (leading ``!``): gh runs it through ``sh`` with the extra ``gh ship``
    arguments available as ``"$@"``. Resolution order mirrors the delegator:

    1. The per-repo delegator ``<repo>/.claude/scripts/pr-ship.sh`` (rig provisions it into every
       managed repo; it in turn execs the canonical ship.sh, repo-local or via the env file).
    2. Outside a managed repo (or a non-git cwd): the canonical ``ci/ship/ship.sh`` resolved from
       ``AGENT_TOOLS_ROOT`` — from the environment, else sourced from the machine env file rig
       apply writes.
    3. Neither resolvable → a diagnostic on stderr + exit 127 (never a silent success).

    POSIX ``sh`` only (no bashisms): gh evaluates the expansion with ``sh``. No machine-specific
    path is baked in, so the bytes are identical on every machine and a re-apply is a byte-equal
    no-op.
    """
    return (
        "!"
        'd="$(git rev-parse --show-toplevel 2>/dev/null || true)"; '
        's="${d:+$d/.claude/scripts/pr-ship.sh}"; '
        'if [ -n "$s" ] && [ -x "$s" ]; then exec "$s" "$@"; fi; '
        # Fallback resolution mirrors the delegator's SAFE env-file contract
        # (ship_delegator_content): an explicit $AGENT_TOOLS_ROOT ALWAYS wins (source is skipped so
        # pointing a shell at another checkout needs no re-apply), and a SYMLINKED env file is
        # refused (rig itself refuses to manage a symlink there — the runtime must draw the same
        # line, never source/execute a symlink target rig won't touch). Export once resolved so a
        # spawned ship.sh inherits the same checkout.
        'r="${AGENT_TOOLS_ROOT:-}"; '
        'if [ -z "$r" ]; then '
        'f="${XDG_CONFIG_HOME:-$HOME/.config}/agent-tools/env"; '
        'if [ -f "$f" ] && [ ! -L "$f" ]; then . "$f"; r="${AGENT_TOOLS_ROOT:-}"; fi; '
        "fi; "
        'if [ -n "$r" ] && [ -x "$r/ci/ship/ship.sh" ]; then '
        'export AGENT_TOOLS_ROOT; exec "$r/ci/ship/ship.sh" "$@"; fi; '
        'echo "gh ship: no ship delegator in this repo and AGENT_TOOLS_ROOT unresolved; '
        "run rig apply.\" >&2; "
        "exit 127"
    )


def gh_config_path() -> Path:
    """Where gh stores its aliases — ``$GH_CONFIG_DIR`` / ``$XDG_CONFIG_HOME/gh`` / ``~/.config/gh``.

    Mirrors gh's own resolution on non-Windows hosts (``GH_CONFIG_DIR`` wins, then
    ``XDG_CONFIG_HOME``, then ``~/.config``). This file is the source of truth we READ (the WRITE
    goes through ``gh alias set``, which preserves every other key); it is also the drift row's
    display ``target``.
    """
    gh_dir = os.environ.get("GH_CONFIG_DIR")
    if gh_dir:
        return Path(gh_dir) / "config.yml"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gh" / "config.yml"


def gh_available() -> bool:
    """True when the ``gh`` CLI is on PATH — the alias can only be managed through it.

    When gh is absent, apply and drift both self-skip (status/apply parity): rig reports nothing to
    do rather than flagging a "missing" alias it could never set.
    """
    return shutil.which("gh") is not None


# Sentinel: gh's config file is PRESENT but cannot be read/parsed (undecodable bytes, no read
# permission, malformed YAML). Distinct from ``None`` (no config / no ``ship`` alias → safe to
# CREATE): an unreadable config must NOT be treated as "absent" and clobbered — rig leaves it be.
_UNREADABLE = object()


@dataclass(frozen=True)
class GhAliasResolution:
    """The shared classification apply and drift both switch on.

    - ``ok``      → the ``ship`` alias already equals the desired expansion (no-op).
    - ``create``  → gh has no ``ship`` alias (apply sets it).
    - ``update``  → a ``ship`` alias exists but differs (apply overwrites it, noting the old value).
    - ``no_gh``   → the ``gh`` CLI is absent; the alias cannot be managed (both sides self-skip).
    - ``unknown`` → gh's config is present but unreadable/malformed; rig refuses to clobber it, so
      apply skips and drift reports nothing (status/apply parity, like ``no_gh``).
    """

    state: str
    current: str | None
    desired: str


def _read_ship_alias() -> object:
    """Read the current ``ship`` alias from gh's config: the expansion, ``None``, or ``_UNREADABLE``.

    Reads gh's config FILE and YAML-parses ``aliases.ship`` — NOT ``gh alias list``, whose output
    is a HUMAN display format (it wraps a shell alias in single quotes and DOUBLES any internal
    single quote). Parsing that display would make a just-written alias compare unequal to the
    desired body, so ``resolve`` would report ``update`` forever and apply would rewrite on every
    run (never idempotent). The YAML scalar round-trips byte-exactly, so an in-sync alias resolves
    to ``ok`` and a re-apply is a true no-op. YAML is imported lazily (rig is stdlib-only at import).

    Returns the ``ship`` expansion (``str``) when set; ``None`` when gh has no config or no ``ship``
    alias (safe to CREATE); ``_UNREADABLE`` when the config is present but cannot be read/parsed
    (undecodable bytes / no read permission / malformed YAML) — so the caller skips rather than
    treating a broken config as "absent" and clobbering it (which would also traceback ``rig
    status`` on a ``UnicodeDecodeError``). A seam the dedicated tests monkeypatch.
    """
    path = gh_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return _UNREADABLE
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a declared dep, present in every real run
        return _UNREADABLE
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return _UNREADABLE
    if not isinstance(data, dict):
        return None
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        return None
    value = aliases.get(GH_SHIP_ALIAS_NAME)
    return value if isinstance(value, str) else None


def resolve_gh_ship_alias() -> GhAliasResolution:
    """Classify the current ``gh ship`` alias against the desired expansion (apply + drift share).

    gh-missing short-circuits to ``no_gh`` BEFORE any read, so a machine without gh is a clean
    no-op on both sides rather than a phantom "missing alias" drift rig could never repair. A
    present-but-unreadable config resolves to ``unknown`` (rig leaves it be, never clobbers).
    """
    desired = gh_ship_alias_expansion()
    if not gh_available():
        return GhAliasResolution("no_gh", None, desired)
    current = _read_ship_alias()
    if current is _UNREADABLE:
        return GhAliasResolution("unknown", None, desired)
    if current is None:
        return GhAliasResolution("create", None, desired)
    if current == desired:
        return GhAliasResolution("ok", str(current), desired)
    return GhAliasResolution("update", str(current), desired)


def set_gh_ship_alias() -> int:
    """Write the ``ship`` alias to the desired expansion via ``gh alias set --clobber``.

    ``--clobber`` overwrites any existing ``ship`` alias in place; gh preserves every OTHER key in
    its config, so this never disturbs unrelated aliases/settings. Returns the gh exit code (0 on
    success), or ``1`` when gh is absent / errors — the caller turns a non-zero into a noted, soft
    failure, never a hard apply error (gh not being installed is not a rig failure). The live-write
    seam; the runner guards it behind ``RIG_GH_ALIAS_DRY_RUN``.
    """
    if not gh_available():
        return 1
    try:
        res = subprocess.run(
            ["gh", "alias", "set", "--clobber", GH_SHIP_ALIAS_NAME, gh_ship_alias_expansion()],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode
