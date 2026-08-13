"""Pure/source-safe helpers for reusable linter config files and directory bundles."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import linter_path_escapes_repo


@dataclass(frozen=True)
class BundleResolution:
    source: Path
    target: Path
    state: str  # ok | create | update | io_error
    detail: str = ""


@dataclass(frozen=True)
class BundleApplyResult:
    status: str
    detail: str
    backup: Path | None = None


def _safe_relative(raw: str) -> bool:
    return bool(raw and raw == raw.strip() and not linter_path_escapes_repo(raw))


def _unsafe_component(root: Path, rel: str, *, allow_missing_leaf: bool) -> str | None:
    cur = root
    parts = Path(rel).parts
    for i, part in enumerate(parts):
        cur = cur / part
        if cur.is_symlink():
            return f"{cur} is a symlink"
        if i < len(parts) - 1 and cur.exists() and not cur.is_dir():
            return f"{cur} is not a directory"
        if i == len(parts) - 1 and not allow_missing_leaf and not cur.exists():
            return f"{cur} does not exist"
    return None


def read_source_text(agent_tools_root: Path, rel: str) -> str:
    """Read one UTF-8 preset file from agent_tools_source, refusing path/symlink escapes."""
    if not _safe_relative(rel):
        raise ValueError(f"unsafe linter source path {rel!r}")
    unsafe = _unsafe_component(agent_tools_root, rel, allow_missing_leaf=False)
    if unsafe:
        raise ValueError(unsafe)
    path = agent_tools_root / rel
    if not path.is_file():
        raise ValueError(f"linter source is not a regular file: {path}")
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read linter source {path}: {exc}") from exc


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"bundle source/target is not a real directory: {root}")
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"bundle contains symlink: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"bundle contains non-regular entry: {rel}")
        out[rel] = path.read_bytes()
    return out


def resolve_bundle(agent_tools_root: Path, repo_root: Path, source_rel: str, target_rel: str) -> BundleResolution:
    source = agent_tools_root / source_rel
    target = repo_root / target_rel
    if not _safe_relative(source_rel):
        return BundleResolution(source, target, "io_error", f"unsafe source path {source_rel!r}")
    if not _safe_relative(target_rel):
        return BundleResolution(source, target, "io_error", f"unsafe target path {target_rel!r}")
    source_unsafe = _unsafe_component(agent_tools_root, source_rel, allow_missing_leaf=False)
    if source_unsafe:
        return BundleResolution(source, target, "io_error", source_unsafe)
    target_unsafe = _unsafe_component(repo_root, target_rel, allow_missing_leaf=True)
    if target_unsafe:
        return BundleResolution(source, target, "io_error", target_unsafe)
    try:
        desired = _tree_snapshot(source)
    except (OSError, ValueError) as exc:
        return BundleResolution(source, target, "io_error", str(exc))
    if not desired:
        return BundleResolution(source, target, "io_error", f"bundle source is empty: {source}")
    if not target.exists():
        return BundleResolution(source, target, "create")
    if target.is_symlink() or not target.is_dir():
        return BundleResolution(source, target, "io_error", f"bundle target is not a real directory: {target}")
    try:
        actual = _tree_snapshot(target)
    except (OSError, ValueError) as exc:
        return BundleResolution(source, target, "io_error", str(exc))
    return BundleResolution(source, target, "ok" if actual == desired else "update")


def _backup_path(target: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f"{target.name}.rig-bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.rig-bak-{stamp}.{n}")
        n += 1
    return candidate


def apply_bundle(agent_tools_root: Path, repo_root: Path, source_rel: str, target_rel: str, on_conflict: str) -> BundleApplyResult:
    r = resolve_bundle(agent_tools_root, repo_root, source_rel, target_rel)
    if r.state == "io_error":
        return BundleApplyResult("error", r.detail)
    if r.state == "ok":
        return BundleApplyResult("skipped", f"bundle already correct: {r.target}")
    if r.state == "update" and on_conflict == "skip":
        return BundleApplyResult("skipped", f"bundle differs, on_conflict=skip: {r.target}")
    backup: Path | None = None
    if r.target.exists():
        if on_conflict == "backup":
            backup = _backup_path(r.target)
            shutil.move(str(r.target), str(backup))
        else:
            shutil.rmtree(r.target)
    r.target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(r.source, r.target, symlinks=False)
    status = "created" if r.state == "create" else ("backed_up" if backup else "updated")
    detail = f"copied linter bundle -> {r.target}"
    if backup:
        detail += f" (backed up prior -> {backup})"
    return BundleApplyResult(status, detail, backup)
