"""The repo list ``rig daily`` reports on.

Default is the two real hyperide product repos this machine has checked out (verified
against their actual git remotes, not guessed from folder names — ``~/work/hyperide``
and ``~/work/hyper-saas-work`` both resolve to the SAME GitHub repo, ``hyperide/hyper-saas``;
the local folder name is not the repo slug). Overridable per-run via ``--repo`` (repeatable)
or persistently via ``~/.config/rig/daily.yaml``'s ``repos:`` list, so the Daily report is
never hardcoded to only these two forever.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import ConfigError

# "owner/name" GitHub slugs, exactly as `gh -R` expects.
DEFAULT_REPOS: tuple[str, ...] = (
    "hyperide/hyper-saas",
    "hyperide/hyper-ext-e2e",
)


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "rig" / "daily.yaml"


def load_repos(
    *,
    config_path: Path | None = None,
    cli_repos: list[str] | None = None,
    explicit: bool = False,
) -> list[str]:
    """Resolve the repo list: explicit ``--repo`` flags win, then the config file's
    ``repos:`` list, else :data:`DEFAULT_REPOS`.

    ``explicit`` marks a user-supplied ``--config PATH`` (as opposed to just checking the
    default ``~/.config/rig/daily.yaml`` location, which a caller may never have written).
    A missing/unreadable/malformed/empty-``repos`` file at the DEFAULT location is a
    convenience no-op — fall back to :data:`DEFAULT_REPOS` silently. The same problem on
    an EXPLICIT ``--config`` path instead raises a fail-closed :class:`ConfigError`: the
    operator asked for a specific file, and silently substituting unrelated repos could
    generate a report — and advance watermarks — for the wrong project with no indication
    (codex review P1 finding). De-duplicated, order-preserving — a repeated
    ``--repo owner/name`` or a duplicate entry in ``daily.yaml`` would otherwise fetch
    and report the same PRs twice, contradicting the "never repeats a PR" contract
    (codex review finding)."""
    if cli_repos:
        return _dedupe(cli_repos)
    path = config_path or default_config_path()
    problem = _file_problem(path)
    if problem is not None:
        if explicit:
            raise ConfigError(
                what=f"--config path {str(path)!r} {problem}",
                why="an explicit --config must point at a real, readable daily.yaml, "
                "not silently fall back to the default repos",
                fix=f"create {path} with a `repos:` list, or drop --config to use the "
                "default repos",
            )
        return list(DEFAULT_REPOS)
    repos = _read_repos_key(path, explicit=explicit)
    if repos:
        return _dedupe(repos)
    return list(DEFAULT_REPOS)


def _file_problem(path: Path) -> str | None:
    """Return a short, safe-to-print problem description if ``path`` can't be used as a
    config file, else ``None``. Never lets a raw :class:`OSError` (e.g.
    :class:`PermissionError` from an unreadable parent directory) escape
    :func:`load_repos` — bare ``Path.is_file()``/``Path.exists()`` only swallow a narrow
    errno set (``ENOENT``, ``ENOTDIR``, ``EBADF``, ``ELOOP``); ``EACCES`` propagates raw
    past both the explicit ``--config`` :class:`ConfigError` contract and the
    default-location silent-fallback promise (review finding, round 7). A permission
    error reading the file's CONTENT (the path itself is stat-able but unreadable) is a
    separate, already-handled case — see :func:`_read_repos_key`'s own ``OSError`` catch."""
    try:
        exists = path.exists()
    except OSError:
        return "could not be checked (permission denied)"
    if not exists:
        return "does not exist"
    try:
        is_file = path.is_file()
    except OSError:
        return "could not be checked (permission denied)"
    if not is_file:
        return "is not a regular file"
    return None


def _dedupe(repos: list[str]) -> list[str]:
    seen: list[str] = []
    for repo in repos:
        if repo not in seen:
            seen.append(repo)
    return seen


def _read_repos_key(path: Path, *, explicit: bool) -> list[str] | None:
    """Read the ``repos:`` list out of ``path``. On the default location (``explicit``
    False) any problem is swallowed and reported as ``None`` (caller falls back to
    :data:`DEFAULT_REPOS`). On an explicit ``--config`` path the same problems raise a
    fail-closed :class:`ConfigError` instead (codex review P1 finding) — see
    :func:`load_repos`."""
    import yaml  # lazy — matches the rest of riglib's yaml-is-optional-at-import-time convention

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        if explicit:
            raise ConfigError(
                what=f"--config path {str(path)!r} could not be read/parsed",
                why=str(exc),
                fix="fix the YAML syntax, or drop --config to use the default repos",
            ) from exc
        return None
    if not isinstance(data, dict):
        if explicit:
            raise ConfigError(
                what=f"--config path {str(path)!r} is not a YAML mapping",
                why=f"top-level YAML value is {type(data).__name__}, expected a mapping "
                "with a `repos:` key",
                fix="edit the file so its top level is a mapping, e.g. "
                "`repos: [owner/name, ...]`",
            )
        return None
    repos = data.get("repos")
    if not isinstance(repos, list) or not repos:
        if explicit:
            raise ConfigError(
                what=f"--config path {str(path)!r} has no valid `repos:` list",
                why="the `repos` key is missing, empty, or not a list",
                fix="add a `repos:` list of `owner/name` GitHub slugs to the file",
            )
        return None
    return [str(r) for r in repos]
