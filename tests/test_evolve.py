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


def test_render_page_exposes_health_bucket_controls_and_treemap_probe_hooks(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    page = EvolveApp(repo_root=history_repo).render_page().decode()

    assert 'class="skip-link" href="#main"' in page
    assert '<main id="main" tabindex="-1">' in page
    assert '<select id="projects" name="project" aria-label="Project">' in page
    assert 'id="reload" aria-label="Reload Snapshot"' in page
    assert 'data-testid="provider-health"' in page
    assert 'aria-live="polite"' in page
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
    assert 'data-testid="treemap-canvas"' in page
    assert 'data-probe="treemap-tile"' in page
    assert "role:'button'" in page
    assert "tabindex:'0'" in page
    assert "window.rigEvolveTreemapProbe" in page
    assert "hasMixedOrientation" in page
    assert "function handleTileKey" in page
    assert "function handleBarKey" in page
    assert "prefers-reduced-motion" in page
    assert "requestAnimationFrame" in page


def test_snapshot_payload_uses_head_keyed_ttl_cache(history_repo: Path):
    from riglib.evolve.web import EvolveApp

    app = EvolveApp(repo_root=history_repo)
    first = app.snapshot_payload(project_path=str(history_repo))
    second = app.snapshot_payload(project_path=str(history_repo))

    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"


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
