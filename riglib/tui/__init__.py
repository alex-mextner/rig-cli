"""The interactive setup wizard (textual). Imported lazily — only `rig init` pulls it in.

The wizard is a thin front-end over the proven headless engine: it builds the same
:class:`~riglib.plan.InstallPlan` and runs the same :func:`~riglib.actions.run_plan`, so
it can never drift from ``rig apply``. ``textual`` is a CORE runtime dependency (pyproject
``[project].dependencies``), so every canonical install of rig brings it. ``run_wizard`` still
raises ``ImportError`` if it is somehow absent (a broken environment); the CLI catches that and
falls back to a one-line message + a non-destructive preview (see ``riglib.cli._setup_preview_no_tui``).
"""

from __future__ import annotations

__all__ = ["run_wizard"]


def run_wizard(repo_root):  # noqa: ANN001, ANN201 — lazy re-export to keep import cheap
    from .app import run_wizard as _run

    return _run(repo_root)
