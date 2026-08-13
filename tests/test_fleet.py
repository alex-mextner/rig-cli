from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from riglib import entrypoint, fleet
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


def test_rig_module_command_preserves_preview_vs_commit(tmp_path):
    repo = _entry("app", tmp_path / "app")
    preview = fleet._rig_module_command("apply", "info", repo)
    commit = fleet._rig_module_command("apply", "commit", repo)
    status = fleet._rig_module_command("status", None, repo)
    assert preview[-4:] == ["apply", "info", "-C", repo.path]
    assert commit[-4:] == ["apply", "commit", "-C", repo.path]
    assert status[-3:] == ["status", "-C", repo.path]


def test_execute_collects_failures_and_keeps_deterministic_order(monkeypatch, tmp_path):
    repos = [
        _entry("z", tmp_path / "z"),
        _entry("a", tmp_path / "a"),
        _entry("m", tmp_path / "m"),
    ]

    def fake_run(repo, *, operation, mode, timeout_s):
        code = 9 if repo.name == "m" else 0
        return fleet.FleetResult(
            repository_id=repo.id,
            name=repo.name,
            path=repo.path,
            operation=f"{operation} {mode}",
            exit_code=code,
            duration_s=0.01,
            stdout=f"out-{repo.name}",
            stderr="boom" if code else "",
        )

    monkeypatch.setattr(fleet, "_run_repository", fake_run)
    report = fleet.execute(repos, operation="apply", mode="info", jobs=3)
    assert [r.name for r in report.results] == ["a", "m", "z"]
    assert [r.name for r in report.failed] == ["m"]
    assert report.summary() == {"selected": 3, "succeeded": 2, "failed": 1}
    assert not report.ok


def test_execute_bounds_worker_count(monkeypatch, tmp_path):
    repos = [_entry(str(i), tmp_path / str(i)) for i in range(2)]
    seen = {}

    class FakePool:
        def __init__(self, max_workers):
            seen["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, repo, **kwargs):
            class F:
                def result(self):
                    return fn(repo, **kwargs)

            return F()

    monkeypatch.setattr(fleet.concurrent.futures, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(fleet.concurrent.futures, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        fleet,
        "_run_repository",
        lambda repo, **kwargs: fleet.FleetResult(
            repo.id, repo.name, repo.path, "status", 0, 0.0, "", ""
        ),
    )
    fleet.execute(repos, operation="status", jobs=99)
    assert seen["workers"] == 2


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"operation": "wat"}, "unsupported fleet operation"),
        ({"operation": "apply", "mode": None}, "mode must"),
        ({"operation": "status", "mode": "info"}, "does not accept"),
        ({"operation": "status", "jobs": 0}, "jobs must"),
        ({"operation": "status", "timeout_s": 0}, "timeout must"),
    ],
)
def test_execute_rejects_invalid_contract(tmp_path, kwargs, match):
    repo = _entry("app", tmp_path / "app")
    with pytest.raises(ValueError, match=match):
        fleet.execute([repo], **kwargs)


def test_run_repository_captures_process_result(monkeypatch, tmp_path):
    repo = _entry("app", tmp_path / "app")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 3, stdout="hello\n", stderr="bad\n")

    monkeypatch.setattr(fleet.subprocess, "run", fake_run)
    result = fleet._run_repository(repo, operation="apply", mode="info", timeout_s=10)
    assert seen["command"][-4:] == ["apply", "info", "-C", repo.path]
    assert seen["kwargs"]["timeout"] == 10
    assert result.exit_code == 3
    assert result.stdout == "hello\n"
    assert result.stderr == "bad\n"


def test_run_repository_turns_timeout_into_visible_failure(monkeypatch, tmp_path):
    repo = _entry("app", tmp_path / "app")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="partial", stderr="")

    monkeypatch.setattr(fleet.subprocess, "run", fake_run)
    result = fleet._run_repository(repo, operation="status", mode=None, timeout_s=2)
    assert result.exit_code == 124
    assert result.stdout == "partial"
    assert "timeout after 2s" in result.stderr


def test_report_json_is_machine_readable(tmp_path):
    repo = _entry("app", tmp_path / "app")
    result = fleet.FleetResult(repo.id, repo.name, repo.path, "status", 0, 0.1, "ok", "")
    report = fleet.FleetReport("status", None, [repo], [result])
    encoded = json.loads(json.dumps(report.to_dict()))
    assert encoded["ok"] is True
    assert encoded["summary"] == {"selected": 1, "succeeded": 1, "failed": 0}
    assert encoded["results"][0]["repository_id"] == repo.id


def test_fleet_apply_parser_defaults_to_preview():
    args = fleet.build_parser().parse_args(["apply"])
    assert args.mode == "info"


def test_fleet_discover_is_preview_by_default(tmp_path, capsys):
    root = tmp_path / "work"
    repo = root / "app"
    (repo / ".git").mkdir(parents=True)
    target = tmp_path / "registry.json"
    assert (
        fleet.main(
            [
                "--registry",
                str(target),
                "discover",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["repositories"][0]["name"] == "app"
    assert not target.exists()


def test_fleet_discover_commit_persists_registry(tmp_path, capsys):
    root = tmp_path / "work"
    (root / "app" / ".git").mkdir(parents=True)
    target = tmp_path / "registry.json"
    assert (
        fleet.main(
            [
                "--registry",
                str(target),
                "discover",
                "--root",
                str(root),
                "--commit",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert RepositoryRegistry.load(target).repositories[0].name == "app"


def test_fleet_tags_preview_then_commit(tmp_path, capsys):
    target = tmp_path / "registry.json"
    repo = _entry("app", tmp_path / "app")
    RepositoryRegistry(repositories=[repo]).save(target)

    assert (
        fleet.main(
            [
                "--registry",
                str(target),
                "tags",
                "set",
                repo.id,
                "production",
                "customer-facing",
            ]
        )
        == 0
    )
    assert RepositoryRegistry.load(target).repositories[0].tags == []
    assert "preview only" in capsys.readouterr().out

    assert (
        fleet.main(
            [
                "--registry",
                str(target),
                "tags",
                "set",
                repo.id,
                "production",
                "--commit",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert RepositoryRegistry.load(target).repositories[0].tags == ["production"]


def test_fleet_apply_uses_registry_selectors(monkeypatch, tmp_path, capsys):
    target = tmp_path / "registry.json"
    web = _entry("web", tmp_path / "web", stack="frontend/ts/react", tags=["production"])
    api = _entry("api", tmp_path / "api", stack="backend/python", tags=["production"])
    RepositoryRegistry(repositories=[web, api]).save(target)
    seen = {}

    def fake_execute(repositories, **kwargs):
        seen["repositories"] = list(repositories)
        seen["kwargs"] = kwargs
        results = [
            fleet.FleetResult(r.id, r.name, r.path, "apply info", 0, 0.0, "preview", "")
            for r in repositories
        ]
        return fleet.FleetReport("apply", "info", list(repositories), results)

    monkeypatch.setattr(fleet, "execute", fake_execute)
    assert (
        fleet.main(
            [
                "--registry",
                str(target),
                "apply",
                "--stack",
                "frontend/ts/react",
                "--tag",
                "production",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [r.name for r in seen["repositories"]] == ["web"]
    assert seen["kwargs"]["mode"] == "info"
    assert payload["results"][0]["name"] == "web"


def test_fleet_nonzero_when_any_repository_fails(monkeypatch, tmp_path, capsys):
    target = tmp_path / "registry.json"
    repo = _entry("bad", tmp_path / "bad")
    RepositoryRegistry(repositories=[repo]).save(target)

    def fake_execute(repositories, **kwargs):
        result = fleet.FleetResult(repo.id, repo.name, repo.path, "status", 5, 0.0, "", "missing")
        return fleet.FleetReport("status", None, list(repositories), [result])

    monkeypatch.setattr(fleet, "execute", fake_execute)
    assert fleet.main(["--registry", str(target), "status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["failed"] == 1


def test_entrypoint_routes_fleet_without_loading_legacy_cli(monkeypatch):
    import riglib.fleet as fleet_module

    seen = {}
    monkeypatch.setattr(fleet_module, "main", lambda argv: seen.setdefault("argv", list(argv)) or 7)
    result = entrypoint.main(["fleet", "list", "--json"])
    assert seen["argv"] == ["list", "--json"]
    # setdefault returns the list, so the entrypoint preserves the delegated return value exactly.
    assert result == ["list", "--json"]


def test_entrypoint_delegates_non_fleet_to_existing_cli(monkeypatch):
    import riglib.cli as cli_module

    monkeypatch.setattr(cli_module, "main", lambda argv: 23 if list(argv) == ["status", "-C", "/x"] else 99)
    assert entrypoint.main(["status", "-C", "/x"]) == 23
