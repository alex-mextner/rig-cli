"""Shared path expansion helpers for rig-owned user config paths."""

from __future__ import annotations

import os
from pathlib import Path


def expand_user_path(path: str) -> Path:
    """Expand ``$VAR``, ``~``, and the portable ``~/.config`` prefix.

    When ``XDG_CONFIG_HOME`` is set, ``~/.config`` and paths below it map there before
    standard environment/user expansion. Relative paths remain relative; callers that need a
    repo or cwd anchor must apply it after this expansion step.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and (path == "~/.config" or path.startswith("~/.config/")):
        path = xdg + path[len("~/.config"):]
    return Path(os.path.expanduser(os.path.expandvars(path)))
