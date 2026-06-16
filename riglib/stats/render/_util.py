"""Tiny shared helpers for the renderers (kept DRY across tui/web/json)."""

from __future__ import annotations


def shorten(s: str, n: int = 46) -> str:
    """Left-elide a long path-like label to ``n`` chars, keeping the tail (the repo name
    lives at the end of a path, so the tail is the informative part). ``n <= 1`` collapses
    to the ellipsis (guards the ``s[-0:]`` == whole-string trap at ``n == 1``)."""
    if len(s) <= n:
        return s
    if n <= 1:
        return "…"
    return "…" + s[-(n - 1):]
