"""The interactive setup wizard (textual). Imported lazily — only `rig setup` pulls it in.

The wizard is a thin front-end over the proven headless engine: it builds the same
:class:`~riglib.plan.InstallPlan` and runs the same :func:`~riglib.actions.run_plan`, so
it can never drift from ``rig apply``. ``textual`` is an optional dependency
(``pip install 'rig-cli[tui]'``); ``run_wizard`` raises ``ImportError`` if it is absent,
and the CLI falls back to a non-interactive default setup.
"""

from __future__ import annotations

__all__ = ["run_wizard"]


def run_wizard(repo_root):  # noqa: ANN001, ANN201 — lazy re-export to keep import cheap
    from .app import run_wizard as _run

    return _run(repo_root)
