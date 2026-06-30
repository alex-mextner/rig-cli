"""Tests for the evolve project discovery sidecar."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_discover_projects_always_includes_current_repo(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("sverklo")

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert projects[0]["path"] == str(repo.resolve())
    assert projects[0]["name"] == "repo"
    assert projects[0]["sources"] == ["current"]
    assert projects[0]["health"]["current"]["status"] == "ok"


def test_discover_projects_parses_sample_sverklo_output(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "repo"
    repo.mkdir()
    registered = tmp_path / "registered"
    registered.mkdir()

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"PATH STATUS\n{registered} ok\nRegistry: {tmp_path / 'registry.json'}\n",
                stderr="",
            )
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    by_path = {project["path"]: project for project in projects}
    assert str(repo.resolve()) in by_path
    assert str(registered.resolve()) in by_path
    assert str((tmp_path / "registry.json").resolve()) not in by_path
    assert by_path[str(registered.resolve())]["sources"] == ["sverklo"]
    assert by_path[str(registered.resolve())]["health"]["sverklo"]["status"] == "ok"


def test_discover_projects_attaches_missing_json_sverklo_name_as_current_alias(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "hyperide"
    repo.mkdir()
    stale_path = tmp_path / "old-folder"
    sverklo_output = json.dumps([{"name": "Hyperide", "path": str(stale_path)}])

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a git repository")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert len(projects) == 1
    current = projects[0]
    assert current["path"] == str(repo.resolve())
    assert current["aliases"] == ["Hyperide"]
    assert current["notes"] == [f"sverklo registry has stale alias Hyperide at missing path {stale_path.resolve()}"]


def test_discover_projects_merges_json_sverklo_name_for_same_remote(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "hyperide"
    repo.mkdir()
    registered = tmp_path / "old-folder"
    registered.mkdir()
    sverklo_output = json.dumps([{"name": "Hyperide", "path": str(registered)}])

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/hyperide.git\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert len(projects) == 1
    current = projects[0]
    assert current["path"] == str(repo.resolve())
    assert current["aliases"] == ["Hyperide"]
    assert current["sources"] == ["current", "sverklo"]
    assert current["health"]["sverklo"]["status"] == "ok"


def test_discover_projects_skips_sverklo_worktree_alias_for_same_remote(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "hyperide"
    repo.mkdir()
    worktree = tmp_path / "hyperide-worktrees" / "HYP-837-master-diagram"
    worktree.mkdir(parents=True)
    sverklo_output = json.dumps([{"name": "HYP-837-master-diagram", "path": str(worktree)}])

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")
        if args == ["git", "-C", str(repo.resolve()), "rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{repo / '.git'}\n", stderr="")
        if args[:3] == ["git", "-C", str(worktree)] and args[3:5] == ["rev-parse", "--path-format=absolute"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"{repo / '.git' / 'worktrees' / 'HYP-837-master-diagram'}\n{repo / '.git'}\n",
                stderr="",
            )
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/hyperide.git\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert len(projects) == 1
    current = projects[0]
    assert current["path"] == str(repo.resolve())
    assert current["aliases"] == []
    assert current["sources"] == ["current"]
    assert current["notes"] == ["sverklo linked worktree skipped"]


def test_discover_projects_skips_sverklo_worktree_alias_for_different_repo(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "hyperide"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    worktree = tmp_path / "other-worktrees" / "feature"
    worktree.mkdir(parents=True)
    sverklo_output = json.dumps(
        [
            {"name": "feature", "path": str(worktree)},
            {"name": "other", "path": str(other)},
        ]
    )

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")
        if args == ["git", "-C", str(repo.resolve()), "rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{repo / '.git'}\n", stderr="")
        if args == ["git", "-C", str(other.resolve()), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{other / '.git'}\n{other / '.git'}\n", stderr="")
        if args[:3] == ["git", "-C", str(worktree)] and args[3:5] == ["rev-parse", "--path-format=absolute"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"{other / '.git' / 'worktrees' / 'feature'}\n{other / '.git'}\n",
                stderr="",
            )
        if args[:3] == ["git", "-C", str(other.resolve())] and args[3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/other.git\n", stderr="")
        if args[:3] == ["git", "-C", str(worktree.resolve())] and args[3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/other.git\n", stderr="")
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/hyperide.git\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert {project["path"] for project in projects} == {str(repo.resolve()), str(other.resolve())}
    other_project = next(project for project in projects if project["path"] == str(other.resolve()))
    assert other_project["name"] == "other"
    assert other_project["aliases"] == []
    assert other_project["sources"] == ["sverklo"]
    assert other_project["notes"] == ["sverklo linked worktree skipped"]


def test_discover_projects_keeps_worktree_only_project_as_worktree_source(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "hyperide"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    worktree = tmp_path / "other-worktrees" / "feature"
    worktree.mkdir(parents=True)
    sverklo_output = json.dumps([{"name": "feature", "path": str(worktree)}])

    def fake_run(args, **kwargs):
        if args == ["sverklo", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")
        if args == ["git", "-C", str(repo.resolve()), "rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{repo / '.git'}\n", stderr="")
        if args[:3] == ["git", "-C", str(worktree)] and args[3:5] == ["rev-parse", "--path-format=absolute"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"{other / '.git' / 'worktrees' / 'feature'}\n{other / '.git'}\n",
                stderr="",
            )
        if args[:3] == ["git", "-C", str(worktree.resolve())] and args[3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/other.git\n", stderr="")
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout="git@github.com:ultra/hyperide.git\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert {project["path"] for project in projects} == {str(repo.resolve()), str(worktree.resolve())}
    worktree_project = next(project for project in projects if project["path"] == str(worktree.resolve()))
    assert worktree_project["name"] == "feature"
    assert worktree_project["aliases"] == []
    assert worktree_project["sources"] == ["worktree"]


def test_discover_projects_dedupes_by_resolved_path(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=f"{link}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    matching = [project for project in projects if project["path"] == str(repo.resolve())]
    assert len(matching) == 1
    assert matching[0]["sources"] == ["current", "sverklo"]
    assert matching[0]["health"]["current"]["status"] == "ok"
    assert matching[0]["health"]["sverklo"]["status"] == "ok"


def test_discover_projects_represents_sverklo_failure_in_health(tmp_path, monkeypatch):
    from riglib.evolve.projects import discover_projects

    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(
            2,
            args,
            output="",
            stderr="database is locked",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    projects = discover_projects(repo)

    assert len(projects) == 1
    current = projects[0]
    assert current["path"] == str(repo.resolve())
    assert current["health"]["sverklo"]["status"] == "error"
    assert "database is locked" in current["health"]["sverklo"]["message"]
    assert current["notes"] == ["sverklo list failed: database is locked"]
