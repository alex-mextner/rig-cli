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
        assert args == ["sverklo", "list"]
        return subprocess.CompletedProcess(args, 0, stdout=sverklo_output, stderr="")

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
