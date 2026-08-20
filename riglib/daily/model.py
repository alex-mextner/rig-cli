"""The one data shape ``rig daily`` passes between fetch -> categorize -> render."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MergedPR:
    """One merged pull request, as read from ``gh pr list --json ...``.

    ``merged_at`` is an ISO-8601 UTC timestamp string (``...Z``) — gh's own format, kept
    as a string end-to-end so no naive local-time conversion can creep in (Alex is in
    Belgrade, UTC+2; the whole pipeline compares in UTC). Use :mod:`riglib.daily.timeutil`
    to parse it when a comparison/max is actually needed.
    """

    repo: str  # "owner/name", e.g. "hyperide/hyper-saas"
    number: int
    title: str
    body: str
    merged_at: str
    url: str
    labels: list[str] = field(default_factory=list)
