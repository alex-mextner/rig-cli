"""The pluggable log-source contract + a tiny registry.

Adding a harness = drop a module in this package that subclasses :class:`LogSource` and
registers an instance via :func:`register`. The command (``riglib.stats.command``) iterates
:func:`all_sources` — it never names a harness. That is the "extensible" the spec asks for:
new harness, zero edits to the aggregator/renderers/dispatch.

A source reports three things:
  * ``available()`` — does this harness's log root exist on THIS machine? (data-driven
    supported-harness list = ``[s.name for s in all_sources() if s.available()]``)
  * ``not_found_note()`` — a human line for when it's absent or its format is unknown.
  * ``iter_invocations()`` — the normalized stream. Parsers SHOULD be defensive: a single
    malformed line/file must never abort the whole harness, let alone the whole command.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..model import ToolInvocation


class LogSource(ABC):
    """One harness's reader. Stateless except for the resolved log root."""

    #: short, stable harness id used everywhere (CLI filter, breakdowns, JSON keys).
    name: str = ""

    def __init__(self, home: Path | None = None) -> None:
        # HOME is resolved at construction so tests can point it at a tmp dir. Everything
        # under the harness root is derived from this, never from a cached absolute path.
        self.home = home or Path(os.path.expanduser("~"))

    @abstractmethod
    def root(self) -> Path:
        """The directory this harness keeps its session logs in (may not exist)."""

    def available(self) -> bool:
        return self.root().exists()

    def not_found_note(self) -> str:
        return f"{self.name}: log root not found ({self.root()}) — harness not installed?"

    @abstractmethod
    def iter_invocations(
        self, *, repos: frozenset[str] | None = None
    ) -> Iterator[ToolInvocation]:
        """Yield normalized invocations. ``repos`` (if given) is a cheap pre-filter hint
        on absolute repo path; callers still filter authoritatively, so a source MAY ignore
        it — but honoring it lets a source skip whole session files it can prove are out of
        scope (cheap perf win on a machine with thousands of sessions)."""


# ── registry ───────────────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, type[LogSource]] = {}


def register(cls: type[LogSource]) -> type[LogSource]:
    """Class decorator: make a LogSource discoverable by name. Idempotent on re-import."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a non-empty .name")
    _REGISTRY[cls.name] = cls
    return cls


def all_sources(home: Path | None = None) -> list[LogSource]:
    """Instantiate every registered source (in registration order)."""
    return [cls(home=home) for cls in _REGISTRY.values()]


def source_names() -> list[str]:
    return list(_REGISTRY)


# ── shared parse helpers (used by the concrete parsers) ─────────────────────────────────
def parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (``...Z`` or offset) into aware UTC. None on failure."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_epoch(value: object) -> datetime | None:
    """Parse an epoch number (seconds OR milliseconds) into aware UTC. None on failure."""
    if value is None:
        return None
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # disambiguate seconds vs milliseconds: a value above 1e12 can't be a plausible
    # seconds-epoch for our era (1e12 s ≈ year 33658), so it must be milliseconds.
    if n > 1e12:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
