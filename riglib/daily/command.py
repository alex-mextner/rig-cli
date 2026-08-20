"""``rig daily`` orchestration: resolve each repo's window, fetch, render, advance
per-repo watermarks.

Wired from ``riglib/cli.py`` as ``cmd_daily``; :func:`run` takes the parsed
``argparse.Namespace`` and returns a process exit code (0 success, 1 when every
configured repo failed to fetch; ``rig``'s own ``errors.guard`` catches anything
unexpected as an internal error).

Known, accepted limitations (tracked as rig-cli#281, not re-litigated here):

1. ``gh pr list`` sorts by CREATION date, not merge date, so a PR created long ago but
   merged only just now can in theory fall past the fetch page and be missed with no
   warning (the truncation heuristic in ``github.py`` only catches the common case:
   hitting the page limit while still inside the window by CREATION order).
2. The watermark is second-granularity and the since-filter is strictly exclusive
   (``>``), so two PRs that merge in the exact same UTC second — where one is somehow
   absent from an otherwise-successful, non-truncated fetch (e.g. a `gh`/GitHub API
   race) — could see the missing one permanently skipped on the next run.

For this tool's actual scale — two repos, single-digit merges/day, a 300-item page —
both are real gaps but low-probability ones; a full fix needs either a merge-time-sorted
query GitHub's ``gh pr list`` does not offer today, or a persisted tie-break cursor
(timestamp + PR id) instead of a bare timestamp.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..errors import ConfigError
from .config import load_repos
from .format_report import render_report
from .github import GhError, fetch_merged_prs
from .model import MergedPR
from .state import load_watermarks, save_watermarks
from .timeutil import now_utc, parse_utc, to_utc_iso

DEFAULT_LOOKBACK_HOURS = 24
_RELATIVE_RE = re.compile(r"^(\d+)([hd])$", re.I)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        metavar="TIMESTAMP",
        help="report PRs merged strictly AFTER this point (exclusive — the same "
        "boundary the saved watermark itself uses, so a re-run never double-reports "
        "the boundary PR): an ISO-8601 timestamp (e.g. 2026-08-19T00:00:00Z) or a "
        "relative window (24h, 7d). Read-only — does NOT advance the saved watermark "
        "(default: each repo's own saved watermark, or the last "
        f"{DEFAULT_LOOKBACK_HOURS}h on that repo's first run).",
    )
    parser.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        help="repo to report on (repeatable). Default: the configured list "
        "(~/.config/rig/daily.yaml `repos:`), else the built-in hyperide repos.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="daily.yaml path (default: ~/.config/rig/daily.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report but do not advance the saved watermark",
    )
    parser.add_argument("--state", metavar="PATH", help=argparse.SUPPRESS)  # test seam


def run(args: argparse.Namespace) -> int:
    explicit_since_dt = _parse_since_arg(args.since) if args.since else None
    state_path = _state_path(args)
    saved_watermarks = {} if explicit_since_dt is not None else load_watermarks(state_path)

    repos = load_repos(
        config_path=Path(args.config).expanduser() if args.config else None,
        cli_repos=args.repo,
    )

    # Snapshot "now" ONCE for the whole run: a repo whose fetch comes back complete but
    # with zero PRs is safe to advance to this instant (nothing merged between its old
    # watermark and here) — otherwise a quiet run leaves NO cursor at all, and a delayed
    # next run (>24h later) falls back to the rolling default lookback and can miss
    # whatever merged in between (codex review P2, round 3).
    run_now = now_utc()
    outcome = _fetch_all(repos, explicit_since_dt, saved_watermarks, run_now)

    if repos and outcome.failed_repos and not outcome.succeeded_repos:
        print(
            "daily: no report — every configured repo failed to fetch merged PRs "
            "(see warnings above). Not a real answer, don't paste it as one.",
            file=sys.stderr,
        )
        return 1

    print(render_report(outcome.prs))

    if explicit_since_dt is None and not args.dry_run and outcome.updated_watermarks:
        save_watermarks({**saved_watermarks, **outcome.updated_watermarks}, path=state_path)
    return 0


@dataclass
class _FetchAllResult:
    prs: list[MergedPR] = field(default_factory=list)
    updated_watermarks: dict[str, str] = field(default_factory=dict)
    succeeded_repos: int = 0
    failed_repos: int = 0


def _fetch_all(
    repos: list[str],
    explicit_since_dt: datetime | None,
    saved_watermarks: dict[str, str],
    run_now: datetime,
) -> _FetchAllResult:
    """Fetch every repo's merged PRs. Each repo uses its OWN saved watermark (or the
    default lookback on its first run) unless ``--since`` was given, in which case every
    repo uses that same explicit window. A repo's watermark only advances when THAT
    repo's fetch both succeeded and returned a complete (non-truncated) page — a failed
    or truncated repo keeps its old watermark so a future run re-checks the same ground
    instead of silently losing whatever this run couldn't see. Found PRs advance the
    watermark to their max ``mergedAt`` (the documented mid-run-safety contract); a
    complete fetch with ZERO PRs still advances to ``run_now`` — proven clean, since the
    fetch covered everything up to "now" and found nothing."""
    outcome = _FetchAllResult()
    for repo in repos:
        since = explicit_since_dt or _since_for_repo(repo, saved_watermarks)
        try:
            result = fetch_merged_prs(repo, since)
        except GhError as exc:
            print(f"daily: warning — skipping {repo}: {exc}", file=sys.stderr)
            outcome.failed_repos += 1
            continue
        outcome.succeeded_repos += 1
        outcome.prs.extend(result.prs)
        if result.complete:
            if result.prs:
                newest = max(parse_utc(pr.merged_at) for pr in result.prs)
                outcome.updated_watermarks[repo] = to_utc_iso(newest)
            else:
                outcome.updated_watermarks[repo] = to_utc_iso(run_now)
    return outcome


def _since_for_repo(repo: str, saved_watermarks: dict[str, str]) -> datetime:
    saved = saved_watermarks.get(repo)
    if saved:
        try:
            return parse_utc(saved)
        except ValueError:
            # A hand-edited or corrupted state entry must degrade to "no watermark for
            # this repo", same as a missing one (state.py's own contract) — never crash
            # the report over a malformed timestamp.
            print(
                f"daily: warning — saved watermark for {repo} ({saved!r}) is not a "
                f"valid timestamp; falling back to the last {DEFAULT_LOOKBACK_HOURS}h",
                file=sys.stderr,
            )
    return now_utc() - timedelta(hours=DEFAULT_LOOKBACK_HOURS)


def _state_path(args: argparse.Namespace) -> Path | None:
    return Path(args.state).expanduser() if getattr(args, "state", None) else None


def _parse_since_arg(raw: str) -> datetime:
    """A relative window (``24h``/``7d``) or an ISO-8601 timestamp. Raises a structured
    :class:`ConfigError` (exit 2, rendered by ``rig``'s own ``errors.guard``) on garbage
    input, rather than letting a raw ``ValueError`` from ``parse_utc`` escape as an
    unhandled crash (codex review P2 finding, round 4)."""
    m = _RELATIVE_RE.match(raw.strip())
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        return now_utc() - delta
    try:
        return parse_utc(raw)
    except ValueError as exc:
        raise ConfigError(
            what=f"--since value {raw!r} is not a valid timestamp or relative window",
            why=str(exc),
            fix="use a relative window (24h, 7d) or an ISO-8601 UTC timestamp, "
            "e.g. --since 2026-08-18T00:00:00Z",
        ) from exc
