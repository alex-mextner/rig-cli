"""`gh ship` delegator provisioning — the ``ship_delegator`` block.

The DURABLE fix for "gh ship must work in every repo": rig provisions the per-repo
``.claude/scripts/pr-ship.sh`` thin delegator (which the repo-keyed ``gh ship`` alias execs)
into every managed repo, and ignores it in ``.git/info/exclude`` so it never dirties the
worktree (ship refuses a dirty tree). Without this, the delegator existed ONLY in agent-tools
and ``gh ship`` failed everywhere else — papered over by a runtime alias fallback.

Covers: the deterministic delegator content, default-ON + opt-out plan gating, config
validation, the idempotent file write, the ``.git/info/exclude`` ignore reconcile (incl. git
worktrees where ``info/exclude`` lives in the common gitdir), and drift parity (apply and drift
read the SAME ``resolve_ship_delegator`` state, so status never misreports the on-disk state).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from riglib.actions.runner import (
    _do_provision_ship_delegator,
    resolve_ship_delegator,
    ship_delegator_content,
    run_plan,
)
from riglib.config import ConfigError, validate
from riglib.drift import detect
from riglib.plan import Action, InstallPlan, build


# ── helpers ──────────────────────────────────────────────────────────────────────
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _canonical_ship(tmp_path: Path) -> Path:
    src = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("#!/usr/bin/env bash\necho ship\n", encoding="utf-8")
    return src


def _action(repo: Path, canonical: Path) -> Action:
    return Action(
        kind="provision_ship_delegator",
        category="ship_delegator",
        item="delegator",
        source=repo,
        target=repo,
        options={"canonical_ship": str(canonical)},
    )


def _apply(repo: Path, canonical: Path, on_conflict: str = "backup"):
    return _do_provision_ship_delegator(_action(repo, canonical), on_conflict)


def _plan_with_action(repo: Path, canonical: Path) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo, canonical))
    return plan


def _delegator_path(repo: Path) -> Path:
    return repo / ".claude" / "scripts" / "pr-ship.sh"


def _exclude_path(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def _loaded(cfg: dict, repo: Path):
    from riglib.config import LoadedConfig

    validate(cfg)
    return LoadedConfig(data=cfg, repo_root=repo)


# ── delegator content ──────────────────────────────────────────────────────────────
def test_content_is_executable_bash_that_delegates(tmp_path):
    canonical = _canonical_ship(tmp_path)
    text = ship_delegator_content(canonical)
    assert text.startswith("#!/usr/bin/env bash\n")
    # must reference ci/ship/ship.sh (both the repo-local branch and the AGENT_TOOLS_ROOT fallback)
    assert "ci/ship/ship.sh" in text
    # must export AGENT_TOOLS_ROOT with the baked root as default (not the full path to ship.sh)
    agent_tools_root = str(canonical.parent.parent.parent)
    assert "AGENT_TOOLS_ROOT" in text
    assert agent_tools_root in text
    assert str(canonical) not in text  # full path to ship.sh must NOT be baked — root only
    assert 'exec' in text


def test_content_is_deterministic_for_a_canonical_path(tmp_path):
    canonical = _canonical_ship(tmp_path)
    assert ship_delegator_content(canonical) == ship_delegator_content(canonical)


def test_content_shell_quotes_a_dangerous_canonical_path():
    # agent_tools_source derives from user-controlled config; a path with $(...) / backticks /
    # quotes must be INERT in the generated script, never executed when `gh ship` runs.
    evil = Path('/tmp/$(touch /tmp/pwned)/`id`/ci/ship/ship.sh')
    text = ship_delegator_content(evil)
    # the raw injection must NOT appear unquoted — neither as an assignment nor as a bare path
    assert "AGENT_TOOLS_ROOT=$(touch" not in text
    assert "AGENT_TOOLS_ROOT=/tmp/$(touch" not in text
    # the agent-tools ROOT (parent.parent.parent of ship.sh) must be present, single-quoted
    assert "'/tmp/$(touch /tmp/pwned)/`id`'" in text
    # the full ship.sh path must NOT be baked (only the root is)
    assert "'/tmp/$(touch /tmp/pwned)/`id`/ci/ship/ship.sh'" not in text
    # the resulting script is valid bash (parses without executing the injection)
    import subprocess as _sp

    rc = _sp.run(["bash", "-n", "-c", text], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_delegator_actually_execs_canonical_ship(tmp_path):
    # END-TO-END: run the GENERATED pr-ship.sh and verify it execs the canonical ship.sh, forwarding
    # args. The canonical writes its args to a file so we can prove delegation happened.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho "ship-ran $*" > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(canonical), encoding="utf-8")
    script.chmod(0o755)
    # run from a NON-git dir so the repo-local branch is skipped and the canonical path is used
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script), "42", "--dry-run"], cwd=rundir, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ship-ran 42 --dry-run"


def test_delegator_prefers_repo_local_ship_over_canonical(tmp_path):
    # the PRIMARY branch: a repo-local ci/ship/ship.sh must win over the rig-baked canonical (this is
    # how agent-tools self-hosts). Prove it execs the repo-local, NOT the canonical.
    import subprocess as _sp

    canonical_marker = tmp_path / "canon_ran.txt"
    local_marker = tmp_path / "local_ran.txt"
    canonical = tmp_path / "agent-tools" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho x > {canonical_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    # a git repo that CARRIES its own ci/ship/ship.sh
    repo = _git_repo(tmp_path / "repo")
    repo_ship = repo / "ci" / "ship" / "ship.sh"
    repo_ship.parent.mkdir(parents=True, exist_ok=True)
    repo_ship.write_text(f'#!/usr/bin/env bash\necho local > {local_marker}\n', encoding="utf-8")
    repo_ship.chmod(0o755)
    script = repo / "pr-ship.sh"
    script.write_text(ship_delegator_content(canonical), encoding="utf-8")
    script.chmod(0o755)
    # run from INSIDE the repo → git rev-parse finds the toplevel → repo-local ship wins
    res = _sp.run([str(script)], cwd=repo, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert local_marker.exists() and not canonical_marker.exists(), "repo-local ship must shadow canonical"


def test_delegator_handles_canonical_path_with_spaces(tmp_path):
    # the common real-world case: agent_tools_source under a dir with a space. shlex.quote handles
    # it; prove the round-trip (path → script → bash exec) finds + runs the canonical.
    out_marker = tmp_path / "ran.txt"
    canonical = tmp_path / "agent tools dir" / "ci" / "ship" / "ship.sh"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(f'#!/usr/bin/env bash\necho ok > {out_marker}\n', encoding="utf-8")
    canonical.chmod(0o755)
    script = tmp_path / "pr-ship.sh"
    script.write_text(ship_delegator_content(canonical), encoding="utf-8")
    script.chmod(0o755)
    import subprocess as _sp

    rundir = tmp_path / "run"
    rundir.mkdir()
    res = _sp.run([str(script)], cwd=rundir, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert out_marker.read_text(encoding="utf-8").strip() == "ok"


# ── create / idempotency ───────────────────────────────────────────────────────────
def test_create_writes_delegator_and_ignores_it(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert resolve_ship_delegator(repo, canonical).state == "create"

    res = _apply(repo, canonical)
    assert res.status == "created"

    deleg = _delegator_path(repo)
    assert deleg.is_file()
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content(canonical)
    # executable bit set
    assert deleg.stat().st_mode & 0o111
    # ignored in .git/info/exclude → never dirties the worktree
    excl = _exclude_path(repo).read_text(encoding="utf-8")
    assert ".claude/scripts/pr-ship.sh" in excl


def test_provisioned_file_does_not_dirty_the_worktree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # a committed file so the tree starts clean
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    # the provisioned delegator must NOT show up as untracked/modified
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


def test_idempotent_second_apply_skips(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    assert _apply(repo, canonical).status == "created"
    second = _apply(repo, canonical)
    assert resolve_ship_delegator(repo, canonical).state == "ok"
    assert second.status == "skipped"


def test_stale_delegator_is_updated(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    assert resolve_ship_delegator(repo, canonical).state == "update"
    res = _apply(repo, canonical)
    assert res.status in ("updated", "backed_up")
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content(canonical)


# ── CRLF / line-ending agreement between drift and reconcile ────────────────────────
def test_crlf_exclude_file_drift_and_reconcile_agree(tmp_path):
    # a .git/info/exclude with CRLF endings must not make drift ("ok") and reconcile ("rewrite")
    # disagree: both read RAW, so once provisioned a re-apply is a no-op and status stays clean.
    from riglib.actions.runner import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    # seed a CRLF user line, then provision
    with excl.open("w", encoding="utf-8", newline="") as fh:
        fh.write("*.log\r\n")
    _apply(repo, canonical)
    # provisioned: now in sync; a second apply is a true no-op (no rewrite churn)
    assert resolve_ship_delegator(repo, canonical).state == "ok"
    before = excl.read_bytes()
    res2 = _apply(repo, canonical)
    assert res2.status == "skipped"
    assert excl.read_bytes() == before  # byte-identical → drift & reconcile agreed
    # the rig block itself is present and the user CRLF line survived verbatim
    assert b"*.log\r\n" in excl.read_bytes()
    assert ship_delegator_exclude_block_text() in excl.read_text(encoding="utf-8")


def test_whitespace_only_exclude_content_is_preserved(tmp_path):
    # a whitespace-only exclude file must keep its blank lines (verbatim) when the block is appended.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("\n\n\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.startswith("\n\n\n")  # pre-existing blanks preserved
    assert ".claude/scripts/pr-ship.sh" in after


# ── non-git directory: file written, exclude step skipped, drift reports the file only ─
def test_non_git_dir_writes_file_skips_exclude(tmp_path):
    plain = tmp_path / "plain"  # NOT a git repo
    plain.mkdir()
    canonical = _canonical_ship(tmp_path)
    # drift: only the missing delegator file (no ignore concept without git)
    report = detect(_plan_with_action(plain, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert len(items) == 1 and items[0].direction == "missing" and items[0].item == "delegator"
    # apply writes the file; no exclude (no git), reported as a non-error
    res = _apply(plain, canonical)
    assert res.status in ("created", "updated")
    assert _delegator_path(plain).is_file()
    assert "no git repo" in res.detail
    # now in sync
    assert resolve_ship_delegator(plain, canonical).state == "ok"


def test_missing_canonical_ship_option_errors(tmp_path):
    # a malformed action with no canonical_ship must ERROR, not write a broken `canonical=` delegator.
    repo = _git_repo(tmp_path / "repo")
    action = Action(
        kind="provision_ship_delegator", category="ship_delegator", item="delegator",
        source=repo, target=repo, options={},  # no canonical_ship
    )
    res = _do_provision_ship_delegator(action, "backup")
    assert res.status == "error" and "canonical_ship" in res.detail
    assert not _delegator_path(repo).exists()


# ── git worktree: info/exclude lives in the COMMON gitdir ───────────────────────────
def test_ignore_resolves_through_git_worktree(tmp_path):
    main = _git_repo(tmp_path / "main")
    (main / "README.md").write_text("# m\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=main, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=main,
        check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feat"], cwd=main, check=True
    )
    canonical = _canonical_ship(tmp_path)
    res = _apply(wt, canonical)
    assert res.status == "created"
    assert _delegator_path(wt).is_file()
    # NON-circular proof: git itself must HONOR the written entry — ask git whether it ignores the
    # delegator (check-ignore exits 0 + names the rule's source file only when an exclude matched).
    ci = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-v", ".claude/scripts/pr-ship.sh"],
        capture_output=True, text=True,
    )
    assert ci.returncode == 0, f"git does not ignore the delegator in the worktree: {ci.stderr}"
    assert "info/exclude" in ci.stdout, ci.stdout
    # and the worktree stays clean (the user-facing invariant)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "", f"worktree dirtied: {status.stdout!r}"


# ── plan gating: default ON + opt-out ───────────────────────────────────────────────
def test_plan_includes_delegator_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(fake_agent_tools)}
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    acts = [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert len(acts) == 1
    assert acts[0].options.get("canonical_ship", "").endswith("ci/ship/ship.sh")


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "version": 1,
        "agent_tools_source": str(fake_agent_tools),
        "ship_delegator": {"enabled": False},
    }
    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]


def test_plan_skips_when_no_canonical_ship_in_checkout(fake_agent_tools, tmp_path):
    import shutil

    from riglib.catalog import Catalog

    # a checkout lacking ci/ship/ship.sh → no delegator action, with a note (fail-closed, no
    # broken delegator pointing at a non-existent canonical script). Work on a COPY of the fixture
    # tree so the unlink never mutates the shared fixture (it is function-scoped today, but a copy
    # keeps this test self-contained regardless of any future scope change).
    src = tmp_path / "at-copy"
    shutil.copytree(fake_agent_tools, src)
    (src / "ci" / "ship" / "ship.sh").unlink()
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"version": 1, "agent_tools_source": str(src)}
    cat = Catalog.scan(str(src))
    plan = build(_loaded(cfg, repo), cat, project_type="cli")
    assert not [a for a in plan.actions if a.kind == "provision_ship_delegator"]
    assert any("ship_delegator" in n for n in plan.notes)


# ── config validation ───────────────────────────────────────────────────────────────
def test_validate_rejects_non_mapping():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": "yes"})


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"nope": True}})


def test_validate_rejects_non_bool_enabled():
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": "yes"}})


def test_validate_accepts_enabled_bool():
    validate({"version": 1, "ship_delegator": {"enabled": False}})


def test_validate_rejects_top_level_null_block():
    # `ship_delegator: ~` at the top level → None, which is not a mapping → ConfigError (consistent
    # with the non-mapping guard; an absent KEY is the way to take the default, not an explicit null).
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": None})


# ── drift parity ────────────────────────────────────────────────────────────────────
def test_drift_missing_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


def test_drift_clean_after_apply(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    report = detect(_plan_with_action(repo, canonical))
    assert not [i for i in report.items if i.category == "ship_delegator"]


def test_drift_modified_delegator(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


def test_drift_missing_ignore_entry(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    # remove the exclude entry → drift should flag the unignored delegator
    _exclude_path(repo).write_text("", encoding="utf-8")
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


# ── io_error: a directory / unreadable file at the delegator path ───────────────────
def test_io_error_directory_at_delegator_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    # a DIRECTORY where the delegator file should be → io_error (apply errors, drift modified).
    deleg = _delegator_path(repo)
    deleg.mkdir(parents=True)
    assert resolve_ship_delegator(repo, canonical).state == "io_error"
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "not a regular file" in res.detail
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "modified"


# ── exclude entry handling: unbalanced markers, multi-pair collapse, no spurious backup ─
def test_unbalanced_exclude_marker_is_not_treated_as_present(tmp_path):
    # a begin marker with NO end is malformed; _reconcile_ship_exclude refuses to touch it, so it
    # must report as drift (NOT a false "ok"), else status reads clean while the file is broken.
    from riglib.actions.runner import SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)  # provision a correct delegator first
    # now corrupt the exclude: a lone begin marker (no end)
    _exclude_path(repo).write_text(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER + "\n", encoding="utf-8")
    r = resolve_ship_delegator(repo, canonical)
    assert r.state == "update" and not r.exclude_ok
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    assert items and items[0].direction == "missing"


def test_duplicate_exclude_blocks_collapse_to_one(tmp_path):
    from riglib.actions.runner import ship_delegator_exclude_block_text

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    block = ship_delegator_exclude_block_text()
    excl = _exclude_path(repo)
    # a hand-added SECOND identical block + a user line between them
    excl.write_text(f"{block}\nkeep-me.txt\n{block}\n", encoding="utf-8")
    # re-apply collapses to exactly one block, preserving the user line
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count("# >>> rig-managed ship delegator (do not edit) >>>") == 1
    assert "keep-me.txt" in after
    # and the resolution now reads clean (balanced, single block)
    assert resolve_ship_delegator(repo, canonical).state == "ok"


def test_mixed_correct_and_wrong_duplicate_blocks_collapse_to_one(tmp_path):
    # two managed blocks where ONE is correct and the other has a wrong body, with user content
    # before/between/after. Reconcile must collapse to exactly one CANONICAL block and preserve every
    # user line verbatim (the splice cursor/newline logic is where an off-by-one would land).
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    good = ship_delegator_exclude_block_text()
    wrong = f"{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# WRONG\n/nope\n{SHIP_DELEGATOR_EXCLUDE_END_MARKER}"
    excl.write_text(f"# head\n{good}\nmid-user-line\n{wrong}\n# tail\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert after.count(SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER) == 1  # collapsed to one block
    assert "/nope" not in after and "# WRONG" not in after  # the wrong block is gone
    assert good in after  # the surviving block is canonical
    for line in ("# head", "mid-user-line", "# tail"):
        assert line in after, f"user line {line!r} not preserved"
    assert resolve_ship_delegator(repo, canonical).state == "ok"


def test_exclude_write_is_atomic_no_partial_on_failure(tmp_path, monkeypatch):
    # if the atomic rename fails mid-reconcile, the pre-existing user content must survive intact
    # (no truncated/empty file) — the whole point of the temp-file + os.replace dance.
    import riglib.actions.runner as runner

    repo = _git_repo(tmp_path / "repo")
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# precious user content\n*.secret\n", encoding="utf-8")
    original = excl.read_text(encoding="utf-8")

    real_replace = os.replace

    def _boom(src, dst, *a, **k):
        if str(dst) == str(excl):
            raise OSError("simulated rename failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(runner.os, "replace", _boom)
    ok, note = runner._reconcile_ship_exclude(excl)
    assert ok is False
    # the original content is untouched (no partial write clobbered it)
    assert excl.read_text(encoding="utf-8") == original
    # no leftover temp files
    assert list(excl.parent.glob("*.rig-tmp")) == []


def test_single_block_with_wrong_body_is_reconciled(tmp_path):
    # a single managed block whose BODY was hand-edited (wrong comment) must be detected as drift and
    # rewritten in place to the canonical block (the common real-world drift, distinct from dupes).
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ship_delegator_exclude_block_text,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    # tamper the block body (a wrong comment + wrong path inside correct, balanced markers)
    excl.write_text(
        f"# top\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n# tampered\n/wrong/path\n"
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n# bottom\n",
        encoding="utf-8",
    )
    assert not resolve_ship_delegator(repo, canonical).exclude_ok  # drift
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert ship_delegator_exclude_block_text() in after  # canonical block restored
    assert "/wrong/path" not in after  # tampered body gone
    assert "# top" in after and "# bottom" in after  # surrounding user content preserved
    assert resolve_ship_delegator(repo, canonical).state == "ok"


def test_unreadable_delegator_file_is_io_error(tmp_path):
    # a delegator file rig cannot READ (mode 000) → io_error (apply errors, drift modified). Skipped
    # when running as root (root bypasses the permission bit, so the read would succeed).
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; the unreadable-file branch can't be exercised")
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# x\n", encoding="utf-8")
    deleg.chmod(0o000)
    try:
        assert resolve_ship_delegator(repo, canonical).state == "io_error"
        res = _apply(repo, canonical)
        assert res.status == "error"
        report = detect(_plan_with_action(repo, canonical))
        items = [i for i in report.items if i.category == "ship_delegator"]
        assert items and items[0].direction == "modified"
    finally:
        deleg.chmod(0o644)  # so tmp cleanup can remove it


def test_no_spurious_backup_when_only_exclude_entry_is_missing(tmp_path):
    # the delegator FILE is already correct; only the exclude entry is gone. apply must NOT rewrite
    # the (correct) file and must NOT create a *.rig-bak-* backup of it — only add the exclude.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry, keep the file
    r = resolve_ship_delegator(repo, canonical)
    assert r.state == "update" and r.file_correct and not r.exclude_ok
    res = _apply(repo, canonical)
    assert res.status == "updated" and res.backup is None
    # no backup file was created next to the delegator
    backups = list(_delegator_path(repo).parent.glob("*.rig-bak-*"))
    assert backups == []
    # the exclude entry is back; now in sync
    assert resolve_ship_delegator(repo, canonical).state == "ok"


def test_skip_conflict_still_ignores_the_left_file_and_keeps_its_mode(tmp_path):
    # on_conflict=skip leaves a hand-edited delegator AS-IS (content AND mode untouched), but the
    # rig-owned exclude is still reconciled so the left file is ignored (status won't re-flag a
    # non-ignored delegator).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# hand-edited, keep me\n", encoding="utf-8")
    deleg.chmod(0o644)  # NON-executable on purpose
    res = _apply(repo, canonical, on_conflict="skip")
    assert res.status == "skipped"
    # file content untouched
    assert "hand-edited" in deleg.read_text(encoding="utf-8")
    # mode untouched — rig must NOT chmod +x a user file it declined to write
    assert deleg.stat().st_mode & 0o111 == 0
    # but the exclude entry was added regardless
    assert ".claude/scripts/pr-ship.sh" in _exclude_path(repo).read_text(encoding="utf-8")


def test_overwrite_conflict_replaces_without_backup(tmp_path):
    # on_conflict=overwrite replaces a hand-edited delegator with the canonical one, NO backup kept.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    deleg = _delegator_path(repo)
    deleg.parent.mkdir(parents=True, exist_ok=True)
    deleg.write_text("#!/usr/bin/env bash\n# stale\n", encoding="utf-8")
    res = _apply(repo, canonical, on_conflict="overwrite")
    assert res.status == "updated" and res.backup is None
    assert deleg.read_text(encoding="utf-8") == ship_delegator_content(canonical)
    assert list(deleg.parent.glob("*.rig-bak-*")) == []
    assert resolve_ship_delegator(repo, canonical).state == "ok"


def test_apply_errors_when_exclude_unwritable(tmp_path, monkeypatch):
    # if .git/info/exclude cannot be written, the delegator is on disk but NOT ignored → the action
    # must report ERROR (not a misleading "created"), because the worktree would be dirtied.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)

    import riglib.actions.runner as runner

    monkeypatch.setattr(runner, "_reconcile_ship_exclude", lambda p: (False, f"could not write {p}: boom"))
    res = _apply(repo, canonical)
    assert res.status == "error"
    assert "NOT git-ignored" in res.detail


def test_drift_reports_both_modified_file_and_missing_ignore(tmp_path):
    # when BOTH the file content differs AND the ignore entry is gone, status must show BOTH issues.
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    _delegator_path(repo).write_text("#!/usr/bin/env bash\n# tampered\n", encoding="utf-8")
    _exclude_path(repo).write_text("", encoding="utf-8")  # drop the ignore entry too
    report = detect(_plan_with_action(repo, canonical))
    items = [i for i in report.items if i.category == "ship_delegator"]
    dirs = sorted(i.direction for i in items)
    assert dirs == ["missing", "modified"], f"expected both, got {[(i.direction, i.item) for i in items]}"


def test_exclude_preserves_user_content_around_the_block(tmp_path):
    # user lines BEFORE and AFTER the managed block must survive a reconcile (splice-cursor regression).
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    excl = _exclude_path(repo)
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text("# my own ignore\n*.log\n", encoding="utf-8")
    _apply(repo, canonical)
    after = excl.read_text(encoding="utf-8")
    assert "# my own ignore" in after and "*.log" in after
    assert ".claude/scripts/pr-ship.sh" in after
    # idempotent: a re-apply preserves it all and adds nothing
    _apply(repo, canonical)
    assert excl.read_text(encoding="utf-8") == after


# ── default-on validation: {} and {enabled: true} are accepted (the production defaults) ─
def test_validate_accepts_empty_block_and_enabled_true():
    validate({"version": 1, "ship_delegator": {}})
    validate({"version": 1, "ship_delegator": {"enabled": True}})
    validate({"version": 1})  # absent block → default ON


def test_validate_rejects_enabled_null():
    # YAML `enabled: ~` → None. We REJECT it (the JSON Schema declares boolean; null is invalid
    # there) — to take the default, OMIT the key rather than set it to null.
    with pytest.raises(ConfigError):
        validate({"version": 1, "ship_delegator": {"enabled": None}})


def test_misordered_exclude_markers_reported_as_drift_not_rewritten(tmp_path):
    # an end marker BEFORE a begin marker is misordered; reconcile refuses it and it reads as drift.
    from riglib.actions.runner import (
        SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
        SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        _reconcile_ship_exclude,
    )

    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    _apply(repo, canonical)
    excl = _exclude_path(repo)
    excl.write_text(
        f"{SHIP_DELEGATOR_EXCLUDE_END_MARKER}\n{SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER}\n", encoding="utf-8"
    )
    # not in sync (drift)
    r = resolve_ship_delegator(repo, canonical)
    assert not r.exclude_ok
    # reconcile refuses (ok=False) and leaves the file untouched
    before = excl.read_text(encoding="utf-8")
    ok, note = _reconcile_ship_exclude(excl)
    assert ok is False and "misordered" in note
    assert excl.read_text(encoding="utf-8") == before


# ── full plan round-trip via run_plan ───────────────────────────────────────────────
def test_run_plan_provisions_and_is_idempotent(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    canonical = _canonical_ship(tmp_path)
    plan = _plan_with_action(repo, canonical)
    plan.on_conflict = "backup"
    report = run_plan(plan)
    assert all(r.status != "error" for r in report.results)
    assert _delegator_path(repo).is_file()
    # second run is a no-op
    report2 = run_plan(plan)
    deleg = [r for r in report2.results if r.action.kind == "provision_ship_delegator"]
    assert deleg and deleg[0].status == "skipped"
