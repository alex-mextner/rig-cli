"""``rig daily`` — a categorized "what shipped" report over merged PRs, pasteable
straight into a Slack Daily channel.

Source of truth is `gh pr list --state merged` (:mod:`riglib.daily.github`) — never a
ticket/Linear status. No AI/LLM call is involved: title/body extraction and
categorization are mechanical (:mod:`riglib.daily.format_report`,
:mod:`riglib.daily.categorize`). A small JSON watermark, PER REPO
(:mod:`riglib.daily.state`), tracks each repo's own last reported ``mergedAt`` so
re-running ``rig daily`` never double-reports a PR — and so a repo that failed to
fetch, or a repo newly added to the config, never borrows another repo's cursor.

Entry point: :func:`run`, wired from ``riglib/cli.py`` as ``cmd_daily``.
"""

from __future__ import annotations

from .command import run

__all__ = ["run"]
