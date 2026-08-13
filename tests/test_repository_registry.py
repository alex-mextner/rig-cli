from __future__ import annotations

import json
from pathlib import Path

import pytest

from riglib import repository_registry as rr


def _repo(path: Path, *, remote: str = "", stack: str | None = None) -> Path:
    path.mkdir(parents=True)
    git = path / ".git"
    git.mkdir()
    config = ["[core]", "\trepositoryformatversion = 0"]
    if remote:
        config += ['[remote "origin"]', f"\turl = {remote}"]
    (git / "config").write_text("\n".join(config) + "\n", encoding="utf-8")
    if stack is not None:
        (path / "rig.yaml").write_text(f"version: 1\nstack: {stack}\n", encoding="utf-8")
    return path


def test_registry_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert rr.registry_path() == tmp_path / "cfg" / "rig" / "repositories.json"


def test_canonical_remote_unifies_common_spellings():
    expected = "github.com/acme/widget"
    assert rr._canonical_remote("git@github.com:Acme/Widget.git") == expected
    assert rr._canonical_remote("https://github.com/Acme/Widget.git") == expected
    assert rr._canonical_remote("ssh://git@github.com/Acme/Widget.git") == expected


def test_discovery_is_deterministic_and_does_not_descend_into_repo(tmp_path):
    root = tmp_path / "work"
    first = _repo(root / "a")
    second = _repo(root / "group" / "b")
    _repo(first / "vendor" / "nested")
    (root / "ignored" / "node_modules" / "pkg" / ".git").mkdir(parents=True)

    assert rr.discover_repository_paths([root]) == [first.resolve(), second.resolve()]


def test_discovery_does_not_follow_directory_symlinks(tmp_path):
    root = tmp_path / "work"
    outside = _repo(tmp_path / "outside")
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert rr.discover_repository_paths([root]) == []


def test_discovery_respects_max_depth(tmp_path):
    root = tmp_path / "work"
    shallow = _repo(root / "one")
    _repo(root / "a" / "b" / "deep")
    assert rr.discover_repository_paths([root], max_depth=1) == [shallow.resolve()]


def test_refresh_uses_remote_identity_and_preserves_metadata_across_move(tmp_path):
    root = tmp_path / "work"
    old = _repo(
        root / "old-name",
        remote="git@github.com:Acme/Widget.git",
        stack="frontend/ts/react",
    )
    reg = rr.RepositoryRegistry.empty().refresh([root])
    assert len(reg.repositories) == 1
    original = reg.repositories[0]
    reg.set_tags(original.id, ["production", "product-a"])
    original.last_status = "clean"
    original.policy_source = "global@abc123"

    moved = root / "new-name"
    old.rename(moved)
    reg.refresh([root])

    assert len(reg.repositories) == 1
    current = reg.repositories[0]
    assert current.id == original.id
    assert current.path == moved.resolve().as_posix()
    assert current.tags == ["product-a", "production"]
    assert current.last_status == "clean"
    assert current.policy_source == "global@abc123"
    assert current.stack == "frontend/ts/react"
    assert not current.stale


def test_refresh_surfaces_deleted_repository_as_stale(tmp_path):
    root = tmp_path / "work"
    repo = _repo(root / "gone", remote="https://github.com/acme/gone.git")
    reg = rr.RepositoryRegistry.empty().refresh([root])
    repo_id = reg.repositories[0].id

    for child in sorted(repo.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    repo.rmdir()

    reg.refresh([root])
    assert [(entry.id, entry.stale) for entry in reg.repositories] == [(repo_id, True)]


def test_refresh_fallback_path_id_is_deterministic_without_remote(tmp_path):
    root = tmp_path / "work"
    repo = _repo(root / "local")
    one = rr.RepositoryRegistry.empty().refresh([root]).repositories[0]
    two = rr.RepositoryRegistry.empty().refresh([root]).repositories[0]
    assert one.id == two.id
    assert one.id == rr._repo_identity(repo.resolve(), "")


def test_select_ors_within_dimension_ands_across_dimensions(tmp_path):
    root = tmp_path / "work"
    _repo(root / "web", remote="https://github.com/acme/web", stack="frontend/ts/react")
    _repo(root / "api", remote="https://github.com/acme/api", stack="backend/python")
    _repo(root / "ops", remote="https://github.com/acme/ops", stack="infra/terraform")
    reg = rr.RepositoryRegistry.empty().refresh([root])
    by_name = {entry.name: entry for entry in reg.repositories}
    reg.set_tags(by_name["web"].id, ["production", "customer-facing"])
    reg.set_tags(by_name["api"].id, ["production"])
    reg.set_tags(by_name["ops"].id, ["internal"])

    selected = reg.select(
        stacks=["frontend/ts/react", "backend/python"],
        tags=["production"],
    )
    assert [entry.name for entry in selected] == ["api", "web"]

    selected = reg.select(repos=[by_name["web"].id, "ops"], tags=["internal"])
    assert [entry.name for entry in selected] == ["ops"]


def test_select_matches_remote_and_root(tmp_path):
    work = tmp_path / "work"
    xp = tmp_path / "xp"
    _repo(work / "web", remote="git@github.com:Acme/Web.git")
    _repo(xp / "lab", remote="https://github.com/acme/lab.git")
    reg = rr.RepositoryRegistry.empty().refresh([work, xp])

    selected = reg.select(repos=["github.com/acme/web"], roots=[work])
    assert [entry.name for entry in selected] == ["web"]
    assert reg.select(repos=["github.com/acme/web"], roots=[xp]) == []


def test_stale_entries_hidden_by_default(tmp_path):
    entry = rr.RepositoryEntry(
        id="dead",
        path=(tmp_path / "gone").as_posix(),
        name="gone",
        root=tmp_path.as_posix(),
        stale=True,
    )
    reg = rr.RepositoryRegistry(repositories=[entry])
    assert reg.select() == []
    assert reg.select(include_stale=True) == [entry]


def test_save_load_roundtrip_and_atomic_tmp_cleanup(tmp_path):
    target = tmp_path / "registry.json"
    reg = rr.RepositoryRegistry(
        roots=["/work"],
        repositories=[
            rr.RepositoryEntry(
                id="abc",
                path="/work/app",
                name="app",
                root="/work",
                remote="https://github.com/acme/app.git",
                stack="backend/python",
                tags=["production"],
            )
        ],
    )
    assert reg.save(target) == target
    assert not target.with_name(".registry.json.tmp").exists()
    loaded = rr.RepositoryRegistry.load(target)
    assert loaded == reg


def test_load_ignores_derived_all_tags_field_for_forward_compatible_export(tmp_path):
    target = tmp_path / "registry.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "roots": [],
                "repositories": [
                    {
                        "id": "abc",
                        "path": "/x",
                        "name": "x",
                        "root": "/",
                        "tags": ["local"],
                        "committed_tags": ["committed"],
                        "all_tags": ["committed", "local"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = rr.RepositoryRegistry.load(target)
    assert loaded.repositories[0].all_tags == ["committed", "local"]


@pytest.mark.parametrize(
    "payload,match",
    [
        ([], "root must be an object"),
        ({"version": 99, "roots": [], "repositories": []}, "unsupported.*version"),
        ({"version": 1, "roots": "no", "repositories": []}, "roots must be a string array"),
        ({"version": 1, "roots": [], "repositories": {}}, "repositories must be an array"),
    ],
)
def test_load_rejects_malformed_registry(tmp_path, payload, match):
    target = tmp_path / "registry.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rr.RegistryError, match=match):
        rr.RepositoryRegistry.load(target)


def test_set_tags_normalizes_and_rejects_unknown_id(tmp_path):
    entry = rr.RepositoryEntry(id="abc", path="/x", name="x", root="/")
    reg = rr.RepositoryRegistry(repositories=[entry])
    reg.set_tags("abc", [" z ", "a", "a", ""])
    assert entry.tags == ["a", "z"]
    with pytest.raises(rr.RegistryError, match="unknown repository id"):
        reg.set_tags("missing", ["x"])


def test_discovery_never_writes_into_repository(tmp_path):
    root = tmp_path / "work"
    repo = _repo(root / "app", remote="https://github.com/acme/app.git", stack="backend/python")
    before = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }
    rr.RepositoryRegistry.empty().refresh([root])
    after = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_cli_discover_preview_does_not_write_registry(tmp_path, capsys):
    root = tmp_path / "work"
    _repo(root / "app", remote="https://github.com/acme/app.git")
    target = tmp_path / "registry.json"
    assert rr.main(["--registry", str(target), "discover", "--root", str(root)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["repositories"][0]["name"] == "app"
    assert not target.exists()


def test_cli_discover_write_then_list_json(tmp_path, capsys):
    root = tmp_path / "work"
    _repo(root / "app", remote="https://github.com/acme/app.git", stack="backend/python")
    target = tmp_path / "registry.json"
    assert (
        rr.main(
            [
                "--registry",
                str(target),
                "discover",
                "--root",
                str(root),
                "--write",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert target.exists()

    reg = rr.RepositoryRegistry.load(target)
    reg.set_tags(reg.repositories[0].id, ["production"])
    reg.save(target)
    assert (
        rr.main(
            [
                "--registry",
                str(target),
                "list",
                "--tag",
                "production",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in output] == ["app"]
