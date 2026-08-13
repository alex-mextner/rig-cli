"""``rig worktree create`` — standardized agent worktree creation.

The ecosystem around rig had grown several different, uncoded conventions for where an
agent/harness worktree lands: Claude Code's own throwaway ``.claude/worktrees/`` (a separate,
already-solved problem — see ``GITIGNORE_DEFAULT_ENTRIES`` in :mod:`riglib.config``, ignored
machine-wide via the GLOBAL git excludes file), a bare ``.worktrees/`` some repos had adopted by
hand INSIDE the repo, and the same ``.worktrees/`` name used one directory level ABOVE the repo by
some harnesses. None of it was rig-owned, so nothing kept the three in sync or registered the
directory anywhere.

This module is the one place a NEW rig-created worktree gets planted, so every repo converges on
a single standardized, discoverable, IN-repo location:

    <repo>/.worktrees/<name>

Before running ``git worktree add``, it registers ``.worktrees/`` in the repo's ``.git/info/
exclude`` — never the committed ``.gitignore``, since this is a local, per-machine scratch-space
convention, not something every clone/contributor should carry — so a freshly created worktree
never shows up as an untracked path in ``git status``. Deliberately reconciled BEFORE the ``git
worktree add`` (not after): the reconcile needs no worktree to exist first, so doing it first
means a marker conflict or an unwritable exclude file is reported WITHOUT ever creating a
worktree — no half-done "created but not ignored" state to explain or clean up. NOTE: ``info/
exclude`` is the repo's COMMON (shared) exclude file — every worktree of a repo, linked or
primary, resolves to the SAME physical file (verified: `git -C <linked-worktree> rev-parse
--git-path info/exclude` names the primary checkout's `.git/info/exclude`, not a per-worktree
private copy) — so this reconcile affects `git status` in every worktree of the repo, not just
the one being created. ``git rev-parse --git-path`` is still required rather than a naive
``<repo>/.git/info/exclude`` string join, because inside a linked worktree ``.git`` is a FILE
(a pointer), not a directory. That reconcile is idempotent (repeat creations don't duplicate the
entry) and lives in :mod:`riglib.actions.runner` (:func:`riglib.actions.runner.
reconcile_worktrees_exclude`), next to the ship-delegator/opencode-bridge excludes it shares its
marker-splice machinery with.

Result shape: like :mod:`riglib.codex_update`, this returns a plain dataclass with an
``exit_code`` (from :mod:`riglib.errors`) rather than raising a :class:`riglib.errors.RigError` —
worktree creation is an imperative one-shot git operation, not a config/catalog/plan validation
path, so it follows the OTHER established result-object precedent for that class of command.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import errors
from .actions.runner import reconcile_worktrees_exclude
from .config import WORKTREES_DIR_NAME

_GIT_TIMEOUT_S = 60


def _partial_state_cleanup_hint(target: Path, branch_name: str) -> str:
    """Copy-pasteable recovery for a ``target``/branch git may have left behind after a failed
    ``git worktree add -b <branch_name>``.

    Both the timeout path and the hook-failure path below can leave this same partial state, so
    they share this one hint (kept in sync in one place) rather than duplicating the wording.
    Quoted with :func:`shlex.quote` — ``target``/``branch_name`` are user-controlled (a worktree
    name may contain spaces; :func:`_invalid_name_reason` only rejects ``/``, ``\\``, empty, and
    ``.``/``..``) and these are meant to be pasted straight into a shell.
    """
    quoted_target = shlex.quote(str(target))
    quoted_branch = shlex.quote(branch_name)
    return (
        f"check `git worktree list` and `git branch`, then `git worktree remove --force "
        f"{quoted_target}` and `git branch -D {quoted_branch}` before retrying — `worktree "
        f"remove` alone leaves the branch behind, so a bare retry fails with 'a branch named "
        f"{branch_name!r} already exists'"
    )


@dataclass(frozen=True)
class WorktreeResult:
    """The outcome of :func:`create`. ``status`` is ``"created"`` or ``"error"``."""

    status: str
    message: str
    path: Path | None = None
    branch: str | None = None
    exclude_note: str = ""
    exit_code: int = 0


def worktree_path(repo_root: Path, name: str) -> Path:
    """The standardized path for a NEW agent worktree: ``<repo>/.worktrees/<name>``."""
    return repo_root / WORKTREES_DIR_NAME / name


def _invalid_name_reason(name: str) -> str | None:
    """Reason ``name`` is unsafe as a single path segment under ``.worktrees/``, else ``None``.

    ``name`` becomes a literal directory component (``worktree_path``) and a default git branch
    name — reject anything that could escape ``.worktrees/`` (a path separator, ``.``/``..``) or
    is empty, rather than let a malformed name silently create a worktree somewhere unexpected.
    """
    if not name or not name.strip():
        return "worktree name must not be empty"
    if name in (".", ".."):
        return f"worktree name {name!r} is not a valid directory name"
    if "/" in name or "\\" in name:
        return f"worktree name {name!r} must be a single path segment (no / or \\)"
    return None


def _invalid_ref_reason(label: str, value: str) -> str | None:
    """Reason ``value`` (a ``--branch``/``--from`` argument) is unsafe to hand to git, else None.

    Both become POSITIONAL arguments on a ``git worktree add`` command line
    (``-b <branch> [<base_ref>]``). A value starting with ``-`` is not a valid git ref/branch
    name anyway (`git check-ref-format` rejects a leading dash), but git's own ARGUMENT PARSER
    reads it as an OPTION before ref-validation ever runs — e.g. a ``base_ref`` of
    ``--no-checkout`` is silently accepted as the flag, git exits 0, and the worktree is left
    unpopulated (verified empirically: `git worktree add <path> -b <b> --no-checkout` creates an
    empty worktree, no error). Rejecting a leading ``-`` up front closes that option-injection
    path outright, rather than trying to enumerate which flags are dangerous.
    """
    if value.startswith("-"):
        return f"{label} {value!r} must not start with '-' (would be read as a git option)"
    return None


def _target_escapes_repo(repo_root: Path, target: Path) -> bool:
    """True if ``target``'s ``.worktrees`` parent resolves OUTSIDE ``repo_root``.

    ``name`` is already validated to be a single path segment with no ``/`` or ``..``
    (:func:`_invalid_name_reason`), so the only way ``target`` (``repo_root/.worktrees/<name>``)
    could land outside the repo is if the ``.worktrees`` directory ITSELF is a symlink pointing
    elsewhere — a stray leftover from a hand-rolled setup, or worse. A first-ever worktree has no
    ``.worktrees`` dir yet, so there is nothing to escape through: it does not exist, so `git
    worktree add` creates it fresh as a plain subdirectory of ``repo_root``. Once it DOES exist,
    resolve it and confirm it still sits under the repo's own resolved root.
    """
    worktrees_dir = target.parent
    if not worktrees_dir.exists():
        return False
    resolved_worktrees_dir = worktrees_dir.resolve(strict=True)
    resolved_repo_root = repo_root.resolve(strict=True)
    try:
        resolved_worktrees_dir.relative_to(resolved_repo_root)
    except ValueError:
        return True
    return False


def _validate_create_args(name: str, branch_name: str, base_ref: str | None) -> str | None:
    """First applicable rejection reason for ``create``'s arguments, else ``None``."""
    for reason in (
        _invalid_name_reason(name),
        _invalid_ref_reason("--branch", branch_name),
        _invalid_ref_reason("--from", base_ref) if base_ref else None,
    ):
        if reason is not None:
            return reason
    return None


def _run_git_worktree_add(
    repo_root: Path, target: Path, branch_name: str, base_ref: str | None
) -> WorktreeResult | None:
    """Run ``git worktree add``. Returns an error :class:`WorktreeResult`, or ``None`` on success.

    Exit-code classification: git not runnable at all (binary missing) is a MISSING DEPENDENCY
    (127); git ran but rejected the request for a domain reason (branch already exists, bad ref,
    a dirty index blocking checkout, …) is a USAGE-class problem — the same bucket as an invalid
    name or an already-occupied target (``EXIT_CONFIG``) — not "rig broke" (``EXIT_INTERNAL``,
    reserved for rig's own unexpected failures, e.g. the exclude reconcile below). A timeout is
    reported as a TIMEOUT, not misreported as "could not run git" (git DID run — it just didn't
    finish within the bound, plausible on a large repo/slow disk).
    """
    cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target), "-b", branch_name]
    if base_ref:
        cmd.append(base_ref)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return WorktreeResult(
            "error",
            f"git worktree add timed out after {_GIT_TIMEOUT_S}s (git may have left a partial "
            f"worktree/branch behind — {_partial_state_cleanup_hint(target, branch_name)})",
            exit_code=errors.EXIT_INTERNAL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return WorktreeResult(
            "error", f"could not run git: {exc}", exit_code=errors.EXIT_MISSING_DEP
        )
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}"
        if target.exists():
            # Git had already created and registered the worktree AND the branch by the time a
            # later step — typically a post-checkout hook — exited non-zero (empirically
            # verified: `create()` already refused if `target` pre-existed, so `target` only
            # exists here because THIS `git worktree add` call created it). Say so explicitly
            # instead of just reporting "failed": a bare failure with no cleanup guidance leads
            # a retry straight into "already exists" — and `git worktree remove` alone leaves
            # the branch behind (verified: a follow-up `git worktree add -b <branch>` then fails
            # with "a branch named '<branch>' already exists"), so both must be named.
            return WorktreeResult(
                "error",
                f"git worktree add failed: {detail} ({target} was created before the failure, "
                f"likely by a post-checkout hook — "
                f"{_partial_state_cleanup_hint(target, branch_name)})",
                exit_code=errors.EXIT_CONFIG,
            )
        return WorktreeResult(
            "error", f"git worktree add failed: {detail}", exit_code=errors.EXIT_CONFIG
        )
    return None


def create(
    repo_root: Path,
    name: str,
    *,
    branch: str | None = None,
    base_ref: str | None = None,
) -> WorktreeResult:
    """Create a linked worktree at ``<repo_root>/.worktrees/<name>``, ignore-registered first.

    ``branch`` defaults to ``name`` (a fresh branch, mirroring plain ``git worktree add <path>
    -b <name>``); ``base_ref`` defaults to git's own default (the current ``HEAD``) when omitted.

    Order matters (see the module docstring): the ``.git/info/exclude`` reconcile runs BEFORE
    ``git worktree add``, so a marker conflict or an unwritable exclude file is reported without
    ever creating a worktree. Only once that succeeds does git actually create the worktree.
    """
    branch_name = branch or name
    reason = _validate_create_args(name, branch_name, base_ref)
    if reason is not None:
        return WorktreeResult("error", reason, exit_code=errors.EXIT_CONFIG)

    target = worktree_path(repo_root, name)
    if target.exists():
        return WorktreeResult(
            "error", f"{target} already exists", path=target, exit_code=errors.EXIT_CONFIG
        )
    if _target_escapes_repo(repo_root, target):
        return WorktreeResult(
            "error",
            f"{target.parent} is a symlink pointing outside {repo_root} — refusing to create "
            "a worktree through it",
            exit_code=errors.EXIT_CONFIG,
        )

    exclude_ok, exclude_note = reconcile_worktrees_exclude(repo_root)
    if not exclude_ok:
        return WorktreeResult(
            "error",
            f"{exclude_note} — refusing to create the worktree until .worktrees/ can be "
            "registered in .git/info/exclude",
            exclude_note=exclude_note,
            exit_code=errors.EXIT_INTERNAL,
        )

    add_error = _run_git_worktree_add(repo_root, target, branch_name, base_ref)
    if add_error is not None:
        return add_error

    return WorktreeResult(
        "created",
        f"created worktree at {target} (branch {branch_name})",
        path=target,
        branch=branch_name,
        exclude_note=exclude_note,
        exit_code=0,
    )
