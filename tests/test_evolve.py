"""Tests for `rig evolve` — the project evolution portal slice.

The first slice is intentionally small but runnable: CLI registration + service argv,
git-backed timeline buckets, a proportional file treemap, and a JSON snapshot the web app can
serve. Rich symbol/LSP/provider overlays build on this base.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    merged = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        **(env or {}),
    }
    subprocess.run(["git", *args], cwd=str(repo), env=merged, check=True, capture_output=True)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def history_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo / "src" / "alpha.py", "def alpha():\n    return 1\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-qm",
        "add alpha",
        env={
            "GIT_AUTHOR_DATE": "2026-01-15T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-01-15T12:00:00+0000",
        },
    )
    _write(repo / "src" / "alpha.py", "def alpha():\n    return 2\n")
    _write(repo / "web" / "beta.ts", "export function beta() { return 3 }\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-qm",
        "change alpha and beta",
        env={
            "GIT_AUTHOR_DATE": "2026-02-03T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-02-03T12:00:00+0000",
        },
    )
    return repo


def test_bare_evolve_prints_help_never_launches(capsys):
    from riglib.cli import main

    rc = main(["evolve"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: rig evolve" in out
    assert "run" in out and "status" in out and "disable" in out


def test_register_exposes_evolve_subcommand_and_verb():
    from riglib.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["evolve", "status", "-C", "/tmp/project", "--port", "9355"])
    assert ns.command == "evolve"
    assert ns.evolve_verb == "status"
    assert ns.cwd == "/tmp/project"
    assert ns.port == 9355


def test_evolve_service_argv_targets_internal_serve(history_repo: Path):
    from riglib.evolve import service

    argv = service._serve_argv(history_repo, 9355)
    assert argv[0] == sys.executable
    assert Path(argv[0]).is_absolute()
    assert argv[1] == "-c"
    assert argv[3:5] == ["evolve", service.SERVE_VERB]
    assert "--port" in argv and "9355" in argv
    assert "-C" in argv and str(history_repo) in argv


def test_git_histogram_buckets_by_month(history_repo: Path):
    from riglib.evolve.git_index import build_histogram

    buckets = build_histogram(history_repo, bucket="month")
    by_id = {b["id"]: b for b in buckets}
    assert by_id["2026-01"]["commits"] == 1
    assert by_id["2026-01"]["changed_files"] == 1
    assert by_id["2026-02"]["commits"] == 1
    assert by_id["2026-02"]["changed_files"] == 2
    assert by_id["2026-02"]["additions"] >= 2


def test_git_histogram_supports_day_week_and_month(history_repo: Path):
    from riglib.evolve.git_index import build_histogram

    day_ids = [b["id"] for b in build_histogram(history_repo, bucket="day")]
    week_ids = [b["id"] for b in build_histogram(history_repo, bucket="week")]
    month_ids = [b["id"] for b in build_histogram(history_repo, bucket="month")]

    assert day_ids == ["2026-01-15", "2026-02-03"]
    assert week_ids == ["2026-W03", "2026-W06"]
    assert month_ids == ["2026-01", "2026-02"]


def test_file_tree_scales_nodes_by_file_size(history_repo: Path):
    from riglib.evolve.structure import build_file_tree

    tree = build_file_tree(history_repo)
    assert tree["kind"] == "repo"
    assert tree["size"] > 0
    child_names = {child["name"] for child in tree["children"]}
    assert {"src", "web"} <= child_names
    src = next(child for child in tree["children"] if child["name"] == "src")
    assert src["children"][0]["name"] == "alpha.py"
    assert src["children"][0]["size"] > 0


def test_file_tree_skips_generated_binary_and_lockfile_noise(tmp_path: Path):
    from riglib.evolve.structure import build_file_tree

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo / "src" / "app.py", "print('ok')\n")
    _write(repo / ".yarn" / "releases" / "yarn-4.5.0.cjs", "generated\n")
    _write(repo / "dist" / "bundle.js", "generated\n")
    _write(repo / "public" / "screenshot.png", "not really png\n")
    _write(repo / "yarn.lock", "lock\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixtures")

    tree = build_file_tree(repo)
    top = {child["name"] for child in tree["children"]}

    assert "src" in top
    assert ".yarn" not in top
    assert "dist" not in top
    assert "yarn.lock" not in top
    public = next((child for child in tree["children"] if child["name"] == "public"), None)
    assert public is None or public["children"] == []


def test_file_tree_tolerates_non_utf8_git_filenames(tmp_path: Path, monkeypatch):
    from riglib.evolve import structure

    repo = tmp_path / "repo"
    repo.mkdir()
    raw_name = b"bad-\xff.py"

    def fake_run(args, **kwargs):
        assert args == ["git", "ls-files", "-z"]
        return subprocess.CompletedProcess(args, 0, stdout=raw_name + b"\0", stderr=b"")

    monkeypatch.setattr(structure.subprocess, "run", fake_run)
    tree = structure.build_file_tree(repo)
    assert tree["children"] == []


def test_snapshot_payload_contains_projects_histogram_tree_and_health(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    payload = EvolveApp(repo_root=history_repo).snapshot_payload(project_path=str(history_repo))
    assert payload["project"]["path"] == str(history_repo)
    assert payload["histogram"]
    assert payload["tree"]["children"]
    assert payload["health"]["git"]["status"] == "ok"
    assert "paths" not in payload["histogram"][0]


def test_snapshot_payload_returns_historical_period_with_selection_metadata(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    payload = EvolveApp(repo_root=history_repo).snapshot_payload(
        project_path=str(history_repo),
        bucket="month",
        period="2026-01",
        selected_path="web/beta.ts",
    )

    assert payload["requested"]["bucket"] == "month"
    assert payload["requested"]["bucket_id"] == "2026-01"
    assert payload["resolution"]["status"] == "ok"
    assert payload["selection"]["path"] == "web/beta.ts"
    assert payload["selection"]["missing"] is True
    assert payload["selection"]["current"] is True
    assert {child["name"] for child in payload["tree"]["children"]} == {"src"}
    assert payload["histogram"]


def test_snapshot_payload_marks_historical_only_selection_not_current(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    _write(history_repo / "old" / "only.py", "def only():\n    return 1\n")
    _git(
        history_repo,
        "add",
        ".",
    )
    _git(
        history_repo,
        "commit",
        "-qm",
        "add old only",
        env={
            "GIT_AUTHOR_DATE": "2026-03-04T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-03-04T12:00:00+0000",
        },
    )
    _git(history_repo, "rm", "-q", "old/only.py")
    _git(
        history_repo,
        "commit",
        "-qm",
        "remove old only",
        env={
            "GIT_AUTHOR_DATE": "2026-04-04T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-04-04T12:00:00+0000",
        },
    )

    payload = EvolveApp(repo_root=history_repo).snapshot_payload(
        project_path=str(history_repo),
        bucket="month",
        period="2026-03",
        selected_path="old/only.py",
    )

    assert payload["selection"]["path"] == "old/only.py"
    assert payload["selection"]["exists"] is True
    assert payload["selection"]["missing"] is False
    assert payload["selection"]["current"] is False


def test_snapshot_payload_marks_missing_current_selection(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    payload = EvolveApp(repo_root=history_repo).snapshot_payload(
        project_path=str(history_repo),
        selected_path="missing/nope.py",
    )

    assert payload["selection"]["path"] == "missing/nope.py"
    assert payload["selection"]["exists"] is False
    assert payload["selection"]["missing"] is True
    assert payload["selection"]["current"] is False


def test_snapshot_payload_marks_untracked_existing_file_not_current(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    _write(history_repo / "scratch" / "draft.py", "def draft():\n    return 1\n")

    payload = EvolveApp(repo_root=history_repo).snapshot_payload(
        project_path=str(history_repo),
        selected_path="scratch/draft.py",
    )

    assert payload["selection"]["path"] == "scratch/draft.py"
    assert payload["selection"]["exists"] is False
    assert payload["selection"]["missing"] is True
    assert payload["selection"]["current"] is False


def test_touches_payload_returns_buckets_for_selected_path(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    payload = EvolveApp(repo_root=history_repo).touches_payload(
        project_path=str(history_repo),
        path="src/alpha.py",
        bucket="month",
    )

    assert payload["bucket_ids"] == ["2026-01", "2026-02"]


def test_touches_payload_respects_bucket_granularity(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    app = EvolveApp(repo_root=history_repo)

    assert app.touches_payload(project_path=str(history_repo), path="src/alpha.py", bucket="day")[
        "bucket_ids"
    ] == ["2026-01-15", "2026-02-03"]
    assert app.touches_payload(project_path=str(history_repo), path="src/alpha.py", bucket="week")[
        "bucket_ids"
    ] == ["2026-W03", "2026-W06"]
    assert app.touches_payload(project_path=str(history_repo), path="src/alpha.py", bucket="month")[
        "bucket_ids"
    ] == ["2026-01", "2026-02"]


def test_symbols_and_relationships_payloads_for_selected_file(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    app = EvolveApp(repo_root=history_repo)
    symbols = app.symbols_payload(project_path=str(history_repo), path="src/alpha.py")
    relationships = app.relationships_payload(project_path=str(history_repo), path="src/alpha.py")

    assert symbols["health"]["status"] == "ok"
    assert symbols["symbols"][0]["name"] == "alpha"
    assert relationships["relationships"]["quality"] == "heuristic-imports-v1"
    assert "capped at" in relationships["relationships"]["message"]
    assert set(relationships["relationships"]) >= {"uses", "used_by"}


def test_relationships_payload_reuses_head_keyed_index_cache(history_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from riglib.evolve import web

    web._RELATIONSHIP_INDEX_CACHE.clear()
    calls = 0
    real_relationship_files = web._relationship_files

    def counted_relationship_files(project: Path) -> list[Path]:
        nonlocal calls
        calls += 1
        return real_relationship_files(project)

    monkeypatch.setattr(web, "_relationship_files", counted_relationship_files)
    app = web.EvolveApp(repo_root=history_repo)

    first = app.relationships_payload(project_path=str(history_repo), path="src/alpha.py")
    second = app.relationships_payload(project_path=str(history_repo), path="src/alpha.py")

    assert first["relationships"]["quality"] == "heuristic-imports-v1"
    assert second["relationships"]["quality"] == "heuristic-imports-v1"
    assert calls == 1


def test_snapshot_file_paths_reuse_head_keyed_cache(history_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from riglib.evolve import web

    web._SNAPSHOT_PATHS_CACHE.clear()
    calls = 0
    real_build_file_tree = web.build_file_tree

    def counted_build_file_tree(project: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return real_build_file_tree(project)

    monkeypatch.setattr(web, "build_file_tree", counted_build_file_tree)

    first = web._snapshot_file_paths(history_repo)
    second = web._snapshot_file_paths(history_repo)

    assert "src/alpha.py" in first
    assert second == first
    assert calls == 1


def test_relationships_payload_returns_structured_error(history_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from riglib.evolve import web

    def broken_relationships(project: Path, rel: str) -> dict[str, object]:
        raise RuntimeError(f"broken index for {rel}")

    monkeypatch.setattr(web, "_relationships", broken_relationships)
    app = web.EvolveApp(repo_root=history_repo)

    payload = app.relationships_payload(project_path=str(history_repo), path="src/alpha.py")

    assert payload["relationships"]["quality"] == "error"
    assert payload["relationships"]["uses"] == []
    assert payload["relationships"]["used_by"] == []
    assert "broken index for src/alpha.py" in payload["relationships"]["message"]


def test_copy_relationship_normalizes_missing_message():
    from riglib.evolve.web import _copy_relationship

    assert _copy_relationship({"quality": "ok", "message": None})["message"] == ""


def test_symbol_relationship_payloads_reject_unknown_projects_and_untracked_files(
    history_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from riglib.evolve.web import EvolveApp

    app = EvolveApp(repo_root=history_repo)
    monkeypatch.setattr(app, "projects_payload", lambda: {"projects": [{"path": str(history_repo)}]})
    other = tmp_path / "other"
    other.mkdir()
    _write(other / "secret.py", "def secret():\n    return 1\n")
    _write(history_repo / "src" / "untracked.py", "def untracked():\n    return 1\n")

    with pytest.raises(PermissionError):
        app.symbols_payload(project_path=str(other), path="secret.py")
    with pytest.raises(PermissionError):
        app.relationships_payload(project_path=str(other), path="secret.py")
    untracked_symbols = app.symbols_payload(project_path=str(history_repo), path="src/untracked.py")
    untracked_relationships = app.relationships_payload(project_path=str(history_repo), path="src/untracked.py")

    assert untracked_symbols["health"]["status"] == "warning"
    assert untracked_relationships["relationships"]["quality"] == "warning"


def test_symbol_relationship_payloads_reject_tracked_symlink_escape(history_repo: Path, tmp_path: Path):
    from riglib.evolve.web import EvolveApp

    outside = tmp_path / "outside.py"
    outside.write_text("def outside():\n    return 1\n", encoding="utf-8")
    link = history_repo / "src" / "outside_link.py"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _git(history_repo, "add", ".")
    _git(history_repo, "commit", "-qm", "track outside symlink")

    app = EvolveApp(repo_root=history_repo)
    symbols = app.symbols_payload(project_path=str(history_repo), path="src/outside_link.py")
    relationships = app.relationships_payload(project_path=str(history_repo), path="src/outside_link.py")

    assert symbols["health"]["status"] == "warning"
    assert relationships["relationships"]["quality"] == "warning"


def test_relationships_resolve_typescript_from_imports(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    _write(history_repo / "web" / "gamma.ts", "export function gamma() { return 4 }\n")
    _write(
        history_repo / "web" / "beta.ts",
        "import { gamma } from './gamma';\nexport function beta() { return gamma() }\n",
    )
    _git(history_repo, "add", ".")
    _git(history_repo, "commit", "-qm", "link beta to gamma")

    app = EvolveApp(repo_root=history_repo)
    beta = app.relationships_payload(project_path=str(history_repo), path="web/beta.ts")
    gamma = app.relationships_payload(project_path=str(history_repo), path="web/gamma.ts")

    assert "web/gamma.ts" in beta["relationships"]["uses"]
    assert "web/beta.ts" in gamma["relationships"]["used_by"]


def test_relationships_resolve_python_relative_imports(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    _write(history_repo / "pkg" / "gamma.py", "def gamma():\n    return 4\n")
    _write(history_repo / "pkg" / "sub" / "local.py", "def local():\n    return 5\n")
    _write(history_repo / "zzz" / "local.py", "def local():\n    return 6\n")
    _write(history_repo / "pkg" / "sub" / "alpha.py", "from ..gamma import gamma\n")
    _write(history_repo / "pkg" / "sub" / "beta.py", "from .local import local\n")
    _git(history_repo, "add", ".")
    _git(history_repo, "commit", "-qm", "link python relatives")

    app = EvolveApp(repo_root=history_repo)
    alpha = app.relationships_payload(project_path=str(history_repo), path="pkg/sub/alpha.py")
    beta = app.relationships_payload(project_path=str(history_repo), path="pkg/sub/beta.py")
    gamma = app.relationships_payload(project_path=str(history_repo), path="pkg/gamma.py")

    assert "pkg/gamma.py" in alpha["relationships"]["uses"]
    assert "pkg/sub/local.py" in beta["relationships"]["uses"]
    assert "zzz/local.py" not in beta["relationships"]["uses"]
    assert "pkg/sub/alpha.py" in gamma["relationships"]["used_by"]


def test_relative_import_resolution_prefers_exact_file_with_suffix():
    from riglib.evolve.web import _resolve_relative_import

    files = {"pkg/sub/foo.py", "pkg/sub/foo.py.ts", "pkg/sub/foo/index.py"}

    assert _resolve_relative_import("pkg/sub", "./foo.py", files) == "pkg/sub/foo.py"
    assert _resolve_relative_import("pkg/sub", "./foo", files) == "pkg/sub/foo.py"


def test_relationships_do_not_mark_shared_suffix_as_used_by(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    _write(history_repo / "app" / "common" / "utils.py", "def app_utils():\n    return 1\n")
    _write(history_repo / "vendor" / "common" / "utils.py", "def vendor_utils():\n    return 2\n")
    _write(history_repo / "app" / "consumer.py", "import vendor.common.utils\n")
    _write(history_repo / "app" / "ambiguous.py", "import utils\n")
    _git(history_repo, "add", ".")
    _git(history_repo, "commit", "-qm", "add duplicate utils modules")

    app = EvolveApp(repo_root=history_repo)
    app_utils = app.relationships_payload(project_path=str(history_repo), path="app/common/utils.py")
    vendor_utils = app.relationships_payload(project_path=str(history_repo), path="vendor/common/utils.py")
    ambiguous = app.relationships_payload(project_path=str(history_repo), path="app/ambiguous.py")

    assert "app/consumer.py" not in app_utils["relationships"]["used_by"]
    assert "app/consumer.py" in vendor_utils["relationships"]["used_by"]
    assert "app/common/utils.py" not in ambiguous["relationships"]["uses"]
    assert "vendor/common/utils.py" not in ambiguous["relationships"]["uses"]


def test_providers_payload_returns_normalized_payloads_and_cache_metadata(
    history_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from riglib.evolve.model import PROVIDER_SCHEMA
    from riglib.evolve.providers import sverklo
    from riglib.evolve.web import EvolveApp

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setattr(sverklo.shutil, "which", lambda name: None)

    first = EvolveApp(repo_root=history_repo).providers_payload(project_path=str(history_repo))
    second = EvolveApp(repo_root=history_repo).providers_payload(project_path=str(history_repo))

    assert first["schema"] == PROVIDER_SCHEMA
    assert first["project"]["path"] == str(history_repo)
    assert first["cache"]["root"].startswith(str(tmp_path / "xdg-cache"))

    first_by_source = {payload["source"]: payload for payload in first["providers"]}
    second_by_source = {payload["source"]: payload for payload in second["providers"]}

    assert {"git", "rig", "sverklo"} <= set(first_by_source)
    assert first_by_source["git"]["schema"] == PROVIDER_SCHEMA
    assert first_by_source["git"]["cache"]["status"] == "miss"
    assert first_by_source["git"]["cache"]["age_s"] is not None
    assert first_by_source["rig"]["status"] == "warning"
    assert first_by_source["sverklo"]["status"] == "error"
    assert first_by_source["sverklo"]["errors"] == [{"message": "sverklo CLI not found on PATH"}]
    assert all(payload["cache"]["status"] == "hit" for payload in second_by_source.values())


def test_providers_api_serves_normalized_payloads_fail_soft(
    history_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from riglib.evolve.providers import sverklo
    from riglib.evolve.web import EvolveApp

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setattr(sverklo.shutil, "which", lambda name: None)
    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode({"project": str(history_repo)})
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/providers?{query}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    by_source = {item["source"]: item for item in payload["providers"]}
    assert payload["project"]["path"] == str(history_repo)
    assert by_source["git"]["status"] == "ok"
    assert by_source["sverklo"]["status"] == "error"
    assert by_source["sverklo"]["cache"]["status"] == "miss"


def test_snapshot_api_accepts_period_and_selected_path(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode(
        {
            "path": str(history_repo),
            "bucket": "month",
            "period": "2026-01",
            "selected_path": "web/beta.ts",
        }
    )
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot?{query}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payload["requested"]["bucket_id"] == "2026-01"
    assert payload["selection"]["path"] == "web/beta.ts"
    assert payload["selection"]["missing"] is True


def test_snapshot_api_rejects_unlisted_project_path(history_repo: Path, tmp_path: Path):
    from riglib.evolve.web import EvolveApp

    outside = tmp_path / "outside"
    outside.mkdir()
    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode(
        {
            "path": str(outside),
            "bucket": "month",
            "period": "2026-01",
        }
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot?{query}", timeout=5)
        body = json.loads(exc.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert exc.value.code == 403
    assert body == {"error": "project not allowed"}


def test_snapshot_api_ignores_control_selected_path(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode(
        {
            "path": str(history_repo),
            "bucket": "month",
            "period": "2026-01",
            "selected_path": "\x00",
        }
    )
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot?{query}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payload["requested"]["selected_path"] is None
    assert payload["selection"]["path"] is None


def test_current_snapshot_api_ignores_control_selected_path(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode(
        {
            "path": str(history_repo),
            "bucket": "month",
            "selected_path": "\x00",
        }
    )
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot?{query}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payload["selection"]["path"] is None


def test_touches_api_rejects_unlisted_project_path(history_repo: Path, tmp_path: Path):
    from riglib.evolve.web import EvolveApp

    outside = tmp_path / "outside"
    outside.mkdir()
    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode({"project": str(outside), "path": "src/alpha.py", "bucket": "month"})
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/touches?{query}", timeout=5)
        body = json.loads(exc.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert exc.value.code == 403
    assert body == {"error": "project not allowed"}


def test_detail_apis_reject_unlisted_project_path(history_repo: Path, tmp_path: Path):
    from riglib.evolve.web import EvolveApp

    outside = tmp_path / "outside"
    outside.mkdir()
    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        for endpoint in ("symbols", "relationships"):
            query = urllib.parse.urlencode({"project": str(outside), "file": "src/alpha.py"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/{endpoint}?{query}", timeout=5)
            body = json.loads(exc.value.read().decode("utf-8"))
            assert exc.value.code == 403
            assert body == {"error": "project not allowed"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_providers_api_rejects_unlisted_project_path(history_repo: Path, tmp_path: Path):
    from riglib.evolve.web import EvolveApp

    outside = tmp_path / "outside"
    outside.mkdir()
    server = EvolveApp(repo_root=history_repo).make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    query = urllib.parse.urlencode({"project": str(outside)})
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/providers?{query}", timeout=5)
        body = json.loads(exc.value.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert exc.value.code == 403
    assert body == {"error": "project not allowed"}


def test_allowed_project_uses_last_successful_project_cache(history_repo: Path, tmp_path: Path, monkeypatch):
    from riglib.evolve.web import EvolveApp

    other = tmp_path / "other"
    _write(other / "src" / "other.py", "def other():\n    return 1\n")
    calls = 0

    def fake_discover(_root):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {"name": history_repo.name, "path": str(history_repo), "aliases": [], "sources": ["current"]},
                {"name": other.name, "path": str(other), "aliases": [], "sources": ["sverklo"]},
            ]
        raise RuntimeError("sverklo down")

    monkeypatch.setattr("riglib.evolve.projects.discover_projects", fake_discover)
    app = EvolveApp(repo_root=history_repo)

    app.projects_payload()
    payload = app.snapshot_payload(project_path=str(other))

    assert calls == 2
    assert payload["project"]["path"] == str(other.resolve())


def test_allowed_project_rejects_removed_project_after_successful_rediscovery(
    history_repo: Path, tmp_path: Path, monkeypatch
):
    from riglib.evolve.web import EvolveApp

    other = tmp_path / "other"
    _write(other / "src" / "other.py", "def other():\n    return 1\n")
    calls = 0

    def fake_discover(_root):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {"name": history_repo.name, "path": str(history_repo), "aliases": [], "sources": ["current"]},
                {"name": other.name, "path": str(other), "aliases": [], "sources": ["sverklo"]},
            ]
        return [{"name": history_repo.name, "path": str(history_repo), "aliases": [], "sources": ["current"]}]

    monkeypatch.setattr("riglib.evolve.projects.discover_projects", fake_discover)
    app = EvolveApp(repo_root=history_repo)

    app.projects_payload()
    with pytest.raises(PermissionError, match="project not allowed"):
        app.snapshot_payload(project_path=str(other))

    assert calls == 2


def test_allowed_project_rejects_removed_project_after_discovery_failure(
    history_repo: Path, tmp_path: Path, monkeypatch
):
    from riglib.evolve.web import EvolveApp

    other = tmp_path / "other"
    _write(other / "src" / "other.py", "def other():\n    return 1\n")
    calls = 0

    def fake_discover(_root):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {"name": history_repo.name, "path": str(history_repo), "aliases": [], "sources": ["current"]},
                {"name": other.name, "path": str(other), "aliases": [], "sources": ["sverklo"]},
            ]
        if calls == 2:
            return [{"name": history_repo.name, "path": str(history_repo), "aliases": [], "sources": ["current"]}]
        raise RuntimeError("sverklo down")

    monkeypatch.setattr("riglib.evolve.projects.discover_projects", fake_discover)
    app = EvolveApp(repo_root=history_repo)

    app.projects_payload()
    app.projects_payload()
    with pytest.raises(PermissionError, match="project not allowed"):
        app.snapshot_payload(project_path=str(other))

    assert calls == 3


def test_render_page_exposes_health_bucket_controls_and_treemap_probe_hooks(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    page = EvolveApp(repo_root=history_repo).render_page().decode()

    assert 'class="skip-link" href="#main"' in page
    assert '<main id="main" tabindex="-1">' in page
    assert 'data-testid="project-trigger"' in page
    assert 'data-testid="project-listbox"' in page
    assert '<input id="projects" name="project" type="hidden"' in page
    assert '<select id="projects"' not in page
    assert 'id="map" aria-label="Project code surface" tabindex="0"' in page
    assert 'id="reload" aria-label="Reload Snapshot"' in page
    assert f'id="projectTriggerLabel" class="projectTriggerLabel">{history_repo.name}</span>' in page
    assert 'data-testid="provider-health"' in page
    assert 'aria-live="polite"' in page
    assert 'id="loading" class="loadingOverlay visible"' in page
    assert "Loading project surface…" in page
    assert "function fetchJson" in page
    assert "if (!response.ok)" in page
    assert "function renderSnapshotError" in page
    assert "snapshotError" in page
    assert "providerSnapshot = {providers:[{source:'snapshot', status:'error'" in page
    assert "let snapshotRequestSeq = 0" in page
    assert "const requestSeq = ++snapshotRequestSeq" in page
    assert "requestSeq !== snapshotRequestSeq" in page
    assert "requestedProject !== projectValue()" in page
    assert "requestedPeriod !== selectedPeriodId" in page
    assert "requestedBucket !== bucket" in page
    assert "if (requestSeq === snapshotRequestSeq) hideLoading()" in page
    assert "async function highlightBars(path)" in page
    assert "const requestedBucket = bucket" in page
    assert "const requestedPath = path || ''" in page
    assert "fetchJson(BASE + '/api/touches?project='" in page
    assert "requestedPath !== activePath" in page
    assert 'data-provider="git"' in page
    assert "function loadProviders" in page
    assert "/api/providers?project=" in page
    assert "data-cache=" in page
    assert "data-error-count=" in page
    assert 'data-testid="bucket-controls"' in page
    assert 'data-bucket="day"' in page
    assert 'data-bucket="week"' in page
    assert 'data-bucket="month"' in page
    assert 'aria-label="Show Day Buckets"' in page
    assert 'id="currentSnapshot" aria-label="Show Current Snapshot"' in page
    assert 'data-testid="treemap-canvas"' in page
    assert 'data-probe="treemap-tile"' in page
    assert "role:'button'" in page
    assert "tabindex:'0'" in page
    assert "window.rigEvolveTreemapProbe" in page
    assert "hasMixedOrientation" in page
    assert "function handleTileKey" in page
    assert "let selectedPeriodId = null" in page
    assert "function selectHistogramPeriod" in page
    assert "function focusHistogramPeriod" in page
    assert "function handleHistogramKey" in page
    assert "function showCurrentSnapshot" in page
    show_current = page.split("function showCurrentSnapshot() {", 1)[1].split("function focusHistogramPeriod", 1)[0]
    assert "ghostSelection = null" not in show_current
    assert "'data-testid':'histogram-column'" in page
    assert "'data-period-id':b.id" in page
    assert "'aria-pressed': selectedPeriodId === b.id ? 'true' : 'false'" in page
    assert "bar.classList.toggle('touched'" in page
    assert "selectedPeriod" in page
    assert "&period=" in page
    assert "&selected_path=" in page
    assert "data.selection" in page
    assert "ghostSelection" in page
    assert "renderDetail(selected || ghostSelection)" in page
    assert "let selectionRequestSeq = 0" in page
    assert "selectionRequestSeq++" in page
    assert "const periodAtStart = selectedPeriodId" in page
    assert "requestSeq !== selectionRequestSeq" in page
    assert "selectedPeriodId !== periodAtStart" in page
    assert "if (selected && !selectedPeriodId) fetchSelectionDetails(selected)" in page
    assert "const previousProject = snapshot && snapshot.project && snapshot.project.path" in page
    assert "snapshot.project.path === previousProject" in page
    assert "projectPath = snapshot.project.path" in page
    assert "snapshot.project.path !== projectPath" in page
    assert "function bindPanZoom" in page
    assert "onwheel" in page
    assert "ev.ctrlKey" in page
    assert "Math.exp(clamped * 0.0025)" in page
    assert "panX * view.w" in page
    assert "pinchStart.center" in page
    assert "panStart = null;" in page
    assert "root.onkeydown" in page
    assert "ArrowLeft" in page
    assert "zoomPill" not in page
    assert "% zoom" not in page
    assert "onpointerdown" in page
    assert "previousWorld" in page
    assert "PAN_CLICK_SUPPRESS_MS" in page
    assert "lastPanAt" in page
    assert "selectedClass" in page
    assert "SYMBOL_ZOOM_THRESHOLD" in page
    assert "function renderSymbolOverlay" in page
    assert "function renderRelationshipArcs" in page
    assert "relationshipArc" in page
    assert "'data-testid':'relationship-arc'" in page
    assert "'data-relation':arc.relation" in page
    assert "'marker-end':marker" in page
    assert "rect.w * zoom" in page
    assert "baseFont / zoom" in page
    assert "function visibleLabelRect" in page
    assert "labelRect.w * zoom" in page
    assert "labelRect.w < r.w * 0.92" in page
    assert "minScreenW" in page
    assert "maxVisibleLines" in page
    assert "clean.length > chars * maxLineCount" in page
    assert "selectedSymbolError" in page
    assert "Symbol provider error:" in page
    assert "Open Current to inspect live symbols." in page
    assert "Open Current to inspect live relationships." in page
    assert "Open Current to inspect live details for this file." in page
    assert '<span class="muted">Kind</span>' not in page
    assert "rel.message" not in page
    assert "relationshipNote" in page
    assert "const missingState = selectedPeriodId ? 'Not present in selected period' : 'Not present in current snapshot'" in page
    assert "/api/symbols?project=" in page
    assert "/api/relationships?project=" in page
    assert "function renderProjectDropdown" in page
    assert "function setProjectValue" in page
    assert "const pathLabel = qs('.projectPath')" in page
    assert "pathLabel.textContent = project.path" in page
    assert "renderDetail(null)" in page
    assert "if (!selectedPeriodId) fetchSelectionDetails(node)" in page
    assert "'project-option-aliases'" in page
    assert "isWorktreeOption" in page
    assert "opt.textContent = p.name + aliases" not in page
    assert "opt.title = p.path;" not in page
    assert "sources: " not in page
    assert "prefers-reduced-motion" in page
    assert "requestAnimationFrame" in page


def test_snapshot_payload_uses_head_keyed_ttl_cache(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    app = EvolveApp(repo_root=history_repo)
    first = app.snapshot_payload(project_path=str(history_repo))
    second = app.snapshot_payload(project_path=str(history_repo))

    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"


def test_snapshot_cache_does_not_reuse_selection_metadata(history_repo: Path):
    from riglib.evolve import web

    web._SNAPSHOT_CACHE.clear()
    app = web.EvolveApp(repo_root=history_repo)

    first = app.snapshot_payload(project_path=str(history_repo), selected_path="src/alpha.py")
    second = app.snapshot_payload(project_path=str(history_repo), selected_path="web/beta.ts")

    assert first["selection"]["path"] == "src/alpha.py"
    assert second["cache"]["status"] == "hit"
    assert second["selection"]["path"] == "web/beta.ts"


def test_evolve_server_handles_requests_on_threads(history_repo: Path):
    import http.server

    from riglib.evolve.web import EvolveApp

    server = EvolveApp(repo_root=history_repo).make_server(0)
    try:
        assert isinstance(server, http.server.ThreadingHTTPServer)
        assert server.daemon_threads is True
    finally:
        server.server_close()


def test_tailnet_host_is_allowed_for_read_only_evolve():
    from riglib.evolve.web import is_allowed_host

    assert is_allowed_host({"Host": "127.0.0.1:8797"})
    assert is_allowed_host({"Host": "ultras-mbp.tailbfe8ea.ts.net"})
    assert not is_allowed_host({"Host": "evil.example.test"})
