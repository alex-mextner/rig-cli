"""The ``rig stats show`` engine: collect → filter → aggregate → render.

This is the orchestration seam the CLI front-end (``riglib.cli``) calls. It owns NO parsing
and NO rendering logic — it wires the pluggable sources to the pure aggregator to the
chosen renderer, applying the user's filters (harness/repo/since/until) in between. New
source, new metric, new renderer: none of them touch this file beyond the registries they
already live in.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from .aggregate import Aggregate, TrendComparison, aggregate, compare_periods
from .model import ToolInvocation
from .render import RENDERERS
from .sources import all_sources


class StatsError(Exception):
    """User-facing argument/usage error (bad date, unknown harness/format)."""


class CollectResult(NamedTuple):
    """What :func:`collect` returns — named so the three-tuple contract is legible."""

    invocations: list[ToolInvocation]
    supported: list[str]  # harnesses whose logs were actually found (data-driven list)
    notes: list[str]  # "not found / unknown format" lines for the rest


class Report(NamedTuple):
    """What :func:`build_report` returns — the renderer-agnostic core's output."""

    aggregate: Aggregate
    meta: dict
    trend: TrendComparison | None


def parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    """``YYYY-MM-DD`` → aware UTC datetime. ``end=True`` snaps to end-of-day (inclusive)."""
    if not value:
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise StatsError(f"invalid date {value!r} (expected YYYY-MM-DD)") from exc
    if end:
        d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
    return d.replace(tzinfo=timezone.utc)


def _norm_repo(path: str) -> str:
    """Normalize a repo/cwd path for equality matching: expand ``~`` and strip a trailing
    slash. We deliberately do NOT ``resolve()`` symlinks — the logs record the cwd the agent
    actually ran in (often a worktree symlink), and resolving could merge distinct worktrees."""
    p = path.strip()
    if p.startswith("~"):
        p = str(Path(p).expanduser())
    return p.rstrip("/") or "/"


def collect(
    *,
    home: Path | None = None,
    harnesses: list[str] | None = None,
    repos: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> CollectResult:
    """Run the selected sources and apply filters.

    ``supported`` is the data-driven list of harnesses whose logs were actually found;
    ``notes`` carries "not found / unknown format" lines for the rest. ``--repo`` values are
    normalized (``~`` expansion, trailing-slash) on both sides so the match isn't brittle.
    """
    sources = all_sources(home=home)
    wanted = set(harnesses) if harnesses else None
    repo_set = frozenset(_norm_repo(r) for r in repos) if repos else None

    invocations: list[ToolInvocation] = []
    supported: list[str] = []
    notes: list[str] = []

    for src in sources:
        if wanted is not None and src.name not in wanted:
            continue
        if not src.available():
            notes.append(src.not_found_note())
            continue
        supported.append(src.name)
        # A parser must never take down the whole command: available() only checks the root
        # EXISTS, but it could be a file, an unreadable dir, or otherwise raise mid-iteration
        # (iterdir/glob on a non-dir, a permission error). Isolate each source — on failure,
        # note it and keep going so the other harnesses still report.
        try:
            for inv in src.iter_invocations(repos=None):
                if not _passes(inv, repo_set, since, until):
                    continue
                invocations.append(inv)
        except OSError as exc:
            notes.append(f"{src.name}: could not read logs ({exc.__class__.__name__})")

    if wanted is not None:
        unknown = wanted - {s.name for s in sources}
        for name in sorted(unknown):
            notes.append(f"{name}: unknown harness (no registered parser)")
    return CollectResult(invocations, supported, notes)


def _passes(
    inv: ToolInvocation,
    repos: frozenset[str] | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if repos is not None and _norm_repo(inv.repo) not in repos:
        return False
    if since is not None or until is not None:
        ts = inv.timestamp
        if ts is None:
            return False  # a date filter is explicit intent → drop undated rows
        if since is not None and ts < since:
            return False
        if until is not None and ts > until:
            return False
    return True


def build_report(
    *,
    home: Path | None = None,
    harnesses: list[str] | None = None,
    repos: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    compare_at: datetime | None = None,
    categories: frozenset[str] | None = None,
) -> Report:
    """Collect + aggregate into a :class:`Report`. The renderer-agnostic core.

    ``categories`` (if given) restricts the aggregated stream to those buckets — e.g.
    ``{"baseline", "ours"}`` for the ``--baseline`` adoption-focused view, dropping the
    external/other noise so the headline ratio is the whole picture.

    Period comparison: an explicit ``compare_at`` splits the SELECTED window before/after
    that date. ``--since`` instead compares the selected ``[since, until]`` window against
    the equally-long window IMMEDIATELY BEFORE it — collecting that prior window separately,
    because the main stream is filtered to ``>= since`` and would otherwise have an empty
    "earlier" half.
    """
    result = collect(home=home, harnesses=harnesses, repos=repos, since=since, until=until)
    invocations = result.invocations
    if categories is not None:
        invocations = [inv for inv in invocations if inv.category in categories]
    agg = aggregate(invocations)

    trend: TrendComparison | None = None
    if compare_at is not None and agg.by_day:
        # explicit pivot inside the selected window → simple before/after split.
        trend = compare_periods(agg, compare_at)
    elif since is not None:
        trend = _since_vs_prior_window(
            home=home, harnesses=harnesses, repos=repos, since=since, until=until,
            categories=categories, later_invocations=result.invocations,
        )

    meta = {
        "supported_harnesses": result.supported,
        "filters": {
            "harnesses": harnesses or "all",
            "repos": repos or "all",
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "notes": result.notes,
    }
    if result.notes:
        meta["note"] = "; ".join(result.notes)
    return Report(agg, meta, trend)


def _since_vs_prior_window(
    *,
    home: Path | None,
    harnesses: list[str] | None,
    repos: list[str] | None,
    since: datetime,
    until: datetime | None,
    categories: frozenset[str] | None,
    later_invocations: list[ToolInvocation],
) -> TrendComparison | None:
    """Compare the selected ``[since, until]`` window against the equally-long window that
    ends just before ``since``. The "later" half reuses the already-collected window stream
    (no third read of the logs); only the prior window is collected fresh.

    The prior window is ``[prior_start, since)`` — its upper bound is EXCLUSIVE of ``since``
    (``prior_end`` is one microsecond before), so an invocation landing exactly at ``since``
    is counted only in the later window, never both. Returns None when there's nothing to
    compare."""
    window_end = until or datetime.now(tz=timezone.utc)
    length = window_end - since
    if length.total_seconds() <= 0:
        return None
    prior_end = since - timedelta(microseconds=1)  # exclusive of `since` → no double-count
    prior_start = since - length
    earlier_result = collect(
        home=home, harnesses=harnesses, repos=repos, since=prior_start, until=prior_end
    )

    def _mix(invs: list[ToolInvocation]) -> Counter:
        c: Counter = Counter()
        for inv in invs:
            if categories is None or inv.category in categories:
                c[inv.category] += 1
        return c

    earlier, later = _mix(earlier_result.invocations), _mix(later_invocations)
    if not earlier and not later:
        return None
    return TrendComparison(
        earlier_label=f"{prior_start.date()}..{since.date()}",
        later_label=f"{since.date()}..{window_end.date()}",
        earlier=earlier,
        later=later,
    )


def run(args) -> int:
    """CLI entry for ``rig stats show``. ``args`` is the argparse namespace from cli.py."""
    fmt = getattr(args, "format", None) or "tui"
    if fmt not in {"json", "tui", "web"}:
        print(f"error: unknown --format {fmt!r} (json|tui|web)")
        return 2
    # A typo like `--harness codx` must NOT silently produce a valid zero-count report (it
    # reads like real data to a script). Validate against the registered parsers up front.
    harnesses = getattr(args, "harness", None)
    if harnesses:
        from .sources import source_names

        known = set(source_names())
        unknown = [h for h in harnesses if h not in known]
        if unknown:
            print(
                f"error: unknown --harness {', '.join(repr(u) for u in unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
            return 2
    try:
        since = parse_date(getattr(args, "since", None))
        until = parse_date(getattr(args, "until", None), end=True)
    except StatsError as exc:
        print(f"error: {exc}")
        return 2
    # A transposed range (--since after --until) would otherwise exit 0 with an empty report
    # that reads like real zero usage. Reject it on the same usage-error path as a bad date.
    if since is not None and until is not None and since > until:
        print(
            f"error: --since ({since.date()}) is after --until ({until.date()}); "
            "the date range is empty"
        )
        return 2

    home = Path(args.home) if getattr(args, "home", None) else None
    # --baseline narrows the report to the two buckets the adoption question is about
    # (built-in baseline vs our ecosystem), dropping external-advertised/other noise.
    categories = frozenset({"baseline", "ours"}) if getattr(args, "baseline", False) else None
    agg, meta, trend = build_report(
        home=home,
        harnesses=harnesses,
        repos=getattr(args, "repo", None),
        since=since,
        until=until,
        categories=categories,
    )
    if categories is not None:
        meta["note"] = (meta.get("note", "") + " [baseline focus: baseline vs ours only]").strip()

    # No harness logs on this machine: json/web still emit a valid (empty) document — the
    # README promises json is "machine-readable data", and a script on a fresh box must get
    # parseable output, not a prose line. Only the human-facing tui short-circuits to a hint.
    if not meta["supported_harnesses"] and fmt == "tui":
        print("rig stats: no harness logs found on this machine.")
        for n in meta.get("notes", []):
            print(f"  - {n}")
        return 0

    if fmt == "web":
        from .render import web

        port = getattr(args, "web_port", None) or 0
        web.serve(agg, meta=meta, trend=trend, port=int(port))
        return 0

    print(RENDERERS[fmt](agg, meta=meta, trend=trend))
    return 0
