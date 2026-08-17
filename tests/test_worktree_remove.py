"""`rig worktree remove` — the inverse of `rig worktree create`.

Covers: happy path removes both the worktree dir and its branch; the branch to delete is read
from ``git worktree list --porcelain -z`` rather than assumed to equal ``name`` (proven via a
``create --branch`` override), correctly for a non-ASCII name and a name containing a literal
``"`` (both legal, both quoted specially by git's default porcelain format — ``-z`` sidesteps
that); a detached-HEAD worktree (no branch) is removed cleanly with no branch-delete attempted; a
STALE registration (worktree dir deleted by hand, not via git) is recovered rather than refused;
name validation and a symlinked ``.worktrees/`` are rejected the same way ``create`` rejects bad
input; a LOCKED worktree and a genuinely nonexistent target surface git's own refusal instead of
being force-removed or guessed at; a branch-lookup failure refuses the WHOLE removal (never
guesses "detached"); a `git branch -D` failure AFTER a successful worktree removal is reported
with the same "leaves the branch behind" reasoning `_partial_state_cleanup_hint` documents for
`create`; and the CLI wiring (`rig worktree remove`) end to end, including the missing-git/not-a-
repo diagnostics `create` already has.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from riglib import errors, worktree
from riglib.cli import main
from riglib.config import WORKTREES_DIR_NAME


# ── helpers (mirrors test_worktree_create.py) ────────────────────────────────────
def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return path


def _branch_exists(repo: Path, branch: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    return branch in res.stdout


# ── worktree.remove — happy path ─────────────────────────────────────────────────
def test_remove_deletes_worktree_and_branch(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")

    res = worktree.remove(repo, "agent-1")

    assert res.status == "removed"
    assert res.exit_code == 0
    assert res.path == repo / WORKTREES_DIR_NAME / "agent-1"
    assert res.branch == "agent-1"
    assert not res.path.exists()
    assert not _branch_exists(repo, "agent-1")


def test_remove_then_recreate_with_the_same_name_succeeds(tmp_path):
    """The actual user-facing contract every doc surface (README, module docstring, CLI
    description) justifies the branch-delete step with: skipping it makes a bare retry of
    `create` fail with "a branch already exists". Close that loop directly, not just by asserting
    the branch ref is gone."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")

    remove_res = worktree.remove(repo, "agent-1")
    assert remove_res.status == "removed"

    recreate_res = worktree.create(repo, "agent-1")

    assert recreate_res.status == "created"
    assert recreate_res.path == repo / WORKTREES_DIR_NAME / "agent-1"
    assert recreate_res.path.is_dir()


def test_remove_respects_branch_override(tmp_path):
    """The branch to delete is READ from git, not guessed as == name (`create --branch` diverges)."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1", branch="feature/custom")

    res = worktree.remove(repo, "agent-1")

    assert res.status == "removed"
    assert res.branch == "feature/custom"
    assert not (repo / WORKTREES_DIR_NAME / "agent-1").exists()
    assert not _branch_exists(repo, "feature/custom")


def test_remove_worktree_with_non_ascii_name(tmp_path):
    """`git worktree list --porcelain` (the default, newline-terminated format) C-quotes non-ASCII
    bytes (octal-escaped) — without `-z` on the lookup, the literal path parse would never match
    `target` and this would misreport "no branch found" while leaving the branch behind."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "café")

    res = worktree.remove(repo, "café")

    assert res.status == "removed"
    assert res.branch == "café"
    assert not (repo / WORKTREES_DIR_NAME / "café").exists()
    assert not _branch_exists(repo, "café")


def test_remove_worktree_with_double_quote_in_name(tmp_path):
    """A literal `"` is a legal worktree name (`_invalid_name_reason` only rejects `/`, `\\`,
    empty, `.`/`..`) AND a legal git branch-name character (`git check-ref-format --branch`
    confirms it) — the default porcelain format quotes it, which `-z` (not just `-c core.
    quotePath=false`) is needed to sidestep."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, 'a"b')

    res = worktree.remove(repo, 'a"b')

    assert res.status == "removed"
    assert res.branch == 'a"b'
    assert not (repo / WORKTREES_DIR_NAME / 'a"b').exists()
    assert not _branch_exists(repo, 'a"b')


def test_remove_finds_branch_despite_case_mismatch_on_case_insensitive_filesystem(tmp_path):
    """On a case-insensitive filesystem (macOS/Windows default), `git worktree list --porcelain`
    still reports the ORIGINAL case a worktree was created with — an exact string match can miss
    a same-file different-case lookup even though the filesystem treats them as one path. Without
    the case-insensitive fallback, this would silently report "no branch found — detached?" while
    the real branch survives, exactly the stranding bug the lookup exists to prevent."""
    repo = _git_repo(tmp_path / "repo")
    # Probe case-sensitivity DIRECTLY rather than inferring it from `remove()`'s own outcome —
    # inferring it from an error result would mask a genuine ci-fallback regression as a "skip".
    (tmp_path / "CaseProbe").write_text("", encoding="utf-8")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("filesystem is case-sensitive here — this scenario doesn't apply")

    worktree.create(repo, "Agent-1")

    res = worktree.remove(repo, "agent-1")  # different case than "Agent-1"

    assert res.status == "removed"
    assert res.branch == "Agent-1"
    assert not _branch_exists(repo, "Agent-1")


def test_remove_detached_worktree_has_no_branch_to_delete(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    target = repo / WORKTREES_DIR_NAME / "detached-1"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD"],
        check=True,
    )

    res = worktree.remove(repo, "detached-1")

    assert res.status == "removed"
    assert res.exit_code == 0
    assert res.branch is None
    assert not target.exists()
    assert "no branch found" in res.message


# ── name / target validation ──────────────────────────────────────────────────────
def test_remove_target_never_created_is_rejected_by_git_itself(tmp_path):
    """No `target.exists()` precheck (see `remove()`'s docstring) — a name that was never created
    at all still fails cleanly, via git's own "is not a working tree" refusal."""
    repo = _git_repo(tmp_path / "repo")

    res = worktree.remove(repo, "ghost")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "not a working tree" in res.message


def test_remove_recovers_a_stale_registration_after_manual_directory_deletion(tmp_path):
    """The recovery case this command exists for: the worktree DIRECTORY was deleted by hand
    (`rm -rf`, not `git worktree remove`), but git still has it REGISTERED with a live branch —
    `remove()` must still find and delete that branch, not refuse just because the directory is
    already gone."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"
    shutil.rmtree(target)
    assert not target.exists()
    assert _branch_exists(repo, "agent-1")  # still registered/branched despite the missing dir

    res = worktree.remove(repo, "agent-1")

    assert res.status == "removed"
    assert res.branch == "agent-1"
    assert not _branch_exists(repo, "agent-1")


def test_remove_name_with_path_separator_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")

    res = worktree.remove(repo, "a/b")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG


def test_remove_empty_name_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")

    res = worktree.remove(repo, "")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG


def test_remove_refuses_through_symlinked_worktrees_dir(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "agent-1").mkdir()
    (repo / WORKTREES_DIR_NAME).symlink_to(outside, target_is_directory=True)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "symlink" in res.message
    assert (outside / "agent-1").exists()  # untouched — refused before any git mutation


def test_remove_refuses_through_a_dangling_symlinked_worktrees_dir(tmp_path):
    """A DANGLING symlink (its target doesn't exist) makes `Path.exists()` return False too —
    without an `is_symlink()` check independent of `exists()`, that would read as "no .worktrees
    dir yet, nothing to escape through" and let the escape guard miss it entirely."""
    repo = _git_repo(tmp_path / "repo")
    (repo / WORKTREES_DIR_NAME).symlink_to(
        Path("/nonexistent-outside-target"), target_is_directory=True
    )
    assert not (repo / WORKTREES_DIR_NAME).exists()  # dangling: exists() is False
    assert (repo / WORKTREES_DIR_NAME).is_symlink()  # but it IS a symlink

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "symlink" in res.message


def test_remove_refuses_when_target_itself_is_a_symlink(tmp_path):
    """`_target_escapes_repo` only checks `.worktrees` (`target.parent`) — a hand-crafted symlink
    at `.worktrees/<name>` ITSELF (not the whole `.worktrees/` dir) would otherwise resolve
    straight through the branch lookup and the git mutation. A real `git worktree add`-created
    worktree root is NEVER a symlink, so refusing here can never block legitimate use — it can
    only ever catch a hand-crafted one, whether it points somewhere real (another worktree
    registered to this same repo, elsewhere) or dangling."""
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / WORKTREES_DIR_NAME).mkdir()
    (repo / WORKTREES_DIR_NAME / "agent-1").symlink_to(outside, target_is_directory=True)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "symlink" in res.message
    assert outside.exists()  # untouched — refused before any git mutation


# ── git-level failures ─────────────────────────────────────────────────────────────
def test_remove_locked_worktree_surfaces_git_error(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(target)], check=True)

    # a single `--force` does NOT override a lock (git requires unlocking or `-f -f`) — verified
    # empirically: `git worktree remove --force` on a locked worktree exits 128 with "cannot
    # remove a locked working tree". remove() must surface that refusal, not silently blow through
    # a lock someone set on purpose.
    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert target.exists()  # nothing removed
    assert _branch_exists(repo, "agent-1")  # branch step never ran — worktree step failed first


def test_remove_refuses_when_branch_lookup_fails(tmp_path, monkeypatch):
    """A `git worktree list --porcelain` failure must NOT be collapsed into "no branch, must be
    detached" — that would remove the worktree, skip `git branch -D`, and report success while
    stranding the real branch. `remove()` must refuse the WHOLE removal instead, before any git
    mutation."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"

    real_run = subprocess.run
    list_cmd = ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == list_cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"simulated listing failure\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    # a non-zero exit means git RAN and rejected the request — a usage-class problem
    # (EXIT_CONFIG), the same bucket `_run_git_worktree_add`/`_run_git_worktree_remove` use for an
    # ordinary git rejection — not EXIT_INTERNAL (reserved for "rig itself broke": a timeout or an
    # unrunnable git binary, see `test_remove_refuses_when_branch_lookup_times_out` below).
    assert res.exit_code == errors.EXIT_CONFIG
    assert target.exists()  # refused BEFORE any git mutation — nothing removed
    assert _branch_exists(repo, "agent-1")


def _assert_oserror_on_step_maps_to_exit_internal(repo, step_cmd, monkeypatch):
    """Shared body for the three `test_*_oserror_is_exit_internal` tests below: patch exactly
    ``step_cmd`` to raise an ``OSError`` other than ``FileNotFoundError``, then assert `remove()`
    reports it as `EXIT_INTERNAL` — the CLI preflight already confirmed `git` is on PATH, so none
    of the three git steps should ever diagnose a transient runtime OSError as "git is not
    installed" (`EXIT_MISSING_DEP`); see `_classify_run_oserror`.
    """
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd == step_cmd:
            raise OSError("simulated EMFILE (too many open files)")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_INTERNAL


def test_branch_lookup_oserror_is_exit_internal(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    step_cmd = ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]

    _assert_oserror_on_step_maps_to_exit_internal(repo, step_cmd, monkeypatch)


def test_worktree_remove_step_oserror_is_exit_internal(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"
    step_cmd = ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)]

    _assert_oserror_on_step_maps_to_exit_internal(repo, step_cmd, monkeypatch)


def test_branch_delete_step_oserror_is_exit_internal(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    step_cmd = ["git", "-C", str(repo), "branch", "-D", "agent-1"]

    _assert_oserror_on_step_maps_to_exit_internal(repo, step_cmd, monkeypatch)


def test_file_not_found_error_is_exit_missing_dep_not_exit_internal(tmp_path, monkeypatch):
    """`_classify_run_oserror` exists specifically to split `FileNotFoundError` (git genuinely
    vanished after the preflight PATH check — still legitimately "missing dependency") from every
    OTHER `OSError` (`EXIT_INTERNAL`, pinned by the three `test_*_oserror_is_exit_internal` tests
    above). Without this test, the `FileNotFoundError` branch could be deleted or inverted and
    nothing would fail."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    list_cmd = ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd == list_cmd:
            raise FileNotFoundError("simulated: git vanished after the preflight PATH check")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_MISSING_DEP


def test_remove_refuses_when_branch_lookup_times_out(tmp_path, monkeypatch):
    """A TIMEOUT on the listing is its own failure class (EXIT_INTERNAL, matching the CLI epilog's
    "1 = timed out") — distinct from an ordinary non-zero exit (EXIT_CONFIG, tested above)."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"

    real_run = subprocess.run
    list_cmd = ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == list_cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_INTERNAL
    assert target.exists()
    assert _branch_exists(repo, "agent-1")


def test_worktree_remove_step_timeout_is_surfaced(tmp_path, monkeypatch):
    """The `git worktree remove` step (as opposed to the listing step above) timing out is its
    own distinct message ("may be left in a partial state") — pin it so it doesn't silently
    regress."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"

    real_run = subprocess.run
    remove_cmd = ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)]

    def fake_run(cmd, *args, **kwargs):
        if cmd == remove_cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_INTERNAL
    assert "partial state" in res.message
    assert _branch_exists(repo, "agent-1")  # branch step never ran — the remove step timed out


def test_branch_delete_step_timeout_is_surfaced(tmp_path, monkeypatch):
    """The `git branch -D` step timing out is distinct from it merely FAILING (tested below): the
    message must still name the copy-pasteable recovery command, and the worktree is confirmed
    already gone by that point."""
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"

    real_run = subprocess.run
    branch_delete_cmd = ["git", "-C", str(repo), "branch", "-D", "agent-1"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == branch_delete_cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_INTERNAL
    assert res.branch == "agent-1"
    assert not target.exists()  # the worktree really was removed — only branch -D timed out
    assert "git branch -D agent-1" in res.message


def test_branch_delete_failure_after_worktree_removal_is_surfaced(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    target = repo / WORKTREES_DIR_NAME / "agent-1"

    real_run = subprocess.run
    branch_delete_cmd = ["git", "-C", str(repo), "branch", "-D", "agent-1"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == branch_delete_cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"simulated failure\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = worktree.remove(repo, "agent-1")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert not target.exists()  # the worktree really was removed — only the branch step failed
    assert "leaves the branch behind" in res.message
    assert "git branch -D agent-1" in res.message


# ── CLI wiring end to end ────────────────────────────────────────────────────────
def test_cli_worktree_remove_end_to_end(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    main(["worktree", "create", "agent-1", "-C", str(repo)])
    capsys.readouterr()

    rc = main(["worktree", "remove", "agent-1", "-C", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "removed" in out
    assert not (repo / WORKTREES_DIR_NAME / "agent-1").exists()
    assert not _branch_exists(repo, "agent-1")


def test_cli_worktree_remove_outside_git_repo_exits_not_a_repo(tmp_path, capsys):
    not_git = tmp_path / "plain"
    not_git.mkdir()

    rc = main(["worktree", "remove", "agent-1", "-C", str(not_git)])
    assert rc == errors.EXIT_NOT_A_REPO
    assert "not a git repository" in capsys.readouterr().out


def test_cli_worktree_remove_missing_git_binary_is_missing_dep_not_not_a_repo(
    tmp_path, capsys, monkeypatch
):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "git" else f"/usr/bin/{name}")

    rc = main(["worktree", "remove", "agent-1", "-C", str(repo)])

    assert rc == errors.EXIT_MISSING_DEP
    assert "git is not installed" in capsys.readouterr().out


def test_cli_worktree_remove_nonexistent_target_exits_config(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")

    rc = main(["worktree", "remove", "ghost", "-C", str(repo)])

    assert rc == errors.EXIT_CONFIG
    assert "not a working tree" in capsys.readouterr().out
