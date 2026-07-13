"""Spotlight-exclude provisioning — PURE planning + the shared filesystem sweep.

What this is
------------
macOS Spotlight (``mds_stores``) reindexes everything under the user's home after a reboot,
including the gigabytes of dependency/build junk in every dev project (``node_modules``,
``dist``, ``target``, ``.venv`` …). The universal, per-directory opt-out on macOS is the
sentinel file ``.metadata_never_index``: an (empty) file with that exact name in a directory
tells Spotlight to skip that directory's contents — no global Search-Privacy list, no
``mdutil -i off`` (which would kill Spotlight wholesale). rig drops the sentinel into every
dependency/build dir under the configured dev roots, and provisions a launchd periodic job that
re-runs the sweep so NEW projects are covered without manual action.

How it is reached
-----------------
- ``plan._build_spotlight`` reads the ``spotlight:`` config block and emits ONE
  ``provision_spotlight`` action.
- ``runner._do_provision_spotlight`` runs :func:`perform_sweep` (drop sentinels) and writes +
  loads the launchd sweep agent (``RIG_SPOTLIGHT_DRY_RUN`` skips the live ``launchctl`` load).
- ``rig spotlight-sweep`` (the launchd job's command) calls the SAME :func:`perform_sweep`.
- ``verify._verify_spotlight`` samples matched dirs for the sentinel and checks the agent loaded.

Design notes
------------
- Unlike the tmux/schedule pure modules (which render config artifacts written elsewhere), the
  filesystem SWEEP is this feature's core operation and has THREE effectful consumers (apply, the
  ``spotlight-sweep`` subcommand, and re-sweeps). Keeping :func:`perform_sweep` here is the single
  source of truth for the walk + drop logic, so the three can never disagree. The plist rendering
  and config resolution ARE pure. Import-time is stdlib-only (the AGENTS.md hard rule).
- The walk PRUNES matched dirs (never descends into a ``node_modules`` it just sentinelled — that
  is the whole point, and descending would be slow), and caps depth so a pathological tree can't
  wedge the sweep.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The exact sentinel filename macOS Spotlight honors to skip a directory's contents.
SENTINEL_NAME = ".metadata_never_index"

# The dev roots swept by default. Configurable via ``spotlight.roots``; HOME-relative so a
# committed/global config stays portable.
DEFAULT_ROOTS: tuple[str, ...] = ("~/work", "~/xp")

# The dependency/build dir BASENAMES excluded by default — frontend AND backend. Config-driven:
# ``spotlight.deny`` REPLACES this set, ``spotlight.extra`` ADDS to it. Grouped by ecosystem for
# readability; membership is a flat basename match.
DEFAULT_DENY: tuple[str, ...] = (
    # frontend / node
    "node_modules", "dist", "build", ".next", "out", "coverage",
    ".turbo", ".nuxt", ".svelte-kit", ".parcel-cache", ".cache",
    # rust
    "target", ".cargo",
    # python
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # go / php
    "vendor",
    # jvm / xcode
    ".gradle", "DerivedData",
)

# The launchd label / plist filename stem (reverse-DNS, Apple convention). One identity for the
# plist Label + filename so install/verify/remove all key off it. Distinct from the tmux-boot and
# model-freshness labels so the three agents never collide.
DEFAULT_BOOT_LABEL = "ai.hyperide.spotlight-exclude"

# The daily re-sweep time (launchd StartCalendarInterval). Early morning, off-peak.
DEFAULT_HOUR = 4
DEFAULT_MINUTE = 30

# A safety cap on the walk depth (levels below each root). Dependency dirs sit at or near a
# project root; matched dirs are pruned, so the remaining traversal is source trees — the cap only
# guards a pathological deep tree from wedging the sweep.
DEFAULT_MAX_DEPTH = 8


@dataclass(frozen=True)
class SweepResult:
    """The outcome of one sweep. Pure data returned by :func:`perform_sweep`."""

    created: list[Path] = field(default_factory=list)  # sentinels newly written
    existing: list[Path] = field(default_factory=list)  # dirs that already had the sentinel
    roots_scanned: list[Path] = field(default_factory=list)  # roots that existed and were walked
    roots_missing: list[Path] = field(default_factory=list)  # configured roots absent on disk

    @property
    def matched(self) -> int:
        """Total dependency/build dirs matched (whether newly sentinelled or already covered)."""
        return len(self.created) + len(self.existing)

    def summary(self) -> str:
        n = len(self.roots_scanned)
        return (
            f"{len(self.created)} new, {len(self.existing)} already covered "
            f"across {n} {'root' if n == 1 else 'roots'}"
        )


@dataclass(frozen=True)
class SpotlightPlan:
    """The desired Spotlight-exclude state, resolved. Pure data; no I/O to construct."""

    roots: tuple[Path, ...]
    deny: frozenset[str]
    sweep_cmd: tuple[str, ...]
    label: str
    hour: int
    minute: int
    max_depth: int
    plist_path: Path | None = None  # launchd only (macOS)
    log_path: Path | None = None  # launchd StandardOut/ErrorPath

    @property
    def human_time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def plist_xml(self) -> str:
        """The launchd plist XML: RunAtLoad + a daily StartCalendarInterval re-sweep, with
        StandardOut/ErrorPath logging from the START (the tmux-boot plist lacked logging and that
        cost a debugging session — this one ships it)."""
        import plistlib

        log = str(self.log_path or (Path.home() / "Library" / "Logs" / f"{self.label}.log"))
        payload = {
            "Label": self.label,
            "ProgramArguments": list(self.sweep_cmd),
            "RunAtLoad": True,
            "StartCalendarInterval": {"Hour": self.hour, "Minute": self.minute},
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def resolve_roots(roots_cfg: object) -> tuple[Path, ...]:
    """Expand the configured dev roots to absolute paths (default ``~/work``, ``~/xp``)."""
    raw = roots_cfg if isinstance(roots_cfg, list) and roots_cfg else list(DEFAULT_ROOTS)
    out: list[Path] = []
    seen: set[Path] = set()
    for entry in raw:
        p = Path(os.path.expanduser(str(entry)))
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def resolve_deny(deny_cfg: object, extra_cfg: object) -> frozenset[str]:
    """Resolve the excluded dir basenames: ``deny`` REPLACES the default set, ``extra`` ADDS."""
    if isinstance(deny_cfg, list) and deny_cfg:
        base = {str(x) for x in deny_cfg}
    else:
        base = set(DEFAULT_DENY)
    if isinstance(extra_cfg, list):
        base |= {str(x) for x in extra_cfg}
    return frozenset(base)


def default_sweep_cmd() -> tuple[str, ...]:
    """The argv the launchd job runs to re-sweep: the current interpreter + ``-m riglib``.

    Mirrors :mod:`riglib.schedule` (which pins ``python3 <checker>``): rig is pip-installed
    editable, so ``<sys.executable> -m riglib spotlight-sweep`` re-enters the same CLI the launchd
    job needs, independent of PATH/console-script resolution at boot time.
    """
    return (sys.executable, "-m", "riglib", "spotlight-sweep")


def build_spotlight(
    *,
    roots: tuple[Path, ...],
    deny: frozenset[str],
    sweep_cmd: tuple[str, ...],
    label: str = DEFAULT_BOOT_LABEL,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> SpotlightPlan:
    """Resolve the desired :class:`SpotlightPlan` for this machine (launchd on macOS)."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_path = Path.home() / "Library" / "Logs" / f"{label}.log"
    return SpotlightPlan(
        roots=roots,
        deny=deny,
        sweep_cmd=sweep_cmd,
        label=label,
        hour=hour,
        minute=minute,
        max_depth=max_depth,
        plist_path=plist_path,
        log_path=log_path,
    )


def sweep_args_from_options(options: dict) -> tuple[tuple[Path, ...], frozenset[str], int]:
    """Extract (roots, deny, max_depth) from an action's ``options`` bag.

    ONE decoder for the ``provision_spotlight`` action shape (``roots``: list[str], ``deny``:
    sorted list[str], ``max_depth``: int), shared by the runner (apply) and the verifier so the
    two can never drift on how they read the same action.
    """
    roots = tuple(Path(p) for p in options.get("roots", ()))
    deny = frozenset(options.get("deny", ()))
    max_depth = int(options.get("max_depth", DEFAULT_MAX_DEPTH))
    return roots, deny, max_depth


def sentinel_path(directory: Path) -> Path:
    """The sentinel file path for a directory."""
    return directory / SENTINEL_NAME


def has_sentinel(directory: Path) -> bool:
    """True when the directory already carries the Spotlight-skip sentinel file."""
    return sentinel_path(directory).is_file()


def iter_target_dirs(
    roots: tuple[Path, ...], deny: frozenset[str], max_depth: int = DEFAULT_MAX_DEPTH
) -> "list[Path]":
    """Return every dependency/build dir (basename in ``deny``) under the roots, read-only.

    PRUNES matched dirs (does not descend into a matched ``node_modules``) and caps traversal at
    ``max_depth`` levels below each root. Shared by the sweep, verify sampling, and dry-run
    reporting so all three see the SAME set. Never writes.
    """
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        root_depth = len(root.parts)
        for dirpath, dirnames, _files in os.walk(root):
            here = Path(dirpath)
            if len(here.parts) - root_depth >= max_depth:
                dirnames[:] = []
                continue
            matched = [d for d in dirnames if d in deny]
            for name in matched:
                found.append(here / name)
            # prune matched dirs (+ symlinked dirs, which os.walk would follow into cycles only
            # with followlinks=True — off by default — but pruning keeps the walk on real trees).
            dirnames[:] = [d for d in dirnames if d not in deny]
    return found


def perform_sweep(
    roots: tuple[Path, ...], deny: frozenset[str], max_depth: int = DEFAULT_MAX_DEPTH
) -> SweepResult:
    """Drop the ``.metadata_never_index`` sentinel into every matched dir. Idempotent.

    Effectful (writes empty sentinel files). Returns a :class:`SweepResult` with per-dir outcomes.
    A dir that already has the sentinel is left untouched and counted as ``existing``.
    """
    created: list[Path] = []
    existing: list[Path] = []
    scanned: list[Path] = []
    missing: list[Path] = []
    for root in roots:
        (scanned if root.is_dir() else missing).append(root)
    for directory in iter_target_dirs(roots, deny, max_depth):
        sentinel = sentinel_path(directory)
        if sentinel.is_file():
            existing.append(sentinel)
            continue
        try:
            sentinel.touch()
            created.append(sentinel)
        except OSError:
            # A permission/IO error on one dir must not abort the whole sweep; skip it.
            continue
    return SweepResult(created=created, existing=existing, roots_scanned=scanned, roots_missing=missing)
