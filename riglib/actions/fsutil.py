"""Filesystem helpers shared by the install actions — stdlib only.

Centralizes the idempotency + conflict-policy + backup logic so each action runner stays
small and every action honors ``on_conflict`` identically.
"""

from __future__ import annotations

import filecmp
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WriteOutcome:
    status: str  # "created" | "updated" | "skipped" | "backed_up"
    detail: str
    backup: Path | None = None


def backup_path(target: Path, *, backup_dir: Path | None = None) -> Path:
    """A unique backup path for ``target``. Adds a numeric suffix if a same-second backup
    already exists, so multiple backups of one file within a run never collide.

    By default the backup is a SIBLING of ``target`` (``<target>.rig-bak-<stamp>``). Pass
    ``backup_dir`` to relocate the backup OUT of ``target``'s parent — required when that
    parent is a natively-scanned skills dir (``~/.agents/skills``), where a sibling
    ``<name>.rig-bak-*/`` backup still carries a ``SKILL.md`` and gets re-discovered by
    opencode as a duplicate skill (rig-cli#57). The backup keeps ``target``'s basename so the
    restore point is still identifiable.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parent = backup_dir if backup_dir is not None else target.parent
    bak = parent / f"{target.name}.rig-bak-{stamp}"
    n = 1
    while bak.exists():
        bak = parent / f"{target.name}.rig-bak-{stamp}.{n}"
        n += 1
    return bak


# backwards-compatible private alias (used internally before this was made public).
_backup_path = backup_path


def dirs_identical(a: Path, b: Path) -> bool:
    """Shallow-ish recursive compare: same file set, same contents."""
    if not a.is_dir() or not b.is_dir():
        return False
    cmp = filecmp.dircmp(str(a), str(b))
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    for sub in cmp.common_dirs:
        if not dirs_identical(a / sub, b / sub):
            return False
    return True


def copy_tree(
    source: Path, target: Path, on_conflict: str, *, backup_dir: Path | None = None
) -> WriteOutcome:
    """Copy a directory tree to ``target`` honoring ``on_conflict``. Idempotent.

    ``backup_dir`` (on_conflict=backup only) relocates the backup of a replaced ``target`` OUT
    of ``target``'s parent — used for skills so a conflict-backup of a natively-scanned skill
    dir does not itself get re-discovered as a duplicate skill (rig-cli#57).
    """
    if target.exists():
        if target.is_dir() and dirs_identical(source, target):
            return WriteOutcome("skipped", f"identical, left as-is: {target}")
        if on_conflict == "skip":
            return WriteOutcome("skipped", f"exists, on_conflict=skip: {target}")
        if on_conflict == "backup":
            bak = _backup_path(target, backup_dir=backup_dir)
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(bak))
            shutil.copytree(str(source), str(target))
            return WriteOutcome("backed_up", f"backed up prior → {bak}", backup=bak)
        # overwrite — the stale target may be a FILE where a dir is expected; remove it
        # with the right primitive (rmtree errors on a non-directory).
        if target.is_dir():
            shutil.rmtree(str(target))
        else:
            target.unlink()
        shutil.copytree(str(source), str(target))
        return WriteOutcome("updated", f"overwrote: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(source), str(target))
    return WriteOutcome("created", f"copied → {target}")


def _remove_any(p: Path) -> None:
    """Remove a path whether it is a file/symlink or a directory (right primitive each)."""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(str(p))
    else:
        p.unlink()


def write_file(target: Path, content: str, on_conflict: str) -> WriteOutcome:
    """Write text to ``target`` honoring ``on_conflict``. Idempotent on identical bytes.

    Handles a stale DIRECTORY sitting where a file is expected (and vice versa): the
    conflict primitives below remove whatever is there with the correct call, so the
    documented conflict policy holds for file-vs-directory collisions too.
    """
    if target.exists():
        existing = target.read_text(encoding="utf-8") if target.is_file() else None
        if existing == content:
            return WriteOutcome("skipped", f"identical: {target}")
        if on_conflict == "skip":
            return WriteOutcome("skipped", f"exists, on_conflict=skip: {target}")
        if on_conflict == "backup":
            bak = _backup_path(target)
            # move (not copy2) so a directory-at-target is preserved intact and removed
            shutil.move(str(target), str(bak))
            target.write_text(content, encoding="utf-8")
            return WriteOutcome("backed_up", f"backed up prior → {bak}", backup=bak)
        # overwrite — remove whatever is there (file or dir) before writing
        _remove_any(target)
        target.write_text(content, encoding="utf-8")
        return WriteOutcome("updated", f"overwrote: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return WriteOutcome("created", f"wrote → {target}")


def copy_file(source: Path, target: Path, on_conflict: str) -> WriteOutcome:
    return write_file(target, source.read_text(encoding="utf-8"), on_conflict)
