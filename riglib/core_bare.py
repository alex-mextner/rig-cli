"""core.bare corruption scanner — catch a working checkout that falsely claims to be bare.

Runtime reach: called by ``rig doctor`` (and ``rig doctor --fix``) to PROACTIVELY catch a
specific, baffling git-config corruption before it bites at runtime. The motivating incident:
rig-cli's main checkout silently acquired ``git config core.bare = true``. A *non-bare working
checkout* with ``core.bare=true`` breaks EVERY git operation in that directory — ``status``,
``diff``, ``commit``, ``worktree`` all fail with ``fatal: this operation must be run in a work
tree`` — and breaks ship's main-refresh step. With no hint of the cause, a one-line config flip
turns into a baffling outage. This scanner names the corrupted checkout + the one-line fix
(``git config core.bare false``), as a structured :class:`errors.RepoCorruptError`.

Detection signature (proven by hand against real git, see tests): a path is the corruption class
when it has the *working-checkout layout* — a ``.git`` directory or file at its root — AND git
reports the path ITSELF as bare via ``rev-parse --is-bare-repository``. Two repos are deliberately
EXCLUDED: (1) a GENUINELY bare repo (e.g. ``foo.git``) keeps its git internals AT the root with NO
``.git`` entry; (2) a legitimate linked worktree of a genuine bare repo has a ``.git`` FILE but git
reports it as not-bare (it has a work tree), so the per-path ``rev-parse`` answer is ``false``. We
use ``rev-parse --is-bare-repository`` rather than reading ``core.bare`` from config because
``core.bare`` lives in the SHARED config: a bare-repo worktree reads ``true`` from it yet is
healthy, so a raw config read would false-positive (and ``--fix`` would then break that valid
setup). We likewise do NOT trust ``git worktree list --porcelain``'s ``bare`` marker — once
``core.bare`` is set, git reports the corrupted checkout AS bare there too, so that flag cannot
discriminate; the on-disk ``.git`` layout plus the per-path ``rev-parse`` verdict can.

Scope: the cwd repo and every worktree it lists. Stdlib-only; never mutates config except through
the explicit :func:`fix_core_bare` (driven by ``rig doctor --fix``).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import errors

# Git env vars that redirect repo resolution or inject config — scrubbed before every git call so
# a value leaked into the process (notably a hook/wrapper env: rig runs from pre-commit) can never
# point the verdict OR the destructive `--fix` at the wrong repo. `core.bare` is read from
# `$GIT_COMMON_DIR`, so that one is as load-bearing as `GIT_DIR`; the `GIT_CONFIG_*` family can
# force a phantom `core.bare`.
_SCRUBBED_GIT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",  # legacy: names the file `git config` operates on — scrub for the --fix write
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        # GIT_CONFIG_PARAMETERS is git's internal channel for propagating ancestor `-c` settings to
        # child processes (e.g. a pre-commit hook) at command-line precedence. Current git does not
        # let it flip `core.bare` for `rev-parse --is-bare-repository` (verified by hand), but it is
        # the most-likely-populated leaked var in a hook env, so scrub it as defense-in-depth.
        "GIT_CONFIG_PARAMETERS",
        # Discovery-control vars: a leaked value could suppress repo discovery from a SUBDIR and
        # make us miss a corruption (a false negative — never a wrong write). Scrub for completeness.
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)
# GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> (command-line-config injection) come in numbered pairs.
_SCRUBBED_GIT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


@dataclass(frozen=True)
class CoreBareFinding:
    """One corrupted checkout: a working tree whose ``core.bare`` is wrongly ``true``."""

    path: Path  # the corrupted checkout's working-tree root
    git_dir: Path  # its ``.git`` directory/file (the working-checkout layout tell)


def _git(args: list[str], cwd: Path) -> str | None:
    """Run ``git <args>`` in ``cwd``; return stripped stdout, or None on any failure.

    Tolerant by design: a missing git binary, a non-zero exit, or a timeout yields None so the
    scan degrades to "nothing to report" rather than crashing ``rig doctor``.

    Scrubs the git-redirection / config-injection env so the verdict (and the destructive
    ``--fix``) bind to ``cwd``'s actual repo, not a leaked env pointing elsewhere. Git leaks these
    into hook/wrapper environments — and rig now runs from a pre-commit hook — so an inherited
    ``GIT_DIR``/``GIT_COMMON_DIR`` would resolve the WRONG repo, while ``GIT_CONFIG_*`` could
    inject a false ``core.bare``. ``core.bare`` itself is read from ``$GIT_COMMON_DIR``, so that
    one is the critical addition beyond ``GIT_DIR``.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_GIT_ENV and not k.startswith(_SCRUBBED_GIT_PREFIXES)}
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _enclosing_repo_root(start: Path) -> Path:
    """The repo root enclosing ``start`` — so a run from a SUBDIR still checks the right place.

    ``rig doctor`` runs from cwd, which may be a subdirectory; the corruption (a wrong
    ``core.bare``) lives at the repo root (its ``.git`` is only there). We resolve the root via
    ``git rev-parse --absolute-git-dir`` (its parent is the worktree root) because that read works
    even on a CORRUPTED checkout, where ``--show-toplevel`` already fails. Falls back to ``start``
    when git can't resolve a git-dir (not a repo / git absent).
    """
    git_dir = _git(["rev-parse", "--absolute-git-dir"], start)
    if not git_dir:
        return start
    p = Path(git_dir)
    # a main checkout's git-dir is ``<root>/.git``; a linked worktree's resolves under the common
    # dir, but its own ``.git`` FILE sits at the worktree root — `worktree list` (below) enumerates
    # those, so the git-dir parent is the right anchor for the common, run-from-subdir case.
    return p.parent if p.name == ".git" else start


def _worktree_roots(repo: Path) -> list[Path]:
    """The working-tree roots to scan: the enclosing checkout of ``repo`` PLUS git's worktree list.

    Always includes ``_enclosing_repo_root(repo)`` first — git's
    ``worktree list --porcelain`` does NOT always name the working-tree root. With a
    ``--separate-git-dir`` checkout (``.git`` is a FILE pointing at a detached gitdir), a corrupting
    ``core.bare=true`` makes the listing report the SEPARATE GITDIR (marked ``bare``), not the work
    tree — so scanning only the listed roots would miss the broken checkout, whose ``.git`` FILE
    lives at the enclosing root. Seeding with the enclosing root catches that (and the run-from-a-
    SUBDIR case); ``scan_repo`` dedupes by resolved path, so the overlap with the listing is free.
    Falls back to just the enclosing root when the listing is unavailable (git absent / corrupted
    so badly the listing itself fails).
    """
    roots: list[Path] = [_enclosing_repo_root(repo)]
    porcelain = _git(["worktree", "list", "--porcelain"], repo)
    if porcelain:
        for line in porcelain.splitlines():
            if line.startswith("worktree "):
                roots.append(Path(line[len("worktree ") :]))
    return roots


def _git_dir_at(path: Path) -> Path | None:
    """The ``.git`` entry at ``path`` (dir for a main checkout, file for a linked worktree).

    Its presence is the working-checkout tell: a genuinely bare repo has git internals at the
    root with NO ``.git`` entry. Returns None when ``path`` has no ``.git`` (e.g. a genuine bare
    repo, or a vanished worktree dir), so such a path is never flagged.
    """
    dot_git = path / ".git"
    return dot_git if dot_git.exists() else None


def _is_effectively_bare(path: Path) -> bool:
    """True iff git considers ``path`` ITSELF bare — the worktree-aware effective answer.

    We must NOT read ``core.bare`` from config directly. ``core.bare`` lives in the SHARED
    ``.git/config``, so a *legitimate* linked worktree of a genuine bare repo (a common workflow —
    bare repo + worktrees) reads ``core.bare=true`` from that shared config even though the
    worktree itself is a perfectly healthy work tree. Flagging it would be a false positive, and
    ``--fix`` writing ``core.bare=false`` there would BREAK that legitimate bare setup. So we ask
    git for the per-path verdict — ``rev-parse --is-bare-repository`` — which a bare-repo worktree
    correctly reports as ``false`` (it has a work tree) while a corrupted main checkout reports
    ``true``. This also normalizes every bool spelling (``1``/``yes``/``on``/any case → bare). A
    read failure (git absent / not a repo) is treated as not-bare.
    """
    return _git(["rev-parse", "--is-bare-repository"], path) == "true"


def scan_repo(repo: Path) -> list[CoreBareFinding]:
    """Scan ``repo`` and its worktrees for the core.bare corruption class.

    The corruption signature: a path with the WORKING-CHECKOUT layout (a ``.git`` dir/file) that
    git nonetheless reports as bare (``rev-parse --is-bare-repository``). Both conditions are
    required — a genuine bare repo (no ``.git`` entry) and a legitimate bare-repo worktree
    (``.git`` file but git knows it has a work tree → not bare) are both correctly EXCLUDED, so
    neither is flagged nor mutated by ``--fix``. Returns one finding per corrupted checkout
    (deduped by resolved path). Never raises — a non-repo / absent git / unreadable path → ``[]``.
    """
    findings: list[CoreBareFinding] = []
    seen: set[Path] = set()
    for root in _worktree_roots(repo):
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        git_dir = _git_dir_at(resolved)
        if git_dir is None:
            continue  # genuine bare repo or vanished dir — not the corruption class
        if _is_effectively_bare(resolved):
            findings.append(CoreBareFinding(path=resolved, git_dir=git_dir))
    return findings


def finding_to_error(finding: CoreBareFinding) -> errors.RepoCorruptError:
    """Render a finding as the structured, user-facing error (what / why / one-line fix)."""
    # shlex.quote the path so the COPY-PASTEABLE fix command survives spaces / shell-metachars
    # (the programmatic fix in fix_core_bare passes argv as a list and never needs this).
    return errors.core_bare_error(
        repo=str(finding.path),
        fix_cmd=f"git -C {shlex.quote(str(finding.path))} config core.bare false",
    )


def fix_core_bare(finding: CoreBareFinding) -> bool:
    """Repair one corrupted checkout by setting ``core.bare=false``. Returns True on REAL success.

    Used only by ``rig doctor --fix`` (an explicit, opt-in repair). Never invoked by the plain
    scan, so a read-only ``rig doctor`` never mutates a repo's config.

    Writes ``core.bare=false`` to the LOCAL config, then RE-VERIFIES via
    ``rev-parse --is-bare-repository`` and reports success only if the checkout is no longer
    effectively bare. The re-check matters because a ``true`` sourced from another scope
    (worktree-scoped config under ``extensions.worktreeConfig``, or an ``[include]``) would survive
    a plain local write — without the re-check ``--fix`` would falsely claim it repaired the repo.
    """
    if _git(["config", "core.bare", "false"], finding.path) is None:
        return False
    return not _is_effectively_bare(finding.path)
