"""Historical file snapshots for the `rig evolve` portal.

Accessed via: future histogram-click APIs. The implementation reads git objects directly with
`ls-tree`/`cat-file` so selecting an older bucket never mutates the user's working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import structure
from .git_index import resolve_bucket_end


def build_historical_snapshot(
    repo_root: Path,
    requested_bucket: str,
    *,
    bucket: str = "month",
    selected_path: str | None = None,
) -> dict[str, Any]:
    """Return a file-level project snapshot at the end of a histogram bucket."""
    resolution = resolve_bucket_end(repo_root, requested_bucket, bucket=bucket)
    selected = _normalize_path(selected_path)
    commit = resolution.get("commit")
    if isinstance(commit, str) and commit:
        tree, tree_health = _build_file_tree_at_commit(repo_root, commit)
        exists = _path_exists_at(repo_root, commit, selected) if selected else None
    else:
        tree = _empty_tree(repo_root)
        tree_health = {"status": str(resolution["status"]), "message": str(resolution["message"])}
        exists = False if selected else None

    return {
        "project": {"path": str(repo_root), "name": repo_root.name},
        "requested": {
            "bucket": bucket,
            "bucket_id": requested_bucket,
            "selected_path": selected or None,
        },
        "resolution": resolution,
        "tree": tree,
        "selection": _selection_metadata(repo_root, selected, exists),
        "health": {"git": tree_health},
    }


def _build_file_tree_at_commit(repo_root: Path, commit: str) -> tuple[dict[str, Any], dict[str, str]]:
    raw = _git_ls_tree(repo_root, commit)
    if raw is None:
        return _empty_tree(repo_root), {"status": "error", "message": "git ls-tree failed"}

    root = _empty_tree(repo_root)
    for rel, size in _iter_tree_files(raw):
        path = Path(rel)
        if structure._skip_rel(path):
            continue
        _insert(root, path.parts, rel, max(1, size))
    _rollup(root)
    _sort(root)
    return root, {"status": "ok", "message": "historical tree available"}


def _git_ls_tree(repo_root: Path, commit: str) -> bytes | None:
    try:
        res = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "-l", commit],
            cwd=str(repo_root),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _iter_tree_files(raw: bytes) -> list[tuple[str, int]]:
    files: list[tuple[str, int]] = []
    for record in raw.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        parts = meta.split()
        if len(parts) < 4 or parts[1] != b"blob":
            continue
        size = int(parts[3]) if parts[3].isdigit() else 1
        rel = raw_path.decode("utf-8", "surrogateescape")
        files.append((rel, size))
    return files


def _selection_metadata(repo_root: Path, selected_path: str | None, exists: bool | None) -> dict[str, Any]:
    if not selected_path:
        return {"path": None, "exists": None, "missing": None, "current": None}
    return {
        "path": selected_path,
        "exists": bool(exists),
        "missing": not bool(exists),
        "current": _path_exists_at(repo_root, "HEAD", selected_path),
    }


def _path_exists_at(repo_root: Path, commit: str, selected_path: str | None) -> bool:
    if not selected_path:
        return False
    try:
        res = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{selected_path}"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _normalize_path(selected_path: str | None) -> str | None:
    if not selected_path:
        return None
    path = selected_path.replace("\\", "/").lstrip("/")
    return path or None


def _empty_tree(repo_root: Path) -> dict[str, Any]:
    return {
        "id": str(repo_root),
        "name": repo_root.name,
        "path": "",
        "kind": "repo",
        "size": 0,
        "children": [],
    }


def _insert(node: dict[str, Any], parts: tuple[str, ...], rel: str, size: int) -> None:
    if len(parts) == 1:
        node["children"].append(
            {"id": rel, "name": parts[0], "path": rel, "kind": "file", "size": size, "children": []}
        )
        return
    head = parts[0]
    child = next((c for c in node["children"] if c["kind"] == "group" and c["name"] == head), None)
    if child is None:
        prefix = "/".join(Path(rel).parts[: len(Path(rel).parts) - len(parts) + 1])
        child = {"id": prefix, "name": head, "path": prefix, "kind": "group", "size": 0, "children": []}
        node["children"].append(child)
    _insert(child, parts[1:], rel, size)


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
