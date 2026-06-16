"""Renderers — json / tui / web. Each consumes an ``Aggregate``; adding one = one module.

The renderers are deliberately a flat registry of ``render`` callables keyed by ``--format``
so ``riglib.stats.command`` dispatches by name. The web renderer additionally exposes
``serve`` (it owns a socket) — the command special-cases that one.
"""

from __future__ import annotations

from . import json_out, tui, web

# format name → callable(agg, *, meta, trend) -> str
RENDERERS = {
    "json": json_out.render,
    "tui": tui.render,
    "web": web.build_html,
}

__all__ = ["RENDERERS", "json_out", "tui", "web"]
