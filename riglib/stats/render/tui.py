"""TUI renderer — a rich terminal report (summary table + bar charts + breakdowns).

``rich`` is lazy-imported INSIDE ``render`` so importing this module (and the whole stats
package) stays stdlib-only and fast. If rich is absent we degrade to a plain-text report
that conveys the same numbers — the command never hard-fails for want of an optional dep.
"""

from __future__ import annotations

from ..aggregate import Aggregate, TrendComparison, adoption_ratio
from ..model import CATEGORIES
from ._util import shorten

# fixed colours per category so bars/tables read consistently. These are RICH colour names;
# the web renderer keeps the SAME four categories in hex (web._CAT_COLOR). The two tables are
# intentionally separate (one terminal palette, one CSS palette) — keep them in sync by
# meaning, not value.
_CAT_COLOR = {
    "baseline": "grey70",
    "ours": "green",
    "external-advertised": "yellow",
    "other": "grey42",
}


def render(agg: Aggregate, *, meta: dict | None = None, trend: TrendComparison | None = None) -> str:
    try:
        from rich.console import Console
    except ImportError:
        return render_plain(agg, meta=meta, trend=trend)

    import io
    import shutil

    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table

    width = min(shutil.get_terminal_size((100, 40)).columns, 120)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=width)

    console.print(_header_panel(agg, meta, Panel, Table))
    console.print(_category_bars(agg, Table))
    console.print(_top_tools_table(agg, Table))
    console.print(_breakdown_panel(agg, "By harness", agg.harness_category, Table, Group, Panel))
    console.print(_breakdown_panel(agg, "By repo", _top_repo_category(agg), Table, Group, Panel))
    console.print(_trend_table(agg, Table))
    if trend is not None:
        console.print(_comparison_table(trend, Table))
    return buf.getvalue()


def _header_panel(agg, meta, Panel, Table):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="bold")
    grid.add_column()
    grid.add_row("Total tool calls", f"[bold]{agg.total}[/bold]")
    grid.add_row("Adoption (ours / ours+baseline)", f"[green bold]{agg.adoption_ratio():.1%}[/green bold]")
    if agg.span_start and agg.span_end:
        grid.add_row("Span", f"{agg.span_start.date()} → {agg.span_end.date()}")
    if agg.undated:
        grid.add_row("Undated calls", f"[yellow]{agg.undated}[/yellow] (in totals, not in trends)")
    harnesses = ", ".join(f"{h}={c}" for h, c in sorted(agg.by_harness.items())) or "(none)"
    grid.add_row("Harnesses", harnesses)
    note = (meta or {}).get("note")
    if note:
        grid.add_row("Note", f"[grey70]{note}[/grey70]")
    return Panel(grid, title="[bold]rig stats — tool adoption[/bold]", border_style="cyan")


def _bar(count: int, total: int, width: int = 30, color: str = "white") -> str:
    if total <= 0:
        return ""
    filled = round(width * count / total)
    return f"[{color}]" + "█" * filled + "[/]" + "[grey30]" + "░" * (width - filled) + "[/]"


def _category_bars(agg, Table):
    table = Table(title="Categories", title_style="bold", expand=False)
    table.add_column("category")
    table.add_column("count", justify="right")
    table.add_column("share", justify="right")
    table.add_column("")
    total = agg.total or 1
    for cat in CATEGORIES:
        cnt = agg.by_category.get(cat, 0)
        color = _CAT_COLOR.get(cat, "white")
        table.add_row(f"[{color}]{cat}[/]", str(cnt), f"{cnt / total:.0%}", _bar(cnt, total, color=color))
    return table


def _top_tools_table(agg, Table, limit: int = 15):
    table = Table(title=f"Top {limit} tools", title_style="bold")
    table.add_column("tool")
    table.add_column("category")
    table.add_column("count", justify="right")
    table.add_column("")
    top = agg.by_tool.most_common(limit)
    maxc = top[0][1] if top else 1
    for tool, cnt in top:
        cat = agg.tool_category.get(tool, "other")
        color = _CAT_COLOR.get(cat, "white")
        table.add_row(tool, f"[{color}]{cat}[/]", str(cnt), _bar(cnt, maxc, width=22, color=color))
    return table


def _breakdown_panel(agg, title, mapping, Table, Group, Panel):
    rows = []
    for key, cats in sorted(mapping.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(cats.values()) or 1
        seg = "  ".join(
            f"[{_CAT_COLOR.get(c, 'white')}]{c[:4]}={cats.get(c, 0)}[/]" for c in CATEGORIES if cats.get(c)
        )
        rows.append(f"[bold]{shorten(key, 48)}[/bold]  ({total})  {seg}")
    body = Group(*rows) if rows else "(no data)"
    return Panel(body, title=f"[bold]{title}[/bold]", border_style="grey50")


def _top_repo_category(agg, limit: int = 8):
    items = sorted(agg.repo_category.items(), key=lambda kv: -sum(kv[1].values()))[:limit]
    return dict(items)


def _trend_table(agg, Table, limit: int = 14):
    if not agg.by_day:
        return Table(title="Trend (no timestamps)")
    table = Table(title=f"Daily trend (last {limit} days with activity)", title_style="bold")
    table.add_column("day")
    for cat in CATEGORIES:
        table.add_column(cat[:4], justify="right", style=_CAT_COLOR.get(cat, "white"))
    table.add_column("adoption", justify="right")
    for day in sorted(agg.by_day)[-limit:]:
        cats = agg.by_day[day]
        ours, base = cats.get("ours", 0), cats.get("baseline", 0)
        ratio = f"{adoption_ratio(ours, base):.0%}" if (ours + base) else "—"
        table.add_row(day, *[str(cats.get(c, 0)) for c in CATEGORIES], ratio)
    return table


def _comparison_table(trend: TrendComparison, Table):
    table = Table(title="Period comparison", title_style="bold")
    table.add_column("category")
    table.add_column(trend.earlier_label, justify="right")
    table.add_column(trend.later_label, justify="right")
    table.add_column("Δ", justify="right")
    for cat in CATEGORIES:
        early, late = trend.earlier.get(cat, 0), trend.later.get(cat, 0)
        d = late - early
        sign = "+" if d > 0 else ""
        table.add_row(f"[{_CAT_COLOR.get(cat, 'white')}]{cat}[/]", str(early), str(late), f"{sign}{d}")
    table.add_row(
        "[bold]adoption[/bold]", "", "", f"{trend.adoption_delta():+.1%}", style="bold"
    )
    return table


# ── plain-text fallback (rich absent) ────────────────────────────────────────────────────
def render_plain(agg: Aggregate, *, meta: dict | None = None, trend: TrendComparison | None = None) -> str:
    lines = ["rig stats — tool adoption", "=" * 40]
    lines.append(f"total tool calls : {agg.total}")
    lines.append(f"adoption ratio   : {agg.adoption_ratio():.1%}  (ours / ours+baseline)")
    if agg.span_start and agg.span_end:
        lines.append(f"span             : {agg.span_start.date()} -> {agg.span_end.date()}")
    if agg.undated:
        lines.append(f"undated calls    : {agg.undated} (counted in totals, not in trends)")
    note = (meta or {}).get("note")
    if note:
        lines.append(f"note             : {note}")
    lines.append("\nby category:")
    total = agg.total or 1
    for cat in CATEGORIES:
        cnt = agg.by_category.get(cat, 0)
        bar = "#" * round(30 * cnt / total)
        lines.append(f"  {cat:<20} {cnt:>6}  {cnt / total:>4.0%}  {bar}")
    lines.append("\ntop tools:")
    for tool, cnt in agg.by_tool.most_common(15):
        lines.append(f"  {tool:<32} {cnt:>6}  [{agg.tool_category.get(tool, 'other')}]")
    lines.append("\nby harness:")
    for h, c in agg.by_harness.most_common():
        lines.append(f"  {h:<16} {c:>6}")
    lines.append("\nby repo:")
    for r, c in agg.by_repo.most_common(12):
        lines.append(f"  {shorten(r, 40):<40} {c:>6}")
    if agg.by_day:
        lines.append("\ndaily trend:")
        for day in sorted(agg.by_day)[-14:]:
            cats = agg.by_day[day]
            seg = " ".join(f"{c[:4]}={cats.get(c, 0)}" for c in CATEGORIES if cats.get(c))
            lines.append(f"  {day}  {seg}")
    if trend is not None:
        lines.append("\nperiod comparison:")
        for cat in CATEGORIES:
            early, late = trend.earlier.get(cat, 0), trend.later.get(cat, 0)
            lines.append(
                f"  {cat:<20} {trend.earlier_label}={early:<5} {trend.later_label}={late:<5} "
                f"delta={late - early:+d}"
            )
        lines.append(f"  adoption delta: {trend.adoption_delta():+.1%}")
    return "\n".join(lines)
