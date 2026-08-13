from pathlib import Path

import pytest

from riglib.linter_carriers import apply_bundle, read_source_text, resolve_bundle


def test_source_text_is_anchored_and_normalized(tmp_path: Path):
    root = tmp_path / "agent-tools"
    root.mkdir()
    (root / "preset.ts").write_bytes(b"export default {}\r\n")
    assert read_source_text(root, "preset.ts") == "export default {}\n"
    with pytest.raises(ValueError):
        read_source_text(root, "../outside")


def test_bundle_create_and_exact_noop(tmp_path: Path):
    source_root = tmp_path / "agent-tools"
    source = source_root / "vendor" / "plugin"
    source.mkdir(parents=True)
    (source / "index.ts").write_text("export {}\n")
    (source / "rules").mkdir()
    (source / "rules" / "a.ts").write_bytes(b"rule\x00bytes")
    repo = tmp_path / "repo"
    repo.mkdir()

    out = apply_bundle(source_root, repo, "vendor/plugin", "tools/plugin", "backup")
    assert out.status == "created"
    assert (repo / "tools/plugin/rules/a.ts").read_bytes() == b"rule\x00bytes"
    assert resolve_bundle(source_root, repo, "vendor/plugin", "tools/plugin").state == "ok"
    assert apply_bundle(source_root, repo, "vendor/plugin", "tools/plugin", "backup").status == "skipped"


def test_extra_target_file_is_drift_and_overwrite_removes_it(tmp_path: Path):
    source_root = tmp_path / "agent-tools"
    source = source_root / "bundle"
    source.mkdir(parents=True)
    (source / "index.ts").write_text("x\n")
    repo = tmp_path / "repo"
    target = repo / "tools/bundle"
    target.mkdir(parents=True)
    (target / "index.ts").write_text("x\n")
    (target / "stale.ts").write_text("stale\n")

    assert resolve_bundle(source_root, repo, "bundle", "tools/bundle").state == "update"
    out = apply_bundle(source_root, repo, "bundle", "tools/bundle", "overwrite")
    assert out.status == "updated"
    assert not (target / "stale.ts").exists()


def test_backup_preserves_previous_tree(tmp_path: Path):
    source_root = tmp_path / "agent-tools"
    source = source_root / "bundle"
    source.mkdir(parents=True)
    (source / "index.ts").write_text("new\n")
    repo = tmp_path / "repo"
    target = repo / "bundle"
    target.mkdir(parents=True)
    (target / "index.ts").write_text("old\n")

    out = apply_bundle(source_root, repo, "bundle", "bundle", "backup")
    assert out.status == "backed_up"
    assert out.backup is not None
    assert (out.backup / "index.ts").read_text() == "old\n"
    assert (target / "index.ts").read_text() == "new\n"


def test_source_symlink_is_rejected(tmp_path: Path):
    source_root = tmp_path / "agent-tools"
    source = source_root / "bundle"
    source.mkdir(parents=True)
    outside = tmp_path / "outside.ts"
    outside.write_text("x")
    (source / "link.ts").symlink_to(outside)
    repo = tmp_path / "repo"
    repo.mkdir()
    r = resolve_bundle(source_root, repo, "bundle", "bundle")
    assert r.state == "io_error"
    assert "symlink" in r.detail


def test_missing_uninitialized_subrepo_fails_closed(tmp_path: Path):
    source_root = tmp_path / "agent-tools"
    source_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    r = resolve_bundle(
        source_root,
        repo,
        "vendor/anti-slop/skills/install-anti-slop/assets/anti-slop",
        "tools/oxlint/anti-slop",
    )
    assert r.state == "io_error"
    assert "does not exist" in r.detail
