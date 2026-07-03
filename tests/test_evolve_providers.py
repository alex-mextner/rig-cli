"""Provider/cache tests for the rig evolve backend foundation."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_provider_payload_serializes_spec_contract(tmp_path: Path):
    from riglib.evolve.model import PROVIDER_SCHEMA, ProviderPayload

    payload = ProviderPayload.ok(
        source="task",
        project_path=tmp_path,
        collected_at="2026-06-30T00:00:00Z",
        data={"events": [{"id": "task:1", "kind": "task.updated"}]},
        links=[{"href": "task://1", "label": "task 1"}],
        raw_ref="raw/task/1.json",
    )

    encoded = payload.to_dict()
    decoded = ProviderPayload.from_dict(encoded)

    assert encoded["schema"] == PROVIDER_SCHEMA
    assert encoded["source"] == "task"
    assert encoded["provider"] == "task"
    assert encoded["project_path"] == str(tmp_path.resolve())
    assert encoded["project"] == str(tmp_path.resolve())
    assert encoded["collected_at"] == "2026-06-30T00:00:00Z"
    assert encoded["generated_at"] == "2026-06-30T00:00:00Z"
    assert encoded["status"] == "ok"
    assert encoded["health"]["status"] == "ok"
    assert encoded["data"]["events"][0]["kind"] == "task.updated"
    assert encoded["links"][0]["href"] == "task://1"
    assert decoded.to_dict() == encoded


def test_provider_payload_records_errors_without_raising(tmp_path: Path):
    from riglib.evolve.model import ProviderPayload

    payload = ProviderPayload.error(
        source="sverklo",
        project_path=tmp_path,
        message="sverklo CLI not found on PATH",
        collected_at="2026-06-30T00:00:00Z",
    )

    encoded = payload.to_dict()

    assert encoded["status"] == "error"
    assert encoded["health"]["message"] == "sverklo CLI not found on PATH"
    assert encoded["errors"] == [{"message": "sverklo CLI not found on PATH"}]


def test_not_wired_provider_payloads_skip_seen_sources_and_keep_placeholder_contract(tmp_path: Path):
    from riglib.evolve.providers import PLANNED_PROVIDER_SOURCES, not_wired_provider_payloads

    payloads = not_wired_provider_payloads(tmp_path, {"task", "review"})
    by_source = {payload.source: payload for payload in payloads}

    assert set(by_source) == set(PLANNED_PROVIDER_SOURCES) - {"task", "review"}
    assert by_source["tg"].project_path == str(tmp_path.resolve())
    assert by_source["tg"].status == "not-wired"
    assert by_source["tg"].message == "Provider not wired yet."
    assert by_source["tg"].to_dict()["health"]["status"] == "not-wired"


def test_provider_cache_round_trip_hit_miss_and_invalidation(tmp_path: Path):
    from riglib.evolve.cache import ProviderCache, ProviderCacheKey
    from riglib.evolve.model import ProviderPayload

    project = tmp_path / "repo"
    project.mkdir()
    cache = ProviderCache(root=tmp_path / "cache")
    head_1 = ProviderCacheKey(project_path=project, version="head-1", provider="git")
    head_2 = ProviderCacheKey(project_path=project, version="head-2", provider="git")
    payload = ProviderPayload.ok(
        source="git",
        project_path=project,
        collected_at="2026-06-30T00:00:00Z",
        data={"head": "head-1"},
    )

    assert cache.get(head_1) is None
    cache.set(head_1, payload)

    cached = cache.get(head_1)
    assert cached is not None
    assert cached.data == {"head": "head-1"}
    assert cache.get(head_2) is None
    assert cache.invalidate(head_1) is True
    assert cache.get(head_1) is None
    assert cache.invalidate(head_1) is False


def test_git_provider_reports_missing_git_as_fail_soft_payload(tmp_path: Path, monkeypatch):
    from riglib.evolve.providers.git import GitProvider

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = GitProvider().collect(tmp_path)

    assert payload.source == "git"
    assert payload.status == "error"
    assert "git" in payload.errors[0]["message"]


def test_rig_provider_reports_rig_yaml_metadata(tmp_path: Path):
    from riglib.evolve.providers.rig import RigProvider

    (tmp_path / "rig.yaml").write_text("version: 1\nproject_tools:\n  enabled: true\n", encoding="utf-8")

    payload = RigProvider().collect(tmp_path)

    assert payload.status == "ok"
    assert payload.data["rig_yaml"]["present"] is True
    assert payload.data["project_tools"]["mentioned"] is True
    assert payload.data["project_tools"]["heuristic"] is True
    assert payload.raw_ref == str((tmp_path / "rig.yaml").resolve())


def test_rig_provider_heuristic_ignores_commented_project_tools(tmp_path: Path):
    from riglib.evolve.providers.rig import RigProvider

    (tmp_path / "rig.yaml").write_text("version: 1\n# project_tools:\n#   haft: true\n", encoding="utf-8")

    payload = RigProvider().collect(tmp_path)

    assert payload.status == "ok"
    assert payload.data["project_tools"]["mentioned"] is False
    assert payload.data["project_tools"]["haft"] is False


def test_sverklo_provider_missing_cli_is_fail_soft(tmp_path: Path, monkeypatch):
    from riglib.evolve.providers import sverklo

    monkeypatch.setattr(sverklo.shutil, "which", lambda name: None)

    payload = sverklo.SverkloProvider().collect(tmp_path)

    assert payload.source == "sverklo"
    assert payload.status == "error"
    assert payload.errors == [{"message": "sverklo CLI not found on PATH"}]


def test_collect_default_providers_does_not_raise_when_tools_fail(tmp_path: Path, monkeypatch):
    from riglib.evolve.providers import collect_default, sverklo

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sverklo.shutil, "which", lambda name: None)

    payloads = collect_default(tmp_path)
    by_source = {payload.source: payload for payload in payloads}

    assert {"git", "rig", "sverklo", "task", "tg", "review", "haft", "serena", "lsp", "tree-sitter"} <= set(
        by_source
    )
    assert by_source["git"].status == "error"
    assert by_source["sverklo"].status == "error"
    assert by_source["rig"].status in {"ok", "warning"}
    assert by_source["task"].status == "not-wired"
    assert by_source["task"].message == "Provider not wired yet."
