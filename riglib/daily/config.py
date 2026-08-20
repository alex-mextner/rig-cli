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

# "owner/name" GitHub slugs, exactly as `gh -R` expects.
DEFAULT_REPOS: tuple[str, ...] = (
    "hyperide/hyper-saas",
    "hyperide/hyper-ext-e2e",
)


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "rig" / "daily.yaml"


def load_repos(*, config_path: Path | None = None, cli_repos: list[str] | None = None) -> list[str]:
    """Resolve the repo list: explicit ``--repo`` flags win, then the config file's
    ``repos:`` list, else :data:`DEFAULT_REPOS`. An empty/missing/unparseable config file
    silently falls back to the default rather than erroring — this is a convenience
    override, not a required file. De-duplicated, order-preserving — a repeated
    ``--repo owner/name`` or a duplicate entry in ``daily.yaml`` would otherwise fetch
    and report the same PRs twice, contradicting the "never repeats a PR" contract
    (codex review finding)."""
    if cli_repos:
        return _dedupe(cli_repos)
    path = config_path or default_config_path()
    if path.is_file():
        repos = _read_repos_key(path)
        if repos:
            return _dedupe(repos)
    return list(DEFAULT_REPOS)


def _dedupe(repos: list[str]) -> list[str]:
    seen: list[str] = []
    for repo in repos:
        if repo not in seen:
            seen.append(repo)
    return seen


def _read_repos_key(path: Path) -> list[str] | None:
    import yaml  # lazy — matches the rest of riglib's yaml-is-optional-at-import-time convention

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    repos = data.get("repos")
    if not isinstance(repos, list) or not repos:
        return None
    return [str(r) for r in repos]
