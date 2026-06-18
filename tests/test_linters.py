"""Per-repo linter/formatter config provisioning — the ``linters`` block.

CTO decision #4136.2: linter settings are provisioned by rig like every other reconciled area.
A config-driven ``linters.items`` map declares, per repo, the tool + repo-relative path + exact
content for each linter/formatter config file; ``rig init``/``apply`` writes/reconciles each file
(create when absent, repair when drifted, NEVER clobber a hand-written file without an
on_conflict-honoring backup), and ``rig status`` reports drift.

Covers: the idempotent file write (created/skipped-when-correct), the never-clobber backup +
skip + overwrite conflict policy, drift parity (apply and drift read the SAME
``resolve_linter_config`` state so status never misreports), config validation (fail-closed on a
malformed block / item, a path that escapes the repo, a bad role), io_error on a directory at the
path, malformed-action guards, default-ON + per-area / per-item opt-out plan gating, and a full
build → run → re-run round-trip under a tmp HOME.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib.actions.runner import (
    LinterConfigResolution,
    _do_provision_linter_config,
    resolve_linter_config,
    run_plan,
)
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build


# ── helpers ──────────────────────────────────────────────────────────────────────
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


_OXFMT_CONTENT = '{\n  "indentWidth": 2,\n  "lineWidth": 100\n}\n'
_RUFF_CONTENT = 'line-length = 100\n[lint]\nselect = ["E", "F", "I"]\n'


def _action(
    repo: Path,
    *,
    item: str = "oxfmt-format",
    tool: str = "oxfmt",
    role: str = "formatter",
    rel_path: str = ".oxfmtrc.jsonc",
    content: str = _OXFMT_CONTENT,
) -> Action:
    return Action(
        kind="provision_linter_config",
        category="linters",
        item=item,
        source=repo,
        target=repo,
        options={"tool": tool, "role": role, "rel_path": rel_path, "content": content},
    )


def _apply(action: Action, on_conflict: str = "backup"):
    return _do_provision_linter_config(action, on_conflict)


def _loaded(cfg: dict, repo: Path) -> LoadedConfig:
    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


def _config(**items) -> dict:
    """A minimal valid config whose ``linters.items`` is the given mapping."""
    return {"version": 1, "linters": {"enabled": True, "items": items}}


# ── apply: create + idempotency ────────────────────────────────────────────────────
def test_create_writes_file_with_exact_content(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = _apply(_action(repo))
    assert res.status == "created"
    target = repo / ".oxfmtrc.jsonc"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == _OXFMT_CONTENT


def test_role_is_rendered_in_apply_and_drift_labels(tmp_path):
    # `role` is reflected in output (not a recorded-but-unused field): a formatter reads as
    # "formatter <tool>:<item>", a linter as "linter <tool>:<item>".
    repo = _git_repo(tmp_path / "repo")
    res = _apply(_action(repo, role="formatter"))
    assert "formatter oxfmt:oxfmt-format" in res.detail
    res2 = _apply(_action(repo, item="rl", tool="ruff", role="linter", rel_path="ruff.toml", content=_RUFF_CONTENT))
    assert "linter ruff:rl" in res2.detail
    # drift renders it too (same label helper, so status and apply agree).
    plan = InstallPlan()
    plan.actions.append(_action(repo, item="missing", role="formatter", rel_path="nope.jsonc"))
    item = next(i for i in detect(plan).items if i.category == "linters")
    assert item.item == "formatter oxfmt:missing"


def test_second_apply_is_a_noop(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert _apply(_action(repo)).status == "created"
    res = _apply(_action(repo))
    assert res.status == "skipped"
    assert res.backup is None  # a correct file is a true no-op — no spurious .rig-bak


def test_nested_path_is_created(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = _apply(_action(repo, rel_path="config/ruff.toml", tool="ruff", role="linter", content=_RUFF_CONTENT))
    assert res.status == "created"
    assert (repo / "config" / "ruff.toml").read_text(encoding="utf-8") == _RUFF_CONTENT


# ── apply: drifted file gets repaired ──────────────────────────────────────────────
def test_stale_file_is_updated(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text("{ stale }\n", encoding="utf-8")
    res = _apply(_action(repo))
    # default on_conflict=backup → the prior hand-edited file is backed up, then rewritten.
    assert res.status == "backed_up"
    assert res.backup is not None and res.backup.exists()
    assert res.backup.read_text(encoding="utf-8") == "{ stale }\n"
    assert (repo / ".oxfmtrc.jsonc").read_text(encoding="utf-8") == _OXFMT_CONTENT


# ── never-clobber: conflict policy ─────────────────────────────────────────────────
def test_on_conflict_skip_leaves_hand_written_file(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text("{ mine }\n", encoding="utf-8")
    res = _apply(_action(repo), on_conflict="skip")
    assert res.status == "skipped"
    # the hand-written file is untouched, no backup created.
    assert (repo / ".oxfmtrc.jsonc").read_text(encoding="utf-8") == "{ mine }\n"
    assert res.backup is None


def test_on_conflict_overwrite_no_backup(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text("{ mine }\n", encoding="utf-8")
    res = _apply(_action(repo), on_conflict="overwrite")
    assert res.status == "updated"
    assert res.backup is None
    assert (repo / ".oxfmtrc.jsonc").read_text(encoding="utf-8") == _OXFMT_CONTENT


def test_correct_file_makes_no_backup_even_on_backup_policy(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text(_OXFMT_CONTENT, encoding="utf-8")
    res = _apply(_action(repo), on_conflict="backup")
    assert res.status == "skipped"
    assert res.backup is None
    # no spurious .rig-bak-* sibling
    assert not list(repo.glob(".oxfmtrc.jsonc.rig-bak-*"))


# ── io_error: directory / symlink / non-UTF-8 / unreadable at the path ──────────────
def test_directory_at_path_is_io_error(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").mkdir()
    res = _apply(_action(repo))
    assert res.status == "error"
    assert "not a regular file" in res.detail


def test_non_utf8_file_is_io_error_not_a_crash(tmp_path):
    # A binary / non-UTF-8 file at the path must classify as io_error (read_text raises
    # UnicodeDecodeError — a ValueError, NOT an OSError — so the resolver must catch both).
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
    assert r.state == "io_error"
    res = _apply(_action(repo))  # must NOT raise
    assert res.status == "error" and "cannot read" in res.detail


def test_symlink_at_path_is_io_error(tmp_path):
    # rig refuses to write THROUGH a symlink (could clobber a file outside the repo / a shared config).
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("external\n", encoding="utf-8")
    (repo / ".oxfmtrc.jsonc").symlink_to(outside)
    r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
    assert r.state == "io_error" and "symlink" in r.detail
    res = _apply(_action(repo))
    assert res.status == "error" and "symlink" in res.detail
    # the symlink target is untouched (rig never wrote through it).
    assert outside.read_text(encoding="utf-8") == "external\n"


def test_dangling_symlink_at_path_is_io_error(tmp_path):
    # A dangling link has exists()==False; without the is_symlink() guard it would read as `create`
    # and write_file would create the link's TARGET outside the tree. Refuse it.
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").symlink_to(tmp_path / "nonexistent-target")
    r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
    assert r.state == "io_error" and "symlink" in r.detail


def test_symlinked_parent_dir_does_not_escape_repo(tmp_path):
    # The dangerous case: a clean rel_path whose PARENT is a symlink out of the repo. The lexical
    # containment check can't see it (rel_path has no `..`/abs), the FINAL component isn't a symlink
    # (the file doesn't exist yet), but write_file would mkdir/write THROUGH the parent link. Refuse.
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "configs").symlink_to(outside)  # configs -> /tmp/.../outside
    r = resolve_linter_config(repo, "configs/ruff.toml", _RUFF_CONTENT)
    assert r.state == "io_error" and "symlink" in r.detail
    res = _apply(_action(repo, rel_path="configs/ruff.toml", tool="ruff", role="linter", content=_RUFF_CONTENT))
    assert res.status == "error" and "symlink" in res.detail
    # the outside dir got NO file written into it (rig refused to follow the parent link).
    assert not (outside / "ruff.toml").exists()
    assert list(outside.iterdir()) == []


def test_in_repo_symlink_leaf_is_refused(tmp_path):
    # A symlink whose target is INSIDE the repo must still be refused — resolving first would hide it
    # and rig would rewrite THROUGH the link, clobbering the real file. Walk lexically, don't resolve.
    repo = _git_repo(tmp_path / "repo")
    (repo / "real-config.jsonc").write_text("real\n", encoding="utf-8")
    (repo / ".oxfmtrc.jsonc").symlink_to(repo / "real-config.jsonc")
    r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
    assert r.state == "io_error" and "symlink" in r.detail
    res = _apply(_action(repo))
    assert res.status == "error" and "symlink" in res.detail
    # the link's real target inside the repo is untouched (no write-through).
    assert (repo / "real-config.jsonc").read_text(encoding="utf-8") == "real\n"


def test_regular_file_at_parent_component_is_io_error(tmp_path):
    # `config` is a regular FILE and path is `config/ruff.toml`: the leaf "doesn't exist" so a naive
    # classify says `create`, then write_file's mkdir(parents=True) raises a bare FileExistsError.
    # Classify it as io_error up front so status and apply agree.
    repo = _git_repo(tmp_path / "repo")
    (repo / "config").write_text("i am a file, not a dir\n", encoding="utf-8")
    r = resolve_linter_config(repo, "config/ruff.toml", _RUFF_CONTENT)
    assert r.state == "io_error" and "not a directory" in r.detail
    res = _apply(_action(repo, rel_path="config/ruff.toml", tool="ruff", role="linter", content=_RUFF_CONTENT))
    assert res.status == "error" and "not a directory" in res.detail
    # the file at the parent component is untouched (rig did not clobber it).
    assert (repo / "config").read_text(encoding="utf-8") == "i am a file, not a dir\n"


def test_unreadable_file_is_io_error(tmp_path):
    import os
    import stat

    repo = _git_repo(tmp_path / "repo")
    f = repo / ".oxfmtrc.jsonc"
    f.write_text("drifted\n", encoding="utf-8")
    os.chmod(f, 0)
    try:
        if os.access(f, os.R_OK):  # running as root bypasses perms — skip the assertion meaningfully
            pytest.skip("cannot make a file unreadable (running as root?)")
        r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
        assert r.state == "io_error" and "cannot read" in r.detail
    finally:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)


# ── containment: a path escaping the repo is rejected at every layer ────────────────
@pytest.mark.parametrize("bad", ["../escape.toml", "../../etc/x", "a/../../b", "/abs.toml"])
def test_runner_refuses_escaping_path(tmp_path, bad):
    # Defense-in-depth: even a hand-built Action that bypassed the validator must NOT write outside.
    repo = _git_repo(tmp_path / "repo")
    res = _apply(_action(repo, rel_path=bad))
    assert res.status == "error" and "escapes the repo" in res.detail


@pytest.mark.parametrize("bad", ["../escape.toml", "/abs.toml"])
def test_drift_flags_escaping_path(tmp_path, bad):
    repo = _git_repo(tmp_path / "repo")
    plan = InstallPlan()
    plan.actions.append(_action(repo, rel_path=bad))
    items = [i for i in detect(plan).items if i.category == "linters"]
    assert len(items) == 1 and "escapes the repo" in items[0].detail


def test_end_positioned_dotdot_rejected_by_validator():
    # `foo/..` resolves to the repo root's parent dir context — a `..` COMPONENT, must be rejected.
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": "foo/..", "content": "c"}))


def test_backslash_path_rejected_by_validator():
    # A Windows-style separator is ambiguous/unsafe across platforms — reject it.
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": "a\\b.toml", "content": "c"}))


def test_dot_path_rejected_by_validator_and_io_error_if_bypassed(tmp_path):
    # `path: "."` names the repo ROOT (`repo_root / "." == repo_root`, a dir) — it can never hold a
    # file. It is rejected AT validation (fail-closed-at-load, like `..`/abs), so a "valid config that
    # can never converge" never reaches apply. Defense-in-depth: a direct resolver/runner call that
    # bypasses validation still classifies it as io_error rather than writing the repo root.
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": ".", "content": "c\n"}))
    repo = _git_repo(tmp_path / "repo")
    assert resolve_linter_config(repo, ".", "c\n").state == "io_error"
    res = _apply(_action(repo, item="x", rel_path=".", content="c\n"))
    assert res.status == "error"


# ── line-ending normalization: CRLF vs LF is NOT drift (universal-newline read) ─────
def test_crlf_vs_lf_is_not_drift(tmp_path):
    # The resolver compares via read_text (universal-newline translation), matching fsutil.write_file
    # EXACTLY — so a CRLF-on-disk file vs LF-in-config is "ok", not drift, and rig won't rewrite it
    # solely to flip line endings. Lock this in: resolver and writer agree (no apply/status disagree).
    repo = _git_repo(tmp_path / "repo")
    crlf = _OXFMT_CONTENT.replace("\n", "\r\n")
    (repo / ".oxfmtrc.jsonc").write_text(crlf, encoding="utf-8", newline="")
    assert resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT).state == "ok"
    res = _apply(_action(repo))
    assert res.status == "skipped"
    # the on-disk CRLF bytes are left as-is (rig agrees they're equivalent; no churn).
    assert (repo / ".oxfmtrc.jsonc").read_bytes() == crlf.encode("utf-8")


def test_crlf_in_config_content_converges_no_perpetual_drift(tmp_path):
    # The inverse direction: a CRLF `content:` literal (pasted on Windows). Without normalizing the
    # DESIRED content, write writes CRLF, read_text normalizes to LF, and the compare never matches —
    # perpetual `update` (apply rewrites every run, status always drifts). The resolver normalizes
    # the desired content to LF, so a re-apply is a true no-op and rig writes an LF-only file.
    repo = _git_repo(tmp_path / "repo")
    crlf_config = _RUFF_CONTENT.replace("\n", "\r\n")
    res = _apply(_action(repo, item="r", tool="ruff", role="linter", rel_path="ruff.toml", content=crlf_config))
    assert res.status == "created"
    # rig writes LF bytes, NOT the CRLF it was handed.
    assert b"\r\n" not in (repo / "ruff.toml").read_bytes()
    # the SAME CRLF config now resolves to `ok` (converged) and a re-apply is a no-op.
    assert resolve_linter_config(repo, "ruff.toml", crlf_config).state == "ok"
    assert _apply(_action(repo, item="r", tool="ruff", role="linter", rel_path="ruff.toml", content=crlf_config)).status == "skipped"


def test_bare_cr_in_config_content_converges(tmp_path):
    # Old-Mac bare `\r` line endings are normalized too (the `.replace("\r", "\n")` leg): a `\r`-only
    # config literal must converge to `ok` and write LF, same as CRLF — no perpetual drift.
    repo = _git_repo(tmp_path / "repo")
    cr_config = _RUFF_CONTENT.replace("\n", "\r")
    res = _apply(_action(repo, item="r", tool="ruff", role="linter", rel_path="ruff.toml", content=cr_config))
    assert res.status == "created"
    assert b"\r" not in (repo / "ruff.toml").read_bytes()
    assert resolve_linter_config(repo, "ruff.toml", cr_config).state == "ok"


def test_genuine_content_difference_is_drift(tmp_path):
    # A real content difference (not just line endings) IS drift and gets reconciled.
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text('{ "indentWidth": 4 }\n', encoding="utf-8")
    assert resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT).state == "update"


# ── registry membership: the area is wired into every dispatch table ────────────────
def test_linters_is_wired_into_every_registry():
    from riglib.actions.runner import _HANDLERS
    from riglib.areas import AREAS
    from riglib.config import _VALID_TOP_KEYS
    from riglib.layers import REPO, layer_for_category

    assert "linters" in _VALID_TOP_KEYS
    assert "provision_linter_config" in _HANDLERS
    assert any(a.key == "linters" and a.layer == REPO for a in AREAS)
    assert layer_for_category("linters") == REPO


# ── malformed action guards ────────────────────────────────────────────────────────
def test_missing_rel_path_errors(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    bad = _action(repo)
    bad.options.pop("rel_path")
    res = _do_provision_linter_config(bad, "backup")
    assert res.status == "error"
    assert "malformed action" in res.detail


def test_missing_content_errors(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    bad = _action(repo)
    bad.options.pop("content")
    res = _do_provision_linter_config(bad, "backup")
    assert res.status == "error"


def test_empty_content_errors_in_runner_and_drift(tmp_path):
    # The validator requires non-empty content, so `content=""` only reaches the runner/drift via a
    # synthetic / replayed Action. Both guards must reject it (mirroring the plan builder) rather than
    # write the 0-byte file the malformed-action guard exists to prevent — and status must agree.
    repo = _git_repo(tmp_path / "repo")
    bad = _action(repo)
    bad.options["content"] = ""
    res = _do_provision_linter_config(bad, "backup")
    assert res.status == "error" and "malformed action" in res.detail
    # the file is NOT written (no 0-byte artifact left behind).
    assert not (repo / ".oxfmtrc.jsonc").exists()
    # drift flags the same broken action as `modified` rather than silently passing it as in-sync.
    plan = InstallPlan()
    plan.actions.append(bad)
    items = [i for i in detect(plan).items if i.category == "linters"]
    assert len(items) == 1 and items[0].direction == "modified"


# ── resolver classification ────────────────────────────────────────────────────────
def test_resolve_states(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    r = resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT)
    assert isinstance(r, LinterConfigResolution) and r.state == "create"
    (repo / ".oxfmtrc.jsonc").write_text(_OXFMT_CONTENT, encoding="utf-8")
    assert resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT).state == "ok"
    (repo / ".oxfmtrc.jsonc").write_text("drifted\n", encoding="utf-8")
    assert resolve_linter_config(repo, ".oxfmtrc.jsonc", _OXFMT_CONTENT).state == "update"


# ── config validation (fail-closed) ────────────────────────────────────────────────
def test_valid_config_accepted(tmp_path):
    cfg = _config(
        oxfmt={"tool": "oxfmt", "role": "formatter", "path": ".oxfmtrc.jsonc", "content": _OXFMT_CONTENT},
        ruff={"tool": "ruff", "path": "ruff.toml", "content": _RUFF_CONTENT},  # role omitted → default
    )
    validate(cfg)  # no raise


def test_non_mapping_block_rejected():
    with pytest.raises(ConfigError, match="linters must be a mapping"):
        validate({"version": 1, "linters": []})


def test_non_bool_enabled_rejected():
    with pytest.raises(ConfigError, match="linters.enabled must be a bool"):
        validate({"version": 1, "linters": {"enabled": "yes"}})


def test_unknown_top_key_rejected():
    with pytest.raises(ConfigError, match="unknown linters key"):
        validate({"version": 1, "linters": {"itmes": {}}})


def test_non_mapping_items_rejected():
    with pytest.raises(ConfigError, match="linters.items must be a mapping"):
        validate({"version": 1, "linters": {"items": []}})


def test_unknown_item_key_rejected():
    with pytest.raises(ConfigError, match="unknown linters.items.x key"):
        validate(_config(x={"tool": "t", "path": "p", "content": "c", "bogus": 1}))


@pytest.mark.parametrize("missing", ["tool", "path", "content"])
def test_required_string_missing_rejected(missing):
    spec = {"tool": "t", "path": "p", "content": "c"}
    del spec[missing]
    with pytest.raises(ConfigError, match=f"linters.items.x.{missing} must be a non-empty string"):
        validate(_config(x=spec))


@pytest.mark.parametrize("empty", ["tool", "path", "content"])
def test_required_string_empty_rejected(empty):
    spec = {"tool": "t", "path": "p", "content": "c"}
    spec[empty] = ""
    with pytest.raises(ConfigError, match=f"linters.items.x.{empty} must be a non-empty string"):
        validate(_config(x=spec))


def test_bad_role_rejected():
    with pytest.raises(ConfigError, match="linters.items.x.role must be one of"):
        validate(_config(x={"tool": "t", "path": "p", "content": "c", "role": "format"}))


@pytest.mark.parametrize("bad_role", [["linter"], {"a": 1}, 5, True])
def test_non_string_role_is_structured_error_not_crash(bad_role):
    # an unhashable role (list/dict) would raise a raw TypeError on `role not in <set>` without the
    # isinstance guard — assert it stays a clean ConfigError instead.
    with pytest.raises(ConfigError, match="linters.items.x.role must be one of"):
        validate(_config(x={"tool": "t", "path": "p", "content": "c", "role": bad_role}))


def test_non_bool_item_enabled_rejected():
    with pytest.raises(ConfigError, match="linters.items.x.enabled must be a bool"):
        validate(_config(x={"tool": "t", "path": "p", "content": "c", "enabled": "no"}))


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../escape", "a/../../b", "../../x"])
def test_path_escaping_repo_rejected(bad_path):
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": bad_path, "content": "c"}))


@pytest.mark.parametrize("win_abs", ["C:/tmp/ruff.toml", "C:\\tmp\\ruff.toml", "\\\\server\\share\\x"])
def test_windows_absolute_path_rejected(win_abs):
    # a Windows drive-absolute (forward- OR back-slashed) validates as "relative" under PurePosixPath
    # on POSIX but is absolute on Windows — reject it so a committed config can't escape cross-OS.
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": win_abs, "content": "c"}))


@pytest.mark.parametrize("git_path", [".git/config", ".git/hooks/pre-commit", "a/.git/x", ".GIT/config"])
def test_path_into_git_dir_rejected(git_path):
    # A path into .git would let a committed rig.yaml rewrite repo metadata or install a hook on
    # apply — a privilege-escalation footgun. Reject `.git` as any component (case-insensitive).
    with pytest.raises(ConfigError, match="must be a repo-relative path inside the repo"):
        validate(_config(x={"tool": "t", "path": git_path, "content": "c"}))


def test_runner_refuses_git_path(tmp_path):
    # Defense-in-depth: a hand-built Action targeting .git is refused at apply too (not just at load).
    repo = _git_repo(tmp_path / "repo")
    before = (repo / ".git" / "config").read_text(encoding="utf-8")
    res = _apply(_action(repo, rel_path=".git/config", content="[evil]\n"))
    assert res.status == "error" and "escapes the repo" in res.detail
    # the real .git/config is untouched (rig refused to write it).
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == before


def test_duplicate_enabled_target_path_rejected():
    # two enabled items provisioning the SAME file would make apply/status churn forever (each apply
    # rewrites + re-backs-up the loser). Reject the collision at load.
    with pytest.raises(ConfigError, match="already provisioned by linters.items"):
        validate(_config(
            a={"tool": "ruff", "path": "ruff.toml", "content": "x\n"},
            b={"tool": "ruff2", "path": "ruff.toml", "content": "y\n"},
        ))


def test_duplicate_path_normalized_is_rejected():
    # `./ruff.toml` and `ruff.toml` are the same file — normalize before comparing.
    with pytest.raises(ConfigError, match="already provisioned by linters.items"):
        validate(_config(
            a={"tool": "ruff", "path": "ruff.toml", "content": "x\n"},
            b={"tool": "ruff2", "path": "./ruff.toml", "content": "y\n"},
        ))


def test_duplicate_path_allowed_when_one_is_disabled():
    # a DISABLED item provisions nothing, so it can't collide — toggling one off resolves the clash.
    validate(_config(
        a={"tool": "ruff", "path": "ruff.toml", "content": "x\n"},
        b={"tool": "ruff2", "path": "ruff.toml", "content": "y\n", "enabled": False},
    ))  # no raise


@pytest.mark.parametrize("padded", [" config/ruff.toml", "config/ruff.toml ", " ../escape ", "\tx.toml"])
def test_whitespace_padded_path_rejected(padded):
    # The runner/drift do NOT strip the path, so a whitespace-padded value would validate as one
    # filename but apply/status would operate on the trimmed path — reject the ambiguity at load.
    with pytest.raises(ConfigError, match="must not have leading/trailing whitespace"):
        validate(_config(x={"tool": "t", "path": padded, "content": "c"}))


# ── plan gating: default ON, opt-out (area + item) ─────────────────────────────────
def _kinds(plan: InstallPlan) -> list[str]:
    return [a.kind for a in plan.actions if a.kind == "provision_linter_config"]


def _plan(cfg: dict, repo: Path) -> InstallPlan:
    from riglib.catalog import Catalog

    loaded = _loaded(cfg, repo)
    catalog = Catalog(source=repo)
    return build(loaded, catalog)


def test_plan_emits_one_action_per_enabled_item(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    cfg = _config(
        oxfmt={"tool": "oxfmt", "role": "formatter", "path": ".oxfmtrc.jsonc", "content": _OXFMT_CONTENT},
        ruff={"tool": "ruff", "path": "ruff.toml", "content": _RUFF_CONTENT},
    )
    actions = [a for a in _plan(cfg, repo).actions if a.kind == "provision_linter_config"]
    assert {a.item for a in actions} == {"oxfmt", "ruff"}
    by_item = {a.item: a for a in actions}
    assert by_item["oxfmt"].options["rel_path"] == ".oxfmtrc.jsonc"
    assert by_item["oxfmt"].options["content"] == _OXFMT_CONTENT


def test_area_opt_out_emits_nothing(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    cfg = {"version": 1, "linters": {"enabled": False, "items": {
        "x": {"tool": "t", "path": "p.toml", "content": "c"}}}}
    assert _kinds(_plan(cfg, repo)) == []


def test_item_opt_out_skips_that_item(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    cfg = _config(
        on={"tool": "t", "path": "on.toml", "content": "c"},
        off={"tool": "t", "path": "off.toml", "content": "c", "enabled": False},
    )
    items = {a.item for a in _plan(cfg, repo).actions if a.kind == "provision_linter_config"}
    assert items == {"on"}


def test_absent_block_emits_nothing(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert _kinds(_plan({"version": 1}, repo)) == []


# ── drift parity (apply and drift agree) ───────────────────────────────────────────
def test_drift_missing_then_clean_after_apply(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    rep = detect(plan)
    missing = [i for i in rep.items if i.category == "linters"]
    assert len(missing) == 1 and missing[0].direction == "missing"
    # apply, then drift must be clean.
    assert _apply(_action(repo)).status == "created"
    rep2 = detect(plan)
    assert [i for i in rep2.items if i.category == "linters"] == []


def test_drift_modified_when_bytes_differ(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").write_text("drifted\n", encoding="utf-8")
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    items = [i for i in detect(plan).items if i.category == "linters"]
    assert len(items) == 1 and items[0].direction == "modified"


def test_drift_io_error_when_directory_at_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".oxfmtrc.jsonc").mkdir()
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    items = [i for i in detect(plan).items if i.category == "linters"]
    assert len(items) == 1 and items[0].direction == "modified"
    assert "not a regular file" in items[0].detail


# ── full round-trip under a tmp HOME ───────────────────────────────────────────────
def test_full_roundtrip_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = _git_repo(tmp_path / "repo")
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    plan.actions.append(_action(repo, item="ruff", tool="ruff", role="linter", rel_path="ruff.toml", content=_RUFF_CONTENT))

    report = run_plan(plan)
    by_item = {r.action.item: r for r in report.results}
    assert by_item["oxfmt-format"].status == "created"
    assert by_item["ruff"].status == "created"
    assert (repo / ".oxfmtrc.jsonc").read_text(encoding="utf-8") == _OXFMT_CONTENT
    assert (repo / "ruff.toml").read_text(encoding="utf-8") == _RUFF_CONTENT

    # re-run: everything already correct → all skipped, no churn, no backups.
    report2 = run_plan(plan)
    assert all(r.status == "skipped" for r in report2.results)
    assert not list(repo.glob("*.rig-bak-*"))
    assert detect(plan).in_sync
