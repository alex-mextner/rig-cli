"""The "last run" watermark — so re-running ``rig daily`` never double-reports a PR.

Lives at ``~/.config/rig/daily-state.json`` (sibling to rig's own global config, but a
separate file — it is a run-cache, not schema-validated policy, so it stays out of
``rig.yaml``/``config.yaml``'s validated cascade entirely).

**Per-repo, not one global scalar** (codex review P1, round 2): a single shared
timestamp is wrong the moment the configured repo set ever changes — adding a repo to
``daily.yaml`` after the first run would silently skip every one of its PRs merged
before the OTHER repos' watermark. Each repo gets its own cursor, so a newly-added repo
with no entry yet naturally starts from "no watermark" (the default lookback) instead
of inheriting an unrelated repo's history.

Contract per repo: its watermark advances to the MAX ``mergedAt`` actually reported for
THAT repo, not to "now" — a PR merged mid-run (between the fetch and the state write)
must still be picked up by the next run rather than being skipped forever. An explicit
``--since`` is a read-only query and must never touch this file (the caller enforces
that; this module only ever writes what it's told to).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_REPOS_KEY = "repos"


def default_state_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "rig" / "daily-state.json"


def load_watermarks(path: Path | None = None) -> dict[str, str]:
    """``{repo: last_reported_merged_at}`` for every repo with a saved watermark.
    Empty if there isn't one yet (first run) or the file is missing/unreadable/
    malformed — a corrupt state file must never crash the report, just fall back to
    "no watermark for any repo". A per-entry value that isn't a non-empty string is
    dropped rather than failing the whole load (one bad repo entry shouldn't blind
    every other repo's watermark)."""
    p = path or default_state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    repos = data.get(_REPOS_KEY)
    if not isinstance(repos, dict):
        return {}
    return {
        str(repo): str(value)
        for repo, value in repos.items()
        if isinstance(value, str) and value
    }


def save_watermarks(watermarks: dict[str, str], path: Path | None = None) -> None:
    """Write ``watermarks`` ATOMICALLY (write to a same-directory temp file, then
    ``os.replace``) — a plain ``write_text`` truncates the file in place first, so a
    crash or disk-full mid-write would leave invalid JSON; ``load_watermarks`` would
    then see it as "no state at all" and every repo would re-report from its default
    lookback. ``os.replace`` is atomic on the same filesystem, which a same-directory
    temp file guarantees. Regression for the codex review P1 finding."""
    p = path or default_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({_REPOS_KEY: watermarks}, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
