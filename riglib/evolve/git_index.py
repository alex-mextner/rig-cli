"""Git history aggregation for the `rig evolve` portal.

Accessed via: `rig evolve` HTTP APIs and tests that need timeline buckets. The module shells
out to git lazily per call and keeps import-time dependencies to the standard library only.

Assumptions: callers pass a git working tree or a directory where `git log` fails cleanly. A
failure returns an empty timeline with health data at the web layer rather than crashing import.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class _Bucket:
    id: str
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    paths: set[str] = field(default_factory=set)

    def to_dict(self, *, include_paths: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "commits": self.commits,
            "changed_files": len(self.paths),
            "additions": self.additions,
            "deletions": self.deletions,
        }
        if include_paths:
            data["paths"] = sorted(self.paths)
        return data


def build_histogram(repo_root: Path, *, bucket: str = "month", include_paths: bool = True) -> list[dict[str, object]]:
    """Return git activity buckets for ``repo_root``.

    ``bucket`` is ``day``, ``week``, or ``month``. Counts are intentionally simple and stable:
    one commit increments one bucket; file counts are unique paths per bucket; binary numstat rows
    count as changed paths but do not add line totals.
    """
    raw = _git_log(repo_root)
    buckets: dict[str, _Bucket] = {}
    current_date: str | None = None
    current_seen = False
    for line in raw.splitlines():
        if line.startswith("commit "):
            parts = line.split()
            current_date = parts[2] if len(parts) >= 3 else None
            if current_date:
                bid = _bucket_id(current_date, bucket)
                if bid not in buckets:
                    buckets[bid] = _Bucket(bid)
                buckets[bid].commits += 1
            current_seen = True
            continue
        if not line.strip() or current_date is None or not current_seen:
            continue
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        added, deleted, path = cols[0], cols[1], cols[2]
        b = buckets[_bucket_id(current_date, bucket)]
        b.paths.add(path)
        if added.isdigit():
            b.additions += int(added)
        if deleted.isdigit():
            b.deletions += int(deleted)
    return [buckets[k].to_dict(include_paths=include_paths) for k in sorted(buckets)]


def build_path_touches(repo_root: Path, path: str, *, bucket: str = "month") -> list[str]:
    """Return bucket ids where ``path`` or a child under it changed."""
    if not path:
        return []
    raw = _git_log(repo_root, path=path)
    touched: set[str] = set()
    for line in raw.splitlines():
        if line.startswith("commit "):
            parts = line.split()
            if len(parts) >= 3:
                touched.add(_bucket_id(parts[2], bucket))
    return sorted(touched)


def resolve_bucket_end(repo_root: Path, requested_bucket: str, *, bucket: str = "month") -> dict[str, Any]:
    """Return the latest commit inside a day/week/month histogram bucket.

    The result is intentionally JSON-shaped because it feeds the HTTP layer later. Missing
    buckets and git errors are represented as status values rather than exceptions.
    """
    try:
        _bucket_id("2000-01-01", bucket)
    except ValueError as exc:
        return _empty_resolution(requested_bucket, bucket, status="error", message=str(exc))

    raw = _git_commit_index(repo_root)
    if raw is None:
        return _empty_resolution(requested_bucket, bucket, status="error", message="git log failed")

    head = _git_head(repo_root)
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        commit, commit_day, commit_time = parts[0], parts[1], parts[2]
        try:
            commit_bucket = _bucket_id(commit_day, bucket)
        except (TypeError, ValueError):
            continue
        if commit_bucket == requested_bucket:
            return {
                "requested_bucket": requested_bucket,
                "bucket": bucket,
                "status": "ok",
                "message": "resolved",
                "commit": commit,
                "time": commit_time,
                "current": bool(head and commit == head),
            }
    return _empty_resolution(
        requested_bucket,
        bucket,
        status="missing",
        message=f"no commit found in {bucket} bucket {requested_bucket}",
    )


def git_health(repo_root: Path) -> dict[str, str]:
    """Cheap health signal for the git provider."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "message": str(exc)}
    if res.returncode == 0 and res.stdout.strip() == "true":
        return {"status": "ok", "message": "git history available"}
    msg = (res.stderr or res.stdout).strip() or "not a git repository"
    return {"status": "error", "message": msg}


def _git_log(repo_root: Path, *, path: str | None = None) -> str:
    cmd = ["git", "log", "--date=short", "--pretty=format:commit %H %ad", "--numstat"]
    if path:
        cmd.extend(["--", path])
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return ""
    return res.stdout


def _git_commit_index(repo_root: Path) -> str | None:
    cmd = ["git", "log", "--date=short", "--pretty=format:%H%x09%ad%x09%aI"]
    try:
        res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _git_head(repo_root: Path) -> str | None:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    head = res.stdout.strip()
    return head or None


def _empty_resolution(requested_bucket: str, bucket: str, *, status: str, message: str) -> dict[str, Any]:
    return {
        "requested_bucket": requested_bucket,
        "bucket": bucket,
        "status": status,
        "message": message,
        "commit": None,
        "time": None,
        "current": False,
    }


def _bucket_id(date: str, bucket: str) -> str:
    if bucket == "day":
        return date
    if bucket == "week":
        from datetime import date as date_type

        y, m, d = (int(part) for part in date.split("-"))
        iso = date_type(y, m, d).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if bucket != "month":
        raise ValueError(f"unknown bucket {bucket!r} (expected day|week|month)")
    return date[:7]
