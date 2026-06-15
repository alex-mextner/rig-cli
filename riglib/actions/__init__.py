"""Install actions — the stdlib-only executor for an :class:`~riglib.plan.InstallPlan`.

Every action obeys the same contract:

- **Idempotent.** Re-running with the same config is a no-op: copies skip-if-identical,
  the global ``core.hooksPath`` set checks the current value first, MCP merges are keyed
  by name.
- **Reversible-noted.** Anything replaced is backed up (per ``on_conflict``) and the
  restore path recorded in the result.
- **Fail-explicit on IO.** A non-writable target is reported per-action, never silently
  skipped.

The executor returns a list of :class:`ActionResult`; both the headless ``rig apply`` and
the TUI Apply screen render the same results — one code path, two front-ends.
"""

from __future__ import annotations

from .runner import ActionResult, ApplyReport, run_plan

__all__ = ["ActionResult", "ApplyReport", "run_plan"]
