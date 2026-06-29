"""Resolve rig's version from the SINGLE source of truth (pyproject `[project] version`).

Reached at import time by ``riglib/__init__.py`` to populate ``__version__`` (what
``rig --version`` prints). The version must never be a hardcoded literal that drifts from
the packaged metadata (rig-cli#70): a stale ``--version`` is a useless freshness signal.

Two resolution paths, in order, so it works both installed and from a live checkout:

1. **Live checkout** — when ``pyproject.toml`` is readable at the repo root (parent of this
   ``riglib/`` package), parse ``[project] version`` directly from it. This is the ALWAYS-
   FRESH path for any source checkout, including editable installs (``pip install -e .``).
   A ``pip install -e .`` creates an in-tree ``rig_cli.egg-info/`` whose version can lag
   behind after a ``pyproject.toml`` bump; ``importlib.metadata`` reads THAT stale egg-info
   rather than the live file. Preferring pyproject first eliminates the stale-shadow
   problem entirely (rig-cli#67).
2. **Installed dist** — when no readable ``pyproject.toml`` is found (a ``pip``/``pipx``
   wheel/sdist install outside any checkout), fall back to
   ``importlib.metadata.version("rig-cli")``, which reads the version baked into the
   wheel at build time.

Stdlib-only at import time (the package-wide rule): ``importlib.metadata`` + ``re`` +
``pathlib``. We deliberately do NOT use ``tomllib`` — it is absent on Python 3.10, which is
still in the supported matrix (``requires-python >= 3.10``). A targeted regex over the
``[project]`` table is enough for a single scalar ``version = "..."`` key.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

# The distribution name declared in pyproject `[project] name`. importlib.metadata keys on
# this, NOT on the import package name (`riglib`) or the script name (`rig`).
_DIST_NAME = "rig-cli"

# Last-resort sentinel: only surfaces if rig is neither installed NOR runnable from a
# checkout that carries a parseable pyproject — a genuinely broken deployment.
_UNKNOWN = "0.0.0+unknown"

# `version = "1.2.3"` inside the `[project]` table. Anchored to the table so a `version`
# key in some other table (e.g. a dependency pin) can never be mistaken for the project's.
_PROJECT_TABLE_RE = re.compile(r"^\[project\]\s*$", re.MULTILINE)
# A real TOML table header starts with `[` followed by a name char — NOT any line starting
# with `[`, which would prematurely end `[project]` on a multi-line array whose continuation
# line happens to start with `[` (e.g. an array-of-arrays element).
_NEXT_TABLE_RE = re.compile(r"^\[[A-Za-z]", re.MULTILINE)
_VERSION_RE = re.compile(r"""^version\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)
# Guard: only trust a pyproject.toml whose `[project] name` matches the dist name, so an
# adjacent host-project pyproject (e.g. `pip install --target ./vendor`) is never mistaken
# for rig's own.
_NAME_RE = re.compile(r"""^name\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)


def _version_from_pyproject() -> str | None:
    """Parse `[project] version` from the repo's pyproject.toml, or None if unreadable.

    Searches `pyproject.toml` at the repo root (parent of this `riglib/` package). Verifies
    that the file's `[project] name` is ``"rig-cli"`` before trusting its version, so an
    adjacent host-project pyproject (e.g. from ``pip install --target ./vendor``) is ignored
    and the resolver falls through to ``importlib.metadata`` instead.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_project_version(text, require_name=_DIST_NAME)


def _parse_project_version(text: str, *, require_name: str | None = None) -> str | None:
    """Extract `[project] version` from pyproject TOML text, or None if absent/mismatched.

    Pure string parse (split from the file read so it is directly unit-testable). Scopes the
    `version =` and `name =` matches to the `[project]` table body — from its header to the
    next REAL table header — so keys in other tables can never be picked up.

    If ``require_name`` is given, returns None when the `[project] name` does not match it.
    This guards against reading an adjacent host-project pyproject.toml as if it were rig's.
    """
    start = _PROJECT_TABLE_RE.search(text)
    if start is None:
        return None
    # Limit the search to the `[project]` table body: from after its header up to the next
    # table header or end of file.
    body_start = start.end()
    next_table = _NEXT_TABLE_RE.search(text, body_start)
    body = text[body_start : next_table.start() if next_table else len(text)]

    if require_name is not None:
        name_match = _NAME_RE.search(body)
        if name_match is None or name_match.group(1) != require_name:
            return None

    match = _VERSION_RE.search(body)
    return match.group(1) if match else None


def resolve_version() -> str:
    """Return rig's version: live checkout pyproject first, then installed dist metadata.

    Checking pyproject.toml first ensures that a ``pip install -e .`` editable install
    never shadows the live version with a stale in-tree egg-info (rig-cli#67). For a
    wheel/sdist install where no pyproject.toml is reachable, we fall back to
    ``importlib.metadata``. Never raises; returns the ``0.0.0+unknown`` sentinel only on a
    genuinely broken deployment (neither source readable).
    """
    pyproject_ver = _version_from_pyproject()
    if pyproject_ver is not None:
        return pyproject_ver
    try:
        return _dist_version(_DIST_NAME)
    except PackageNotFoundError:
        pass
    return _UNKNOWN
