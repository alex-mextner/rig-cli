"""UTC timestamp parsing shared by ``rig daily``'s fetch/state/CLI layers.

Every timestamp in this feature — ``gh``'s ``mergedAt``, the watermark file, the
``--since`` flag — is compared in UTC throughout. gh emits ``mergedAt`` as
``YYYY-MM-DDTHH:MM:SSZ``; Python's ``fromisoformat`` before 3.11 chokes on the trailing
``Z``, so it is normalized to ``+00:00`` before parsing (this repo supports Python
>=3.10 per pyproject.toml).
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (``Z`` or explicit offset) into an aware UTC datetime."""
    normalized = ts.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        # A naive input is treated as UTC rather than silently adopting the local zone —
        # every caller in this feature already deals exclusively in UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    """Render an aware datetime back to gh's ``...Z`` shape (round-trips through :func:`parse_utc`)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
