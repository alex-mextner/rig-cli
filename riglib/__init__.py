"""rig — the dev-environment umbrella driver.

`rig` sets up a repository (and a developer's machine) from a committed, declarative
config (`rig.yaml`) by applying content from the `agent-tools` umbrella repo: skills,
agent-hooks, git-hooks/dispatcher, CI gates, and MCP registrations.

Design rules mirrored from the sibling Python CLIs (review-cli, tg-cli):

- **Stdlib-only at import time.** Every module in this package imports only the standard
  library when loaded. Heavy/optional dependencies (``yaml``, the ``textual`` TUI) are
  imported lazily inside the function that needs them, so ``rig --help`` and the headless
  apply path stay fast and dependency-light.
- **One executor, two front-ends.** The interactive wizard and ``rig apply`` share the
  same plan builder and action runner; the TUI is a thin front-end over the proven engine.
- **rig.yaml is committed by default.** It is the reproducible source of truth. Global
  config (``~/.config/rig/config.yaml``) is the fallback; the per-repo ``rig.yaml``
  overrides it. Scope is by location, never a flag.
"""

from __future__ import annotations

from ._version import resolve_version

# Resolved from the single source of truth (pyproject `[project] version`) — never a
# hardcoded literal that silently drifts from the packaged metadata (rig-cli#70). See
# `riglib/_version.py` for the installed-vs-checkout resolution order.
__version__ = resolve_version()

__all__ = ["__version__"]
