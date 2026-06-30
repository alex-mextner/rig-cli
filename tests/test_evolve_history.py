"""Historical snapshot tests for `rig evolve`.

The UI will use this backend to render a file tree as of the end of a selected histogram bucket.
These tests use a real git repo so rename/delete/change behavior exercises git plumbing, not
mocked state.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        **(env or {}),
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=merged,
        check=True,
        capture_output=True,
        text=True,
    )


def _write(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return body


def _commit(repo: Path, message: str, when: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-qm",
        message,
        env={
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        },
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def snapshot_repo(tmp_path: Path) -> tuple[Path, dict[str, str], dict[str, int]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")

    alpha_v1 = _write(repo / "src" / "alpha.py", "def alpha():\n    return 1\n")
    jan_add = _commit(repo, "add alpha", "2026-01-05T12:00:00+0000")

    alpha_v2 = _write(repo / "src" / "alpha.py", "def alpha():\n    return 200\n")
    jan_change = _commit(repo, "change alpha", "2026-01-20T12:00:00+0000")

    (repo / "src" / "alpha.py").rename(repo / "src" / "bravo.py")
    feb_rename = _commit(repo, "rename alpha to bravo", "2026-02-03T12:00:00+0000")

    (repo / "src" / "bravo.py").unlink()
    current = _write(repo / "src" / "current.py", "def current():\n    return 4\n")
    mar_delete = _commit(repo, "delete bravo and add current", "2026-03-04T12:00:00+0000")

    commits = {
        "jan_add": jan_add,
        "jan_change": jan_change,
        "feb_rename": feb_rename,
        "mar_delete": mar_delete,
    }
    sizes = {
        "alpha_v1": len(alpha_v1.encode("utf-8")),
        "alpha_v2": len(alpha_v2.encode("utf-8")),
        "current": len(current.encode("utf-8")),
    }
    return repo, commits, sizes


def _paths(node: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    if node.get("kind") == "file":
        out.add(str(node["path"]))
    for child in node.get("children", []):
        out.update(_paths(child))
    return out


def _file_node(node: dict[str, Any], path: str) -> dict[str, Any]:
    if node.get("kind") == "file" and node.get("path") == path:
        return node
    for child in node.get("children", []):
        found = _file_node(child, path)
        if found:
            return found
    return {}


def test_resolves_bucket_end_commit_for_day_week_and_month(
    snapshot_repo: tuple[Path, dict[str, str], dict[str, int]],
) -> None:
    from riglib.evolve.git_index import resolve_bucket_end

    repo, commits, _sizes = snapshot_repo

    month = resolve_bucket_end(repo, "2026-01", bucket="month")
    week = resolve_bucket_end(repo, "2026-W06", bucket="week")
    day = resolve_bucket_end(repo, "2026-03-04", bucket="day")

    assert month["status"] == "ok"
    assert month["commit"] == commits["jan_change"]
    assert month["time"].startswith("2026-01-20T12:00:00")
    assert month["current"] is False

    assert week["status"] == "ok"
    assert week["commit"] == commits["feb_rename"]

    assert day["status"] == "ok"
    assert day["commit"] == commits["mar_delete"]
    assert day["current"] is True


def test_builds_file_tree_snapshot_at_bucket_end_with_selection_metadata(
    snapshot_repo: tuple[Path, dict[str, str], dict[str, int]],
) -> None:
    from riglib.evolve.history import build_historical_snapshot

    repo, commits, sizes = snapshot_repo

    jan = build_historical_snapshot(repo, "2026-01", bucket="month", selected_path="src/current.py")
    feb = build_historical_snapshot(repo, "2026-02", bucket="month", selected_path="src/alpha.py")
    mar = build_historical_snapshot(repo, "2026-03", bucket="month", selected_path="src/bravo.py")

    assert jan["resolution"]["commit"] == commits["jan_change"]
    assert _paths(jan["tree"]) == {"src/alpha.py"}
    assert _file_node(jan["tree"], "src/alpha.py")["size"] == sizes["alpha_v2"]
    assert jan["selection"] == {
        "path": "src/current.py",
        "exists": False,
        "missing": True,
        "current": True,
    }

    assert feb["resolution"]["commit"] == commits["feb_rename"]
    assert _paths(feb["tree"]) == {"src/bravo.py"}
    assert feb["selection"]["path"] == "src/alpha.py"
    assert feb["selection"]["missing"] is True
    assert feb["selection"]["current"] is False

    assert mar["resolution"]["commit"] == commits["mar_delete"]
    assert mar["resolution"]["current"] is True
    assert _paths(mar["tree"]) == {"src/current.py"}
    assert _file_node(mar["tree"], "src/current.py")["size"] == sizes["current"]
    assert mar["selection"]["missing"] is True
    assert mar["selection"]["current"] is False


def test_missing_bucket_snapshot_is_fail_soft(
    snapshot_repo: tuple[Path, dict[str, str], dict[str, int]],
) -> None:
    from riglib.evolve.history import build_historical_snapshot

    repo, _commits, _sizes = snapshot_repo

    snapshot = build_historical_snapshot(repo, "2025-12", bucket="month", selected_path="src/current.py")

    assert snapshot["resolution"]["status"] == "missing"
    assert snapshot["resolution"]["commit"] is None
    assert snapshot["tree"]["children"] == []
    assert snapshot["selection"]["missing"] is True
    assert snapshot["selection"]["current"] is True
    assert snapshot["health"]["git"]["status"] == "missing"
