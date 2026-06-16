"""Log-source parsers, one per harness. Importing this package self-registers them all.

Each concrete source uses ``@register`` (in ``.base``) so the dispatch in
``riglib.stats.command`` can iterate ``all_sources()`` without naming a harness. Adding a
harness is TRULY one file: drop ``riglib/stats/sources/<name>.py`` with a
``@register``-decorated ``LogSource`` subclass — the auto-discovery below imports every
sibling module (running its decorator), so there's no central list to edit.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import LogSource, all_sources, register, source_names


def _discover() -> None:
    """Import every concrete parser module so its ``@register`` decorator runs. Auto-discovery
    keeps the "add a harness = add a file" contract honest — no manual import list to forget.
    Sorted for a deterministic registration order (= output order)."""
    for mod in sorted(m.name for m in pkgutil.iter_modules(__path__)):
        if mod in {"base"} or mod.startswith("_"):
            continue  # base = the ABC/registry; _shellutil etc. = private helpers, not parsers
        importlib.import_module(f"{__name__}.{mod}")


_discover()

__all__ = ["LogSource", "all_sources", "register", "source_names"]
