"""core.bare corruption scanner — detect a working checkout that falsely claims to be bare.

Every test builds a THROWAWAY git repo under ``tmp_path`` (never the developer's real repos) and
sets ``core.bare`` explicitly, so the suite is hermetic and never mutates a machine repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from riglib import core_bare, errors


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Pin hooks/signing off so the dev machine's global pre-commit gate + GPG can't no-op a
    # test commit, and never prompt — these repos are hermetic throwaways under tmp_path.
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _make_checkout(tmp_path: Path, name: str = "repo") -> Path:
    """A throwaway non-bare git repo with one commit (the working-checkout layout: a ``.git`` dir)."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@rig.test")
    _git(repo, "config", "user.name", "rig-test")
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def _make_bare_with_worktree(tmp_path: Path) -> Path:
    """A GENUINE bare repo (core.bare=true BY DESIGN) plus a legitimate linked worktree.

    Returns the worktree path. This is the false-positive case the scanner must NOT flag: the
    worktree reads ``core.bare=true`` from the shared config yet is a healthy work tree.
    """
    seed = _make_checkout(tmp_path, name="seed")
    bare = tmp_path / "genuine.git"
    _git(tmp_path, "init", "-q", "-b", "main", "--bare", str(bare))
    _git(seed, "push", "-q", str(bare), "main")
    wt = tmp_path / "bare-wt"
    _git(bare, "worktree", "add", "-q", str(wt), "main")
    return wt


def test_healthy_checkout_is_clean(tmp_path):
    repo = _make_checkout(tmp_path)
    assert core_bare.scan_repo(repo) == []


def test_corrupted_checkout_is_flagged(tmp_path):
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    findings = core_bare.scan_repo(repo)
    assert len(findings) == 1
    assert findings[0].path == repo.resolve()
    assert findings[0].git_dir == (repo.resolve() / ".git")


def test_genuine_bare_repo_is_not_flagged(tmp_path):
    """A real bare repo has core.bare=true BY DESIGN — it must never be reported as corruption."""
    bare = tmp_path / "genuine.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert _git(bare, "config", "--get", "core.bare").stdout.strip() == "true"
    assert core_bare.scan_repo(bare) == []


def test_legit_bare_repo_worktree_is_not_flagged(tmp_path):
    """A linked worktree of a GENUINE bare repo (common bare+worktrees workflow) reads
    core.bare=true from the SHARED config but is a healthy work tree — it must NOT be flagged.

    This is the destructive-fix trap: a raw config read flags it, and --fix would then write
    core.bare=false into the genuine bare repo's shared config and BREAK it. The scanner uses the
    worktree-aware `rev-parse --is-bare-repository` (false for this worktree) so it is excluded.
    """
    wt = _make_bare_with_worktree(tmp_path)
    # the shared config genuinely reads true, yet the worktree itself is not bare …
    assert _git(wt, "config", "--type=bool", "--get", "core.bare").stdout.strip() == "true"
    assert _git(wt, "rev-parse", "--is-bare-repository").stdout.strip() == "false"
    # … so the scanner must report NOTHING (no false positive, no candidate for --fix).
    assert core_bare.scan_repo(wt) == []


def test_corruption_detected_in_separate_git_dir_checkout(tmp_path):
    """A `--separate-git-dir` checkout (`.git` is a FILE pointing at a detached gitdir) that goes
    core.bare=true is broken at the WORK TREE, but `git worktree list` reports the separate gitdir
    (marked bare), not the work tree. The scan must still flag the work tree — it does because
    `_worktree_roots` always seeds the enclosing checkout root (Codex review finding)."""
    work = tmp_path / "work"
    work.mkdir()
    gitdir = tmp_path / "separate-gitdir"
    _git(tmp_path, "init", "-q", "-b", "main", "--separate-git-dir", str(gitdir), str(work))
    _git(work, "config", "user.email", "t@rig.test")
    _git(work, "config", "user.name", "rig-test")
    (work / "f").write_text("x", encoding="utf-8")
    _git(work, "add", "f")
    _git(work, "commit", "-qm", "i")
    _git(work, "config", "core.bare", "true")
    # the THING UNDER TEST: the broken work tree must be flagged even though the worktree listing
    # (which on this git points at the separate gitdir, not `work`) doesn't name it.
    flagged = {f.path for f in core_bare.scan_repo(work)}
    assert work.resolve() in flagged
    # best-effort observation of the trap this guards against (git-version-dependent, so it informs
    # rather than gates — the real assertion is above): the work tree is genuinely broken, and the
    # listing names the separate gitdir, not `work`.
    if "this operation must be run in a work tree" not in _git(work, "status", "-sb", check=False).stderr:
        return
    listed = [
        ln[len("worktree ") :]
        for ln in _git(work, "worktree", "list", "--porcelain", check=False).stdout.splitlines()
        if ln.startswith("worktree ")
    ]
    if str(work) in listed:  # a future git that lists the work tree makes the trap moot, not failed
        return
    assert str(gitdir) in listed


def test_corruption_detected_when_worktree_list_fails(tmp_path, monkeypatch):
    """If `git worktree list` itself fails on a badly-corrupted repo, the fallback resolves the
    root via `rev-parse --absolute-git-dir` and STILL flags the corruption (the incident's core
    scenario, where the listing may be unreliable)."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    real_git = core_bare._git

    def _git_no_worktree_list(args, cwd):
        if args[:2] == ["worktree", "list"]:
            return None  # simulate the listing failing on this corrupted repo
        return real_git(args, cwd)

    monkeypatch.setattr(core_bare, "_git", _git_no_worktree_list)
    flagged = {f.path for f in core_bare.scan_repo(repo)}
    assert repo.resolve() in flagged  # the fallback root still catches it


def test_corruption_detected_from_a_subdirectory(tmp_path):
    """`rig doctor` runs from cwd, which may be a SUBDIR; the corruption lives at the root (.git is
    only there). Scanning from a subdir must still anchor at the repo root and flag it."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    flagged = {f.path for f in core_bare.scan_repo(sub)}
    assert repo.resolve() in flagged


def test_non_git_dir_yields_nothing(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert core_bare.scan_repo(plain) == []


def test_shared_core_bare_flags_main_checkout_not_worktree(tmp_path):
    """Setting core.bare=true on a non-bare repo that HAS a worktree breaks only the MAIN checkout.

    ``core.bare`` lives in the shared config, but git resolves it per-path: the main checkout
    becomes effectively bare (status/diff/commit fail there) while the linked worktree keeps a
    working tree and operates normally. The scanner — using the worktree-aware
    ``rev-parse --is-bare-repository`` — must flag ONLY the main checkout, never the still-healthy
    worktree (flagging it, and --fixing it, would be the false-positive/destructive trap).
    """
    repo = _make_checkout(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    _git(repo, "config", "core.bare", "true")
    flagged = {f.path for f in core_bare.scan_repo(repo)}
    assert repo.resolve() in flagged  # the main checkout IS broken → flagged
    assert wt.resolve() not in flagged  # the worktree still works → NOT flagged


def test_fix_command_quotes_paths_with_spaces(tmp_path):
    """The human-readable fix command must survive a path with spaces (copy-paste safety)."""
    repo = _make_checkout(tmp_path, name="my repo")
    _git(repo, "config", "core.bare", "true")
    err = core_bare.finding_to_error(core_bare.scan_repo(repo)[0])
    # the quoted path round-trips through the shell as ONE argument
    import shlex

    assert "my repo" in err.fix
    git_idx = err.fix.index("git -C ") + len("git -C ")
    after = shlex.split(err.fix[git_idx:])
    assert after[0] == str(repo.resolve())  # the path is a single shell token, not two


def test_finding_renders_structured_error(tmp_path):
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    finding = core_bare.scan_repo(repo)[0]
    err = core_bare.finding_to_error(finding)
    assert isinstance(err, errors.RepoCorruptError)
    assert err.exit_code == errors.EXIT_REPO_CORRUPT
    assert str(repo.resolve()) in err.what
    assert "core.bare false" in err.fix


def test_fix_repairs_the_corruption(tmp_path):
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    finding = core_bare.scan_repo(repo)[0]
    assert core_bare.fix_core_bare(finding) is True
    # after the fix, core.bare is false AND a re-scan finds nothing
    assert _git(repo, "config", "--get", "core.bare").stdout.strip() == "false"
    assert core_bare.scan_repo(repo) == []


def test_fix_reports_failure_when_bareness_survives_local_write(tmp_path):
    """If core.bare=true comes from a WORKTREE/INCLUDE scope, a plain --local write doesn't clear
    the effective bareness — fix_core_bare must re-check and report False, not a false success."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "core.bare", "true")  # bareness sourced from worktree scope
    finding = core_bare.scan_repo(repo)[0]
    # the local-write succeeds but the repo is STILL effectively bare → fix must report failure.
    assert core_bare.fix_core_bare(finding) is False
    assert _git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "true"


def test_scan_is_read_only_never_mutates_config(tmp_path):
    """The plain scan must NEVER touch config — only the explicit fix_core_bare repairs.

    A corrupted repo stays corrupted after a scan (the read-only `rig doctor` invariant); repair
    happens only under `--fix`.
    """
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    before = _git(repo, "config", "--get", "core.bare").stdout.strip()
    core_bare.scan_repo(repo)
    core_bare.scan_repo(repo)  # twice, to be sure
    after = _git(repo, "config", "--get", "core.bare").stdout.strip()
    assert before == after == "true"  # scan left the (still-corrupted) config untouched


def test_scan_is_idempotent_and_deduped(tmp_path):
    """Two list entries resolving to the same path must not double-report."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    findings = core_bare.scan_repo(repo)
    paths = [f.path for f in findings]
    assert len(paths) == len(set(paths))


@pytest.mark.parametrize(
    "leak",
    [
        {"GIT_DIR": ".git", "GIT_WORK_TREE": "."},
        {"GIT_COMMON_DIR": ".git"},  # core.bare is read from $GIT_COMMON_DIR — must be scrubbed too
    ],
)
def test_scan_ignores_leaked_git_env(tmp_path, monkeypatch, leak):
    """A leaked git-redirection env (e.g. from a pre-commit hook) must NOT redirect the scan/--fix
    to another repo — the verdict binds to the scanned path's real repo, not the ambient env."""
    healthy = _make_checkout(tmp_path, name="healthy")
    corrupt = _make_checkout(tmp_path, name="corrupt")
    _git(corrupt, "config", "core.bare", "true")
    # point the ambient env at the CORRUPT repo, then scan the HEALTHY one.
    for key, rel in leak.items():
        monkeypatch.setenv(key, str(corrupt / rel) if rel != "." else str(corrupt))
    assert core_bare.scan_repo(healthy) == []  # bound to `healthy`, unfooled by the leaked env


def test_git_calls_scrub_redirection_and_config_env(tmp_path, monkeypatch):
    """Every git invocation must run with the redirection / config-injection env stripped — the
    contract that protects the verdict AND the destructive --fix from a leaked hook env."""
    captured = {}

    def _fake_run(argv, **kwargs):
        captured.update(kwargs.get("env") or {})

        class R:
            returncode = 0
            stdout = "false"

        return R()

    leaked = {
        "GIT_DIR": "/x/.git",
        "GIT_WORK_TREE": "/x",
        "GIT_COMMON_DIR": "/x/.git",
        "GIT_CONFIG": "/x/cfg",
        "GIT_CONFIG_GLOBAL": "/x/g",
        "GIT_CONFIG_SYSTEM": "/x/s",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_PARAMETERS": "'core.bare'='true'",
        "GIT_CEILING_DIRECTORIES": "/x",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "PATH": os.environ.get("PATH", ""),  # a normal var must survive
    }
    for k, v in leaked.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(core_bare.subprocess, "run", _fake_run)
    core_bare._git(["rev-parse", "--is-bare-repository"], tmp_path)
    for k in leaked:
        if k == "PATH":
            assert captured.get("PATH") == leaked["PATH"]  # unrelated env preserved
        else:
            assert k not in captured, f"{k} leaked into the git subprocess env"


def test_scan_never_raises_on_missing_git(tmp_path, monkeypatch):
    """If the git binary can't run, the scan degrades to [] rather than crashing doctor."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")

    def _boom(*_a, **_k):
        raise OSError("git not found")

    monkeypatch.setattr(core_bare.subprocess, "run", _boom)
    assert core_bare.scan_repo(repo) == []


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_non_true_core_bare_is_not_flagged(tmp_path, value):
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", value)
    assert core_bare.scan_repo(repo) == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "True", "YES"])
def test_bool_true_equivalents_are_flagged(tmp_path, value):
    """Git treats 1/yes/on (any case) as bare too — a checkout breaks identically, so the scanner
    must flag every boolean-true spelling, not just the literal `true` (review finding)."""
    repo = _make_checkout(tmp_path)
    _git(repo, "config", "core.bare", value)
    findings = core_bare.scan_repo(repo)
    assert len(findings) == 1, f"core.bare={value} should be flagged as corruption"
    assert findings[0].path == repo.resolve()
