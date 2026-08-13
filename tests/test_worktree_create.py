"""`rig worktree create` — the standardized ``.worktrees/<name>`` agent-worktree convention.

Covers: fresh creation lands at ``<repo>/.worktrees/<name>``; the ``.git/info/exclude`` entry is
registered exactly once (idempotent — a second, third, … creation never duplicates the marker
block); git itself honors the ignore (non-circular proof via `git check-ignore`) so the primary
checkout's `git status` stays clean; the exclude resolve is worktree-aware (it still has to be
FOUND via `git rev-parse --git-path`, but resolves to the repo's single COMMON/shared exclude
file, not a private per-worktree one — verified directly, not just asserted); name/ref validation
(including rejecting a `-`-prefixed `--branch`/`--from` value, which git's own option parser would
otherwise read as a flag); a symlinked `.worktrees` directory is refused rather than followed
outside the repo; a failed exclude reconcile refuses to create the worktree at all (no half-done
state); the low-level marker-block reconcile's conflict/collapse behavior (mirrors
``test_ship_delegator.py`` / ``test_global_excludes.py``); and the CLI wiring (`rig worktree
create`, including `--branch`/`--from`) end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from riglib import errors, worktree
from riglib.actions.runner import reconcile_worktrees_exclude, worktrees_exclude_block_text
from riglib.cli import main
from riglib.config import (
    WORKTREES_DIR_NAME,
    WORKTREES_EXCLUDE_BEGIN_MARKER,
    WORKTREES_EXCLUDE_END_MARKER,
)


# ── helpers ──────────────────────────────────────────────────────────────────────
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


def _exclude_path(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


# ── worktree.create — happy path ─────────────────────────────────────────────────
def test_create_lands_at_dot_worktrees_name(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = worktree.create(repo, "agent-1")

    assert res.status == "created"
    assert res.exit_code == 0
    assert res.path == repo / WORKTREES_DIR_NAME / "agent-1"
    assert res.path.is_dir()
    assert (res.path / "README.md").is_file()  # a real, checked-out worktree
    assert res.branch == "agent-1"


def test_create_registers_exclude_entry_once(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")

    content = _exclude_path(repo).read_text(encoding="utf-8")
    assert content.count(WORKTREES_EXCLUDE_BEGIN_MARKER) == 1
    assert content.count(WORKTREES_EXCLUDE_END_MARKER) == 1
    assert f"/{WORKTREES_DIR_NAME}/" in content


def test_second_create_does_not_duplicate_exclude_entry(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")
    first = _exclude_path(repo).read_text(encoding="utf-8")

    res2 = worktree.create(repo, "agent-2")
    second = _exclude_path(repo).read_text(encoding="utf-8")

    assert res2.status == "created"
    assert res2.exclude_note.startswith("already registered")
    assert second == first  # byte-identical — a true no-op on the exclude file
    assert second.count(WORKTREES_EXCLUDE_BEGIN_MARKER) == 1


def test_third_create_still_a_single_block(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    for name in ("agent-1", "agent-2", "agent-3"):
        res = worktree.create(repo, name)
        assert res.status == "created"

    content = _exclude_path(repo).read_text(encoding="utf-8")
    assert content.count(WORKTREES_EXCLUDE_BEGIN_MARKER) == 1
    for name in ("agent-1", "agent-2", "agent-3"):
        assert (repo / WORKTREES_DIR_NAME / name).is_dir()


def test_worktree_is_ignored_by_git_and_status_stays_clean(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree.create(repo, "agent-1")

    # non-circular proof: ask GIT ITSELF whether it ignores the directory (check-ignore exits 0
    # + names the exclude file only when a rule actually matched).
    ci = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-v", f"{WORKTREES_DIR_NAME}/agent-1"],
        capture_output=True,
        text=True,
    )
    assert ci.returncode == 0, f"git does not ignore .worktrees/: {ci.stderr}"
    assert "info/exclude" in ci.stdout, ci.stdout

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "", f"primary checkout dirtied: {status.stdout!r}"


def test_branch_override(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = worktree.create(repo, "agent-1", branch="feature/x")

    assert res.branch == "feature/x"
    assert res.path == repo / WORKTREES_DIR_NAME / "agent-1"
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature/x"],
        capture_output=True, text=True, check=True,
    )
    assert "feature/x" in branches.stdout


def test_base_ref_override(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    base_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # advance the primary branch past base_commit
    (repo / "second.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "second"],
        cwd=repo, check=True,
    )

    res = worktree.create(repo, "agent-1", base_ref=base_commit)
    assert res.status == "created"
    head = subprocess.run(
        ["git", "-C", str(res.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == base_commit


# ── worktree-aware exclude resolution ────────────────────────────────────────────
def test_creating_from_inside_a_linked_worktree_registers_the_shared_common_exclude(tmp_path):
    """``info/exclude`` is git's COMMON (shared) exclude file, not a private per-worktree one.

    ``git -C <linked-worktree> rev-parse --git-path info/exclude`` still has to be used to FIND
    the path (a naive ``<wt>/.git/info/exclude`` string join fails — ``.git`` is a FILE inside a
    linked worktree, not a directory) — but the file that resolves to is the SAME physical file
    as the primary checkout's ``.git/info/exclude``. This proves that by registering the entry
    from the SAME path the primary checkout would use.
    """
    main_repo = _git_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feat"], cwd=main_repo, check=True
    )

    res = worktree.create(wt, "nested")
    assert res.status == "created"

    exclude_via_worktree = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    exclude_via_main = subprocess.run(
        ["git", "-C", str(main_repo), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    resolved_via_wt = Path(exclude_via_worktree) if Path(exclude_via_worktree).is_absolute() else wt / exclude_via_worktree
    resolved_via_main = (
        Path(exclude_via_main) if Path(exclude_via_main).is_absolute() else main_repo / exclude_via_main
    )
    assert resolved_via_wt.resolve() == resolved_via_main.resolve()  # the SAME physical file
    assert f"/{WORKTREES_DIR_NAME}/" in resolved_via_wt.read_text(encoding="utf-8")
    # ...and it is therefore visible from the PRIMARY checkout too, not scoped to the linked one.
    assert f"/{WORKTREES_DIR_NAME}/" in resolved_via_main.read_text(encoding="utf-8")


# ── name validation / conflict handling ──────────────────────────────────────────
def test_name_with_path_separator_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = worktree.create(repo, "a/b")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert not (repo / WORKTREES_DIR_NAME).exists()


def test_empty_name_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = worktree.create(repo, "")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG


def test_dot_and_dotdot_names_are_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert worktree.create(repo, ".").status == "error"
    assert worktree.create(repo, "..").status == "error"


def test_target_already_exists_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / WORKTREES_DIR_NAME / "agent-1").mkdir(parents=True)

    res = worktree.create(repo, "agent-1")
    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "already exists" in res.message


def test_git_worktree_add_failure_is_surfaced(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # a branch collision makes `git worktree add -b agent-1` fail — git's own stderr must
    # come through, not a swallowed/misleading success. A branch collision is a USAGE-class
    # problem (bad input), not "rig broke" — same bucket as an invalid name.
    subprocess.run(["git", "-C", str(repo), "branch", "agent-1"], check=True)

    res = worktree.create(repo, "agent-1")
    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert not (repo / WORKTREES_DIR_NAME / "agent-1").exists()


def test_base_ref_starting_with_dash_is_rejected_not_passed_to_git(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # proof this is a REAL vulnerability, not a hypothetical: unguarded, `--from=--no-checkout`
    # would be read by git's OWN option parser as the --no-checkout FLAG (not an invalid ref),
    # and `git worktree add` would exit 0 with an unpopulated worktree — verified empirically.
    res = worktree.create(repo, "agent-1", base_ref="--no-checkout")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "must not start with" in res.message
    assert not (repo / WORKTREES_DIR_NAME).exists()


def test_branch_starting_with_dash_is_rejected(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    res = worktree.create(repo, "agent-1", branch="--force")

    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert not (repo / WORKTREES_DIR_NAME).exists()


def test_symlinked_worktrees_dir_is_refused(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / WORKTREES_DIR_NAME).symlink_to(outside, target_is_directory=True)

    res = worktree.create(repo, "agent-1")
    assert res.status == "error"
    assert res.exit_code == errors.EXIT_CONFIG
    assert "symlink" in res.message
    assert not (outside / "agent-1").exists()


def test_failed_exclude_reconcile_refuses_to_create_the_worktree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    # an unbalanced marker pair is a refused conflict (see test_reconcile_unbalanced_markers_
    # is_a_refused_conflict) — create() must check this BEFORE running `git worktree add`, so
    # a broken exclude file never leaves a half-created, unignored worktree behind.
    _exclude_path(repo).parent.mkdir(parents=True, exist_ok=True)
    _exclude_path(repo).write_text(f"{WORKTREES_EXCLUDE_BEGIN_MARKER}\nstray\n", encoding="utf-8")

    res = worktree.create(repo, "agent-1")
    assert res.status == "error"
    assert res.exit_code == errors.EXIT_INTERNAL
    assert not (repo / WORKTREES_DIR_NAME / "agent-1").exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "agent-1"],
        capture_output=True, text=True, check=True,
    )
    assert "agent-1" not in branches.stdout  # no branch created either — nothing half-done


# ── low-level reconcile: byte-stable block, conflict, and collapse ──────────────
def test_exclude_block_text_is_byte_stable():
    block = worktrees_exclude_block_text()
    assert block == (
        f"{WORKTREES_EXCLUDE_BEGIN_MARKER}\n"
        "# rig-created agent worktrees live under .worktrees/; ignored so they don't dirty "
        "the primary checkout.\n"
        f"/{WORKTREES_DIR_NAME}/\n"
        f"{WORKTREES_EXCLUDE_END_MARKER}"
    )


def test_reconcile_preserves_existing_user_lines(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _exclude_path(repo).parent.mkdir(parents=True, exist_ok=True)
    _exclude_path(repo).write_text("*.log\nnode_modules/\n", encoding="utf-8")

    ok, note = reconcile_worktrees_exclude(repo)
    assert ok
    content = _exclude_path(repo).read_text(encoding="utf-8")
    assert "*.log" in content
    assert "node_modules/" in content
    assert WORKTREES_EXCLUDE_BEGIN_MARKER in content


def test_reconcile_collapses_duplicated_blocks(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    block = worktrees_exclude_block_text()
    _exclude_path(repo).parent.mkdir(parents=True, exist_ok=True)
    # simulate a prior NON-idempotent writer that appended the block twice
    _exclude_path(repo).write_text(f"{block}\n\n{block}\n", encoding="utf-8")

    ok, note = reconcile_worktrees_exclude(repo)
    assert ok
    content = _exclude_path(repo).read_text(encoding="utf-8")
    assert content.count(WORKTREES_EXCLUDE_BEGIN_MARKER) == 1


def test_reconcile_unbalanced_markers_is_a_refused_conflict(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _exclude_path(repo).parent.mkdir(parents=True, exist_ok=True)
    _exclude_path(repo).write_text(f"{WORKTREES_EXCLUDE_BEGIN_MARKER}\nstray\n", encoding="utf-8")
    before = _exclude_path(repo).read_text(encoding="utf-8")

    ok, note = reconcile_worktrees_exclude(repo)
    assert not ok
    assert "unbalanced" in note
    # refused, not guessed at — file must be untouched
    assert _exclude_path(repo).read_text(encoding="utf-8") == before


def test_reconcile_no_git_repo_is_a_clean_noop(tmp_path):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    ok, note = reconcile_worktrees_exclude(not_git)
    assert ok
    assert "no git repo" in note


# ── CLI wiring end to end ────────────────────────────────────────────────────────
def test_cli_worktree_create_end_to_end(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    rc = main(["worktree", "create", "agent-1", "-C", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert str(repo / WORKTREES_DIR_NAME / "agent-1") in out
    assert (repo / WORKTREES_DIR_NAME / "agent-1").is_dir()


def test_cli_worktree_create_second_time_is_idempotent(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    main(["worktree", "create", "agent-1", "-C", str(repo)])
    capsys.readouterr()
    rc = main(["worktree", "create", "agent-2", "-C", str(repo)])

    assert rc == 0
    content = _exclude_path(repo).read_text(encoding="utf-8")
    assert content.count(WORKTREES_EXCLUDE_BEGIN_MARKER) == 1


def test_cli_worktree_create_outside_git_repo_exits_not_a_repo(tmp_path, capsys):
    not_git = tmp_path / "plain"
    not_git.mkdir()

    rc = main(["worktree", "create", "agent-1", "-C", str(not_git)])
    assert rc == errors.EXIT_NOT_A_REPO
    assert "not a git repository" in capsys.readouterr().out


def test_cli_worktree_create_branch_and_from_flags_are_wired(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    base_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    rc = main(
        [
            "worktree", "create", "agent-1",
            "-C", str(repo),
            "--branch", "feature/cli-wired",
            "--from", base_commit,
        ]
    )

    assert rc == 0
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature/cli-wired"],
        capture_output=True, text=True, check=True,
    )
    assert "feature/cli-wired" in branches.stdout
    head = subprocess.run(
        ["git", "-C", str(repo / WORKTREES_DIR_NAME / "agent-1"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == base_commit


def test_cli_worktree_create_rejects_dash_prefixed_from(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")
    # the space-separated form (`--from --no-checkout`) is caught by ARGPARSE ITSELF ("expected
    # one argument", since the next token looks like another flag) before our code ever runs; the
    # `--from=value` form is the one that actually reaches `worktree.create` unfiltered — that is
    # the form this test (and the vulnerability) is about.
    rc = main(["worktree", "create", "agent-1", "-C", str(repo), "--from=--no-checkout"])

    assert rc == errors.EXIT_CONFIG
    assert not (repo / WORKTREES_DIR_NAME).exists()


def test_cli_worktree_no_subcommand_prints_help(capsys):
    rc = main(["worktree"])
    assert rc == 0
    assert "worktree" in capsys.readouterr().out
