"""Project discovery for the rig evolve portal."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

SVERKLO_TIMEOUT_SECONDS = 5


def discover_projects(current_root: Path) -> list[dict[str, Any]]:
    """Discover projects for the evolve portal, failing soft for optional sources."""
    projects: dict[Path, dict[str, Any]] = {}
    remote_cache: dict[Path, str] = {}
    current_path = _resolved(current_root)
    current_common_dir = _git_common_dir(current_path)
    _add_project(projects, current_path, "current")

    sverklo_status, sverklo_entries, sverklo_message = _sverklo_projects()
    if sverklo_status == "ok":
        resolved_entries: list[tuple[Path, str, Path | None]] = []
        for entry in sverklo_entries:
            path = entry["path"]
            name = entry.get("name") or path.name
            if not path.exists():
                _attach_missing_alias(projects, current_path, name, path)
                continue
            worktree_common_dir = _linked_worktree_common_dir(path)
            resolved_entries.append((path, name, worktree_common_dir))
        for path, name, worktree_common_dir in resolved_entries:
            if worktree_common_dir is not None:
                continue
            target = _merge_target(projects, path, remote_cache)
            _add_project(projects, target or path, "sverklo", alias=name)
        for path, _name, worktree_common_dir in resolved_entries:
            if worktree_common_dir is None:
                continue
            if (
                current_common_dir is not None
                and worktree_common_dir == current_common_dir
                and _resolved(path) != current_path
            ):
                projects[current_path]["notes"].append("sverklo linked worktree skipped")
                continue
            target = _merge_target(projects, path, remote_cache)
            if target is not None:
                projects[target]["notes"].append("sverklo linked worktree skipped")
                continue
            _add_project(projects, path, "worktree")
    else:
        current = projects[current_path]
        current["health"]["sverklo"] = {
            "status": sverklo_status,
            "message": sverklo_message,
        }
        current["notes"].append(f"sverklo list failed: {sverklo_message}")

    return list(projects.values())


def _add_project(projects: dict[Path, dict[str, Any]], path: Path, source: str, *, alias: str | None = None) -> None:
    resolved = _resolved(path)
    project = projects.get(resolved)
    if project is None:
        project = {
            "name": resolved.name or str(resolved),
            "path": str(resolved),
            "aliases": [],
            "sources": [],
            "health": {},
            "notes": [],
        }
        projects[resolved] = project
    if alias and alias != project["name"] and alias not in project["aliases"]:
        project["aliases"].append(alias)
    if source not in project["sources"]:
        project["sources"].append(source)
    project["health"][source] = {"status": "ok"}


def _sverklo_projects() -> tuple[str, list[dict[str, Any]], str]:
    try:
        completed = subprocess.run(
            ["sverklo", "list"],
            capture_output=True,
            check=True,
            text=True,
            timeout=SVERKLO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", [], _format_sverklo_error(exc)
    return "ok", _parse_sverklo_entries(completed.stdout), ""


def _parse_sverklo_output(output: str) -> list[Path]:
    return [entry["path"] for entry in _parse_sverklo_entries(output)]


def _parse_sverklo_entries(output: str) -> list[dict[str, Any]]:
    json_entries = _parse_sverklo_json(output)
    if json_entries is not None:
        return _dedupe_entries(json_entries)

    entries: list[dict[str, Any]] = []
    pending_name: str | None = None
    for line in output.splitlines():
        path = _path_from_text_line(line)
        if path is not None:
            entries.append({"name": pending_name or path.name, "path": path})
            pending_name = None
            continue
        name = _name_from_text_line(line)
        if name:
            pending_name = name
    seen: set[Path] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        resolved = _resolved(entry["path"])
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append({"name": entry.get("name") or resolved.name, "path": resolved})
    return deduped


def _parse_sverklo_json(output: str) -> list[dict[str, Any]] | None:
    stripped = output.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return list(_entries_from_json_value(data))


def _entries_from_json_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        path = Path(value).expanduser()
        return [{"name": path.name, "path": path}]
    if isinstance(value, list):
        entries: list[dict[str, Any]] = []
        for item in value:
            entries.extend(_entries_from_json_value(item))
        return entries
    if isinstance(value, dict):
        for key in ("path", "root", "repo", "repository", "workspace"):
            item = value.get(key)
            if isinstance(item, str):
                path = Path(item).expanduser()
                return [{"name": _name_from_json_entry(value, path), "path": path}]
        for key in ("projects", "repositories", "repos"):
            item = value.get(key)
            if isinstance(item, list):
                return _entries_from_json_value(item)
    return []


def _name_from_json_entry(value: dict[str, Any], path: Path) -> str:
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return path.name


def _path_from_text_line(line: str) -> Path | None:
    text = line.strip()
    if not text:
        return None
    if text.lower().startswith("registry:"):
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        candidate = token.strip(",;")
        if candidate.startswith(("~", "/")):
            return Path(candidate).expanduser()
    return None


def _name_from_text_line(line: str) -> str | None:
    text = line.strip()
    if not text or text.lower().startswith(("registry:", "registered repositories", "last indexed:", "path ")):
        return None
    if text.endswith(":"):
        return None
    if text.startswith(("~", "/")):
        return None
    return text


def _merge_target(projects: dict[Path, dict[str, Any]], path: Path, remote_cache: dict[Path, str]) -> Path | None:
    resolved = _resolved(path)
    if resolved in projects:
        return resolved
    remote = _git_remote_cached(remote_cache, resolved)
    if not remote:
        return None
    for existing in projects:
        if _git_remote_cached(remote_cache, existing) == remote:
            return existing
    return None


def _git_remote_cached(cache: dict[Path, str], path: Path) -> str:
    resolved = _resolved(path)
    if resolved not in cache:
        cache[resolved] = _git_remote(resolved)
    return cache[resolved]


def _attach_missing_alias(projects: dict[Path, dict[str, Any]], current_path: Path, name: str, path: Path) -> None:
    current = projects[current_path]
    if path.parent == current_path.parent and _probably_old_name(name, current["name"]):
        if name and name not in current["aliases"]:
            current["aliases"].append(name)
        current["notes"].append(f"sverklo registry has stale alias {name} at missing path {path}")
    else:
        current["notes"].append(f"sverklo registry entry skipped because path is missing: {path}")


def _probably_old_name(alias: str, canonical: str) -> bool:
    if not alias or not canonical:
        return False
    prefix = ""
    for a, b in zip(alias.lower(), canonical.lower()):
        if a != b:
            break
        prefix += a
    return len(prefix) >= 5


def _git_remote(path: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if res.returncode != 0:
        return ""
    return _normalize_remote(res.stdout.strip())


def _linked_worktree_common_dir(path: Path) -> Path | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    git_dir = Path(lines[0])
    common_dir = Path(lines[1])
    return common_dir if git_dir != common_dir else None


def _git_common_dir(path: Path) -> Path | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    value = res.stdout.strip()
    return Path(value) if value else None


def _normalize_remote(remote: str) -> str:
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.removeprefix("git@github.com:")
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote.rstrip("/")


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Path] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        resolved = _resolved(entry["path"])
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append({"name": entry.get("name") or resolved.name, "path": resolved})
    return deduped


def _resolved(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def _format_sverklo_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        message = _clean_message(exc.stderr) or _clean_message(exc.output)
        return message or f"exit {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout} seconds"
    return _clean_message(str(exc)) or exc.__class__.__name__


def _clean_message(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
