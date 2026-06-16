"""JSON renderer — the canonical, machine-readable shape every other renderer derives from.

``to_dict`` is the contract: tui/web both consume the SAME nested dict (via the Aggregate),
and tests assert against this dict, so it is the stable surface. Keep keys stable; add, do
not rename.
"""

from __future__ import annotations

import json
from datetime import datetime

from ..aggregate import Aggregate, TrendComparison
from ..model import CATEGORIES


def to_dict(
    agg: Aggregate,
    *,
    meta: dict | None = None,
    trend: TrendComparison | None = None,
) -> dict:
    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    out: dict = {
        "meta": meta or {},
        "summary": {
            "total": agg.total,
            "undated": agg.undated,
            "adoption_ratio": round(agg.adoption_ratio(), 4),
            "span_start": _iso(agg.span_start),
            "span_end": _iso(agg.span_end),
        },
        # all "by X" maps are sorted COUNT-DESCENDING (it's a "top N" report; the biggest
        # users come first, uniformly — harness, repo, and their category breakdowns).
        "by_category": {c: agg.by_category.get(c, 0) for c in CATEGORIES},
        "by_tool": _counter_sorted(agg.by_tool, agg.tool_category),
        "by_harness": dict(agg.by_harness.most_common()),
        "by_repo": dict(agg.by_repo.most_common()),
        "harness_category": {
            h: {c: cats.get(c, 0) for c in CATEGORIES}
            for h, cats in sorted(agg.harness_category.items(), key=lambda kv: -sum(kv[1].values()))
        },
        "repo_category": {
            r: {c: cats.get(c, 0) for c in CATEGORIES}
            for r, cats in sorted(agg.repo_category.items(), key=lambda kv: -sum(kv[1].values()))
        },
        "category_tools": {c: dict(agg.category_tools[c].most_common()) for c in CATEGORIES},
        "trends": {
            "by_day": {d: {c: cats.get(c, 0) for c in CATEGORIES} for d, cats in sorted(agg.by_day.items())},
            "by_week": {w: {c: cats.get(c, 0) for c in CATEGORIES} for w, cats in sorted(agg.by_week.items())},
        },
    }
    if trend is not None:
        out["comparison"] = {
            "earlier_label": trend.earlier_label,
            "later_label": trend.later_label,
            "earlier": {c: trend.earlier.get(c, 0) for c in CATEGORIES},
            "later": {c: trend.later.get(c, 0) for c in CATEGORIES},
            "adoption_delta": round(trend.adoption_delta(), 4),
        }
    return out


def _counter_sorted(counter, tool_category: dict[str, str]) -> list[dict]:
    """by_tool as a sorted list of {tool, count, category} — a list keeps display order."""
    return [
        {"tool": tool, "count": count, "category": tool_category.get(tool, "other")}
        for tool, count in counter.most_common()
    ]


def render(agg: Aggregate, *, meta: dict | None = None, trend: TrendComparison | None = None) -> str:
    return json.dumps(to_dict(agg, meta=meta, trend=trend), indent=2)
