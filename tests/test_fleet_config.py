from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from riglib import fleet_config
from riglib.fleet import FleetReport, FleetResult
from riglib.repository_registry import RepositoryEntry, RepositoryRegistry


def _entry(name: str, path: Path, *, stack: str | None = None, tags=()) -> RepositoryEntry:
    return RepositoryEntry(
        id=f"id-{name}",
        path=path.as_posix(),
        name=name,
        root=path.parent.as_posix(),
        stack=stack,
        tags=list(tags),
    )


def test_config_command_preview_and_commit(tmp_path):
    repo = _entry("app", tmp_path / "app")
    preview = fleet_config._config_command(
        repo, "ci.enabled", "true", commit=False
    )
    commit = fleet_config._config_command(
        repo, "ci.enabled", "true", commit=True
    )
    assert preview[-4:] == ["ci.enabled", "true", "-C", repo.path]
    assert "--commit" not in preview
    assert commit[-1] == "--commit"


def test_global_write_uses_no_apply_once(tmp_path):
    repo = _entry("app", tmp_path / "app")
    command = fleet_config._config_command(
        repo,
        "ci.enabled",
        "true",
        commit=False,
        is_global=True,
        no_apply=True,
    )
    assert "--global" in command
    assert "--no-apply" in command
    assert "--commit" not in command


def test_run_config_captures_exit(monkeypatch, tmp_path):
    repo = _entry("app", tmp_path / "app")
    monkeypatch.setattr(
        fleet_config.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, "preview", "bad"),
    )
    result = fleet_config._run_config(
        repo,
        path="ci.enabled",
        value="true",
        commit=False,
    )
    assert result.exit_code == 2
    assert result.stdout == "preview"
    assert result.stderr == "bad"


def test_repo_config_collect_and_continue(monkeypatch, tmp_path):
    repos = [_entry("b", tmp_path / "b"), _entry("a", tmp_path / "a")]

    def fake_run(repo, **kwargs):
        return FleetResult(
            repo.id,
            repo.name,
            repo.path,
            "config set repo",
            3 if repo.name == "b" else 0,
            0.0,
            "",
            "bad" if repo.name == "b" else "",
        )

    monkeypatch.setattr(fleet_config, "_run_config", fake_run)
    report = fleet_config.execute_repo_config(
        repos,
        path="ci.enabled",
        value="true",
        commit=True,
        jobs=2,
    )
    assert [result.name for result in report.results] == ["a", "b"]
    assert report.summary() == {"selected": 2, "succeeded": 1, "failed": 1}


def test_global_commit_aborts_before_write_when_any_preview_fails(monkeypatch, tmp_path):
    repos = [_entry("a", tmp_path / "a"), _entry("b", tmp_path / "b")]
    calls = []

    def fake_run(repo, **kwargs):
        calls.append((repo.name, kwargs))
        code = 2 if repo.name == "b" else 0
        return FleetResult(repo.id, repo.name, repo.path, "preview", code, 0.0, "", "bad" if code else "")

    monkeypatch.setattr(fleet_config, "_run_config", fake_run)
    report = fleet_config.execute_global_config(
        repos,
        path="ci.enabled",
        value="true",
        commit=True,
        jobs=2,
    )
    assert not report.ok
    assert report.write is None
    assert report.reconcile is None
    assert len(calls) == 2
    assert all(not kwargs.get("no_apply", False) for _, kwargs in calls)


def test_global_commit_writes_once_then_reconciles_same_selection(monkeypatch, tmp_path):
    repos = [_entry("a", tmp_path / "a"), _entry("b", tmp_path / "b")]
    calls = []

    def fake_run(repo, **kwargs):
        calls.append((repo.name, kwargs))
        return FleetResult(repo.id, repo.name, repo.path, "config", 0, 0.0, "", "")

    monkeypatch.setattr(fleet_config, "_run_config", fake_run)
    seen = {}

    def fake_execute(selected, **kwargs):
        seen["selected"] = list(selected)
        seen["kwargs"] = kwargs
        results = [FleetResult(r.id, r.name, r.path, "apply commit", 0, 0.0, "", "") for r in selected]
        return FleetReport("apply", "commit", list(selected), results)

    monkeypatch.setattr(fleet_config, "execute", fake_execute)
    report = fleet_config.execute_global_config(
        repos,
        path="ci.enabled",
        value="true",
        commit=True,
        jobs=2,
    )
    assert report.ok
    writes = [kwargs for _, kwargs in calls if kwargs.get("no_apply")]
    assert len(writes) == 1
    assert writes[0]["is_global"] is True
    assert [r.name for r in seen["selected"]] == ["a", "b"]
    assert seen["kwargs"]["mode"] == "commit"


def test_global_preview_never_writes_or_reconciles(monkeypatch, tmp_path):
    repos = [_entry("a", tmp_path / "a")]
    monkeypatch.setattr(
        fleet_config,
        "_run_config",
        lambda repo, **kwargs: FleetResult(repo.id, repo.name, repo.path, "preview", 0, 0.0, "", ""),
    )
    monkeypatch.setattr(
        fleet_config,
        "execute",
        lambda *a, **k: pytest.fail("preview must not reconcile"),
    )
    report = fleet_config.execute_global_config(
        repos,
        path="ci.enabled",
        value="true",
        commit=False,
    )
    assert report.ok
    assert report.write is None
    assert report.reconcile is None


def test_cli_requires_selector_or_explicit_all_repos(tmp_path, capsys):
    target = tmp_path / "registry.json"
    RepositoryRegistry(repositories=[_entry("a", tmp_path / "a")]).save(target)
    assert (
        fleet_config.main(
            ["--registry", str(target), "set", "ci.enabled", "true"]
        )
        == 2
    )
    assert "selector or explicit --all-repos" in capsys.readouterr().err


def test_cli_rejects_global_with_subset_selector(tmp_path, capsys):
    target = tmp_path / "registry.json"
    RepositoryRegistry(repositories=[_entry("a", tmp_path / "a")]).save(target)
    assert (
        fleet_config.main(
            [
                "--registry",
                str(target),
                "set",
                "ci.enabled",
                "true",
                "--global",
                "--repo",
                "a",
            ]
        )
        == 2
    )
    assert "workspace-wide" in capsys.readouterr().err


def test_cli_selects_stack_and_tag_for_repo_overrides(monkeypatch, tmp_path, capsys):
    target = tmp_path / "registry.json"
    web = _entry("web", tmp_path / "web", stack="frontend/ts/react", tags=["production"])
    api = _entry("api", tmp_path / "api", stack="backend/python", tags=["production"])
    RepositoryRegistry(repositories=[web, api]).save(target)
    seen = {}

    def fake_execute(repositories, **kwargs):
        seen["repos"] = list(repositories)
        seen["kwargs"] = kwargs
        results = [FleetResult(r.id, r.name, r.path, "preview", 0, 0.0, "", "") for r in repositories]
        return FleetReport("config-set", "info", list(repositories), results)

    monkeypatch.setattr(fleet_config, "execute_repo_config", fake_execute)
    assert (
        fleet_config.main(
            [
                "--registry",
                str(target),
                "set",
                "ci.enabled",
                "true",
                "--stack",
                "frontend/ts/react",
                "--tag",
                "production",
                "--json",
            ]
        )
        == 0
    )
    assert [repo.name for repo in seen["repos"]] == ["web"]
    assert seen["kwargs"]["commit"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["selected"] == 1


def test_cli_all_repos_global_preview(monkeypatch, tmp_path, capsys):
    target = tmp_path / "registry.json"
    repos = [_entry("a", tmp_path / "a"), _entry("b", tmp_path / "b")]
    RepositoryRegistry(repositories=repos).save(target)
    seen = {}

    def fake_global(repositories, **kwargs):
        seen["repos"] = list(repositories)
        seen["kwargs"] = kwargs
        preview_results = [
            FleetResult(r.id, r.name, r.path, "preview", 0, 0.0, "", "") for r in repositories
        ]
        preview = FleetReport("config-set-global", "info", list(repositories), preview_results)
        return fleet_config.GlobalMutationReport("ci.enabled", "true", preview)

    monkeypatch.setattr(fleet_config, "execute_global_config", fake_global)
    assert (
        fleet_config.main(
            [
                "--registry",
                str(target),
                "set",
                "ci.enabled",
                "true",
                "--all-repos",
                "--global",
                "--json",
            ]
        )
        == 0
    )
    assert sorted(repo.name for repo in seen["repos"]) == ["a", "b"]
    assert seen["kwargs"]["commit"] is False
    assert json.loads(capsys.readouterr().out)["scope"] == "global"
