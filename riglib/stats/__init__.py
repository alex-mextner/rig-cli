"""``rig stats`` — tool-adoption analytics over agent-harness session logs.

A clean three-stage pipeline, each stage independently extensible:

  sources (one per harness)  →  ToolInvocation stream  →  aggregate (pure)  →  renderers

  * ``riglib.stats.sources``  — pluggable parsers (``@register``); add a harness = add a file.
  * ``riglib.stats.aggregate``— pure reductions into an ``Aggregate`` (counts/breakdowns/trends).
  * ``riglib.stats.render``   — json / tui / web; add an output = add a ``render`` callable.

The umbrella ``riglib.stats.command`` wires them with the user's filters. Everything here is
stdlib-only at import time; ``rich`` (tui) and ``http.server``/``webbrowser`` (web) are
lazy-imported inside their renderers so ``rig --help`` stays fast and dependency-light.
"""

from __future__ import annotations

from .aggregate import Aggregate, aggregate, compare_periods
from .command import build_report, collect, run
from .model import CATEGORIES, ToolInvocation

__all__ = [
    "Aggregate",
    "CATEGORIES",
    "ToolInvocation",
    "aggregate",
    "build_report",
    "collect",
    "compare_periods",
    "run",
]
