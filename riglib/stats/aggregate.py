"""The aggregator: pure reductions over a ``ToolInvocation`` stream → an ``Aggregate``.

No I/O, no harness/render knowledge — just counting. This is the single canonical data
model every renderer (json/tui/web) draws from, so a new renderer is one function that
reads an ``Aggregate`` and never re-touches the logs. Keep it stdlib-only and deterministic
(sorted outputs) so tests can assert exact shapes.

Time buckets: each invocation with a timestamp lands in a day bucket (``YYYY-MM-DD``) and a
week bucket (ISO week, ``YYYY-Www``). Invocations without a timestamp still count toward
every total/breakdown; they simply don't appear in the time series (and are reported as
``undated`` so the number is honest).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .model import CATEGORIES, ToolInvocation


def adoption_ratio(ours: int, baseline: int) -> float:
    """ours / (ours + baseline) — the single definition of the headline adoption number.
    0.0 when there's no baseline. Both ``Aggregate`` and ``TrendComparison`` project through
    this so the metric can never drift between the summary and the period comparison."""
    denom = ours + baseline
    return (ours / denom) if denom else 0.0


@dataclass
class Aggregate:
    total: int = 0
    undated: int = 0  # invocations counted in totals but with no timestamp
    by_category: Counter[str] = field(default_factory=Counter)
    by_tool: Counter[str] = field(default_factory=Counter)
    # tool → category (so a renderer can colour a tool by its bucket)
    tool_category: dict[str, str] = field(default_factory=dict)
    by_repo: Counter[str] = field(default_factory=Counter)
    by_harness: Counter[str] = field(default_factory=Counter)
    # nested: harness → category → count, repo → category → count (the CTO's breakdowns).
    # Annotated as defaultdict (the real runtime type) so readers know missing keys yield an
    # empty Counter, not a KeyError.
    harness_category: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    repo_category: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # category → tool → count (top tools within a bucket)
    category_tools: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # time series: bucket-key → category → count
    by_day: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    by_week: defaultdict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    span_start: datetime | None = None
    span_end: datetime | None = None

    def adoption_ratio(self) -> float:
        """ours / (ours + baseline) — the headline adoption number. 0.0 when no baseline."""
        return adoption_ratio(self.by_category.get("ours", 0), self.by_category.get("baseline", 0))


def aggregate(invocations: Iterable[ToolInvocation]) -> Aggregate:
    """Reduce a stream of invocations into a single ``Aggregate``. Pure."""
    agg = Aggregate()
    for inv in invocations:
        agg.total += 1
        agg.by_category[inv.category] += 1
        agg.by_tool[inv.tool_name] += 1
        agg.tool_category.setdefault(inv.tool_name, inv.category)
        agg.by_repo[inv.repo] += 1
        agg.by_harness[inv.harness] += 1
        agg.harness_category[inv.harness][inv.category] += 1
        agg.repo_category[inv.repo][inv.category] += 1
        agg.category_tools[inv.category][inv.tool_name] += 1
        ts = inv.timestamp
        if ts is None:
            agg.undated += 1
            continue
        agg.by_day[ts.strftime("%Y-%m-%d")][inv.category] += 1
        iso = ts.isocalendar()
        agg.by_week[f"{iso[0]}-W{iso[1]:02d}"][inv.category] += 1
        if agg.span_start is None or ts < agg.span_start:
            agg.span_start = ts
        if agg.span_end is None or ts > agg.span_end:
            agg.span_end = ts
    return agg


@dataclass
class TrendComparison:
    """Period-over-period adoption change, for ``--since``-style questions."""

    earlier_label: str
    later_label: str
    earlier: Counter[str]
    later: Counter[str]

    def delta(self, category: str) -> int:
        return self.later.get(category, 0) - self.earlier.get(category, 0)

    def adoption_delta(self) -> float:
        def ratio(c: Counter[str]) -> float:
            return adoption_ratio(c.get("ours", 0), c.get("baseline", 0))

        return ratio(self.later) - ratio(self.earlier)


def compare_periods(agg: Aggregate, split: datetime) -> TrendComparison:
    """Split the day-series at ``split`` (UTC) into before/after and diff the category mix.

    Lets the CTO ask "did adoption move after I shipped X?" — everything strictly before
    ``split`` is the baseline period, everything on/after is the comparison period.
    """
    pivot = split.strftime("%Y-%m-%d")
    earlier: Counter[str] = Counter()
    later: Counter[str] = Counter()
    for day, cats in agg.by_day.items():
        target = earlier if day < pivot else later
        for cat in CATEGORIES:
            if cats.get(cat):
                target[cat] += cats[cat]
    return TrendComparison(
        earlier_label=f"< {pivot}",
        later_label=f">= {pivot}",
        earlier=earlier,
        later=later,
    )
