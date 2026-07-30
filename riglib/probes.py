"""The neutral activation-probe registry — one ProbeResult type, one fan-out point.

Kept harness-agnostic so ``cmd_doctor`` (and ``DoctorReport.probes``) never accumulates a
per-harness import list: a new probe registers here, the doctor command stays untouched.
The omp guard-activation probe (:mod:`riglib.omp_probe`) is the first (and today only)
registered probe.

Stdlib-only at import time (the repo import rule).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProbeResult:
    """One activation-probe outcome. ``ok`` is tri-state: True proven, False FAILED, None skipped."""

    name: str
    ok: bool | None
    detail: str


from collections.abc import Callable


def _registry() -> list[tuple[Callable[[], bool], Callable[[], ProbeResult]]]:
    """``(enabled_fn, probe_fn)`` pairs — a new probe registers HERE, the doctor command
    stays untouched. Imports are lazy (a probe module may pull in harness-specific code)."""
    from .omp_probe import probe_enabled, probe_omp_guard

    return [(probe_enabled, probe_omp_guard)]


def any_probe_enabled() -> bool:
    """True when at least one registered probe's opt-in gate is on (cheap — no model call).
    Lets the doctor command announce probing WITHOUT importing a specific probe module."""
    return any(enabled() for enabled, _ in _registry())


def run_probes() -> list[ProbeResult]:
    """Every enabled registered probe's result — empty unless the probe's own opt-in gate is on."""
    return [probe() for enabled, probe in _registry() if enabled()]
