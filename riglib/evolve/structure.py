"""Current-file treemap model for the `rig evolve` portal.

Accessed via: the snapshot API. The first slice is file-level so the portal is useful before
language-specific parsers are wired; later symbol nodes can attach under file nodes without
changing the outer JSON shape.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

_SKIP_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".next",
    ".svn",
    ".venv",
    ".yarn",
    "__pycache__",
    "agent-scratch",
    "build",
    "cloned-projects",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_SKIP_FILENAMES = {
    "bun.lockb",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
_SKIP_SUFFIXES = {
    ".7z",
    ".avif",
    ".bmp",
    ".br",
    ".db",
    ".gif",
    ".gz",
    ".heic",
    ".ico",
    ".jpeg",
    ".jpg",
    ".lock",
    ".map",
    ".mov",
    ".mp4",
    ".otf",
    ".pdf",
    ".png",
    ".sqlite",
    ".tar",
    ".tgz",
    ".ttf",
    ".webm",
    ".woff",
    ".woff2",
    ".zip",
}
_SYMBOL_SUFFIXES = {
    ".cjs",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".mts",
    ".py",
    ".ts",
    ".tsx",
}


def build_file_tree(repo_root: Path, *, include_symbols: bool = False) -> dict[str, Any]:
    """Return a proportional hierarchy for tracked files in ``repo_root``."""
    root = {
        "id": str(repo_root),
        "name": repo_root.name,
        "path": "",
        "kind": "repo",
        "size": 0,
        "children": [],
    }
    for rel in _files(repo_root):
        path = repo_root / rel
        try:
            size = max(1, path.stat().st_size)
        except OSError:
            continue
        symbols = _symbols_for_file(repo_root, rel) if include_symbols else []
        _insert(root, rel.parts, rel.as_posix(), size, symbols)
    _rollup(root)
    _sort(root)
    return root


def _files(repo_root: Path) -> list[Path]:
    try:
        res = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        res = None
    if res is not None and res.returncode == 0 and res.stdout:
        return [
            rel
            for p in res.stdout.split(b"\0")
            if p and not _skip_rel(rel := Path(p.decode("utf-8", "surrogateescape")))
        ]

    out: list[Path] = []
    for base, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            full = Path(base) / name
            try:
                rel = full.relative_to(repo_root)
                if not _skip_rel(rel):
                    out.append(rel)
            except ValueError:
                continue
    return out


def _skip_rel(rel: Path) -> bool:
    parts = rel.parts
    if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in parts[:-1]):
        return True
    name = rel.name
    if name in _SKIP_FILENAMES:
        return True
    return rel.suffix.lower() in _SKIP_SUFFIXES


def _insert(
    node: dict[str, Any],
    parts: tuple[str, ...],
    rel: str,
    size: int,
    symbols: list[dict[str, Any]],
) -> None:
    if len(parts) == 1:
        node["children"].append(
            {"id": rel, "name": parts[0], "path": rel, "kind": "file", "size": size, "children": symbols}
        )
        return
    head = parts[0]
    child = next((c for c in node["children"] if c["kind"] == "group" and c["name"] == head), None)
    if child is None:
        prefix = "/".join(Path(rel).parts[: len(Path(rel).parts) - len(parts) + 1])
        child = {"id": prefix, "name": head, "path": prefix, "kind": "group", "size": 0, "children": []}
        node["children"].append(child)
    _insert(child, parts[1:], rel, size, symbols)


def _symbols_for_file(repo_root: Path, rel: Path) -> list[dict[str, Any]]:
    if rel.suffix.lower() not in _SYMBOL_SUFFIXES:
        return []
    path = repo_root / rel
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    from .symbols import extract_symbols

    return extract_symbols(path, source, repo_root=repo_root)


def _rollup(node: dict[str, Any]) -> int:
    if node["kind"] == "file":
        return int(node["size"])
    total = sum(_rollup(child) for child in node["children"])
    node["size"] = total
    return total


def _sort(node: dict[str, Any]) -> None:
    if node["kind"] in {"repo", "group"}:
        node["children"].sort(key=lambda c: (0 if c["kind"] == "group" else 1, c["name"]))
    for child in node["children"]:
        _sort(child)
