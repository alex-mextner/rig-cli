"""config-web SCOPE discovery — turns config-web from a single-repo tool into a machine-wide one
(rig-cli#310).

A "scope" is one tab in the browser: either a rig-managed REPO (a checkout with a committed
``rig.yaml``) or the single GLOBAL scope (``~/.config/rig/config.yaml`` alone, no repo overlay).

Discovery is READ-ONLY and deliberately reuses the existing machine-local
:mod:`riglib.repository_registry` rather than re-walking the filesystem from a web request:
that module's own docstring says discovery ("refresh") is a deliberate separate act (today,
``rig fleet``'s), and re-scanning disk on every page load would be slow and surprising. When the
registry is absent or empty, config-web falls back to exactly today's behavior: only the repo it
was started in (``-C`` / cwd), plus the Global scope — so an existing single-repo workflow is
unaffected.

Every scope's ``id`` doubles as its allowlist key: config-web's endpoints (``/edit``,
``/api/plan``, ``/api/apply``, ``/api/drift``) MUST resolve a request's ``scope`` id through
:func:`resolve_scope` against the SAME list :func:`discover_scopes` returned for this server
instance — never accept an arbitrary path from the browser. A repo scope's id is its own resolved
absolute path (stable, human-legible in logs, and impossible to collide with the reserved
``GLOBAL_SCOPE_ID``, which is not a valid absolute path).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GLOBAL_SCOPE_ID = "global"


@dataclass(frozen=True)
class Scope:
    """One config-web tab: a rig-managed repo, or the single Global (machine-wide-only) scope."""

    id: str
    label: str
    repo_root: Path | None  # None for the Global scope
    is_global: bool

    @property
    def is_repo(self) -> bool:
        return not self.is_global


def _global_scope() -> Scope:
    return Scope(id=GLOBAL_SCOPE_ID, label="Global", repo_root=None, is_global=True)


def _repo_scope(path: Path) -> Scope:
    resolved = path.resolve()
    return Scope(id=str(resolved), label=resolved.name, repo_root=resolved, is_global=False)


def discover_scopes(home_repo: Path) -> list[Scope]:
    """The scopes this config-web instance serves: every rig-managed repo + the Global scope.

    ``home_repo`` is the repo config-web was started against (``-C`` / cwd) — always included
    even if it is not (yet) in the repository registry, or has no committed ``rig.yaml`` (a fresh
    checkout mid-``rig init``), so the existing single-repo workflow never regresses. Additional
    repos come from :func:`riglib.repository_registry.RepositoryRegistry.load` (read-only, no
    filesystem walk here), filtered to non-stale entries with a committed ``rig.yaml`` — a bare
    git checkout with no rig config is not "rig-managed" and would just be noise as a tab.

    Order: ``home_repo`` first, then the rest alphabetically by name, then the Global scope last.
    """
    from .repository_registry import RepositoryRegistry  # lazy: keeps module import light

    home = home_repo.resolve()
    seen = {home}
    scopes = [_repo_scope(home)]

    try:
        registry = RepositoryRegistry.load()
    except Exception:  # noqa: BLE001 — a malformed/corrupt registry must not break config-web
        registry = None

    others: list[Scope] = []
    if registry is not None:
        for entry in registry.repositories:
            if entry.stale:
                continue
            try:
                # RepositoryRegistry.load() only type-checks the tag arrays (see
                # repository_registry.py) — a structurally-valid JSON registry can still carry a
                # non-string `path` (e.g. `"path": null`, a hand-edited or corrupted registry
                # file), which Path() raises TypeError on. One malformed entry must not 500 the
                # whole console — every scope endpoint calls this on every request (found in
                # review): skip that entry and keep discovering the rest.
                path = Path(entry.path).resolve()
            except (TypeError, ValueError):
                continue
            if path in seen:
                continue
            if not (path / "rig.yaml").is_file():
                continue
            seen.add(path)
            others.append(_repo_scope(path))
    others.sort(key=lambda s: s.label.lower())

    scopes.extend(others)
    scopes.append(_global_scope())
    return scopes


def resolve_scope(scopes: list[Scope], scope_id: str | None) -> Scope | None:
    """Resolve a request's ``scope`` id against the server's discovered allowlist.

    Returns ``None`` for an unknown id (including an empty/missing one when a caller wants a
    default) — callers must reject the request rather than fall back to an arbitrary path. This
    is the ONLY place a scope id from the browser is trusted; every multi-repo endpoint must go
    through it.
    """
    if not scope_id:
        return None
    for scope in scopes:
        if scope.id == scope_id:
            return scope
    return None


def default_scope(scopes: list[Scope]) -> Scope:
    """The scope shown on a bare GET ``/`` — the home repo (discover_scopes always puts it first)."""
    return scopes[0]
