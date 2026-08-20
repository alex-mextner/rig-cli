"""Fetch merged PRs via ``gh pr list`` — the SOURCE OF TRUTH for "did this ship".

Deliberately does NOT use ``gh pr list --search "merged:>=..."``: search goes through
GitHub's search index (lagged, and ``--state`` gets folded into the query string where it
can silently conflict with other terms). Instead this fetches a generous page of merged
PRs (``--state merged``, plain REST-backed listing) and filters by ``mergedAt`` client-side
— the same shape pm-cli's own ``adapters/github.py`` uses for reading PR facts via ``gh``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

from .model import MergedPR
from .timeutil import parse_utc

# gh pr list defaults to --limit 30 and sorts by CREATED, not merged — both would silently
# truncate a busy window. A page this size comfortably covers a day (or several) of merges
# for these repos; _fetch_one warns on stderr if it looks like the page was too small.
_PAGE_LIMIT = 300
_SUBPROCESS_TIMEOUT_SECONDS = 30


class GhError(RuntimeError):
    """``gh`` is missing, unauthenticated, or the repo/query failed."""


@dataclass(frozen=True)
class FetchResult:
    """One repo's fetch outcome: the in-window PRs, plus whether the page might be
    missing some (see :func:`fetch_merged_prs`). The caller (``command.py``) uses
    ``complete`` to decide whether it is safe to advance the saved watermark — reporting
    from an incomplete page is fine (better than reporting nothing), but PERSISTING a
    watermark derived from an incomplete page would permanently hide whatever the page
    missed, since the exclusive since-filter never looks at anything before it again."""

    prs: list[MergedPR]
    complete: bool


def fetch_merged_prs(repo: str, since_utc) -> FetchResult:
    """Every PR in ``repo`` merged STRICTLY AFTER ``since_utc`` (an aware UTC datetime).

    Exclusive, not inclusive — load-bearing for the no-double-report contract: the saved
    watermark is set to the ``mergedAt`` of the last PR actually reported, so an inclusive
    ``>=`` would re-report that exact PR on every subsequent run forever (caught by
    running `rig daily` twice in a row during testing; the second run must report
    nothing new).

    Raises :class:`GhError` when ``gh`` itself can't be run or the repo is unreachable —
    the caller decides whether that's fatal for the whole report or just this one repo.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "list",
                "-R", repo,
                "--state", "merged",
                "--limit", str(_PAGE_LIMIT),
                "--json", "number,title,body,mergedAt,url,labels",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise GhError("`gh` CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"`gh pr list -R {repo}` timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s") from exc

    if proc.returncode != 0:
        raise GhError(f"`gh pr list -R {repo}` failed: {proc.stderr.strip() or 'unknown error'}")

    try:
        raw = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise GhError(f"`gh pr list -R {repo}` returned unparseable JSON") from exc
    if not isinstance(raw, list):
        # `--json` always emits a JSON array for `gh pr list`; a non-list root (e.g. an
        # error object from a `gh` version/flag mismatch) would otherwise crash
        # `raw[-1]` in `_page_may_be_truncated` with an uncaught `KeyError`/`TypeError`,
        # escaping `command.py`'s per-repo `except GhError` and aborting every other
        # repo's report too (codex review P1 finding, round 5).
        raise GhError(f"`gh pr list -R {repo}` returned a non-list JSON root ({type(raw).__name__})")

    prs, any_skipped = _parse_and_filter(repo, raw, since_utc)
    truncated = _page_may_be_truncated(raw, since_utc)
    if truncated:
        print(
            f"daily: warning — {repo} returned the full {_PAGE_LIMIT}-PR page (sorted by "
            "creation date) and the OLDEST entry on that page was still merged inside the "
            "requested window — an even-older-created PR merged in-window may be missing. "
            "Narrow --since or raise the page size. The saved watermark will NOT advance "
            "for this run so nothing is silently lost.",
            file=sys.stderr,
        )
    # A skipped malformed record COULD have belonged in the window — it must be treated
    # the same as truncation: mark the fetch incomplete so the caller withholds the
    # watermark advance, or the omission becomes permanent (codex review P1 finding,
    # round 4: skipping-but-still-`complete=True` silently lost the skipped PR forever).
    return FetchResult(prs=prs, complete=not (truncated or any_skipped))


def _page_may_be_truncated(raw: list, since_utc) -> bool:
    """True only when the page hit its limit AND the oldest (last, since gh sorts by
    creation date descending) entry on that page is still inside the window — i.e. there
    could be an even older-created PR, merged in-window, that never made it onto this
    page. A page that hit the limit but has already aged PAST the window on its last
    entry is NOT truncated for this query — flagging it anyway would fire on nearly every
    call against a repo with >``_PAGE_LIMIT`` total merged PRs, which is not a useful
    warning. Any exception reading the last entry (not a dict, garbage `mergedAt`) is
    treated the SAME as a missing `mergedAt` — can't prove otherwise, warn conservatively
    (codex review P2 finding, round 4: this used to let a malformed last item's exception
    escape uncaught, past `command.py`'s per-repo `except GhError`)."""
    if len(raw) < _PAGE_LIMIT:
        return False
    try:
        merged_at = raw[-1].get("mergedAt")
        if not merged_at:
            return True
        return parse_utc(str(merged_at)) >= since_utc
    except (TypeError, ValueError, AttributeError):
        return True


def _parse_and_filter(repo: str, raw: list, since_utc) -> tuple[list[MergedPR], bool]:
    """Parse every item, keep the ones merged after ``since_utc``. A single malformed
    record (bad ``number``, unparseable ``mergedAt``, not even a dict) is WARNED and
    skipped rather than crashing the whole fetch — one bad remote record must not take
    down every other repo's report along with it (codex review P2 finding, round 3: this
    used to let a stray ``ValueError``/``KeyError``/``TypeError`` escape
    ``fetch_merged_prs`` entirely, past the ``except GhError`` in ``command.py``'s
    per-repo loop). Returns ``(prs, any_skipped)`` — the caller treats ``any_skipped``
    as incomplete (see :func:`fetch_merged_prs`)."""
    prs: list[MergedPR] = []
    any_skipped = False
    for item in raw:
        try:
            pr = _parse_one(repo, item)
            if pr is not None and parse_utc(pr.merged_at) > since_utc:
                prs.append(pr)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            any_skipped = True
            print(f"daily: warning — {repo} returned a malformed PR record, skipping it: {exc}", file=sys.stderr)
    return prs, any_skipped


def _parse_one(repo: str, item: dict) -> MergedPR | None:
    merged_at = item.get("mergedAt")
    if not merged_at:
        return None  # defensive: --state merged should guarantee this, never trust blindly
    labels = [lbl.get("name", "") for lbl in (item.get("labels") or []) if isinstance(lbl, dict)]
    return MergedPR(
        repo=repo,
        number=int(item["number"]),
        title=str(item.get("title", "")),
        body=str(item.get("body") or ""),
        merged_at=str(merged_at),
        url=str(item.get("url", "")),
        labels=[l for l in labels if l],
    )
