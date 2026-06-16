"""The normalized record every log-source parser emits, plus the small enums around it.

This is the SPINE of the stats pipeline: parsers (``riglib.stats.sources.*``) read a
harness's on-disk session logs and yield a stream of :class:`ToolInvocation`; the
aggregator (``riglib.stats.aggregate``) reduces that stream into counts; the renderers
(``riglib.stats.render.*``) draw the counts. Keep this module stdlib-only and free of any
harness/render knowledge — it is the contract both sides agree on, nothing more.

Invariants:
  * ``timestamp`` is a timezone-aware UTC ``datetime`` (parsers normalize). It MAY be None
    only when a log genuinely lacks one — aggregation tolerates that (those rows just drop
    out of time-bucketed series, never out of the totals).
  * ``category`` is one of :data:`CATEGORIES`. ``tool_name`` is the display label we count
    by (e.g. ``Bash``, ``review (cli)``, ``mcp__serena__find_symbol``).
  * ``raw_tool`` is the unmodified tool identifier from the log (``Bash``, ``exec_command``,
    ``run_shell_command``, ``mcp__...``) so a renderer can show provenance; ``tool_name`` is
    the post-taxonomy label (a Bash-that-ran-``review`` becomes ``review (cli)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# The four buckets every invocation lands in. Order is the canonical display order.
CATEGORIES: tuple[str, ...] = ("baseline", "ours", "external-advertised", "other")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One tool call, normalized across harnesses. Frozen so it's safe to fan out / hash."""

    timestamp: datetime | None
    harness: str  # "claude-code" | "codex" | "gemini" | "opencode" | ...
    repo: str  # absolute repo/cwd path of the session ("(unknown)" if undecodable)
    session: str  # session id (file stem, usually)
    tool_name: str  # post-taxonomy display label we count by
    category: str  # one of CATEGORIES
    raw_tool: str  # the unmodified tool identifier from the log
    detail: str = ""  # optional: the bash command, mcp args summary, etc.
