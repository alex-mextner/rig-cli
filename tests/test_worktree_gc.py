"""`rig worktree gc` — classify every worktree of a repo and clean up the safe classes.

Covers, with REAL temporary git repos (never mocked git itself): liveness wins over every other
classification, including one that would otherwise read as merged/stale (proves the ordering);
`prunable` (worktree dir deleted by hand, still registered) is removed; `merged`/`closed` PRs are
removed on `--yes`; `dirty` is never removed regardless of flags; `no-pr-stale` is reported but
kept without `--include-stale`, then removed once both `--yes` and `--include-stale` are given;
`--dry-run` never mutates anything even with `--yes`; disk-usage summing only runs for entries
targeted for removal; the `rig status` stale-worktree summary line; the CLI wiring end to end
(explicit `--repo` and the registry-driven multi-repo fan-out when `--repo` is omitted).

`pr_lookup` and `liveness_check` are always INJECTED fakes here — never real `gh`/`pgrep` calls —
per this module's own design (both are explicitly injectable dependencies).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from riglib import worktree, worktree_gc
from riglib.cli import main
from riglib.repository_registry import RepositoryEntry, RepositoryRegistry


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


def _future_iso() -> str:
    """A timestamp safely AFTER "now" — used as a fake PR merged/closed date so a freshly created
    test worktree's own commit (dated "now") never reads as having "outlived" the PR (see
    `_branch_outlived_its_pr`'s date comparison): a fixed PAST date string would make every test
    worktree's real creation-time commit look newer than the PR's resolution, which is exactly the
    "still active" signal that function looks for — correct in production (a real merge always
    precedes any later commit), but backwards for a hermetic test whose one commit is dated at
    whatever moment the test happened to run."""
    import datetime

    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()


def _no_live(path: Path) -> bool:
    return False


def _no_pr(branch: str):
    return None


def _fake_liveness(live_paths: set[Path]):
    def _check(path: Path) -> bool:
        return path.resolve() in {p.resolve() for p in live_paths}

    return _check


def _fake_pr_lookup(mapping: dict[str, worktree_gc.PrInfo]):
    def _lookup(branch: str):
        return mapping.get(branch)

    return _lookup


def _make_worktree(repo: Path, name: str, branch: str | None = None) -> Path:
    res = worktree.create(repo, name, branch=branch)
    assert res.status == "created", res.message
    return res.path


def _set_old_mtime(path: Path, days: int) -> None:
    """Force `_last_activity_utc` to look old — it takes the MAX of the last commit's date and the
    worktree's own `.git` pointer-file mtime (see its docstring), so BOTH signals must be backdated
    or the (very recently created, by the test itself) `.git` mtime would win and the worktree
    would still read as "recent" regardless of the rewritten commit date.

    `--amend` REWRITES the commit (a new SHA), diverging the branch from whatever else shared its
    original tip (e.g. the repo's default branch) — since `_has_unpushed_commits` now requires
    reachability from SOME ref other than the branch's own (see its docstring), an amended-but-
    otherwise-untagged commit would itself look exactly like unrecoverable local-only work and
    misclassify as `dirty`. Tagging the amended commit gives it that "known elsewhere" signal
    without being a remote push — a deliberate, minimal stand-in so tests that only care about
    AGE (not about the unpushed-commit safety net, covered by its own dedicated tests) don't
    accidentally trip it."""
    import datetime
    import os
    import uuid

    old_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    old = old_dt.isoformat()
    env = {**os.environ, "GIT_COMMITTER_DATE": old}  # ours must win over any ambient value
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "-C", str(path), "commit", "--amend", "--no-edit", f"--date={old}",
        ],
        check=True,
        env=env,
    )
    subprocess.run(["git", "-C", str(path), "tag", f"snapshot-{uuid.uuid4().hex[:8]}"], check=True)
    old_ts = old_dt.timestamp()
    os.utime(path / ".git", (old_ts, old_ts))
    os.utime(path, (old_ts, old_ts))


# ── classification ordering: liveness wins over everything ──────────────────────
def test_live_worktree_is_kept_even_if_also_merged_and_stale(tmp_path):
    """This is the ordering test: the worktree is BOTH merged (its PR is merged) AND old-and-clean
    (would otherwise be no-pr-stale) — if liveness weren't checked first, either branch of
    classification would mark it removable. It must come back `live` regardless."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=100)

    entries = worktree_gc.plan_gc(
        repo,
        include_stale=True,
        liveness_check=_fake_liveness({wt}),
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=1, state="MERGED", merged_at="2020-01-01")}),
    )

    assert len(entries) == 1
    assert entries[0].classification == "live"
    assert entries[0].plan_removable is False


# ── prunable ──────────────────────────────────────────────────────────────────────
def test_prunable_worktree_is_removed_on_yes(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    import shutil

    shutil.rmtree(wt)  # deleted by hand, not via `git worktree remove` — still registered

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.classification == "prunable"
    assert entry.removed is True
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
    assert "agent-1" not in result.stdout


# ── dirty is never removed ────────────────────────────────────────────────────────
def test_dirty_worktree_is_never_removed(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    (wt / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        include_stale=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=7, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert entry.removed is False
    assert wt.is_dir()


# ── merged / closed ────────────────────────────────────────────────────────────────
def test_merged_pr_worktree_is_removed_on_yes(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {"agent-1": worktree_gc.PrInfo(number=42, state="MERGED", merged_at=_future_iso())}
        ),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert "#42" in entry.reason
    assert entry.removed is True
    assert not wt.exists()


def test_closed_pr_worktree_is_removed_on_yes(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {"agent-1": worktree_gc.PrInfo(number=9, state="CLOSED", closed_at=_future_iso())}
        ),
    )

    entry = report.entries[0]
    assert entry.classification == "closed"
    assert entry.removed is True
    assert not wt.exists()


def test_merged_worktree_not_removed_without_yes(tmp_path):
    """Bare gc (no --yes) is report-only, even for a class that WOULD be safe to remove."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=False,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is False
    assert wt.exists()


# ── no-pr-stale requires --yes AND --include-stale ─────────────────────────────────
def test_no_pr_stale_reported_but_kept_without_include_stale(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=30)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, include_stale=False, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "no-pr-stale"
    assert entry.removed is False
    assert wt.exists()


def test_no_pr_stale_removed_with_yes_and_include_stale(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=30)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, include_stale=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "no-pr-stale"
    assert entry.removed is True
    assert not wt.exists()


def test_no_pr_stale_with_unique_unpushed_commit_is_kept_dirty_not_stale(tmp_path):
    """Review finding (Opus, round 16): the final unpushed-commit guard on the no-PR path (present,
    clean, aged, no PR at all — the sole safety net before a `no-pr-stale` removal) was never
    exercised by a real unique-commit scenario; every existing no-pr-stale test's worktree shares
    its commit with the repo's own default branch. An aged, clean, no-PR worktree whose tip is a
    UNIQUE commit (no remote, no other branch, no tag) must classify `dirty`, never `no-pr-stale`
    — on a real `--yes --include-stale` run, this is what stands between an idle local worktree
    and losing its only copy of that commit."""
    import datetime
    import os

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    (wt / "unique.txt").write_text("exists nowhere else\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "unique.txt"], check=True)
    # backdated WITHOUT tagging (unlike `_set_old_mtime`, which deliberately tags to keep OTHER
    # age-only tests from tripping the unpushed-commit check) — this commit must stay genuinely
    # unreachable from anywhere else for the scenario under test.
    old_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    old = old_dt.isoformat()
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "-C", str(wt), "commit", "-qm", "unique work", f"--date={old}",
        ],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": old},
    )
    old_ts = old_dt.timestamp()
    os.utime(wt / ".git", (old_ts, old_ts))
    os.utime(wt, (old_ts, old_ts))

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, include_stale=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert entry.removed is False
    assert wt.exists()


def test_no_pr_recent_worktree_is_active_not_stale(tmp_path):
    """Clean, no PR, but recent — must not be classified stale just because there is no PR."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    entries = worktree_gc.plan_gc(
        repo, older_than_days=14, liveness_check=_no_live, pr_lookup=_no_pr
    )

    assert entries[0].classification == "active"
    assert entries[0].plan_removable is False


def test_older_than_days_boundary_exactly_n_days_is_stale(tmp_path):
    """Review-requested coverage: the `>=` boundary in `_classify_clean_worktree` — exactly
    `--older-than-days` old must already count as stale (not "one day short")."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=14)

    entries = worktree_gc.plan_gc(repo, older_than_days=14, liveness_check=_no_live, pr_lookup=_no_pr)

    assert entries[0].classification == "no-pr-stale"


def test_older_than_days_boundary_one_day_short_is_active(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=13)

    entries = worktree_gc.plan_gc(repo, older_than_days=14, liveness_check=_no_live, pr_lookup=_no_pr)

    assert entries[0].classification == "active"


def test_open_pr_worktree_is_active_and_kept(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=100)  # even if very old, an OPEN PR keeps it active

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        include_stale=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=3, state="OPEN")}),
    )

    entry = report.entries[0]
    assert entry.classification == "active"
    assert entry.removed is False


# ── unpushed local commits on an otherwise "merged"/"stale" branch are never removed ─
def _add_remote_and_push(repo: Path, wt: Path, branch: str) -> Path:
    """Give `repo` a real `origin` remote (a second bare repo) and push `branch` from `wt` to it —
    needed to exercise `_has_unpushed_commits`'s general "reachable from some OTHER ref (a remote-
    tracking ref, another local branch, or a tag)" check with a genuine remote-tracking ref as that
    "elsewhere" proof (as opposed to a same-repo default-branch commit, or a tag, doing the same
    job). Idempotent for the remote/`origin` setup, so it can be called more than once for the
    SAME `repo` to push several branches to the same remote."""
    remote = repo.parent / f"{repo.name}-remote.git"
    if not remote.exists():
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(wt), "push", "-q", "-u", "origin", branch], check=True)
    return remote


def test_merged_branch_with_unpushed_followup_commit_is_kept_not_removed(tmp_path):
    """The data-loss scenario a review round caught: a PR merges, the agent then commits ONE MORE
    local fix on the same branch and never pushes it. `git status --porcelain` is empty (nothing
    UNCOMMITTED) so a naive dirty-check alone would call this safe — it must not be removed."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    (wt / "followup.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "followup.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unpushed followup"],
        check=True,
    )

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert "unpushed" in entry.reason
    assert entry.removed is False
    assert wt.exists()


def test_present_worktree_unpushed_check_not_defeated_by_same_named_tag(tmp_path):
    """Review finding (Opus, round 11): `_resolve_own_branch_ref` used to call `git symbolic-ref
    --short HEAD`, but git's unambiguous-shortening rules resolve a TAG before a branch of the
    same name — with both `refs/heads/agent-1` and `refs/tags/agent-1` present, `--short` prints
    `heads/agent-1`, not `agent-1`, and the code then built the nonsense ref
    `refs/heads/heads/agent-1`, which matches nothing — silently defeating the exclusion and
    making a present worktree's OWN branch look like "reachable elsewhere" (itself). Without the
    fix (dropping `--short`), this classifies `merged` instead of `dirty` at PLAN time already —
    this is the present-worktree twin of the tag-shadow test already covering the prunable path."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    subprocess.run(["git", "-C", str(repo), "tag", "agent-1"], check=True)  # same name as the branch
    (wt / "followup.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "followup.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unpushed followup"],
        check=True,
    )

    entries = worktree_gc.plan_gc(
        repo,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    assert entries[0].classification == "dirty"
    assert entries[0].plan_removable is False


def test_merged_branch_fully_pushed_is_still_removed(tmp_path):
    """Sanity check for the fix above: a repo WITH a real remote, where HEAD IS reachable from a
    remote-tracking ref, must still classify + remove normally — the new check must not turn every
    remote-tracked repo's merged worktree into a false "dirty"."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is True
    assert not wt.exists()


def test_no_remote_at_all_does_not_block_removal(tmp_path):
    """A repo with NO remote configured, where the worktree's branch shares its exact tip commit
    with the repo's own default branch (the common case: `rig worktree create` branches without
    diverging) — safe to remove, because that commit IS reachable from elsewhere (the default
    branch), not because "no remote" is itself treated as automatically safe."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is True


def test_prunable_with_unique_local_commit_and_no_remote_is_kept_dirty(tmp_path):
    """Review finding (Opus, round 7) — the DATA-LOSS scenario on a BARE `--yes` (no
    `--include-stale` needed): a genuinely local-only repo (no remote at all) whose worktree has a
    commit that exists NOWHERE else — not on the default branch, not on any remote, not tagged —
    then had its directory `rm -rf`'d by hand. The earlier "no remote at all -> automatically
    safe" carve-out would have classified this `prunable` and force-removed the branch, discarding
    the only reference to that commit. It must now classify `dirty` and be kept."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    (wt / "unique.txt").write_text("exists nowhere else\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "unique.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unique work"],
        check=True,
    )
    shutil.rmtree(wt)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert entry.removed is False


def test_prunable_toctou_recheck_catches_a_branch_that_shares_a_now_removed_commit(tmp_path, monkeypatch):
    """Review finding (Opus + Codex, round 8): the pre-removal recheck used to be skipped
    ENTIRELY for prunable entries (its `if entry.path.exists()` gate covers dirty AND unpushed
    together). Two worktrees whose branches point at the SAME unique commit: `A` (present,
    merged) and `B` (prunable). At planning time each looks "safe" because the OTHER's branch ref
    still exists. Whichever removes FIRST deletes its own branch ref; the SECOND one's stale
    recheck (skipped because its directory is gone) would then proceed to delete its branch too —
    losing the commit outright. The recheck must re-verify the second one's branch via
    `repo_root`, not skip it — regardless of WHICH of the two runs first (`git worktree list`'s
    entry order is not something this test should assume; only the outcome — exactly one survives
    — is the actual safety property under test, per a review-flagged flake risk)."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt_a = _make_worktree(repo, "agent-a")
    (wt_a / "shared.txt").write_text("shared unique work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt_a), "add", "shared.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt_a), "commit", "-qm", "shared work"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(wt_a), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    wt_b = _make_worktree(repo, "agent-b", branch="agent-b")
    # move agent-b's OWN tip to A's exact commit FROM WITHIN its own worktree — `git branch -f`
    # from outside would be refused (git won't force-move a branch checked out elsewhere)
    subprocess.run(["git", "-C", str(wt_b), "reset", "-q", "--hard", sha], check=True)
    shutil.rmtree(wt_b)  # B is now prunable, sharing A's exact (otherwise unique) commit

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-a": worktree_gc.PrInfo(number=1, state="MERGED")}),
    )

    by_branch = {e.branch: e for e in report.entries}
    # Removal ORDER is not guaranteed (git's own worktree-list ordering, not this test's to
    # assume) — the safety property is that only ONE of the two can ever be removed: whichever
    # goes first legitimately proceeds (its own branch is still the "elsewhere" proof for the
    # other, at that instant); the SECOND one's fresh recheck must then catch that its own
    # "elsewhere" proof (the first one's branch) is now gone, and must be skipped, not removed.
    removed = [e for e in (by_branch["agent-a"], by_branch["agent-b"]) if e.removed]
    skipped = [e for e in (by_branch["agent-a"], by_branch["agent-b"]) if not e.removed]
    assert len(removed) == 1
    assert len(skipped) == 1
    assert skipped[0].skipped_reason is not None


def test_prunable_detached_head_with_unique_commit_is_kept_dirty(tmp_path):
    """Review finding (Opus + Codex, round 8): the unpushed-commit safety check was gated on
    `info.branch`, so a DETACHED-HEAD worktree (no branch at all — `git worktree add <path>
    <commit>`) skipped it entirely and went straight to `prunable` once its directory was gone.
    Git still holds a detached worktree's commit live via its own administrative HEAD until the
    worktree is truly removed — the same "may be the only copy" risk a branch-backed worktree has.
    """
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    (wt / "unique.txt").write_text("exists nowhere else\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "unique.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unique work"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
    # the leftover branch ref would ALSO keep the commit reachable — delete it so the detached
    # worktree really is the ONLY thing referencing this commit, the scenario under test
    subprocess.run(["git", "-C", str(repo), "branch", "-D", "agent-1"], check=True)
    detached = repo.parent / "detached-wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(detached), sha], check=True)
    shutil.rmtree(detached)  # now a PRUNABLE, DETACHED-HEAD worktree holding a unique commit

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.branch is None
    assert entry.classification == "dirty"
    assert entry.removed is False


# ── a branch that outlived its own merged/closed PR is kept, not removed ────────────
def test_branch_with_commits_after_its_merged_pr_is_kept_active(tmp_path):
    """A long-lived branch (e.g. `develop`) merged via one PR, then given a NEW commit afterward
    — the PR's own resolution date proves the branch is still alive, independent of remote-
    tracking state (no remote configured here at all, so `_has_unpushed_commits` would have
    nothing to say either way)."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    past_merged_at = "2020-01-01T00:00:00+00:00"  # the commit below postdates this

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {"agent-1": worktree_gc.PrInfo(number=42, state="MERGED", merged_at=past_merged_at)}
        ),
    )

    entry = report.entries[0]
    assert entry.classification == "active"
    assert "still active" in entry.reason
    assert entry.removed is False
    assert wt.exists()


def test_merged_branch_with_zero_remote_tracking_refs_still_removed_via_date_check(tmp_path):
    """When a repo has NO remote-tracking refs AT ALL (this branch's was the only one, and it too
    is gone — e.g. every branch in the repo has been squash-merged and pruned), removal must still
    proceed normally. NOTE this is no longer exercising a dedicated "no remote" carve-out in
    `_has_unpushed_commits` (that shortcut was removed by the round-7 fix — see the module's
    "Known limitations" section) — it passes here because the branch's tip is unchanged from the
    repo's OWN default branch (`_make_worktree` doesn't diverge), which is itself a valid
    "reachable elsewhere" ref regardless of any remote. Kept as a regression test for the
    "zero remote-tracking refs" shape specifically, even though the underlying mechanism proving
    safety has changed."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    subprocess.run(["git", "-C", str(repo), "update-ref", "-d", "refs/remotes/origin/agent-1"], check=True)
    assert not subprocess.run(
        ["git", "-C", str(repo), "branch", "-r"], capture_output=True, text=True
    ).stdout.strip()

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {"agent-1": worktree_gc.PrInfo(number=42, state="MERGED", merged_at=_future_iso())}
        ),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is True


def test_merged_branch_pruned_while_repo_still_has_other_remote_refs_is_kept_dirty(tmp_path):
    """The REAL squash-merge + prune scenario, precisely: git fetch --prune removed only THIS
    branch's remote-tracking ref, but the repo still has OTHER remote-tracking refs (proving a real
    remote is actively used) — the "no remote at all" carve-out must NOT apply here. Per the
    module's documented safety-over-effectiveness trade-off (see its "Known limitations" section),
    this must be kept `dirty` (a human needs to look), never auto-removed on the date check alone —
    the date check cannot tell a genuinely-pushed-then-pruned branch apart from one that simply
    never got pushed before its "merge"."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    # a commit UNIQUE to this branch — without one, both worktrees would share the exact same
    # base commit and `--contains` would trivially find it reachable from EITHER branch's ref
    (wt / "agent1-only.txt").write_text("unique to agent-1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "agent1-only.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "agent-1 work"],
        check=True,
    )
    _add_remote_and_push(repo, wt, "agent-1")
    other_wt = _make_worktree(repo, "keep-me-tracked")
    _add_remote_and_push(repo, other_wt, "keep-me-tracked")  # a SECOND branch stays tracked
    subprocess.run(["git", "-C", str(repo), "update-ref", "-d", "refs/remotes/origin/agent-1"], check=True)
    assert "keep-me-tracked" in subprocess.run(
        ["git", "-C", str(repo), "branch", "-r"], capture_output=True, text=True
    ).stdout

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {"agent-1": worktree_gc.PrInfo(number=42, state="MERGED", merged_at=_future_iso())}
        ),
    )

    by_branch = {e.branch: e for e in report.entries}
    assert by_branch["agent-1"].classification == "dirty"
    assert by_branch["agent-1"].removed is False
    assert wt.exists()


def test_realistic_merged_removal_past_date_branch_predates_merge(tmp_path):
    """The path a REAL run actually takes: a `merged_at` in the PAST (not the future — every other
    merged-removal test here uses a future/omitted date so the freshly-created test worktree's own
    commit doesn't look "newer than the merge"), with the branch's own last activity predating that
    merge date, so `_branch_outlived_its_pr` takes its ORDINARY `last_activity <= resolved_at`
    branch (returns `None`, not the "no date at all" fallback branch) and falls through to a normal
    `merged` removal. Review-caught: every other test exercised either the future-date shortcut or
    the missing-date fallback, never this one."""
    import datetime

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _set_old_mtime(wt, days=30)  # branch's last activity: 30 days ago
    merged_at = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)
    ).isoformat()  # merged 20 days ago — AFTER the branch's last activity

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED", merged_at=merged_at)}),
    )

    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is True
    assert not wt.exists()


# ── PR-priority: an OPEN PR must beat a MERGED one for a reused branch name ─────────
def test_pr_index_prefers_open_over_merged_for_reused_branch_name():
    """Review finding: a branch reused across PRs with different base branches (`gh pr list
    --state all` returns both under the same headRefName) must resolve to OPEN, not MERGED — an
    open PR is a hard keep everywhere else in this module, so picking MERGED here would remove a
    worktree still backing a live PR."""
    raw = [
        {"number": 1, "state": "MERGED", "headRefName": "foo", "mergedAt": "2026-01-01", "url": "u1"},
        {"number": 2, "state": "OPEN", "headRefName": "foo", "url": "u2"},
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert by_branch["foo"].state == "OPEN"
    assert by_branch["foo"].number == 2


def test_pr_index_prefers_merged_over_closed():
    raw = [
        {"number": 1, "state": "CLOSED", "headRefName": "bar", "closedAt": "2026-01-01", "url": "u1"},
        {"number": 2, "state": "MERGED", "headRefName": "bar", "mergedAt": "2026-01-02", "url": "u2"},
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert by_branch["bar"].state == "MERGED"


def test_pr_index_excludes_cross_repository_fork_prs():
    """A fork PR's `headRefName` is the branch name IN THE FORK, with no repository qualifier —
    `gh pr list --state all` can return a fork PR and a local-clone PR under the exact same bare
    branch name. Only the local-clone record may drive a classification; the fork record must be
    dropped, not merged into the same index slot."""
    raw = [
        {
            "number": 1, "state": "MERGED", "headRefName": "feature",
            "mergedAt": "2026-01-01", "url": "u1", "isCrossRepository": True,
        },
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert "feature" not in by_branch


def test_pr_index_keeps_non_fork_pr_alongside_excluded_fork_pr():
    raw = [
        {
            "number": 1, "state": "MERGED", "headRefName": "feature",
            "mergedAt": "2026-01-01", "url": "u1", "isCrossRepository": True,
        },
        {
            "number": 2, "state": "OPEN", "headRefName": "feature",
            "url": "u2", "isCrossRepository": False,
        },
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert by_branch["feature"].number == 2
    assert by_branch["feature"].state == "OPEN"


def test_pr_index_drops_merged_or_closed_record_missing_its_resolution_date():
    """Review finding (Codex, round 20): real `gh` always populates `mergedAt`/`closedAt` for a
    resolved PR, but a `gh --json` field-list rename/typo (or a malformed proxy) could silently
    set it `None` on every record. Trusting that as "not outlived" would classify the branch
    removable purely on `_has_unpushed_commits`'s say-so — which only protects against losing
    UNREACHABLE commits, not against deleting a branch that is still genuinely active but already
    fully pushed. Dropping the record here (branch reads as "no PR found") keeps the failure
    inside the same double-gated no-pr-stale/active buckets every other malformed shape falls
    back to."""
    raw = [
        {"number": 1, "state": "MERGED", "headRefName": "no-merge-date", "mergedAt": None, "url": "u1"},
        {"number": 2, "state": "CLOSED", "headRefName": "no-close-date", "closedAt": "not-a-date", "url": "u2"},
        {"number": 3, "state": "MERGED", "headRefName": "has-date", "mergedAt": "2026-01-01", "url": "u3"},
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert "no-merge-date" not in by_branch
    assert "no-close-date" not in by_branch
    assert by_branch["has-date"].number == 3


def test_pr_index_drops_a_record_with_an_unrecognized_state():
    """Review finding (Sonnet, round 21): a malformed/renamed `--json` field, or a future GitHub
    PR state value beyond OPEN/MERGED/CLOSED, used to still get INSERTED into the index (at
    priority 9, surviving only as the sole record for its branch) — `_classify_clean_worktree`'s
    four sequential `pr.state == ...` checks all miss an unrecognized state, falling through to
    "no PR found" even though a PR record technically exists. A clean, pushed, idle branch with
    such a record would then classify `no-pr-stale` (removable with `--yes --include-stale`)
    instead of being kept for a human to look at. Must be dropped the same way a missing
    resolution date already is."""
    raw = [
        {"number": 1, "state": "DRAFT", "headRefName": "weird-state", "url": "u1"},
        {"number": 2, "state": "", "headRefName": "empty-state", "url": "u2"},
        {"number": 3, "state": "OPEN", "headRefName": "normal", "url": "u3"},
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert "weird-state" not in by_branch
    assert "empty-state" not in by_branch
    assert by_branch["normal"].number == 3


def test_pr_index_skips_non_dict_list_elements_without_crashing():
    """Review finding: a broken `gh`/proxy response returning valid JSON like `[null]` or `[1]`
    must be skipped, per this function's own documented "a malformed record is skipped, never
    crashes the fetch" contract — `item.get(...)` on a non-dict element would otherwise raise
    `AttributeError` uncaught."""
    raw = [None, 1, "oops", {"number": 3, "state": "OPEN", "headRefName": "ok"}]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert by_branch["ok"].number == 3
    assert len(by_branch) == 1


def test_pr_index_rejects_null_and_non_string_head_ref_name():
    """Review finding: `str(item["headRefName"])` would turn `null` into the literal string
    "None" — matching any REAL local branch coincidentally named "None" and driving its
    classification off garbage data. A `null`/numeric/empty `headRefName` must be skipped, not
    coerced into a fake branch name."""
    raw = [
        {"number": 1, "state": "MERGED", "headRefName": None, "mergedAt": "2026-01-01", "url": "u1"},
        {"number": 2, "state": "MERGED", "headRefName": 123, "mergedAt": "2026-01-01", "url": "u2"},
        {"number": 3, "state": "MERGED", "headRefName": "", "mergedAt": "2026-01-01", "url": "u3"},
        {"number": 4, "state": "OPEN", "headRefName": "real-branch", "url": "u4"},
    ]

    by_branch = worktree_gc._index_prs_by_branch(raw)

    assert "None" not in by_branch
    assert "123" not in by_branch
    assert list(by_branch) == ["real-branch"]


def test_make_default_pr_lookup_degrades_to_no_pr_when_gh_missing(tmp_path, monkeypatch, capsys):
    """Review-requested coverage: `make_default_pr_lookup`'s degrade path (gh missing/unauth) was
    only exercised indirectly. `gh` not found on PATH must make the returned lookup answer "no PR
    found" for every branch (never raise), with a loud stderr warning."""
    repo = _git_repo(tmp_path / "repo")

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "gh":
            raise FileNotFoundError("gh not found")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    lookup = worktree_gc.make_default_pr_lookup(repo)

    assert lookup("any-branch") is None
    err = capsys.readouterr().err
    assert "worktree-gc: warning" in err
    assert "gh" in err


# ── a prunable worktree's branch is ALSO checked for unpushed commits ───────────────
def test_prunable_worktree_with_unpushed_branch_commits_is_kept(tmp_path):
    """A worktree directory deleted by hand (not via `git worktree remove`) BEFORE its one local
    commit was ever pushed must not be auto-removed just because the directory is gone — the
    branch ref itself still holds that commit in the primary repo's object store."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    (wt / "followup.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "followup.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unpushed"],
        check=True,
    )
    shutil.rmtree(wt)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert "unpushed" in entry.reason
    assert entry.removed is False


def test_prunable_worktree_fully_pushed_is_still_removed(tmp_path):
    """Sanity check: a prunable worktree whose branch IS fully pushed must still be removed
    normally — the new check must not turn every prunable worktree into a false "dirty"."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    shutil.rmtree(wt)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "prunable"
    assert entry.removed is True


def test_prunable_unpushed_check_not_shadowed_by_same_named_tag(tmp_path):
    """Review finding: `git ... --contains <bare-name>` resolves a bare ref name through git's own
    disambiguation order, which checks `refs/tags/<name>` BEFORE `refs/heads/<name>` — a tag
    sharing the branch's name but pointing at an ALREADY-PUSHED commit would otherwise make the
    unpushed-commit check inspect the TAG's (safe) commit instead of the actual branch tip."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    # a tag with the SAME NAME as the branch, pointing at the already-pushed commit
    subprocess.run(["git", "-C", str(repo), "tag", "agent-1"], check=True)
    # now add an unpushed commit on the actual branch
    (wt / "followup.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "followup.txt"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "unpushed"],
        check=True,
    )
    shutil.rmtree(wt)

    report = worktree_gc.run_gc(
        repo, dry_run=False, yes=True, liveness_check=_no_live, pr_lookup=_no_pr
    )

    entry = report.entries[0]
    assert entry.classification == "dirty"
    assert entry.removed is False


def test_process_cwd_does_not_truncate_a_path_containing_a_unicode_line_separator(monkeypatch):
    """Review finding (Opus, round 18): `str.splitlines()` breaks on far more than `"\\n"` — also
    `\\r`/`\\v`/`\\f`/U+2028/U+2029/etc — every one of which is a legal byte in a real POSIX
    directory name, but NONE of which `lsof -Fn` itself uses as a field separator (that's always a
    literal `"\\n"`). Before this fix, a cwd containing one of those would get silently truncated
    to a strict PARENT of the real path, which fails the liveness check UNSAFE (a genuinely live
    worktree misread as not-live) for a reason distinct from the non-UTF-8-name case this same
    function already guards against."""
    # An explicit \u2028 escape, not a character pasted invisibly into the source — review asked
    # (Opus, round 20) for confirmation this isn't a plain space, which would make `split("\\n")`
    # and `str.splitlines()` behave identically and this test would pass against EITHER
    # implementation, catching nothing.
    weird_path = "/tmp/weird\u2028dir"
    assert weird_path.splitlines() != [weird_path]  # the exact property this fix depends on

    def fake_run(cmd, *args, **kwargs):
        assert cmd[0] == "lsof"
        stdout = f"p123\nn{weird_path}\n".encode("utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = worktree_gc._process_cwd("123")

    assert result == Path(weird_path)


# ── liveness snapshot: a pid that EXITED mid-scan is "not live"; a live pid lsof can't read
# still fails CLOSED (GH-353) ────────────────────────────────────────────────────────────
def _fake_kill_probe(monkeypatch, alive: dict[str, bool]) -> list[str]:
    """Monkeypatch `os.kill` as a SIGNAL-0 probe only: records every probed pid, raises
    `ProcessLookupError` for pids `alive` maps to False. Asserts `sig == 0` — a fake accepting
    any signal would let a regression that actually SIGNALS an agent process pass unnoticed."""
    probed: list[str] = []

    def fake_kill(pid, sig):
        assert sig == 0, f"liveness probe must send signal 0, not {sig}"
        probed.append(str(pid))
        if not alive[str(pid)]:
            raise ProcessLookupError(f"pid {pid} is gone")

    monkeypatch.setattr(worktree_gc.os, "kill", fake_kill)
    return probed


def test_live_process_cwds_skips_a_pid_that_exited_between_pgrep_and_lsof(tmp_path, monkeypatch, capsys):
    """GH-353: a pid matched by `pgrep` whose `lsof` lookup then fails BECAUSE THE PROCESS EXITED
    in between is, by definition, not live — it must be skipped (`continue`), NOT poison the whole
    snapshot to "untrusted → every worktree is live". Observed live: a dry run over ~/xp reported 4
    removable entries while a pass minutes later reported 8 — the difference was exactly the repos
    a since-exited agent pid had been marked "live" for. Two pids, so the test proves the loop
    CONTINUES past the dead one and still records the live one (a single-pid case can't tell
    "skipped" from "returned early with only the own cwd")."""
    real_run = subprocess.run
    live_cwd = tmp_path / "live-agent-wt"
    live_cwd.mkdir()

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n456\n", stderr="")
        if cmd[0] == "lsof":
            pid = cmd[cmd.index("-p") + 1]
            if pid == "123":  # exited between pgrep and lsof
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=f"p456\nn{live_cwd}\n".encode(), stderr=b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    probed = _fake_kill_probe(monkeypatch, {"123": False})

    result = worktree_gc._live_process_cwds()

    assert result is not None, "a vanished pid must not degrade the snapshot to untrusted"
    assert live_cwd.resolve() in result, "the loop must continue past the dead pid to the live one"
    assert len(result) == 2  # own cwd + the live agent's cwd; nothing recorded for pid 123
    assert probed == ["123"], "only the pid whose lsof failed is probed; a resolved cwd needs none"
    assert "worktree-gc: warning" not in capsys.readouterr().err


def test_live_process_cwds_fails_closed_when_a_live_pid_cwd_is_unknown(monkeypatch, capsys):
    """The fail-safe direction is KEPT for genuine uncertainty: `pgrep` matched a pid, `lsof`
    failed for it, and the signal-0 probe says the process is STILL RUNNING (permission problem,
    lsof timeout, a transient failure). A real agent may be sitting in a worktree we can't see —
    the WHOLE snapshot becomes untrusted (`None`) and the degradation is reported on stderr, not
    silently dropped as a partial (falsely "confirmed empty") result."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n", stderr="")
        if cmd[0] == "lsof":
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")  # lsof failed for pid 123
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    probed = _fake_kill_probe(monkeypatch, {"123": True})

    assert worktree_gc._live_process_cwds() is None
    assert probed == ["123"]
    err = capsys.readouterr().err
    assert "worktree-gc: warning" in err
    assert "123" in err and "still running" in err


def test_live_process_cwds_fails_closed_when_lsof_is_missing_for_a_live_pid(monkeypatch, capsys):
    """`lsof` not installed at all (`OSError` out of `subprocess.run`) for a pid that IS alive:
    nothing about that pid's cwd is knowable, so the snapshot must still degrade to untrusted —
    the probe only downgrades a failure when the pid itself is gone."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n", stderr="")
        if cmd[0] == "lsof":
            raise FileNotFoundError("lsof: command not found")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    _fake_kill_probe(monkeypatch, {"123": True})

    assert worktree_gc._live_process_cwds() is None
    assert "worktree-gc: warning" in capsys.readouterr().err


def test_live_process_cwds_probe_permission_error_still_fails_closed(monkeypatch, capsys):
    """`os.kill(pid, 0)` raising `PermissionError` means the process EXISTS (we just may not
    signal it) — that is the "alive, cwd unknown" case, never "vanished". Only `ProcessLookupError`
    (ESRCH) downgrades an lsof failure to "not live"."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n", stderr="")
        if cmd[0] == "lsof":
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
        return real_run(cmd, *args, **kwargs)

    def fake_kill(pid, sig):
        assert sig == 0
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(worktree_gc.os, "kill", fake_kill)

    assert worktree_gc._live_process_cwds() is None
    assert "worktree-gc: warning" in capsys.readouterr().err


def test_live_process_cwds_untrusted_snapshot_warns_on_stderr(monkeypatch, capsys):
    """Review finding: a degraded (untrusted) liveness snapshot used to fail SILENTLY — every
    worktree reads `live` with a reason that flatly claims a process was found there, with no
    signal anywhere that the check itself couldn't be verified. Must warn on stderr."""

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="pgrep: command not found")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = worktree_gc._live_process_cwds()

    assert result is None
    err = capsys.readouterr().err
    assert "worktree-gc: warning" in err
    assert "could not be verified" in err


def test_live_process_cwds_contains_only_own_cwd_when_pgrep_finds_nothing(monkeypatch):
    """No claude/codex/opencode process matched — the snapshot is not EMPTY, though: it always
    contains THIS process's own cwd (see `_live_process_cwds`'s docstring on why)."""
    from pathlib import Path as _Path

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")  # pgrep: no match
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert worktree_gc._live_process_cwds() == [_Path.cwd().resolve()]


# ── `_liveness_check_from_snapshot`: the two safety-critical semantics, directly ────
def test_liveness_check_from_snapshot_matches_a_subdirectory_of_the_live_cwd(tmp_path):
    """Review-requested coverage: every OTHER test injects an exact-match `_fake_liveness` — the
    real production function's SUBDIRECTORY-match behavior itself was never directly exercised. A
    process cwd'd one level BELOW a worktree root must still read that worktree as live (a process
    doesn't have to sit at the worktree's exact top level to be "using" it)."""
    wt = tmp_path / "agent-1"
    (wt / "subdir").mkdir(parents=True)
    check = worktree_gc._liveness_check_from_snapshot([(wt / "subdir").resolve()])

    assert check(wt) is True


def test_liveness_check_from_snapshot_does_not_match_the_parent_directory(tmp_path):
    """The inverse of the subdirectory case: a process cwd'd in the PARENT of a worktree (e.g. the
    primary checkout itself) must NOT make that worktree read as live — `relative_to` direction
    matters (child-contains-check, not either-direction overlap)."""
    parent = tmp_path
    wt = parent / "agent-1"
    wt.mkdir()
    check = worktree_gc._liveness_check_from_snapshot([parent.resolve()])

    assert check(wt) is False


def test_liveness_check_from_snapshot_none_means_everything_reads_live(tmp_path):
    """The untrusted-snapshot fail-safe, exercised directly: `cwds=None` (the snapshot itself
    couldn't be trusted) must make EVERY path read as live, unconditionally."""
    check = worktree_gc._liveness_check_from_snapshot(None)

    assert check(tmp_path / "anything") is True
    assert check(tmp_path) is True


def test_own_process_cwd_is_always_treated_as_live(tmp_path, monkeypatch):
    """Review finding (Codex, round 10): `rig worktree gc` run FROM INSIDE the very worktree being
    evaluated must never remove its own cwd out from under itself — the `pgrep` pattern only
    matches claude/codex/opencode process argv, missing the `rig` process (and whatever invoked
    it — a plain shell, a script) entirely. Without this, a bare `rig worktree gc --repo . --yes`
    run from inside a clean, merged linked worktree would classify its own cwd non-live and could
    force-remove the directory (and delete the branch) out from under the very invocation doing
    the removing."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")  # no agent processes
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.chdir(wt)

    result = worktree_gc._live_process_cwds()

    assert result is not None
    assert wt.resolve() in result


def test_own_cwd_unreadable_fails_safe_not_crash(monkeypatch, capsys):
    """Review finding (Opus, round 11): `Path.cwd()` raises `FileNotFoundError` when the caller's
    OWN cwd was deleted out from under it (e.g. `rm -rf` the worktree you were standing in, then
    run `rig worktree gc --repo /elsewhere`) — this was previously uncaught and would crash the
    whole command instead of degrading the liveness snapshot (fail-safe toward "live"), like every
    other failure mode this function already handles."""

    def fake_cwd():
        raise FileNotFoundError("cwd no longer exists")

    monkeypatch.setattr(worktree_gc.Path, "cwd", staticmethod(fake_cwd))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""),
    )

    result = worktree_gc._live_process_cwds()

    assert result is None
    assert "warning" in capsys.readouterr().err


# ── TOCTOU: a fresh liveness recheck runs immediately before removal ────────────────
def test_toctou_recheck_skips_removal_when_worktree_becomes_live_before_execution(tmp_path):
    """A stateful fake liveness_check: not-live during planning, live by the time
    `_execute_removals` re-checks it immediately before the destructive git call — simulating an
    agent starting a session in the window between planning and execution."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    calls = {"n": 0}

    def flaky_liveness(path: Path) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # not live on the (single) planning call, live on the recheck

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=flaky_liveness,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.removed is False
    assert entry.remove_error is None
    assert entry.skipped_reason is not None
    assert "became live" in worktree_gc.render_report(report)


def test_toctou_recheck_skips_removal_when_worktree_becomes_dirty_before_execution(tmp_path, monkeypatch):
    """Review finding: the original TOCTOU recheck only re-verified LIVENESS — a non-agent process
    (or a human) writing an uncommitted file into the worktree in that same window was still
    invisible to it. A stateful `_is_dirty` fake: clean on the (single) planning call, dirty on the
    execution recheck."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    calls = {"n": 0}

    def flaky_is_dirty(path: Path):
        calls["n"] += 1
        return (False, 0) if calls["n"] == 1 else (True, 1)

    monkeypatch.setattr(worktree_gc, "_is_dirty", flaky_is_dirty)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.removed is False
    assert entry.remove_error is None
    assert entry.skipped_reason is not None
    assert "became dirty" in worktree_gc.render_report(report)
    assert wt.exists()


def test_one_entrys_recheck_exception_does_not_abort_the_rest_of_the_removal_run(tmp_path, monkeypatch):
    """Review finding (Opus, round 18): a pre-removal recheck call can raise for reasons that are
    a property of ONE entry's path (a symlink loop makes `.resolve()` raise `RuntimeError`/
    `OSError`; `EACCES` is not among the errnos `Path.exists()`/`is_symlink()` swallow) — before
    this fix, that exception escaped `_execute_removals` entirely, aborting every LATER entry's
    removal and discarding the report for every EARLIER entry this same run had already removed.
    Verifies the OTHER entry still gets removed, and the failing one is reported via
    `remove_error`, not silently dropped or allowed to crash the whole run."""
    repo = _git_repo(tmp_path / "repo")
    wt1 = _make_worktree(repo, "agent-1")
    wt2 = _make_worktree(repo, "agent-2")

    real_recheck = worktree_gc._recheck_dirty_and_unpushed

    def flaky_recheck(repo_root, entry):
        if entry.branch == "agent-1":
            raise OSError("simulated EACCES")
        return real_recheck(repo_root, entry)

    monkeypatch.setattr(worktree_gc, "_recheck_dirty_and_unpushed", flaky_recheck)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {
                "agent-1": worktree_gc.PrInfo(number=1, state="MERGED"),
                "agent-2": worktree_gc.PrInfo(number=2, state="MERGED"),
            }
        ),
    )

    by_branch = {e.branch: e for e in report.entries}
    assert by_branch["agent-1"].removed is False
    assert "pre-removal recheck failed" in (by_branch["agent-1"].remove_error or "")
    assert by_branch["agent-2"].removed is True
    assert wt1.exists()
    assert not wt2.exists()


def test_one_entrys_classify_exception_does_not_abort_planning_for_the_rest(tmp_path):
    """Review finding (Opus, round 18): liveness is checked FIRST, absolutely, in
    `classify_worktree` (by design) — but that means a path that makes `liveness_check` itself
    raise (the same symlink-loop `.resolve()` case as the recheck above) used to escape BEFORE
    this entry's own symlink guard ever ran, aborting `plan_gc` for every OTHER worktree in the
    repo too. Must fail toward `live` (never removable) for the ONE broken entry, and classify
    every other worktree normally."""
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")
    _make_worktree(repo, "agent-2")

    def flaky_liveness(path: Path) -> bool:
        if path.name == "agent-1":
            raise OSError("simulated symlink loop")
        return False

    entries = worktree_gc.plan_gc(repo, liveness_check=flaky_liveness, pr_lookup=_no_pr)

    by_branch = {e.branch: e for e in entries}
    assert by_branch["agent-1"].classification == "live"
    assert by_branch["agent-1"].plan_removable is False
    assert "could not classify" in by_branch["agent-1"].reason
    # the other worktree was unaffected by agent-1's classification blowing up
    assert by_branch["agent-2"].classification in {"active", "no-pr-stale"}


def test_recheck_verifies_the_planned_branch_not_whatever_head_currently_is(tmp_path, monkeypatch):
    """Review finding (Opus, round 9): the pre-removal recheck must verify the SPECIFIC branch
    `_remove_worktree_and_branch` is about to `git branch -D`, not bare `HEAD` — a human/agent
    could check out a DIFFERENT, already-pushed branch in the SAME worktree directory during the
    window between planning and removal. `agent-1` is clean+pushed at planning time (classified
    `merged`); during the window (simulated via a monkeypatched `_is_dirty` side effect, since a
    real run has no pause point to inject a real checkout into) it gains an unpushed commit and
    THEN gets checked out to `other-safe` (a different, fully-pushed branch). A bare-HEAD recheck
    would see `other-safe` (safe) and wrongly proceed to `git branch -D agent-1`, destroying the
    unpushed commit; the fix checks `refs/heads/agent-1` specifically."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")
    # `other-safe` must NOT itself be checked out anywhere (git refuses to check out a branch
    # that's already checked out in another worktree) — create + push it from the PRIMARY
    # checkout instead of making a dedicated worktree for it, so it's free to be checked out
    # into `wt` below.
    subprocess.run(["git", "-C", str(repo), "branch", "other-safe"], check=True)
    _add_remote_and_push(repo, repo, "other-safe")

    real_is_dirty = worktree_gc._is_dirty
    mutated = {"done": False}

    def fake_is_dirty(path: Path):
        if path == wt and not mutated["done"]:
            mutated["done"] = True
            (wt / "unpushed.txt").write_text("unpushed local work\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt), "add", "unpushed.txt"], check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt), "commit", "-qm", "local fix"],
                check=True,
            )
            subprocess.run(["git", "-C", str(wt), "checkout", "-q", "other-safe"], check=True)
        return real_is_dirty(path)

    monkeypatch.setattr(worktree_gc, "_is_dirty", fake_is_dirty)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {
                "agent-1": worktree_gc.PrInfo(number=1, state="MERGED"),
                "other-safe": worktree_gc.PrInfo(number=2, state="MERGED"),
            }
        ),
    )

    by_branch = {e.branch: e for e in report.entries}
    assert by_branch["agent-1"].removed is False
    assert by_branch["agent-1"].skipped_reason is not None
    result = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True)
    assert "agent-1" in result.stdout  # branch (and its unpushed commit) must survive


def test_recheck_catches_a_detached_checkout_with_a_new_unique_commit(tmp_path, monkeypatch):
    """Review finding (Codex, round 14) — the MIRROR-IMAGE gap of the test above: checking ONLY
    the planned branch is not enough either. `agent-1` (clean+pushed at planning time, classified
    `merged`) is left UNTOUCHED during the window, but the worktree gets checked out to a DETACHED
    HEAD and a NEW commit is made there — never on `agent-1` at all. The planned-branch recheck
    alone would see `agent-1` still safe and proceed; but `git worktree remove --force` destroys
    the worktree's own administrative HEAD pointer, the ONLY reference to that detached commit,
    the moment it runs. The recheck must ALSO verify whatever is CURRENTLY checked out, not just
    the plan-time branch."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    _add_remote_and_push(repo, wt, "agent-1")

    # The mutation must happen strictly BETWEEN planning and the recheck, not during (or
    # overlapping) the planning call itself — otherwise planning's OWN unconditional unpushed
    # check (which also inspects current HEAD) would catch it first, correctly, but that would
    # test planning's protection, not the recheck's. `_is_dirty(wt)` is called exactly once
    # during planning and once during the recheck — mutate on the SECOND call specifically.
    real_is_dirty = worktree_gc._is_dirty
    calls = {"n": 0}

    def fake_is_dirty(path: Path):
        if path == wt:
            calls["n"] += 1
            if calls["n"] == 2:
                subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach", "HEAD"], check=True)
                (wt / "detached-work.txt").write_text("unique detached commit\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(wt), "add", "detached-work.txt"], check=True)
                subprocess.run(
                    [
                        "git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(wt), "commit", "-qm", "detached fix",
                    ],
                    check=True,
                )
        return real_is_dirty(path)

    monkeypatch.setattr(worktree_gc, "_is_dirty", fake_is_dirty)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=1, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.removed is False
    assert entry.skipped_reason is not None
    assert wt.exists()  # the worktree (and its detached commit) must survive


def test_classify_prunable_with_directory_still_present_is_kept_dirty(tmp_path):
    """Review finding (Opus, round 9): git marks a worktree `prunable` when its ADMINISTRATIVE
    gitdir link is broken/missing — NOT necessarily because the worktree's own directory is gone.
    A partial deletion (only the worktree's `.git` pointer file removed by hand) leaves the
    directory's actual files sitting there; a naive check would run `git -C <path> status` in a
    directory with no valid `.git`, which git resolves by walking UP to the PRIMARY repo and
    silently reporting on the WRONG repository. Must classify `dirty` outright whenever the
    directory still exists, never trust it as a clean prune candidate."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    (wt / ".git").unlink()  # simulate a partial hand-deletion: only the gitdir pointer is gone

    entries = worktree_gc.plan_gc(repo, liveness_check=_no_live, pr_lookup=_no_pr)

    assert len(entries) == 1
    assert entries[0].classification == "dirty"
    assert entries[0].plan_removable is False
    assert wt.is_dir()  # the directory (and whatever files it holds) is untouched
    assert wt.exists()


def test_registered_worktree_path_replaced_by_symlink_is_kept_dirty(tmp_path):
    """Review finding (Codex, round 12): a genuine `git worktree add`-created root is NEVER a
    symlink (git always creates a real directory there — the same invariant
    `riglib.worktree.remove` already refuses to operate through). If a registered worktree's path
    is replaced by a symlink to some OTHER directory, `exists()` alone would follow the link and
    happily let every downstream `git -C <path>` call operate on wherever it points. Must be
    classified `dirty` and kept, checked via `is_symlink()` specifically (not `exists()`)."""
    import shutil

    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(wt)
    wt.symlink_to(elsewhere)

    entries = worktree_gc.plan_gc(repo, liveness_check=_no_live, pr_lookup=_no_pr)

    assert len(entries) == 1
    assert entries[0].classification == "dirty"
    assert entries[0].plan_removable is False
    assert wt.is_symlink()  # untouched


# ── --dry-run never mutates anything, even with --yes ───────────────────────────────
def test_dry_run_never_mutates_even_with_yes(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=True,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    assert report.dry_run is True
    entry = report.entries[0]
    assert entry.classification == "merged"
    assert entry.removed is False
    assert entry.plan_removable is True  # it WOULD be removed — just not this run
    assert wt.exists()
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True)
    assert "agent-1" in result.stdout


def test_bare_gc_no_yes_defaults_to_dry_run(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=False,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    assert report.dry_run is True


def test_dry_run_yes_message_does_not_tell_user_to_pass_yes_again(tmp_path):
    """`--yes --dry-run`: the report must not say "pass --yes" when --yes was already given —
    review finding: the old wording was misleading in exactly this combination."""
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")

    report = worktree_gc.run_gc(
        repo,
        dry_run=True,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    rendered = worktree_gc.render_report(report)
    assert "pass --yes" not in rendered
    assert "would be removed" in rendered


# ── a `git worktree lock`ed tree is always kept, never even attempted ───────────────
def test_locked_worktree_is_kept_even_when_otherwise_removable(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(wt), "--reason", "manual hold"], check=True)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        include_stale=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.classification == "active"
    assert "locked" in entry.reason
    assert entry.removed is False
    assert wt.exists()


# ── a branch-delete failure AFTER a successful worktree removal is never hidden ─────
def test_branch_delete_failure_is_reported_not_hidden_behind_removed(tmp_path, monkeypatch):
    """Review finding: `_action_word` used to check `removed` before `remove_error`, so a
    worktree-removed-but-branch-D-failed entry rendered as a bare "removed" with the stranded-
    branch error silently dropped — the one failure this module's two-step contract exists to
    surface."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    real_run = subprocess.run
    branch_delete_cmd = ["git", "-C", str(repo), "branch", "-D", "--", "agent-1"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == branch_delete_cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"simulated failure\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    entry = report.entries[0]
    assert entry.removed is True  # the worktree really is gone
    assert entry.remove_error is not None
    assert not wt.exists()

    rendered = worktree_gc.render_report(report)
    assert "branch -D failed" in rendered
    assert "action: removed\n" not in rendered  # must not render a bare, error-hiding "removed"


# ── --older-than-days must be positive ───────────────────────────────────────────────
def test_cli_worktree_gc_rejects_non_positive_older_than_days(tmp_path, capsys):
    repo = _git_repo(tmp_path / "repo")

    rc = main(["worktree", "gc", "--repo", str(repo), "--older-than-days", "0"])

    assert rc == 2
    assert "--older-than-days" in capsys.readouterr().out


# ── disk-usage summing only for removable/removed entries ───────────────────────────
def test_disk_usage_only_computed_for_removable_entries(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    kept_wt = _make_worktree(repo, "kept")  # active — never removable
    (kept_wt / "big.bin").write_bytes(b"0" * 5000)
    removed_wt = _make_worktree(repo, "removed")
    (removed_wt / "big.bin").write_bytes(b"0" * 5000)
    subprocess.run(["git", "-C", str(removed_wt), "add", "big.bin"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(removed_wt), "commit", "-qm", "data"],
        check=True,
    )  # must be CLEAN to classify as merged, not dirty
    # and its new commit must be reachable from SOMEWHERE other than its own branch — push it, or
    # `_has_unpushed_commits` correctly reads it as unique local-only work and keeps it as `dirty`
    _add_remote_and_push(repo, removed_wt, "removed")

    report = worktree_gc.run_gc(
        repo,
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup({"removed": worktree_gc.PrInfo(number=1, state="MERGED")}),
    )

    by_branch = {e.branch: e for e in report.entries}
    assert by_branch["kept"].size_bytes is None
    assert by_branch["removed"].size_bytes is not None
    assert by_branch["removed"].size_bytes >= 5000
    assert report.total_reclaimed_bytes >= 5000


def test_gc_error_when_git_worktree_list_fails(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    real_run = subprocess.run
    list_cmd = ["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]

    def fake_run(cmd, *args, **kwargs):
        if cmd == list_cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(worktree_gc.WorktreeGcError):
        worktree_gc.list_worktrees(repo)


# ── the PRIMARY worktree is never a gc candidate, even invoked from a linked tree ──
def test_list_worktrees_excludes_primary_even_when_repo_root_is_a_linked_worktree(tmp_path):
    """`git worktree list` always reports the primary FIRST, regardless of which worktree's
    directory you run it from. If gc instead compared `info.path.resolve() == repo_root.resolve()`
    to spot the primary, calling it with `repo_root` pointed at a LINKED worktree (exactly what
    `rig worktree gc --repo <a-worktree>`, or `rig status` run from inside one, does) would
    misidentify things two ways at once: it would wrongly EXCLUDE `wt` itself (it matches
    `repo_root`) and wrongly INCLUDE the real primary as a gc candidate. Both must be the other
    way around: the real primary is never returned, and `wt` — a perfectly normal linked worktree,
    just one this call happens to be rooted at — IS returned as an ordinary candidate."""
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    infos = worktree_gc.list_worktrees(wt)  # note: called AS IF `wt` were the repo root

    paths = {info.path.resolve() for info in infos}
    assert repo.resolve() not in paths  # the true primary is never a candidate
    assert wt.resolve() in paths  # `wt` is still a normal candidate, not mistaken for the primary


def test_list_worktrees_handles_a_bare_primary_without_dropping_the_first_linked_worktree(tmp_path):
    """Review finding: `_parse_worktree_record` returns `None` for a BARE primary entry (it has
    no worktree to gc). If the old code filtered `None` records out BEFORE slicing off index 0,
    a bare primary would already be gone from the filtered list by the time the slice ran — so the
    slice would incorrectly drop the FIRST REAL linked worktree instead of the (already-absent)
    bare primary. Slicing the RAW records first (by position) fixes this regardless of whether
    record 0 happens to parse to a worktree or not."""
    bare = tmp_path / "repo.git"
    # `init.defaultBranch=trunk` (deliberately NOT "main") makes this regression hermetic — review-
    # caught (Codex, CI-fix round): on a runner where the global default already happens to be
    # "main", the old implicit-HEAD `worktree add -b feat1 <wt1>` call below would ALSO pass,
    # silently making the fixed-vs-broken behavior indistinguishable. Pinning it away from "main"
    # here means this test actually exercises the unborn-HEAD failure mode being guarded against,
    # regardless of whatever this machine's/CI's own git config defaults to.
    subprocess.run(
        ["git", "-c", "init.defaultBranch=trunk", "init", "-q", "--bare", str(bare)], check=True
    )
    wt_main = tmp_path / "wt-main"
    subprocess.run(["git", "-C", str(bare), "worktree", "add", "-q", "-b", "main", str(wt_main)], check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(wt_main), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    wt1 = tmp_path / "wt1"
    # Explicit start-point ("main"), not implicit HEAD: a bare repo's OWN `HEAD` stays an unborn
    # symbolic ref to whatever `init.defaultBranch` resolves to on THIS machine/CI runner (which
    # may not be "main") until something actually commits on that specific ref — the `wt_main`
    # commit above landed on the "main" branch a fresh linked worktree created, not necessarily on
    # the bare repo's own default branch. Relying on implicit HEAD made this test pass locally
    # (where `init.defaultBranch=main`) and fail in CI ("fatal: invalid reference: HEAD") wherever
    # it isn't — a real, environment-dependent bug in the fixture itself, not the code under test.
    subprocess.run(
        ["git", "-C", str(bare), "worktree", "add", "-q", "-b", "feat1", str(wt1), "main"], check=True
    )

    infos = worktree_gc.list_worktrees(bare)

    paths = {info.path.resolve() for info in infos}
    assert paths == {wt_main.resolve(), wt1.resolve()}  # BOTH linked worktrees, neither dropped


def test_run_gc_anchored_at_a_removable_linked_worktree_removes_both(tmp_path):
    """Review finding: `rig worktree gc --repo <a-worktree>` resolves `repo_root` to THAT LINKED
    worktree's own path (via `detect_environment`'s `git rev-parse --show-toplevel`), not
    necessarily the primary. If mutating git calls stayed anchored to that path, removing the very
    worktree `git -C <that-path>` was invoked from would strand its own branch AND break every
    subsequent `git -C <that-now-gone-path>` call for the REST of the run. Calling `run_gc` with
    `repo_root` set to one of two removable linked worktrees must still cleanly remove BOTH."""
    repo = _git_repo(tmp_path / "repo")
    wt1 = _make_worktree(repo, "agent-1")
    wt2 = _make_worktree(repo, "agent-2")

    report = worktree_gc.run_gc(
        wt1,  # NOTE: repo_root is a LINKED worktree, not the primary
        dry_run=False,
        yes=True,
        liveness_check=_no_live,
        pr_lookup=_fake_pr_lookup(
            {
                "agent-1": worktree_gc.PrInfo(number=1, state="MERGED"),
                "agent-2": worktree_gc.PrInfo(number=2, state="MERGED"),
            }
        ),
    )

    assert len(report.entries) == 2
    by_branch = {e.branch: e for e in report.entries}
    assert by_branch["agent-1"].removed is True
    assert by_branch["agent-1"].remove_error is None
    assert by_branch["agent-2"].removed is True
    assert by_branch["agent-2"].remove_error is None
    assert not wt1.exists()
    assert not wt2.exists()
    result = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True)
    assert "agent-1" not in result.stdout
    assert "agent-2" not in result.stdout


# ── `rig status` stale-worktree summary ──────────────────────────────────────────
def test_status_reports_stale_worktree_count(tmp_path, capsys, monkeypatch, fake_agent_tools):
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(
        worktree_gc,
        "make_default_pr_lookup",
        lambda repo_root: _fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=5, state="MERGED")}),
    )

    rc = main(["status", "-C", str(repo)])

    out = capsys.readouterr().out
    assert "stale worktree" in out
    assert "1 merged" in out
    assert rc in (0, 3, 5)  # stale-worktree note must not itself change the drift exit code


def test_status_silent_when_no_stale_worktrees(tmp_path, capsys, monkeypatch, fake_agent_tools):
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)

    main(["status", "-C", str(repo)])

    out = capsys.readouterr().out
    assert "stale worktree" not in out


def test_status_skip_worktree_gc_env_opt_out(tmp_path, capsys, monkeypatch, fake_agent_tools):
    """Review-requested coverage: `RIG_STATUS_SKIP_WORKTREE_GC=1` must skip the stale-worktree
    check ENTIRELY — no classification, no `gh`/`pgrep` calls at all — even for a repo that
    genuinely has a stale worktree that would otherwise be reported."""
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    monkeypatch.setenv("RIG_STATUS_SKIP_WORKTREE_GC", "1")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("stale_worktree_counts must not be called when the opt-out is set")

    monkeypatch.setattr(worktree_gc, "stale_worktree_counts", fail_if_called)

    rc = main(["status", "-C", str(repo)])

    assert "stale worktree" not in capsys.readouterr().out
    assert rc in (0, 3, 5)


def test_status_survives_gc_failure(tmp_path, capsys, monkeypatch, fake_agent_tools):
    """A `gh`/git failure underneath the stale-worktree check must never break `rig status`."""
    repo = _git_repo(tmp_path / "repo")
    _make_worktree(repo, "agent-1")
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))

    def _boom(repo_root, **kwargs):
        raise worktree_gc.WorktreeGcError("simulated failure")

    monkeypatch.setattr(worktree_gc, "stale_worktree_counts", _boom)

    rc = main(["status", "-C", str(repo)])

    assert rc in (0, 3, 5)


# ── CLI wiring end to end ────────────────────────────────────────────────────────
def test_cli_worktree_gc_explicit_repo_reports_without_removing(tmp_path, capsys, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(
        worktree_gc, "make_default_pr_lookup",
        lambda repo_root: _fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    rc = main(["worktree", "gc", "--repo", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "merged" in out
    assert "would be removed" in out
    assert wt.exists()


def test_cli_worktree_gc_yes_actually_removes(tmp_path, capsys, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(
        worktree_gc, "make_default_pr_lookup",
        lambda repo_root: _fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    rc = main(["worktree", "gc", "--repo", str(repo), "--yes"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "removed" in out
    assert not wt.exists()


def test_cli_worktree_gc_dry_run_overrides_yes(tmp_path, capsys, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    wt = _make_worktree(repo, "agent-1")

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(
        worktree_gc, "make_default_pr_lookup",
        lambda repo_root: _fake_pr_lookup({"agent-1": worktree_gc.PrInfo(number=42, state="MERGED")}),
    )

    rc = main(["worktree", "gc", "--repo", str(repo), "--yes", "--dry-run"])

    assert rc == 0
    assert wt.exists()


def test_cli_worktree_gc_not_a_repo(tmp_path, capsys):
    not_git = tmp_path / "plain"
    not_git.mkdir()

    rc = main(["worktree", "gc", "--repo", str(not_git)])

    from riglib import errors

    assert rc == errors.EXIT_NOT_A_REPO


def test_cli_worktree_gc_explicit_empty_repo_does_not_fan_out(tmp_path, monkeypatch, capsys):
    """Review finding (Codex, round 16): `--repo ''` (an EXPLICIT empty string — e.g. a shell
    variable used for `--repo` that happened to be unset) must be treated as an explicit single-
    repo target, NOT as "omitted" — the old `if repo_arg:` truthiness check treated an empty
    string identically to "not given" and silently widened to the machine-wide, potentially
    destructive registry fan-out instead of the single-repo path the caller asked for."""
    from riglib import errors

    monkeypatch.chdir(tmp_path)  # an empty path resolves to the cwd — make that a non-repo

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the registry fan-out must not run for an explicit --repo ''")

    monkeypatch.setattr(RepositoryRegistry, "load", staticmethod(fail_if_called))

    rc = main(["worktree", "gc", "--repo", ""])

    assert rc == errors.EXIT_NOT_A_REPO


def test_cli_worktree_gc_no_registry_reports_and_exits_zero(tmp_path, capsys, monkeypatch):
    """`--repo` omitted with an empty/absent registry is a soft "nothing to do", not an error."""
    monkeypatch.setattr(
        RepositoryRegistry, "load", classmethod(lambda cls, path=None: RepositoryRegistry.empty())
    )

    rc = main(["worktree", "gc"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no rig-managed repositories" in out


def test_cli_worktree_gc_multi_repo_via_registry(tmp_path, capsys, monkeypatch):
    """`--repo` omitted fans out over every repo the repository registry already knows about."""
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    _make_worktree(repo_a, "agent-1")
    _make_worktree(repo_b, "agent-2")

    registry = RepositoryRegistry(
        repositories=[
            RepositoryEntry(id="a", path=str(repo_a), name="repo-a", root=str(tmp_path)),
            RepositoryEntry(id="b", path=str(repo_b), name="repo-b", root=str(tmp_path)),
        ]
    )
    registry.save()  # writes under the isolated $XDG_CONFIG_HOME the autouse fixture sets

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _no_pr)

    rc = main(["worktree", "gc"])

    assert rc == 0
    out = capsys.readouterr().out
    assert str(repo_a) in out
    assert str(repo_b) in out


def test_cli_worktree_gc_bad_registry_entry_gets_documented_exit_code_and_others_still_run(
    tmp_path, capsys, monkeypatch
):
    """Review finding: a stale/hand-edited registry entry pointing at a non-repo used to reach
    `git worktree list` directly and surface as a generic exit 2 (`git worktree list failed`), not
    the documented exit 6 ("not a git repository") `--repo` itself gives for the same input. The
    bad entry must be preflighted the SAME way, AND must not stop the other repos from running."""
    repo_a = _git_repo(tmp_path / "repo-a")
    _make_worktree(repo_a, "agent-1")
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    registry = RepositoryRegistry(
        repositories=[
            RepositoryEntry(id="a", path=str(repo_a), name="repo-a", root=str(tmp_path)),
            RepositoryEntry(id="bad", path=str(not_a_repo), name="not-a-repo", root=str(tmp_path)),
        ]
    )
    registry.save()

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _no_pr)

    from riglib import errors

    rc = main(["worktree", "gc"])

    assert rc == errors.EXIT_NOT_A_REPO
    out = capsys.readouterr().out
    assert str(repo_a) in out  # the good repo still ran and printed its report
    assert "not a git repository" in out


def test_cli_worktree_gc_empty_registry_path_is_skipped_not_widened_to_cwd(
    tmp_path, capsys, monkeypatch
):
    """Review finding (Codex, round 22): `Path("")` resolves to the CURRENT DIRECTORY — a
    hand-edited/corrupted registry entry with an empty `path` used to reach that conversion
    unvalidated, which could then run a destructive `--yes` fan-out against whatever unrelated
    repo `rig` happens to be invoked from, never validated as the actually-registered repo. Must
    be skipped and reported per-entry, with the OTHER, valid entry still processed."""
    repo_a = _git_repo(tmp_path / "repo-a")
    _make_worktree(repo_a, "agent-1")

    registry = RepositoryRegistry(
        repositories=[
            RepositoryEntry(id="a", path=str(repo_a), name="repo-a", root=str(tmp_path)),
            RepositoryEntry(id="empty-path", path="", name="empty-path", root=str(tmp_path)),
        ]
    )
    registry.save()

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _no_pr)

    from riglib import errors

    rc = main(["worktree", "gc"])

    assert rc == errors.EXIT_NOT_A_REPO
    out = capsys.readouterr().out
    assert str(repo_a) in out  # the good repo still ran and printed its report
    assert "empty-path" in out and "invalid path" in out


def test_cli_worktree_gc_null_registry_path_degrades_the_whole_fan_out_not_a_crash(
    tmp_path, capsys, monkeypatch
):
    """Review finding (Codex, round 22): a non-string `path` (`RepositoryRegistry.load` only
    type-checks the tag arrays, per its own docstring) makes `RepositoryRegistry.select()`'s own
    sort-by-path raise `TypeError` for the WHOLE call, before this module's own per-entry
    validation ever gets a chance to skip just the one bad row — fixing `select()` itself is out
    of scope here (shared code every registry consumer uses, not owned by this ticket). Must
    degrade to a reported failure, the same as an unreadable registry file already does, rather
    than letting the exception escape as an unhandled crash."""
    repo_a = _git_repo(tmp_path / "repo-a")
    _make_worktree(repo_a, "agent-1")

    registry = RepositoryRegistry(
        repositories=[
            RepositoryEntry(id="a", path=str(repo_a), name="repo-a", root=str(tmp_path)),
            RepositoryEntry(id="null-path", path=None, name="null-path", root=str(tmp_path)),
        ]
    )
    registry.save()

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _no_pr)

    from riglib import errors

    rc = main(["worktree", "gc"])

    assert rc == errors.EXIT_NOT_A_REPO
    out = capsys.readouterr().out
    assert "malformed" in out


def test_cli_worktree_gc_unexpected_exception_in_one_repo_does_not_abort_the_fan_out(
    tmp_path, capsys, monkeypatch
):
    """Review finding (Opus, round 15): `_run_worktree_gc_for_repo` used to catch only
    `WorktreeGcError` — any OTHER exception `run_gc` could raise (e.g. a `PermissionError` from a
    directory whose permissions changed underneath it) would abort the WHOLE multi-repo registry
    fan-out, silently skipping every repo after it. One repo's `run_gc` raising an unexpected
    exception must degrade to a reported per-repo failure (exit `EXIT_INTERNAL`), and the OTHER
    repo must still run."""
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    _make_worktree(repo_b, "agent-1")

    registry = RepositoryRegistry(
        repositories=[
            RepositoryEntry(id="a", path=str(repo_a), name="repo-a", root=str(tmp_path)),
            RepositoryEntry(id="b", path=str(repo_b), name="repo-b", root=str(tmp_path)),
        ]
    )
    registry.save()

    monkeypatch.setattr(worktree_gc, "_default_liveness_factory", lambda: _no_live)
    monkeypatch.setattr(worktree_gc, "make_default_pr_lookup", lambda repo_root: _no_pr)

    real_run_gc = worktree_gc.run_gc

    def flaky_run_gc(repo_root, **kwargs):
        if str(repo_root) == str(repo_a.resolve()):
            raise PermissionError("simulated: directory permissions changed underneath it")
        return real_run_gc(repo_root, **kwargs)

    monkeypatch.setattr(worktree_gc, "run_gc", flaky_run_gc)

    from riglib import errors

    rc = main(["worktree", "gc"])

    assert rc == errors.EXIT_INTERNAL
    out = capsys.readouterr().out
    assert "unexpected failure" in out
    assert str(repo_b) in out  # repo-b still ran despite repo-a's crash


# ── report rendering escapes terminal control characters ────────────────────────────
def test_sanitize_for_terminal_escapes_esc_and_other_control_bytes():
    """Review finding: a worktree path or branch name containing ANSI/OSC control bytes (legal on
    POSIX) must never reach the terminal raw — that would let a maliciously crafted path inject
    deceptive text or cursor/clipboard-control sequences into an operator's read of the report."""
    dangerous = "before\x1b[31mFAKE ERROR\x1b[0mafter"

    sanitized = worktree_gc._sanitize_for_terminal(dangerous)

    assert "\x1b" not in sanitized
    assert "\\x1b" in sanitized
    assert "before" in sanitized and "after" in sanitized


def test_render_report_sanitizes_branch_and_path(tmp_path):
    info = worktree_gc.WorktreeInfo(path=tmp_path / "wt\x1b[31m", branch="agent\x1b[0m-1")
    classified = worktree_gc.ClassifiedWorktree(info, "active", "no PR found; recent activity", removable_class=False)
    entry = worktree_gc.GcEntry(classified, plan_removable=False)
    report = worktree_gc.GcReport(repo_root=tmp_path, entries=[entry], dry_run=True)

    rendered = worktree_gc.render_report(report)

    assert "\x1b" not in rendered


def test_sanitize_for_terminal_escapes_lone_surrogates():
    """Review finding: `_decode` uses `errors="surrogateescape"` so a non-UTF-8 byte round-trips
    losslessly through `str` as a lone surrogate — printing that to a strict-UTF-8 stdout (the
    Linux default) raises an uncaught `UnicodeEncodeError` at print time, losing the report of
    what was just removed. Lone surrogates must be escaped too, not just C0/DEL control bytes."""
    dangerous = "path-with-\udcff-bad-byte"

    sanitized = worktree_gc._sanitize_for_terminal(dangerous)

    assert "\udcff" not in sanitized
    assert "\\u" in sanitized
    sanitized.encode("utf-8")  # must not raise


def test_parse_iso8601_z_rejects_non_string_input():
    """Review finding: a malformed `gh` record could carry a numeric/list `mergedAt`/`closedAt`
    instead of a string — `value.replace(...)` on a non-string would otherwise raise an uncaught
    `AttributeError`, violating this module's "a malformed record is skipped, never crashes"
    contract."""
    assert worktree_gc._parse_iso8601_z(12345) is None
    assert worktree_gc._parse_iso8601_z(["2026-01-01"]) is None
    assert worktree_gc._parse_iso8601_z({"date": "2026-01-01"}) is None
    assert worktree_gc._parse_iso8601_z(None) is None
    assert worktree_gc._parse_iso8601_z("2026-01-01T00:00:00Z") is not None
