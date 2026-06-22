"""Resolve rig's version from the SINGLE source of truth (pyproject `[project] version`).

Reached at import time by ``riglib/__init__.py`` to populate ``__version__`` (what
``rig --version`` prints). The version must never be a hardcoded literal that drifts from
the packaged metadata (rig-cli#70): a stale ``--version`` is a useless freshness signal.

Two resolution paths, in order, so it works both installed and from a live checkout:

1. **Installed dist** — ``importlib.metadata.version("rig-cli")`` reads the version baked
   into the wheel/sdist at build time. This is the truth for a ``pip``/``pipx`` install.
2. **Live checkout** — rig usually runs from its repo via the ``bin/rig`` ``sys.path``
   shim (the checkout IS the binary; no install metadata exists), so importlib.metadata
   raises ``PackageNotFoundError``. We then parse ``[project] version`` straight out of the
   repo's ``pyproject.toml``, which is the SAME source the wheel build reads — so both
   paths converge on one value and cannot disagree.

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


def _version_from_pyproject() -> str | None:
    """Parse `[project] version` from the repo's pyproject.toml, or None if unreadable.

    Searches `pyproject.toml` at the repo root (parent of this `riglib/` package). Scopes
    the `version =` match to the `[project]` table so it cannot pick up a `version` key
    belonging to a different table.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_project_version(text)


def _parse_project_version(text: str) -> str | None:
    """Extract `[project] version` from pyproject TOML text, or None if absent.

    Pure string parse (split from the file read so it is directly unit-testable). Scopes the
    `version =` match to the `[project]` table body — from its header to the next REAL table
    header — so a `version` key in another table can never be picked up.
    """
    start = _PROJECT_TABLE_RE.search(text)
    if start is None:
        return None
    # Limit the search to the `[project]` table body: from after its header up to the next
    # table header or end of file.
    body_start = start.end()
    next_table = _NEXT_TABLE_RE.search(text, body_start)
    body = text[body_start : next_table.start() if next_table else len(text)]

    match = _VERSION_RE.search(body)
    return match.group(1) if match else None


def resolve_version() -> str:
    """Return rig's version: installed dist metadata first, then the checkout's pyproject.

    Never raises; falls back to a `0.0.0+unknown` sentinel only when neither source is
    available (a broken deployment), so `rig --version` always prints something.
    """
    try:
        return _dist_version(_DIST_NAME)
    except PackageNotFoundError:
        pass
    return _version_from_pyproject() or _UNKNOWN
