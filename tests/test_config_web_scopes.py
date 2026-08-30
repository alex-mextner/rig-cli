"""Tests for config-web's machine-wide scope discovery (riglib/config_web_scopes.py, rig-cli#310).

Covers: home repo always included; other rig-managed repos discovered from the (read-only)
repository registry; stale/no-rig.yaml entries excluded; the Global scope always present and
last; and resolve_scope() as the allowlist gate multi-repo endpoints must use.
"""

from __future__ import annotations

from pathlib import Path

from riglib import config_web_scopes as scopes_mod
from riglib.repository_registry import RegistryError, RepositoryEntry, RepositoryRegistry


def _write_registry(monkeypatch, tmp_path: Path, entries: list[RepositoryEntry]) -> Path:
    registry_file = tmp_path / "registry" / "repositories.json"
    registry = RepositoryRegistry(roots=[str(tmp_path)], repositories=entries)
    registry.save(registry_file)

    # Capture the REAL classmethod before patching, so the replacement doesn't recurse into
    # itself (RepositoryRegistry.load is looked up fresh on the class at call time).
    original_load = RepositoryRegistry.load

    def _load(path=None):
        return original_load(registry_file)

    monkeypatch.setattr(RepositoryRegistry, "load", staticmethod(_load))
    return registry_file


def _entry(path: Path, *, stale: bool = False) -> RepositoryEntry:
    return RepositoryEntry(
        id=f"id-{path.name}", path=str(path), name=path.name, root=str(path.parent), stale=stale
    )


def test_home_repo_always_included_even_without_rigyaml(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(monkeypatch, tmp_path, [])
    scopes = scopes_mod.discover_scopes(home)
    repo_scopes = [s for s in scopes if s.is_repo]
    assert len(repo_scopes) == 1
    assert repo_scopes[0].repo_root == home.resolve()


def test_global_scope_always_present_and_last(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(monkeypatch, tmp_path, [])
    scopes = scopes_mod.discover_scopes(home)
    assert scopes[-1].is_global
    assert scopes[-1].id == scopes_mod.GLOBAL_SCOPE_ID
    assert scopes[-1].repo_root is None


def test_other_rig_managed_repos_are_discovered(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "rig.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_registry(monkeypatch, tmp_path, [_entry(other)])
    scopes = scopes_mod.discover_scopes(home)
    repo_ids = {s.id for s in scopes if s.is_repo}
    assert str(other.resolve()) in repo_ids


def test_repo_without_committed_rigyaml_is_not_a_scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bare = tmp_path / "bare"
    bare.mkdir()  # no rig.yaml
    _write_registry(monkeypatch, tmp_path, [_entry(bare)])
    scopes = scopes_mod.discover_scopes(home)
    repo_ids = {s.id for s in scopes if s.is_repo}
    assert str(bare.resolve()) not in repo_ids


def test_stale_registry_entry_is_excluded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "rig.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_registry(monkeypatch, tmp_path, [_entry(gone, stale=True)])
    scopes = scopes_mod.discover_scopes(home)
    repo_ids = {s.id for s in scopes if s.is_repo}
    assert str(gone.resolve()) not in repo_ids


def test_home_repo_is_never_duplicated_when_also_in_registry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "rig.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_registry(monkeypatch, tmp_path, [_entry(home)])
    scopes = scopes_mod.discover_scopes(home)
    repo_ids = [s.id for s in scopes if s.is_repo]
    assert repo_ids.count(str(home.resolve())) == 1


def test_malformed_registry_falls_back_to_home_plus_global(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()

    def _raise(path=None):
        raise RegistryError("corrupt")

    monkeypatch.setattr(RepositoryRegistry, "load", staticmethod(_raise))
    scopes = scopes_mod.discover_scopes(home)
    assert [s.is_repo for s in scopes] == [True, False]
    assert scopes[-1].is_global


def test_resolve_scope_rejects_unknown_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(monkeypatch, tmp_path, [])
    scopes = scopes_mod.discover_scopes(home)
    assert scopes_mod.resolve_scope(scopes, "/not/a/real/scope") is None
    assert scopes_mod.resolve_scope(scopes, None) is None
    assert scopes_mod.resolve_scope(scopes, "") is None


def test_resolve_scope_accepts_a_discovered_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(monkeypatch, tmp_path, [])
    scopes = scopes_mod.discover_scopes(home)
    home_scope = scopes[0]
    resolved = scopes_mod.resolve_scope(scopes, home_scope.id)
    assert resolved is home_scope
    global_resolved = scopes_mod.resolve_scope(scopes, scopes_mod.GLOBAL_SCOPE_ID)
    assert global_resolved is not None and global_resolved.is_global


def test_default_scope_is_home_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(monkeypatch, tmp_path, [])
    scopes = scopes_mod.discover_scopes(home)
    assert scopes_mod.default_scope(scopes).repo_root == home.resolve()


def test_malformed_registry_entry_path_does_not_crash_discovery(tmp_path, monkeypatch):
    """A structurally-valid registry entry with a non-string `path` (e.g. hand-edited/corrupted
    JSON with `"path": null`) must be skipped, not crash the whole console -- RepositoryRegistry
    .load() only type-checks the tag arrays, not `path` itself (found in review).
    """
    home = tmp_path / "home"
    home.mkdir()
    good = tmp_path / "good"
    good.mkdir()
    (good / "rig.yaml").write_text("version: 1\n", encoding="utf-8")
    bad_entry = RepositoryEntry(id="id-bad", path=None, name="bad", root=str(tmp_path))  # type: ignore[arg-type]
    _write_registry(monkeypatch, tmp_path, [bad_entry, _entry(good)])

    scopes = scopes_mod.discover_scopes(home)  # must not raise

    repo_ids = {s.id for s in scopes if s.is_repo}
    assert str(good.resolve()) in repo_ids
