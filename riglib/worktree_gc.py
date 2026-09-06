"""``rig worktree gc`` — classify and clean up the worktree sprawl every agent leaves behind.

``rig worktree create``/``remove`` (:mod:`riglib.worktree`) standardize where a NEW worktree
lands. This module is the other half: reconciling every worktree ``git`` already knows about for
a repo — wherever it physically lives (the standardized ``<repo>/.worktrees/<name>``, an inherited
ad hoc convention like ``<repo>-worktrees/`` or ``<repo>-wt-*``, or anywhere else ``git worktree
add`` was pointed) — against reality: is it still in use, does it have uncommitted work, did its
PR merge or close, or has it simply been abandoned. ``git worktree list`` already tracks every
worktree registered to a repo regardless of physical location, so that is the one source of truth
this module reads from; it never re-derives "where worktrees live" from a naming convention.

Classification order (evaluated top to bottom, each one short-circuits the rest):

    live > prunable > dirty > merged / closed / active (open PR) > no-pr-stale / active

**Liveness wins over everything, with no exception** — a worktree a live agent is actually
sitting in must never be touched even if its branch also has a merged PR or looks ancient, so the
liveness check is the very first thing :func:`classify_worktree` does, before any of the
disk/PR-based checks that would otherwise say "safe to remove".

Only ``merged``/``closed``/``prunable`` are removable on a bare ``--yes``. ``no-pr-stale`` needs
the EXPLICIT ``--include-stale`` flag on top of ``--yes`` — a clean, PR-less worktree with no
recent activity is very likely abandoned, but "very likely" is not "certain", so it gets an extra
opt-in. ``dirty``/``live``/``active`` (an open PR, or recent PR-less activity) are never
auto-removed by this command at all.

Both cross-process dependencies are injectable so the classifier is unit-testable without a real
running agent or real network access:

- ``liveness_check: Callable[[Path], bool]`` — default :func:`default_liveness_check` shells out
  to ``pgrep``/``lsof`` (see its docstring for the fail-safe-toward-"live" reasoning).
- ``pr_lookup: Callable[[str], PrInfo | None]`` — default built by :func:`make_default_pr_lookup`,
  which fetches every PR for the repo ONCE via ``gh pr list`` and looks branches up in that index
  (one subprocess for the whole repo, not one per worktree).

Known limitations (deliberate trade-offs, not oversights — surfaced across several review rounds):

- **A squash-merged branch whose remote-tracking ref was later pruned may report ``dirty``
  instead of auto-cleaning.** ``merged``/``closed`` removal requires BOTH "no commits after the
  PR resolved" (:func:`_branch_outlived_its_pr`, date-based, survives a prune) AND "HEAD is
  reachable from some remote-tracking ref" (:func:`_has_unpushed_commits`, always re-checked —
  see :func:`_classify_clean_worktree`). After GitHub auto-deletes a merged branch and a `git
  fetch --prune` removes its now-dangling remote-tracking ref, the second check can no longer be
  satisfied even for a genuinely finished branch, so it is kept for a human to clear by hand
  rather than auto-removed. This is INTENTIONAL: the alternative (trust the date alone) would
  also silently pass a commit made just BEFORE the PR merged but never actually pushed — safety
  wins over auto-clean effectiveness for this one configuration.
- **The pre-removal recheck (liveness/dirty/unpushed) narrows but does not fully close the TOCTOU
  window.** A chdir or a new commit landing in the fractions-of-a-second between the recheck
  returning and the actual ``git worktree remove --force`` call is not caught by anything that
  runs BEFORE that call. Closing it fully would need OS-level coordination shared with every
  process that might touch the repo, which nothing in this ecosystem implements.
- **A narrower variant of the same window:** if a worktree's own ``.git`` pointer file is removed
  (not the whole directory — a partial hand-edit) in that SAME window, between a successful
  planning-time classification and the pre-removal recheck, the recheck's ``git -C <path> status``
  and reachability checks resolve the same way :func:`_classify_prunable`'s docstring already
  describes for the PLANNING-time case: git walks UP to the PRIMARY repo and evaluates the wrong
  repository, potentially reading "safe" for a directory that actually still holds untracked
  content. :func:`_classify_prunable` closes this at planning time (a directory that still exists
  is never trusted as a clean prune candidate); the pre-removal recheck does not re-run that same
  check for an entry that was genuinely present and valid at planning time. Deliberately not
  closed here, given how narrow the trigger is (hand-deleting exactly the `.git` pointer file,
  not the directory, in the same already-documented TOCTOU window above) relative to the
  complexity of re-validating a worktree's own administrative state on every single recheck.
- **No defense against adversarially backdated commit timestamps.** Both the outlived-check and
  the worktree-age check trust ``git log``'s committer date, which is user-settable
  (``GIT_COMMITTER_DATE``/``commit --date``). This module assumes a trusted environment (an
  agent's own worktrees), not one where a hostile actor is deliberately forging git metadata to
  defeat the classifier.
- **Ignored files are not specially protected.** ``git worktree remove --force`` recursively
  deletes the whole directory, including untracked-but-gitignored content (a local ``.env``, a
  generated artifact) — the SAME property `rig worktree remove` already has and this module does
  not attempt to change; gc's own safety checks are about COMMITTED work reachability, not about
  every file that happens to live in the directory.
- **PR matching is by branch NAME and resolution DATE, not the PR's actual head commit SHA.** A
  branch reused after its PR merged — reset to an old commit and force-pushed — could in principle
  fool the date-based check if the reused history happens to predate that old PR's ``mergedAt`` and
  the branch happens to already be pushed. Verifying against the PR's actual head OID would close
  this, at the cost of another `gh` field/round-trip; not implemented given how deliberately
  unusual the trigger is (reset + force-push a branch back onto old history right after its own PR
  merged).
- **A repo whose `gh pr list` page (`_GH_PAGE_LIMIT`, currently 500) doesn't reach far enough back
  could miss an old but still-OPEN PR**, reading its branch as "no PR found". This can only ever
  reach the ``no-pr-stale`` bucket (never ``merged``/``closed``, which need an actual PR record),
  and removing THAT bucket already requires both ``--yes`` AND the explicit ``--include-stale``
  opt-in — the same double-gate that already covers a `gh` lookup failing outright (see
  :func:`make_default_pr_lookup`'s docstring). Not paginated further given that existing gate.
- **This clone being a FORK makes PR matching go dark, safely.** When the local clone's own
  `origin` is a fork (a standard OSS contribution layout — PRs are opened from the fork against
  upstream), every relevant PR reads `isCrossRepository: true` from `gh pr list` run in THIS
  clone and is excluded (see :func:`_index_prs_by_branch`) — so `merged`/`closed` never fires for
  this topology, and every branch falls to ``no-pr-stale``/``active``. Never wrongly removes
  anything (the same double-gate as above still applies), but the merged/closed auto-clean is
  effectively inert for a fork-based clone.
- **PR state is a single per-run snapshot** (`make_default_pr_lookup`'s ONE `gh pr list` fetch),
  not re-verified before each removal. A NEW PR opened on an already-`merged`-classified branch,
  in the window between that snapshot and this entry's actual removal, is not caught — the
  pre-removal recheck (:func:`_execute_removals`) re-verifies liveness/dirty/unpushed but not PR
  state, since doing so would mean an extra `gh pr list` round-trip per removable entry, defeating
  the one-fetch-per-run design this module otherwise relies on for speed. Accepted as narrow (this
  specific sequence — a same-branch PR reopened in the exact window of one `gc` run) and lower
  priority than the safety gaps already fixed above.
- **The registry fan-out (`--repo` omitted) does not deduplicate entries that resolve to the SAME
  underlying repo.** If the machine-local repository registry contains BOTH a primary checkout and
  one of its own linked worktrees as separate entries (an unusual registry configuration — e.g. a
  `discover --root` pointed directly at a `.worktrees/` directory, not the normal case), the first
  iteration resolves to the shared primary and may remove the linked tree; a LATER iteration then
  preflights that now-missing registered path and reports the documented exit 6 for it — a
  confusing but NON-DESTRUCTIVE outcome (the underlying repo state is fine; only the per-entry
  report/exit code for the redundant registry row is wrong). Not deduplicated given the narrow,
  self-inflicted trigger and the added complexity of resolving every selected path's primary
  before the fan-out even starts.
- **`rig worktree gc` protects its OWN process's cwd (see `_live_process_cwds`) but not other
  agents'/processes' cwds beyond the `claude`/`codex`/`opencode` pgrep pattern.** A different tool
  or shell script sitting in a worktree, not matching that pattern and not the invoking process
  itself, is not specially protected — the same liveness contract this module has always had.
- **Requires git >= 2.36** (uses `git worktree list --porcelain -z`, same requirement `rig
  worktree remove` already documents). On an older git, a prunable removal may fail outright
  (surfacing as `remove_error`, not silent data loss) rather than the `-z` parser ever running.
- **`_last_activity_utc`'s worktree-creation-mtime signal can be reset by anything that rewrites
  the `.git` pointer file** — `git worktree repair`/`move`, or a backup/rsync/restore that resets
  file mtimes — making a genuinely old, abandoned worktree read as recently "active" again. Only
  ever pushes a worktree AWAY from removal (the safe direction), never toward it — an
  effectiveness limitation, not a correctness one.
- **Error/warning text from `gh`'s own stderr, and a hand-edited registry entry's path, are NOT
  run through `_sanitize_for_terminal`** — only the removal REPORT body (paths/branches/reasons
  gc itself derives) is. A compromised `gh` binary or wrapper, or a registry file a user already
  has local write access to edit by hand, could in principle inject terminal control sequences
  through those specific message paths. Given the attacker already needs the same local access
  either scenario requires (arbitrary `gh` execution, or write access to the registry file this
  process already trusts), this is treated as a lower-value hardening pass than the worktree-
  path/branch-name sanitization above (which guards content that originates from an ordinary git
  repo — the actually plausible "untrusted input" for this tool) and is not implemented here.
- **`rig status`'s stale-worktree check can block for up to `_GH_TIMEOUT_S` (30s)** waiting on an
  offline/unauthenticated `gh pr list` before its best-effort `except Exception` can let `status`
  finish. `RIG_STATUS_SKIP_WORKTREE_GC=1` is the documented opt-out (see `cli.py`); a
  status-specific shorter deadline was considered but not implemented, matching the "noisy by
  default, opt-out available" trade-off this module already made for the stderr warnings on the
  exact same code path (see the entry above on PR-state snapshotting).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import errors

DEFAULT_OLDER_THAN_DAYS = 14

_GIT_TIMEOUT_S = 60
_PGREP_TIMEOUT_S = 5
_LSOF_TIMEOUT_S = 5
_GH_TIMEOUT_S = 30
_GH_PAGE_LIMIT = 500
_LIVE_PROCESS_PATTERN = "claude|codex|opencode"

# Classifications that are ever candidates for removal — the ones `rig status` counts as "stale"
# and the ones a report renders with a size/removability story. `dirty`/`live`/`active` never are.
# ONE ordered tuple is the source of truth (never re-typed elsewhere, e.g. `cli.py`'s status
# breakdown line) so adding a new stale class can't silently fall out of one copy but not another.
STALE_CLASSIFICATIONS_ORDERED = ("merged", "closed", "prunable", "no-pr-stale")
STALE_CLASSIFICATIONS = frozenset(STALE_CLASSIFICATIONS_ORDERED)
_AUTO_REMOVABLE_ALWAYS = STALE_CLASSIFICATIONS - {"no-pr-stale"}


class WorktreeGcError(RuntimeError):
    """``git worktree list`` itself could not be trusted (missing git, timeout, non-zero exit).

    Carries an ``exit_code`` (an :mod:`riglib.errors` ``EXIT_*`` constant) so the CLI can surface
    the same stable per-class exit codes ``rig worktree create``/``remove`` already document.
    """

    def __init__(self, message: str, exit_code: int = errors.EXIT_INTERNAL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ── data shapes ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PrInfo:
    """One GitHub PR, as read from ``gh pr list``. ``state`` is ``gh``'s own spelling:
    ``"OPEN"``/``"CLOSED"``/``"MERGED"``."""

    number: int
    state: str
    merged_at: str | None = None
    closed_at: str | None = None
    url: str = ""


LivenessCheck = Callable[[Path], bool]
PrLookup = Callable[[str], PrInfo | None]


@dataclass(frozen=True)
class WorktreeInfo:
    """One entry from ``git worktree list --porcelain -z``, minus the primary worktree."""

    path: Path
    branch: str | None
    head_sha: str | None = None
    prunable: bool = False
    prunable_reason: str = ""
    locked: bool = False
    locked_reason: str = ""


@dataclass(frozen=True)
class ClassifiedWorktree:
    """The pure classification result for one :class:`WorktreeInfo` — no flags, no disk size."""

    info: WorktreeInfo
    classification: str
    reason: str
    removable_class: bool  # True for merged/closed/prunable/no-pr-stale; False otherwise


@dataclass
class GcEntry:
    """A classified worktree plus the run's decision for it (flags-aware) and its outcome."""

    classified: ClassifiedWorktree
    plan_removable: bool
    size_bytes: int | None = None
    removed: bool = False
    remove_error: str | None = None
    skipped_reason: str | None = None

    @property
    def path(self) -> Path:
        return self.classified.info.path

    @property
    def branch(self) -> str | None:
        return self.classified.info.branch

    @property
    def classification(self) -> str:
        return self.classified.classification

    @property
    def reason(self) -> str:
        return self.classified.reason


@dataclass(frozen=True)
class GcReport:
    repo_root: Path
    entries: list[GcEntry] = field(default_factory=list)
    dry_run: bool = True
    include_stale: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(entry.classification for entry in self.entries))

    @property
    def total_reclaimed_bytes(self) -> int:
        return sum(e.size_bytes or 0 for e in self.entries if e.removed)

    @property
    def total_reclaimable_bytes(self) -> int:
        return sum(e.size_bytes or 0 for e in self.entries if e.plan_removable)


# ── `git worktree list --porcelain -z` parsing ──────────────────────────────────
def _decode(data: bytes) -> str:
    """Best-effort display decode — see :func:`riglib.worktree._decode_message` for why not
    ``text=True`` (locale-dependent decoding could crash on a non-UTF-8 byte in git's output)."""
    return data.decode("utf-8", errors="replace")


def _parse_worktree_records(raw_stdout: bytes) -> list[dict[str, str]]:
    """Group ``-z``-delimited fields into one dict per worktree (a blank field is the record
    separator). Decoded with ``surrogateescape`` so a non-UTF-8 path round-trips losslessly —
    see :func:`riglib.worktree._find_worktree_branch` for the identical reasoning."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_field in raw_stdout.split(b"\x00"):
        field_text = raw_field.decode("utf-8", errors="surrogateescape")
        if field_text == "":
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field_text.partition(" ")
        current.setdefault(key, value)
    if current:
        records.append(current)
    return records


def _parse_worktree_record(record: dict[str, str]) -> WorktreeInfo | None:
    raw_path = record.get("worktree")
    if raw_path is None or "bare" in record:
        return None  # not a worktree entry, or the bare repo record itself
    branch: str | None = None
    if "branch" in record:
        value = record["branch"]
        branch = value[len("refs/heads/") :] if value.startswith("refs/heads/") else value
    return WorktreeInfo(
        path=Path(raw_path),
        branch=branch,
        head_sha=record.get("HEAD") or None,
        prunable="prunable" in record,
        prunable_reason=record.get("prunable", ""),
        locked="locked" in record,
        locked_reason=record.get("locked", ""),
    )


def _run_worktree_list_porcelain(repo_root: Path, *, timeout: int = _GIT_TIMEOUT_S) -> list[dict[str, str]]:
    """``git -C repo_root worktree list --porcelain -z``, parsed into raw records. Shared by
    :func:`list_worktrees` and :func:`_resolve_primary_worktree` so both read the SAME listing
    machinery rather than two independently-maintained subprocess calls."""
    cmd = ["git", "-C", str(repo_root), "worktree", "list", "--porcelain", "-z"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorktreeGcError(f"git worktree list timed out after {timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = errors.EXIT_MISSING_DEP if isinstance(exc, FileNotFoundError) else errors.EXIT_INTERNAL
        raise WorktreeGcError(f"could not run git worktree list: {exc}", exit_code=exit_code) from exc
    if res.returncode != 0:
        detail = _decode(res.stderr).strip() or _decode(res.stdout).strip()
        raise WorktreeGcError(
            f"git worktree list failed: {detail or f'exit {res.returncode}'}", exit_code=errors.EXIT_CONFIG
        )
    return _parse_worktree_records(res.stdout)


def _resolve_primary_worktree(repo_root: Path) -> Path:
    """The actual PRIMARY worktree's path for ``repo_root`` — which may itself be a LINKED
    worktree (e.g. ``rig worktree gc --repo <a-worktree>``, or `rig status` run from inside one;
    ``detect_environment``'s own ``git rev-parse --show-toplevel`` resolves to whichever worktree
    the caller pointed it at, not necessarily the primary).

    Every MUTATING or branch-ref git invocation in this module (``git worktree remove``, ``git
    branch -D``, the unpushed-commit check) must be anchored to the PRIMARY, never to a linked
    worktree that might itself be removed partway through the SAME run — review-caught: anchoring
    to a linked worktree that gets removed breaks not just that entry's own branch-delete step but
    EVERY SUBSEQUENT ``git -C <that-now-gone-path>`` call in the rest of the run, since the
    directory git was told to run in no longer exists.

    Falls back to ``repo_root`` unchanged if the listing itself can't be trusted — callers already
    handle a :class:`WorktreeGcError` from the same underlying listing downstream (via
    :func:`list_worktrees`), so silently returning the original path here just means the
    (already-about-to-fail) calls below surface the same underlying error normally, rather than
    this resolution step failing in a way nothing downstream expects.
    """
    try:
        records = _run_worktree_list_porcelain(repo_root)
    except WorktreeGcError:
        return repo_root
    if not records:
        return repo_root
    primary_path = records[0].get("worktree")
    return Path(primary_path) if primary_path else repo_root


def list_worktrees(repo_root: Path, *, timeout: int = _GIT_TIMEOUT_S) -> list[WorktreeInfo]:
    """Every LINKED worktree ``git`` knows about for ``repo_root`` (the primary worktree — the
    repo checkout itself — is excluded; there is nothing for gc to do with it)."""
    records = _run_worktree_list_porcelain(repo_root, timeout=timeout)
    if not records:
        return []
    # `git worktree list` ALWAYS reports the main (primary) working tree as the FIRST record —
    # verified empirically both from the primary checkout AND from `git -C <a-linked-worktree>
    # worktree list` (same order either way), so this holds even when `repo_root` passed in here
    # IS itself a linked worktree (e.g. `rig worktree gc --repo <a-worktree>`, or `rig status` run
    # from inside one — exactly where agents live). Comparing `info.path.resolve() ==
    # repo_root.resolve()` instead would have EXCLUDED `repo_root` (wrong — it isn't the primary)
    # and INCLUDED the real primary as a gc candidate in that case; index-0 is the one signal git
    # actually guarantees.
    #
    # Slicing the RAW records (before parsing/filtering), not the parsed list, matters too: a
    # primary that is itself a BARE repository (a legitimate git layout) parses to `None` in
    # `_parse_worktree_record` (it has a "bare" field, no worktree to gc) and would otherwise
    # silently vanish from a post-filter list — shifting everything else left by one and making
    # the slice drop the first REAL linked worktree instead of the (already-gone) bare primary.
    linked_records = records[1:]
    return [info for info in (_parse_worktree_record(r) for r in linked_records) if info is not None]


# ── liveness (injectable) ────────────────────────────────────────────────────────
def _process_cwd(pid: str) -> Path | None:
    """``lsof`` output is captured as RAW BYTES (not ``text=True``) — a process cwd'd in a
    directory whose name isn't valid UTF-8 would otherwise raise an uncaught
    ``UnicodeDecodeError`` out of ``subprocess.run`` itself, crashing `rig worktree gc` entirely
    instead of just failing this one pid's lookup (its caller already treats that as "couldn't
    determine" and fails safe).

    Decoded with ``surrogateescape``, NOT :func:`_decode`'s ``replace`` — deliberately, and load-
    bearing: this path gets compared (via ``relative_to`` in :func:`_liveness_check_from_snapshot`)
    against a worktree path that ``_parse_worktree_records`` ALSO decodes with
    ``surrogateescape``. A non-UTF-8 byte decoded two DIFFERENT ways (``replace``'s lossy U+FFFD
    here vs. ``surrogateescape``'s lossless round-trip there) would make the identical real
    filesystem path compare as unequal — silently reporting a genuinely live agent as "not live"
    for a worktree with a non-UTF-8 name (review-caught: the wrong direction for a liveness check
    that is supposed to fail SAFE, not unsafe, on anything it can't cleanly resolve).

    Split on plain ``"\\n"``, NOT ``str.splitlines()`` — deliberately (review-caught): ``-Fn``'s
    field separator is always a literal newline byte, but ``splitlines()`` ALSO breaks on
    ``\\r``/``\\v``/``\\f``/U+2028/U+2029/etc, every one of which is a legal byte in a real
    directory name. A cwd path containing one of those (not ``\\n`` itself) would otherwise get
    silently truncated to a strict PARENT of the real path, failing this same liveness check
    unsafe again for a different reason than the encoding one above.
    """
    try:
        res = subprocess.run(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, timeout=_LSOF_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.decode("utf-8", errors="surrogateescape").split("\n"):
        if line.startswith("n"):
            return Path(line[1:])
    return None


def _live_process_cwds() -> list[Path] | None:
    """One ``pgrep -u <own uid> -f`` plus one ``lsof`` per matched pid — every running
    ``claude``/``codex``/``opencode`` process OWNED BY THIS USER, its cwd resolved. ``None`` means
    the check itself can't be trusted (``pgrep`` missing/erroring, OR any matched pid's cwd
    couldn't be determined), distinct from "checked every matched pid, found nothing relevant"
    (``[]``).

    ``-u <own uid>`` matters: without it, ``pgrep -f`` matches ANY user's process whose argv
    happens to contain "claude"/"codex"/"opencode" — a foreign (e.g. root-owned) process this user
    can't `lsof` into then poisons the WHOLE snapshot to ``None`` (review-caught), silently
    defeating cleanup for every worktree machine-wide even though no agent of THIS user's is
    anywhere near them. Scoping to the caller's own uid loses nothing: `lsof` can't read another
    user's cwd anyway, so a foreign match could never have contributed a real answer.

    A matched pid whose ``lsof`` lookup fails poisons the WHOLE snapshot to ``None`` rather than
    being silently dropped — a process that exited between ``pgrep`` and ``lsof`` looks identical
    to ``lsof`` itself being unavailable/denied/timing out for that one pid, and only the former is
    safe to treat as "not relevant". Silently dropping it (the earlier, review-caught version of
    this function) could make a real live agent's cwd vanish from the snapshot while `pgrep` still
    proves an agent process exists — exactly the "confirmed not live" false negative this whole
    liveness check exists to prevent, in exchange for occasionally over-protecting one worktree.

    A ``None`` return is reported LOUDLY (stderr) here, once per degraded snapshot, rather than
    silently — every worktree is about to be classified `live` with a reason that CLAIMS a running
    process was actually found there, which is only true in the common case; a caller (`rig
    worktree gc`, `rig status`) reading only that per-entry text would otherwise have no way to
    know the check degraded machine-wide (review-caught: the safe *direction* was already correct,
    but the report was misleading with zero signal that anything was uncertain).

    THIS PROCESS's OWN cwd is ALWAYS included in the snapshot, unconditionally — not discovered
    via ``pgrep`` at all. The pattern only matches ``claude``/``codex``/``opencode`` process argv,
    which misses both the ``rig`` process itself and whatever invoked it (a plain shell, a script)
    — review-caught: without this, `rig worktree gc --repo . --yes` run FROM INSIDE a clean,
    merged linked worktree would classify its own cwd as non-live and could force-remove the
    directory (and delete the branch) out from under the very invocation doing the removing.
    """
    try:
        pgrep = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", _LIVE_PROCESS_PATTERN],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return _report_liveness_snapshot_untrusted("pgrep could not be run")
    if pgrep.returncode not in (0, 1):  # 1 == "no processes matched" per pgrep(1), not an error
        return _report_liveness_snapshot_untrusted(f"pgrep exited {pgrep.returncode}")

    try:
        cwds = [Path.cwd().resolve()]  # see docstring: THIS process's own cwd is always "live"
    except OSError:
        # e.g. the caller's own cwd was itself deleted out from under it (`rm -rf` the worktree
        # you were standing in, then `rig worktree gc --repo /elsewhere`) — `Path.cwd()` raises
        # `FileNotFoundError` in that case (review-caught: previously uncaught, would have
        # crashed the whole command instead of just degrading this one signal).
        return _report_liveness_snapshot_untrusted("could not determine this process's own cwd")
    for pid in (p for p in pgrep.stdout.split() if p.isdigit()):
        cwd = _process_cwd(pid)
        if cwd is None:
            return _report_liveness_snapshot_untrusted(f"could not determine cwd of matched pid {pid}")
        cwds.append(cwd.resolve())
    return cwds


def _report_liveness_snapshot_untrusted(reason: str) -> None:
    print(
        f"worktree-gc: warning — liveness check could not be verified ({reason}); every worktree "
        "will be treated as live (kept, never removed) until this is resolved",
        file=sys.stderr,
    )
    return None


def _liveness_check_from_snapshot(cwds: list[Path] | None) -> LivenessCheck:
    """Build a ``Callable[[Path], bool]`` over an ALREADY-TAKEN :func:`_live_process_cwds`
    snapshot — no further subprocess calls, just path comparisons. Fails SAFE toward "live"
    (``True``) when ``cwds`` is ``None`` (the snapshot itself couldn't be trusted) — a false "live"
    merely skips one worktree for this run, while a false "not live" could delete a tree a real
    agent is sitting in."""

    def _check(path: Path) -> bool:
        if cwds is None:
            return True
        resolved_target = path.resolve()
        for cwd in cwds:
            try:
                cwd.relative_to(resolved_target)
                return True
            except ValueError:
                continue
        return False

    return _check


def default_liveness_check(path: Path) -> bool:
    """True if a running ``claude``/``codex``/``opencode`` process has ``path`` (or a subdirectory
    of it) as its cwd. A fresh, single-path check — takes its OWN ``pgrep``/``lsof`` snapshot on
    every call, so it is the right default for a one-off ``classify_worktree`` call but the WRONG
    one to call once per worktree in a loop (see :func:`_default_liveness_factory`, which
    :func:`plan_gc` actually uses: one snapshot shared across every worktree in a run, rather than
    re-running ``pgrep``/``lsof`` per worktree — O(worktrees) subprocess spawns on the ~70-worktree
    repos GH-329 was filed about would otherwise add tens of seconds to a single `gc`/`status` run).
    """
    return _liveness_check_from_snapshot(_live_process_cwds())(path)


def _default_liveness_factory() -> LivenessCheck:
    """The liveness check :func:`plan_gc` actually defaults to: ONE snapshot for the whole run."""
    return _liveness_check_from_snapshot(_live_process_cwds())


# ── PR lookup (injectable) ───────────────────────────────────────────────────────
class GhLookupError(RuntimeError):
    """``gh pr list`` could not be run/trusted — see :func:`make_default_pr_lookup`."""


def _fetch_pr_index(repo_root: Path) -> dict[str, PrInfo]:
    """One ``gh pr list --state all`` call for the whole repo, indexed by head branch name.

    A single call (rather than one ``gh`` invocation per worktree) is both cheaper — matters for
    `rig status`, which calls into this on every invocation — and simpler to reason about. Like
    :mod:`riglib.daily.github`, a very large ``--state all`` history beyond ``_GH_PAGE_LIMIT``
    could in principle miss an old PR; unlike ``daily``, that risk here is one-sided and
    self-limiting: a branch this misses just reads as "no PR found" and falls through to the
    ``no-pr-stale`` bucket, which is never removed without both ``--yes`` AND ``--include-stale``.
    """
    try:
        res = subprocess.run(
            [
                "gh", "pr", "list",
                "--state", "all",
                "--limit", str(_GH_PAGE_LIMIT),
                "--json", "number,state,headRefName,mergedAt,closedAt,url,isCrossRepository",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise GhLookupError("`gh` CLI not found on PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        # OSError (not just SubprocessError) matters here: `gh` present but unreadable/
        # unexecutable (a PermissionError, say) raises a plain OSError, not a SubprocessError.
        raise GhLookupError(f"`gh pr list` failed: {exc}") from exc
    if res.returncode != 0:
        raise GhLookupError(f"`gh pr list` failed: {res.stderr.strip() or 'unknown error'}")

    try:
        raw = json.loads(res.stdout or "[]")
    except ValueError as exc:
        raise GhLookupError("`gh pr list` returned unparseable JSON") from exc
    if not isinstance(raw, list):
        raise GhLookupError(f"`gh pr list` returned a non-list JSON root ({type(raw).__name__})")

    return _index_prs_by_branch(raw)


# OPEN outranks MERGED outranks CLOSED — deliberately, and safety-critical: a branch name can be
# reused across PRs with DIFFERENT base branches (e.g. `foo` merged into `main` as PR #1, then the
# same head branch `foo` opened again against `release-2.x` as PR #2 — `gh pr list --state all`
# returns both under the same `headRefName`). Picking MERGED here (the original ordering) would
# classify a live, still-open PR's worktree as `merged` and remove it on a bare `--yes` — an open
# PR is defined as a hard keep everywhere else in this module, so its priority must reflect that.
_PR_STATE_PRIORITY = {"OPEN": 0, "MERGED": 1, "CLOSED": 2}


def _index_prs_by_branch(raw: list) -> dict[str, PrInfo]:
    """Best PR per branch — OPEN beats MERGED beats CLOSED (see :data:`_PR_STATE_PRIORITY`), for
    when a branch has more than one PR across its lifetime. A malformed record is skipped, never
    crashes the fetch.

    Cross-repository (fork) PRs are skipped entirely — a fork PR's ``headRefName`` is the branch
    name IN THE FORK, and `gh pr list --state all` returns it under that bare name with no
    repository qualifier. A LOCAL branch in THIS clone can coincidentally share that exact name
    (e.g. both called ``feature``) with no relationship to the fork PR at all; indexing it would
    let an unrelated fork PR's state (say, a fork's OWN merged/closed history) drive a
    classification for a local worktree it has nothing to do with. This clone's worktrees can only
    ever correspond to PRs whose head is in THIS repository, so cross-repo records are simply not
    useful data for this index and are dropped, not merged in.

    A MERGED/CLOSED record whose own resolution date (``mergedAt``/``closedAt``) is missing or
    doesn't parse is ALSO dropped here, same as any other malformed shape (review-caught, Codex
    round 20) — the realistic trigger isn't hostile data, it's a `gh --json` field-list rename or
    typo silently setting the field ``None`` on EVERY record, which would otherwise disable
    :func:`_branch_outlived_its_pr`'s date check across the board while looking like a normal
    MERGED/CLOSED result. Dropping the record here (branch reads as "no PR found") keeps that
    failure inside the SAME double-gated ``no-pr-stale``/``active`` buckets every other malformed-
    data case already falls back to, rather than trusting an unparseable date and classifying the
    branch removable purely on :func:`_has_unpushed_commits`'s say-so (which only protects against
    losing UNREACHABLE commits, not against deleting a branch that is still genuinely in active use
    but happens to already be fully pushed — e.g. a long-lived `release` branch with legitimate
    post-merge commits).
    """
    by_branch: dict[str, PrInfo] = {}
    for item in raw:
        if not isinstance(item, dict):
            # A non-dict element (`null`, a bare number, …) from a broken `gh`/proxy response —
            # skip it here, BEFORE the `.get()` below, which would otherwise raise AttributeError
            # and escape the documented "a malformed record is skipped, never crashes" contract.
            continue
        if item.get("isCrossRepository"):
            continue
        raw_branch = item.get("headRefName")
        if not isinstance(raw_branch, str) or not raw_branch:
            # `str(None)` would otherwise silently become the literal branch name "None" — a
            # malformed `headRefName: null`/numeric/empty record would then match any REAL local
            # branch coincidentally named "None" and drive its classification off garbage data
            # (review-caught: violates this function's own "a malformed record is skipped"
            # contract, which the non-dict-element guard above only partially covered).
            continue
        try:
            branch = raw_branch
            candidate = PrInfo(
                number=int(item["number"]),
                state=str(item.get("state", "")),
                merged_at=item.get("mergedAt"),
                closed_at=item.get("closedAt"),
                url=str(item.get("url", "")),
            )
        except (TypeError, ValueError, KeyError):
            continue
        if candidate.state not in _PR_STATE_PRIORITY:
            # An unrecognized `state` (a malformed/renamed `--json` field, or a future GitHub PR
            # state value beyond OPEN/MERGED/CLOSED) — review-caught (Sonnet, round 21): without
            # this guard, such a record still got INSERTED (at priority 9, so it only survives as
            # the sole record for its branch), and `_classify_clean_worktree`'s four sequential
            # `pr.state == ...` checks all miss it, falling through to "no PR found" even though
            # `pr is not None` — inconsistent with this same function's existing "drop, don't
            # guess" handling of a missing resolution date just below. Dropped here for the same
            # reason: the branch reads as "no PR found", which only ever reaches the double-gated
            # no-pr-stale/active buckets, never a wrongly-confident merged/closed removal.
            continue
        if candidate.state == "MERGED" and _parse_iso8601_z(candidate.merged_at) is None:
            continue
        if candidate.state == "CLOSED" and _parse_iso8601_z(candidate.closed_at) is None:
            continue
        existing = by_branch.get(branch)
        if existing is None or _PR_STATE_PRIORITY.get(candidate.state, 9) < _PR_STATE_PRIORITY.get(
            existing.state, 9
        ):
            by_branch[branch] = candidate
    return by_branch


def make_default_pr_lookup(repo_root: Path) -> PrLookup:
    """Build a ``pr_lookup`` closure over one ``gh pr list`` fetch for ``repo_root``.

    On failure (``gh`` missing, unauthenticated, offline, …) the returned lookup always answers
    "no PR found" rather than raising — a lookup failure must never abort classification for the
    rest of the repo's worktrees. That degrades gracefully: a branch with a real merged/closed PR
    would then read as "no PR found", but the only bucket that reaches is ``no-pr-stale`` — which
    removes NOTHING without both ``--yes`` and the explicit ``--include-stale`` opt-in. The
    degradation is still reported LOUDLY (stderr) rather than silently — "fail-explicit on IO" per
    this repo's AGENTS.md — so a report full of ``no-pr-stale`` never looks like a clean read.
    """
    try:
        by_branch: dict[str, PrInfo] | None = _fetch_pr_index(repo_root)
    except GhLookupError as exc:
        print(
            f"worktree-gc: warning — `gh pr list` unavailable ({exc}); every branch will read "
            "as having no PR (no-pr-stale still requires --include-stale to remove)",
            file=sys.stderr,
        )
        by_branch = None

    def _lookup(branch: str) -> PrInfo | None:
        return None if by_branch is None else by_branch.get(branch)

    return _lookup


# ── per-worktree checks ──────────────────────────────────────────────────────────
def _is_dirty(path: Path) -> tuple[bool, int]:
    """``(is_dirty, changed_file_count)``. A git-status failure is conservatively reported dirty
    (never let an unreadable worktree look "safely clean" and get auto-removed). Raw bytes, not
    ``text=True`` (see :func:`_decode`) — an uncommitted file with a non-UTF-8 name would
    otherwise raise an uncaught ``UnicodeDecodeError`` straight out of ``subprocess.run``.

    ``--untracked-files=normal`` pins untracked-file reporting ON regardless of ambient
    ``status.showUntrackedFiles`` config (repo, global, or system) — without it, a user/repo with
    that set to ``no`` would make an untracked-but-not-ignored file (e.g. a hand-written notes.md
    never `git add`ed) invisible to `--porcelain`, reading the worktree as clean and removing it
    (review-caught: the documented "ignored files aren't specially protected" limitation covers
    IGNORED files only — this closes the same gap for ordinary untracked ones).
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return True, 0
    if res.returncode != 0:
        return True, 0
    lines = [line for line in _decode(res.stdout).splitlines() if line.strip()]
    return bool(lines), len(lines)


def _resolve_own_branch_ref(git_dir: Path, ref: str) -> str | None:
    """The full ``refs/heads/<name>`` that ``ref`` itself names, when it names a branch —
    ``None`` for a detached ``HEAD`` or anything that isn't a branch. Used by
    :func:`_has_unpushed_commits` to EXCLUDE a branch from its own "is this reachable from
    something else" check: a branch trivially "contains" its own tip, so without excluding it,
    every branch would always show as "reachable from itself" and the check would never fire.

    Deliberately NOT ``symbolic-ref --short HEAD``: git's unambiguous-shortening rules resolve a
    TAG before a branch of the same name, so with both ``refs/heads/agent-1`` and
    ``refs/tags/agent-1`` present, ``--short`` cannot shorten to the bare ``agent-1`` (that would
    be ambiguous with the tag) and instead prints ``heads/agent-1`` — this function would then
    build the nonsense ref ``refs/heads/heads/agent-1``, which matches nothing in the
    ``for-each-ref`` output, silently defeating the exclusion for exactly the same same-named-tag
    hazard :func:`_prunable_unpushed_ref` already documents guarding against for the prunable
    path (review-caught: the present-worktree path had the identical hazard through a different
    mechanism). ``symbolic-ref`` WITHOUT ``--short`` always returns the full, unambiguous
    ``refs/heads/<name>`` form — verified empirically against a same-named tag.
    """
    if ref.startswith("refs/heads/"):
        return ref
    if ref != "HEAD":
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(git_dir), "symbolic-ref", "-q", "HEAD"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None  # detached HEAD — nothing to exclude
    name = _decode(res.stdout).strip()
    return name or None


def _has_unpushed_commits(git_dir: Path, ref: str = "HEAD") -> bool:
    """True if ``ref`` (default ``HEAD``, resolved from ``git_dir``) is not reachable from ANY
    ref OTHER than its own branch — i.e. this may be a commit that exists nowhere else: not on any
    remote-tracking ref, not on any other local branch, not on any tag. A git-status-clean worktree
    is NOT the same as a SAFE-TO-DELETE worktree: an agent's PR can merge, the agent then commits
    two more local fixes on the SAME branch and never pushes — `git status --porcelain` is empty
    (nothing UNCOMMITTED), but those two commits would be unrecoverable the moment `git worktree
    remove` + `git branch -D` runs.

    Checks `refs/heads` (other local branches), `refs/remotes` (pushed), AND `refs/tags` — NOT
    remote-tracking refs alone (a review-caught gap: a repo with zero remote-tracking refs was
    treated as "nothing can be unpushed", but that reasoning only holds for the PR-based
    classifications, which are impossible without a real `gh`-queryable remote in the first place;
    `prunable` and the no-PR `no-pr-stale` path have NO such precondition and are reachable in a
    genuinely local-only repo — a unique local commit there is just as unrecoverable once deleted,
    remote or not). The branch's OWN ref is excluded from the "found elsewhere" search (see
    :func:`_resolve_own_branch_ref`) — a branch trivially contains its own tip, so without
    excluding it this check would never fire for anything.

    ``git_dir``/``ref`` are independent so the SAME check covers two shapes: a live worktree
    checks its own ``HEAD`` from its own directory; a PRUNABLE worktree (directory already gone —
    see :func:`classify_worktree`) has no directory to run git in at all, so it instead checks its
    BRANCH NAME from the primary repo's directory (the branch ref itself still lives in the shared
    object store even once the worktree's working directory is deleted by hand).

    Conservative on any failure: a subprocess error, or ``ref`` unreachable from every OTHER ref,
    reports ``True`` (assume unsafe) rather than silently trusting a merged-looking or unowned
    branch that could hide unpushed work. Local-only (no `git fetch` freshness guarantee) but
    strictly safer than not checking at all.
    """
    own_ref = _resolve_own_branch_ref(git_dir, ref)
    try:
        res = subprocess.run(
            [
                "git", "-C", str(git_dir), "for-each-ref", "--format=%(refname)",
                "--contains", ref, "refs/heads", "refs/remotes", "refs/tags",
            ],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if res.returncode != 0:
        return True
    others = [line for line in _decode(res.stdout).splitlines() if line.strip() and line.strip() != own_ref]
    return not others


def _parse_iso8601_z(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp that may use a literal ``Z`` UTC suffix — both ``%cI`` (a UTC
    commit renders its offset as a literal ``Z``, verified empirically) and ``gh``'s own
    ``mergedAt``/``closedAt`` timestamps use this form. ``datetime.fromisoformat`` only accepts
    ``Z`` from Python 3.11 on, and this repo's own CI matrix (``.github/workflows/ci.yml``) runs
    3.10, so it is normalized by hand here — ONE helper so a commit date and a PR resolution date
    are parsed identically and stay directly comparable.

    A NAIVE result (no offset in ``value`` at all — real `gh`/`%cI` output never omits one, but a
    malformed/synthetic record could) is normalized to UTC rather than returned as-is: comparing a
    naive and an aware datetime raises ``TypeError`` in the caller, and this function exists
    specifically so two differently-sourced timestamps stay directly, safely comparable.

    ``value`` is typed as plain ``object``, not ``str | None`` — it comes straight from parsed
    JSON (a PR's ``mergedAt``/``closedAt``), so a broken/malformed `gh` response could hand this a
    number, list, or dict instead of a string. A non-string is rejected the same way ``None``/
    empty already is (review-caught: ``value.replace(...)`` on a non-string would otherwise raise
    an uncaught ``AttributeError``, violating this module's "a malformed record is skipped, never
    crashes" contract).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _last_commit_date_utc(path: Path) -> datetime | None:
    """The last commit's committer date, or ``None`` if git can't answer. Raw bytes, not
    ``text=True`` — ``%cI`` output is normally pure ASCII, but consistent with every other git
    call in this module (see :func:`_decode`) rather than a special case that could bite if git
    ever changes what it emits here."""
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "log", "-1", "--format=%cI"], capture_output=True, timeout=_GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return _parse_iso8601_z(_decode(res.stdout).strip())


def _worktree_creation_mtime_utc(path: Path) -> datetime | None:
    """A proxy for "when was THIS WORKTREE created" — independent of its base commit's own
    (possibly much older) date. ``<worktree>/.git`` is a FILE (a gitdir pointer) that ``git
    worktree add`` writes fresh at creation time, so its mtime approximates worktree age even for
    a worktree branched from an ancient base ref. Falls back to the worktree directory's own mtime
    if the ``.git`` pointer file itself can't be stat'd."""
    for candidate in (path / ".git", path):
        try:
            return datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
    return None


def _last_activity_utc(path: Path) -> datetime | None:
    """The MORE RECENT of the last commit's date and the worktree's own creation time.

    Using the commit date ALONE misclassifies a worktree branched from an old base ref (e.g. `rig
    worktree create agent-9 --from <old-tag>`, or simply a repo whose default branch hasn't moved
    recently) as immediately `no-pr-stale` — it was created seconds ago but its one commit is
    weeks old (a review-caught false positive). Taking the max of both signals means a worktree is
    "recently active" if EITHER it has a recent commit OR it was recently created — exactly what
    "how old is this stale-candidate worktree" should mean.
    """
    candidates = [d for d in (_last_commit_date_utc(path), _worktree_creation_mtime_utc(path)) if d is not None]
    return max(candidates) if candidates else None


# ── classification ───────────────────────────────────────────────────────────────
def _prunable_unpushed_ref(info: WorktreeInfo) -> str | None:
    """The ref to check reachability for on a worktree with NO working directory (prunable) —
    ``refs/heads/<branch>`` when it had one, else its raw ``HEAD`` commit SHA for a DETACHED
    worktree (``for-each-ref --contains`` accepts a bare commit-ish just as well as a ref name).
    ``None`` only if neither is available (shouldn't happen for a real ``git worktree list``
    record, but a missing ``HEAD`` field must not silently skip the safety check either — see
    :func:`_classify_prunable`, which treats ``None`` as "can't verify, keep it").

    A detached-HEAD worktree has ``info.branch is None``, and WITHOUT this fallback the
    unpushed-commit check never ran for it at all (review-caught): git still holds its commit
    live via the worktree's own administrative ``HEAD`` file until the worktree is truly removed,
    exactly the same "may be the only copy" risk a branch-backed worktree has.

    ``refs/heads/<branch>``, never the bare branch name: `git ... --contains <name>` resolves a
    bare ref name through git's normal disambiguation order, which checks `refs/tags/<name>`
    BEFORE `refs/heads/<name>` — a same-named TAG pointing at an already-pushed commit would make
    `--contains` inspect the TAG's commit instead of the branch's, silently reporting "reachable"
    for a branch whose own tip is actually unpushed (review-caught).
    """
    if info.branch:
        return f"refs/heads/{info.branch}"
    return info.head_sha


def _classify_prunable(info: WorktreeInfo, repo_root: Path) -> ClassifiedWorktree:
    """A registered-but-missing worktree — normally safe to prune, EXCEPT when its branch (or, for
    a detached HEAD, its raw commit) holds work that exists nowhere else. There is no working
    directory left to run `git status`/`git log` in, but the ref itself still lives in the primary
    repo's shared object store, so the unpushed-commit check still runs — just rooted at
    ``repo_root`` (see :func:`_prunable_unpushed_ref`, :func:`_has_unpushed_commits`). Without
    this, a worktree directory deleted by hand (`rm -rf` instead of `git worktree remove`) BEFORE
    its work was ever pushed would be auto-removed on a bare `--yes` with no safety net at all — a
    review-caught gap: `prunable` short-circuited straight to "safe to remove" with none of the
    dirty/unpushed checks a still-present worktree gets. A ``None`` ref (no branch AND no HEAD sha
    on record — shouldn't happen for a real listing) is treated as "can't verify" and kept, not
    trusted as safe.

    Git marks a worktree ``prunable`` when its ADMINISTRATIVE gitdir link is missing/broken — NOT
    necessarily because the worktree's own DIRECTORY is gone (a partial deletion, e.g. only the
    worktree's ``.git`` pointer file got removed by hand, can leave the rest of the files sitting
    there). If ``info.path`` still exists, this function refuses to trust it as a clean prune
    candidate at all: `git -C <path>` with no valid `.git` there walks UP the filesystem hierarchy
    and silently reports on the PRIMARY repo instead (review-caught) — any dirty/unpushed check run
    against that path would be evaluating the wrong repository entirely. Classified `dirty`
    unconditionally in that case, for a human to look at by hand.
    """
    reason = info.prunable_reason or "worktree directory no longer exists on disk"
    if info.path.exists():
        return ClassifiedWorktree(
            info,
            "dirty",
            "registered prunable (git considers its admin gitdir broken), but the directory "
            "itself is still present — refusing to guess at its state; needs a manual look",
            removable_class=False,
        )
    ref = _prunable_unpushed_ref(info)
    if ref is None or _has_unpushed_commits(repo_root, ref):
        return ClassifiedWorktree(
            info,
            "dirty",
            "worktree directory is gone, but its branch has commits not on any remote-tracking "
            "ref (possible unpushed work) — kept for manual recovery",
            removable_class=False,
        )
    return ClassifiedWorktree(info, "prunable", reason, removable_class=True)


def classify_worktree(
    info: WorktreeInfo,
    *,
    repo_root: Path,
    liveness_check: LivenessCheck,
    pr_lookup: PrLookup,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    now: datetime | None = None,
) -> ClassifiedWorktree:
    """Classify one worktree. Order is load-bearing: liveness first (absolutely, no exception),
    then an explicit `git worktree lock`, then a symlinked root (never a legitimate worktree —
    see below), then prunable (itself unpushed-commit-checked — see :func:`_classify_prunable`),
    then dirty, then the PR-based buckets (which check unpushed commits of their own when there's
    no PR to compare a date against — see :func:`_classify_clean_worktree`) — see the module
    docstring."""
    if liveness_check(info.path):
        return ClassifiedWorktree(
            info, "live", "a running claude/codex/opencode process has this cwd", removable_class=False
        )

    if info.locked:
        # `git worktree lock` is an explicit human "don't touch this" marker, independent of PR
        # state or activity — respect it outright rather than classifying normally and letting
        # `git worktree remove --force` refuse it downstream (which would just add per-entry
        # `remove_error` noise to every `--yes` run instead of a clean, informative "kept").
        return ClassifiedWorktree(
            info, "active", f"locked: {info.locked_reason or 'no reason given'}", removable_class=False
        )

    if info.path.is_symlink():
        # A genuine `git worktree add`-created root is NEVER a symlink — git always creates a
        # real directory there (the same invariant `riglib.worktree.remove` already refuses to
        # operate through). Checked with `is_symlink()`, NOT `exists()`: a symlink whose target
        # happens to exist would otherwise sail through this classifier and every downstream
        # `git -C <path>` call would silently operate on wherever the link points — review-caught
        # (Codex): a registered worktree path replaced by a symlink is not something this
        # module's threat model should trust as "the same worktree git registered".
        return ClassifiedWorktree(
            info,
            "dirty",
            "registered worktree path is a symlink, not a real directory — refusing to operate "
            "through it; needs a manual look",
            removable_class=False,
        )

    if info.prunable or not info.path.exists():
        return _classify_prunable(info, repo_root)

    is_dirty, n_changed = _is_dirty(info.path)
    if is_dirty:
        return ClassifiedWorktree(
            info, "dirty", f"dirty: {n_changed} changed file(s)", removable_class=False
        )

    pr = pr_lookup(info.branch) if info.branch else None
    return _classify_clean_worktree(info, pr, older_than_days, now or datetime.now(timezone.utc))


def _branch_outlived_its_pr(
    info: WorktreeInfo, pr: PrInfo, last_activity: datetime | None
) -> ClassifiedWorktree | None:
    """``None`` when the branch's tip is NOT newer than the PR's own resolution date — the branch
    did not gain new work AFTER the PR resolved (this half of the safety story is still needed
    even though :func:`_has_unpushed_commits` ALSO runs unconditionally afterward — see the
    caller: this catches the "gained commits AFTER merging" case that a pushed-ness check alone
    cannot, since a commit made and pushed to a DIFFERENT branch state after merge would still
    read as "reachable from some remote ref" if it happened to get pushed too). Otherwise
    classifies the worktree ``active``: its branch has commits dated AFTER the PR resolved,
    meaning it outlived that one PR — e.g. a long-lived branch like ``develop`` that gets merged
    repeatedly, or any branch reused after its PR closed. Review-caught: `rig worktree gc`
    reconciles EVERY worktree git knows about, not just rig-created scratch ones — without this
    check, a human's long-lived, repeatedly-merged branch worktree would be destroyed on a bare
    ``--yes`` the moment its most recent PR happened to merge, even though the branch is still
    very much alive.

    A ``PrInfo`` reaching this function is assumed already-validated: :func:`_index_prs_by_branch`
    is the one place raw `gh` JSON becomes a ``PrInfo``, and it drops a MERGED/CLOSED record whose
    resolution date doesn't parse (review-caught, Codex round 20 — see that function's docstring
    for why the guard belongs there and not here).
    """
    resolved_at = _parse_iso8601_z(pr.merged_at if pr.state == "MERGED" else pr.closed_at)
    if resolved_at is None or last_activity is None or last_activity <= resolved_at:
        return None
    return ClassifiedWorktree(
        info,
        "active",
        f"PR #{pr.number} {pr.state.lower()}, but the branch has commits after that — still active",
        removable_class=False,
    )


def _classify_clean_worktree(
    info: WorktreeInfo, pr: PrInfo | None, older_than_days: int, now: datetime
) -> ClassifiedWorktree:
    """The PR-based branch of classification, reached only once liveness/prunable/dirty are
    already ruled out. Split out from :func:`classify_worktree` to keep that function short."""
    last_activity = _last_activity_utc(info.path)

    if pr is not None and pr.state in ("MERGED", "CLOSED"):
        outlived = _branch_outlived_its_pr(info, pr, last_activity)
        if outlived is not None:
            return outlived
        # The date check above only proves the branch got NO commits AFTER its PR resolved — it
        # says NOTHING about whether a commit made BEFORE the merge was ever actually pushed (a
        # review-caught gap: "committed locally at 10:05, PR merges at 10:10" predates the merge
        # date and would otherwise sail through as "not outlived" while never having reached the
        # remote at all). ALWAYS verify pushed-ness too, not just as a fallback for a missing date
        # — deliberately accepting the KNOWN trade-off this creates for a squash-merged branch
        # whose remote-tracking ref was later pruned (`git fetch --prune` after GitHub auto-deletes
        # the merged branch): such a worktree now reports `dirty` (kept, needs a human look) rather
        # than being auto-cleaned, which is a real effectiveness regression for that one configuration
        # but the only sound choice between "occasionally fails to auto-clean" and "occasionally
        # destroys real unpushed work" — see the module docstring's "Known limitations" section.
        if _has_unpushed_commits(info.path):
            return ClassifiedWorktree(
                info,
                "dirty",
                f"PR #{pr.number} {pr.state.lower()}, but HEAD has commits not on any "
                "remote-tracking ref (possible unpushed work)",
                removable_class=False,
            )

    if pr is not None and pr.state == "MERGED":
        return ClassifiedWorktree(
            info, "merged", f"PR #{pr.number} merged {pr.merged_at or ''}".strip(), removable_class=True
        )
    if pr is not None and pr.state == "CLOSED":
        return ClassifiedWorktree(
            info, "closed", f"PR #{pr.number} closed {pr.closed_at or ''}".strip(), removable_class=True
        )
    if pr is not None and pr.state == "OPEN":
        return ClassifiedWorktree(info, "active", f"PR #{pr.number} open", removable_class=False)

    # No PR found at all — there's no PR resolution date to compare against (the check
    # `_branch_outlived_its_pr` does above), so the unpushed-commit check is the ONLY safety net
    # left before a `no-pr-stale` classification, which is why it's scoped to just this branch
    # rather than gating every worktree unconditionally (a `merged`/`closed` branch that has NOT
    # outlived its PR is already provably safe from the date comparison above, remote-tracking
    # state or not — see `_branch_outlived_its_pr`'s docstring).
    if _has_unpushed_commits(info.path):
        return ClassifiedWorktree(
            info,
            "dirty",
            "clean working tree, but HEAD has commits not on any remote-tracking ref "
            "(possible unpushed work)",
            removable_class=False,
        )

    age_days = (now - last_activity).days if last_activity else None
    if age_days is not None and age_days >= older_than_days:
        return ClassifiedWorktree(
            info,
            "no-pr-stale",
            f"no PR found; last activity {age_days} day(s) ago (>= --older-than-days {older_than_days})",
            removable_class=True,
        )
    return ClassifiedWorktree(info, "active", "no PR found; recent activity", removable_class=False)


def _plan_removable(classified: ClassifiedWorktree, *, include_stale: bool) -> bool:
    """Whether this run's FLAGS actually target ``classified`` for removal — distinct from
    ``removable_class`` (the classification's category), since ``no-pr-stale`` needs the explicit
    ``--include-stale`` opt-in on top of its category membership."""
    if classified.classification in _AUTO_REMOVABLE_ALWAYS:
        return True
    if classified.classification == "no-pr-stale":
        return include_stale
    return False


# ── planning / execution ─────────────────────────────────────────────────────────
def plan_gc(
    repo_root: Path,
    *,
    include_stale: bool = False,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    liveness_check: LivenessCheck | None = None,
    pr_lookup: PrLookup | None = None,
) -> list[GcEntry]:
    """Classify every worktree of ``repo_root``. Touches disk only for read-only git/gh/pgrep
    calls — never removes anything and never computes directory sizes (see :func:`run_gc`).

    ``liveness_check``/``pr_lookup`` default to ``None`` and are resolved to
    :func:`_default_liveness_factory`/:func:`make_default_pr_lookup` INSIDE the body (a plain
    global lookup at call time) rather than as ordinary default-parameter values — a default
    parameter value is bound once, at function-definition time, so a caller (or a test) that
    patches the module-level default AFTER import would otherwise never see its own patch take
    effect here. The liveness default is a FACTORY (called once per `plan_gc` run, not once per
    worktree) so its one `pgrep`/`lsof` snapshot is shared across every worktree classified in
    this run — seeing :func:`default_liveness_check` used here instead would mean one fresh
    `pgrep` + one `lsof` per matched pid PER WORKTREE, hundreds of subprocess spawns on the
    ~70-worktree repos GH-329 was filed about.

    Lists worktrees BEFORE building the (potentially network-calling) default ``pr_lookup`` — a
    repo with zero linked worktrees (the common case for `rig status` on most repos) must never
    pay for a `gh pr list` call it will throw away unused.

    Resolves ``repo_root`` to its PRIMARY worktree FIRST (see :func:`_resolve_primary_worktree`) —
    ``repo_root`` may itself be a linked worktree, and every downstream git call in this function
    (and, for `run_gc`, the removal calls after it) must be anchored to the primary, never to a
    worktree that could be removed partway through the same run.
    """
    primary = _resolve_primary_worktree(repo_root)
    infos = list_worktrees(primary)
    if not infos:
        return []

    effective_liveness = liveness_check or _default_liveness_factory()
    effective_lookup = pr_lookup or make_default_pr_lookup(primary)
    entries = []
    for info in infos:
        classified = _classify_worktree_or_fail_safe(
            info,
            repo_root=primary,
            liveness_check=effective_liveness,
            pr_lookup=effective_lookup,
            older_than_days=older_than_days,
        )
        entries.append(GcEntry(classified, _plan_removable(classified, include_stale=include_stale)))
    return entries


def _classify_worktree_or_fail_safe(
    info: WorktreeInfo,
    *,
    repo_root: Path,
    liveness_check: LivenessCheck,
    pr_lookup: PrLookup,
    older_than_days: int,
) -> ClassifiedWorktree:
    """Wraps :func:`classify_worktree` so ONE entry's unexpected exception can never abort
    classification of every OTHER worktree in the same run — review-caught gap: because liveness
    is checked absolutely first (by design, see :func:`classify_worktree`'s docstring), a path that
    makes ``liveness_check`` itself raise (a symlink loop → ``RuntimeError``/``OSError`` from
    ``Path.resolve()``) used to escape before this entry's OWN symlink guard ever got a chance to
    catch it, taking down the whole repo's plan with it. Fails toward the safest classification
    (``live``, non-removable) for THIS entry alone, exactly like a degraded liveness snapshot
    already does elsewhere in this module, and lets every other worktree classify normally.
    """
    try:
        return classify_worktree(
            info,
            repo_root=repo_root,
            liveness_check=liveness_check,
            pr_lookup=pr_lookup,
            older_than_days=older_than_days,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring above
        return ClassifiedWorktree(
            info,
            "live",
            f"could not classify ({exc}) — treated as live, needs a manual look",
            removable_class=False,
        )


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive size of ``path`` on disk. A single unreadable file/dir (permission,
    or a race with concurrent removal) is skipped rather than aborting the whole sum."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _remove_worktree_and_branch(repo_root: Path, entry: GcEntry) -> None:
    """Best-effort ``git worktree remove --force`` + ``git branch -D``, operating on the entry's
    OWN registered path (not assumed to be under the standardized ``.worktrees/<name>`` location —
    gc must clean up every naming convention this ecosystem inherited, not just rig's own).
    A branch-delete failure is recorded but does not undo ``removed=True``: the worktree really is
    gone by that point, mirroring :func:`riglib.worktree.remove`'s own two-step contract.
    """
    # `--` before the path for the SAME reason `branch -D` gets one below (review-caught, Sonnet
    # round 23, for consistency with that already-established defense): `entry.path` comes from
    # git's OWN `worktree list` output, always an absolute (`/`-prefixed) path in practice, so
    # this isn't closing a reachable gap — just costing nothing to match the trust-boundary
    # treatment already applied to `entry.branch` two lines below.
    cmd = ["git", "-C", str(repo_root), "worktree", "remove", "--force", "--", str(entry.path)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        entry.remove_error = f"git worktree remove failed: {exc}"
        return
    if res.returncode != 0:
        detail = _decode(res.stderr).strip() or _decode(res.stdout).strip()
        entry.remove_error = f"git worktree remove failed: {detail or f'exit {res.returncode}'}"
        return

    entry.removed = True
    if not entry.branch:
        return
    # `--` before the branch name as defensive hardening (review-flagged): `entry.branch` comes
    # from git's OWN `worktree list` output, not from `_invalid_name_reason`-validated input the
    # way `rig worktree create`'s branch names are, so it's worth not assuming its shape. Verified
    # empirically that this specific concern (a branch literally named `-something`) can't actually
    # arise in practice — `git check-ref-format --branch` itself rejects any branch name starting
    # with `-` at creation time, so `entry.branch` can never carry one — but `--` costs nothing for
    # every legal branch name and matches the same option-injection defense
    # `riglib.worktree._invalid_ref_reason` already documents for `create`'s own inputs.
    branch_cmd = ["git", "-C", str(repo_root), "branch", "-D", "--", entry.branch]
    try:
        bres = subprocess.run(branch_cmd, capture_output=True, timeout=_GIT_TIMEOUT_S)
        if bres.returncode != 0:
            detail = _decode(bres.stderr).strip() or _decode(bres.stdout).strip()
            entry.remove_error = f"worktree removed, but git branch -D failed: {detail}"
    except (OSError, subprocess.SubprocessError) as exc:
        entry.remove_error = f"worktree removed, but git branch -D failed: {exc}"


def _execute_removals(repo_root: Path, entries: list[GcEntry], liveness_check: LivenessCheck | None) -> None:
    """Attempt every planned removal; one entry's failure must not skip the rest.

    Re-checks liveness, dirtiness, AND unpushed-commit safety IMMEDIATELY before EACH removal —
    never the batched planning-time snapshot :func:`plan_gc` shares across every worktree. That
    snapshot (and the checks that ran alongside it) can be tens of seconds stale by the time PR
    lookup, disk sizing, and any earlier entries' removals finish: an agent can start a session in
    a worktree in that window (the liveness half), a human/non-agent process can write an
    uncommitted file into it (the dirtiness half), or COMMIT a new local fix during that same
    window (only a fresh unpushed-commit check catches that one — `git status` alone reads clean).
    Any of the three tripping SKIPS the removal rather than proceeding, and is reported as such —
    never silently retried as if nothing needed doing.

    ``liveness_check`` is the caller's ORIGINAL override (or ``None``) — deliberately NOT a single
    pre-built closure shared across the whole loop. When it's ``None``, :func:`_default_liveness_factory`
    is called FRESH for EVERY entry, taking a brand-new `pgrep`/`lsof` snapshot each time — a
    review-caught gap: on a large (say, ~70-worktree) run where each removal takes a second or two,
    one shared snapshot from the start of this loop could be 40+ seconds stale by the time entry
    #35's turn comes, exactly the kind of staleness this whole recheck exists to catch. When the
    caller DID inject an explicit callable (tests, mainly), that SAME object is reused for every
    entry — a stateful fake can still simulate "became live partway through" across calls, exactly
    as before.

    This narrows, but does not fully close, the TOCTOU window down to `git worktree remove
    --force`'s own execution — a chdir or a new commit landing in the fractions-of-a-second between
    this recheck returning and that git call actually running is not caught by any check that runs
    BEFORE the git call. Closing that residual sliver would need OS-level coordination shared with
    every possible agent/human process touching the repo, which no tool in this ecosystem
    implements; it is accepted as a known, documented residual risk rather than solved here.
    """
    for entry in entries:
        if not entry.plan_removable:
            continue
        try:
            _execute_one_removal(repo_root, entry, liveness_check)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring below
            entry.remove_error = f"pre-removal recheck failed: {exc}"


def _execute_one_removal(
    repo_root: Path, entry: GcEntry, liveness_check: LivenessCheck | None
) -> None:
    """One entry's slice of :func:`_execute_removals`'s loop body, split out so the CALLER can wrap
    it in a single ``try/except`` — review-caught gap: the liveness recheck's ``path.resolve()``
    (symlink-loop → ``RuntimeError``/``OSError``) and the stat calls in
    :func:`_recheck_dirty_and_unpushed` (``EACCES`` is not among the errnos ``Path`` swallows) can
    both raise for reasons that are properties of ONE entry's path, not the run as a whole. Before
    this split, either raising escaped the whole loop, aborted every LATER entry's removal, and
    discarded the report for every EARLIER entry this same run had already removed — the opposite
    of "one entry's failure must not skip the rest." Each raise is now caught per-entry by the
    caller and recorded via ``remove_error``, exactly like a `git worktree remove` failure already
    is.
    """
    liveness_recheck = liveness_check or _default_liveness_factory()
    if liveness_recheck(entry.path):
        entry.skipped_reason = "became live (a running agent) between planning and removal"
        return
    skip_reason = _recheck_dirty_and_unpushed(repo_root, entry)
    if skip_reason is not None:
        entry.skipped_reason = skip_reason
        return
    _remove_worktree_and_branch(repo_root, entry)


def _recheck_dirty_and_unpushed(repo_root: Path, entry: GcEntry) -> str | None:
    """The dirty/unpushed half of :func:`_execute_removals`'s pre-removal recheck — split out to
    keep that function's own body short. Returns a skip reason, or ``None`` when clear to remove.

    A PRESENT worktree rechecks its own directory for dirtiness (`git status`), then rechecks
    unpushed-ness against BOTH the worktree's CURRENT actual state (bare ``HEAD`` from its own
    path) AND, when ``entry.branch`` is set, the SPECIFIC PLANNED BRANCH — ``refs/heads/
    <entry.branch>`` from ``repo_root`` — independently; either one being unsafe skips the
    removal. Checking only one of the two was tried and review-caught twice, in both directions:

    - Checking ONLY current ``HEAD`` misses this: `_remove_worktree_and_branch` always
      force-deletes ``entry.branch`` (the name captured at PLANNING time). If a human/agent
      checked out a DIFFERENT, already-pushed branch in that same directory during the window
      between planning and this recheck, current ``HEAD`` reads as that OTHER (safe) branch and
      passes, while the actual `git branch -D` call still deletes the ORIGINAL branch's unpushed
      work.
    - Checking ONLY the planned branch misses this: if that same human/agent instead checked out
      a DETACHED HEAD and made a NEW commit there (never on ``entry.branch`` at all), the planned
      branch still reads safe — but `git worktree remove --force` destroys the worktree's own
      administrative HEAD pointer, which was the ONLY reference to that detached commit, the
      moment it runs.

    Checking BOTH closes each gap the other one has. A detached-HEAD present worktree
    (``entry.branch is None`` from the START — no plan-time branch to double-check) only ever had
    the one thing to check: whatever commit its own current HEAD points at.

    A PRUNABLE entry (no directory — see :func:`classify_worktree`) has nothing to run `git
    status` in, but its branch/HEAD ref itself could still have advanced since planning (another
    process committing to the same branch name, say) — review-caught: the original recheck
    silently skipped BOTH checks for a missing directory, reusing only the ONE-TIME planning-time
    verdict for the rest of the run. Re-verifies via :func:`_prunable_unpushed_ref`, mirroring
    :func:`_classify_prunable`'s own planning-time check exactly, so a prunable entry gets the
    SAME fresh-immediately-before-removal guarantee a present one does.

    Also re-checks ``is_symlink()`` — a defense-in-depth twin of the same check in
    :func:`classify_worktree`, in case the path was replaced by a symlink in the window between
    planning and this recheck (a genuinely planned-safe entry can't reach here already symlinked,
    since that would have been classified `dirty`, never `plan_removable`, at planning time).
    """
    if entry.path.is_symlink():
        return "worktree path is now a symlink, not a real directory — refusing to remove through it"
    if entry.path.exists():
        is_dirty, n_changed = _is_dirty(entry.path)
        if is_dirty:
            return f"became dirty ({n_changed} changed file(s)) between planning and removal"
        unpushed = _has_unpushed_commits(entry.path)  # whatever is CURRENTLY checked out
        if not unpushed and entry.branch:
            unpushed = _has_unpushed_commits(repo_root, f"refs/heads/{entry.branch}")  # the PLANNED branch
        if unpushed:
            return "gained a commit with no remote-tracking ref between planning and removal"
        return None

    ref = _prunable_unpushed_ref(entry.classified.info)
    if ref is None or _has_unpushed_commits(repo_root, ref):
        return "branch gained a commit with no remote-tracking ref between planning and removal"
    return None


def run_gc(
    repo_root: Path,
    *,
    dry_run: bool,
    yes: bool,
    include_stale: bool = False,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    liveness_check: LivenessCheck | None = None,
    pr_lookup: PrLookup | None = None,
) -> GcReport:
    """Classify, size, and — only when ``yes`` is set and ``dry_run`` is not — actually remove.

    ``--dry-run`` always wins over ``--yes`` (mirrors ``rig apply --dry-run`` forcing a preview
    even under ``apply commit``). Disk size is computed ONLY for entries this run's flags target
    for removal (``plan_removable``) — never for every worktree, so gc stays fast on a repo with
    many live/active worktrees and a few huge removable ones.

    ``liveness_check``, when given, is used for BOTH the planning-time classification AND the
    fresh per-entry recheck immediately before removal (see :func:`_execute_removals`) — a caller
    that injects a stateful fake can simulate "became live in between" by returning a different
    answer on the second call. The real default ALSO resolves through the SAME seam for both
    roles — :func:`_default_liveness_factory` — called ONCE for planning (one shared, cached
    snapshot across every worktree, cheap for a many-worktree repo) and called AGAIN, FRESH, for
    EVERY SINGLE entry's recheck inside :func:`_execute_removals` (not once for the whole removal
    loop — a large run can take entry #35's turn tens of seconds after entry #1's, and a snapshot
    shared across the whole loop would be exactly that stale by then). Deliberately NOT :func:`default_liveness_check` for
    the real recheck default: that function does its own always-live pgrep/lsof scan of the WHOLE
    machine, which is genuinely live-sensitive on a dev box that (like the one this was written on)
    has other real ``claude``/``codex``/``opencode`` sessions running — routing the recheck through
    a *different* default than planning would make it possible to mock one without the other and
    silently reintroduce a non-hermetic, environment-dependent test.
    """
    # Resolved HERE too (not just inside `plan_gc`): the removal calls below need the SAME
    # primary-anchored path `plan_gc` classified against, and resolving independently — rather
    # than trusting `entries` to carry it — keeps this function correct even if `plan_gc`'s
    # internals change. `plan_gc` re-resolves the identical value from the identical `repo_root`,
    # so this costs one extra (cheap, local, no-network) `git worktree list` call, never a
    # different answer.
    primary = _resolve_primary_worktree(repo_root)
    entries = plan_gc(
        primary,
        include_stale=include_stale,
        older_than_days=older_than_days,
        liveness_check=liveness_check,
        pr_lookup=pr_lookup,
    )
    for entry in entries:
        if entry.plan_removable:
            entry.size_bytes = _dir_size_bytes(entry.path) if entry.path.exists() else 0

    effective_dry_run = dry_run or not yes
    if not effective_dry_run:
        _execute_removals(primary, entries, liveness_check)

    return GcReport(repo_root=primary, entries=entries, dry_run=effective_dry_run, include_stale=include_stale)


def stale_worktree_counts(
    repo_root: Path,
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    liveness_check: LivenessCheck | None = None,
    pr_lookup: PrLookup | None = None,
) -> dict[str, int]:
    """Cheap, dry-run-only classification summary for ``rig status``: no disk-size walk, no
    removal — just how many worktrees fall into each stale-eligible bucket. Raises
    :class:`WorktreeGcError` if ``git worktree list`` itself can't be trusted; callers decide how
    to degrade (``rig status`` treats this as best-effort and swallows it, see
    ``RIG_STATUS_SKIP_WORKTREE_GC`` in ``riglib/cli.py`` for a hard opt-out of the ``gh``/pgrep
    calls this still makes for a repo that DOES have linked worktrees — a repo with none pays
    nothing, per :func:`plan_gc`'s own list-before-lookup ordering)."""
    entries = plan_gc(
        repo_root,
        include_stale=True,
        older_than_days=older_than_days,
        liveness_check=liveness_check,
        pr_lookup=pr_lookup,
    )
    return dict(Counter(entry.classification for entry in entries))


# ── report rendering ──────────────────────────────────────────────────────────────
def _human_size(n: int | None) -> str:
    if not n:
        return "0 B"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _action_word(entry: GcEntry) -> str:
    """The action taken/planned for one entry.

    ``remove_error`` is checked BEFORE ``removed`` — deliberately, not incidentally:
    :func:`_remove_worktree_and_branch` sets BOTH when the worktree itself is removed but the
    follow-up ``git branch -D`` fails, and that message already says "worktree removed, but git
    branch -D failed: …" — checking ``removed`` first would print a bare "removed" and hide the
    stranded-branch error entirely, the exact failure this module's two-step removal contract
    exists to surface (a real bug this review caught: the branch-stranding case is the one thing
    a human reading the report most needs to see).

    ``skipped_reason`` is checked next (before the plain "removed"/dry-run fallthrough): it means
    :func:`_execute_removals` DID run for this entry but its fresh liveness recheck found the
    worktree live — a TOCTOU-safety skip, distinct from both a git failure and an actual removal.

    When ``plan_removable`` is true but none of ``remove_error``/``removed``/``skipped_reason`` is
    set, execution never ran at all — i.e. this was a dry run (``run_gc`` only calls
    :func:`_execute_removals` when NOT a dry run, and that call always sets one of the three on
    every removable entry).
    """
    if entry.remove_error:
        return entry.remove_error
    if entry.skipped_reason:
        return f"kept — {entry.skipped_reason}"
    if entry.removed:
        return "removed"
    if entry.plan_removable:
        return "would be removed (dry run — nothing changed)"
    if entry.classification == "no-pr-stale":
        return "kept — pass --yes --include-stale to remove"
    return "kept"


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")


def _sanitize_for_terminal(s: str) -> str:
    """Escape ASCII control bytes (0x00-0x1F, 0x7F — including ESC, which starts every ANSI/OSC
    sequence) AND lone UTF-16 surrogates (U+D800-DFFF) to a literal ``\\xHH``/``\\uHHHH`` form
    before printing untrusted content. A worktree path or branch name can legally contain
    arbitrary bytes on POSIX; without this, a maliciously crafted one could inject terminal
    control sequences into the report an operator reads (deceptive overwritten text, cursor/
    clipboard manipulation) — a review-caught hardening gap. The surrogate range matters
    separately: this module decodes git/lsof output with ``errors="surrogateescape"`` (see
    :func:`_decode`) precisely so a non-UTF-8 byte round-trips losslessly through ``str`` — but
    printing such a string to a strict-UTF-8 stdout (the Linux default) raises an uncaught
    ``UnicodeEncodeError`` at PRINT time, which on the removal-report path would mean the operator
    loses the record of what was just removed.
    """

    def _escape(match: "re.Match[str]") -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

    return _CONTROL_CHAR_RE.sub(_escape, s)


def _render_entry(entry: GcEntry) -> str:
    branch = _sanitize_for_terminal(entry.branch) if entry.branch else "(detached)"
    path = _sanitize_for_terminal(str(entry.path))
    reason = _sanitize_for_terminal(entry.reason)
    action = _sanitize_for_terminal(_action_word(entry))
    size = f", {_human_size(entry.size_bytes)}" if entry.size_bytes else ""
    return (
        f"  [{entry.classification:11s}] {path} (branch: {branch}){size}\n"
        f"      why: {reason}\n"
        f"      action: {action}"
    )


def _render_summary(report: GcReport) -> str:
    counts = report.counts()
    parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no worktrees"
    if report.total_reclaimed_bytes:
        return f"summary: {parts} — reclaimed {_human_size(report.total_reclaimed_bytes)}"
    if report.total_reclaimable_bytes:
        return f"summary: {parts} — {_human_size(report.total_reclaimable_bytes)} reclaimable"
    return f"summary: {parts}"


def render_report(report: GcReport) -> str:
    lines = [f"rig worktree gc — {_sanitize_for_terminal(str(report.repo_root))}"]
    if not report.entries:
        lines.append("  no linked worktrees found")
        return "\n".join(lines)
    for entry in sorted(report.entries, key=lambda e: str(e.path)):
        lines.append(_render_entry(entry))
    lines.append("")
    lines.append(_render_summary(report))
    return "\n".join(lines)
