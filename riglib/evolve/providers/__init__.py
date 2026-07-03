"""Provider adapters for `rig evolve`.

Accessed via: backend provider collection and future provider-inspection APIs. This package keeps
tool failures fail-soft so a missing optional CLI never blocks the portal.

Assumptions: each provider exposes a ``name`` and ``collect(project_path)`` returning a
``ProviderPayload``; any unexpected provider exception is converted to an error payload here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..model import ProviderPayload
from .git import GitProvider
from .rig import RigProvider
from .sverklo import SverkloProvider

PLANNED_PROVIDER_SOURCES = ("task", "tg", "review", "haft", "serena", "lsp", "tree-sitter")


class Provider(Protocol):
    name: str

    def collect(self, project_path: str | Path) -> ProviderPayload:
        ...


def default_providers() -> list[Provider]:
    return [GitProvider(), RigProvider(), SverkloProvider()]


def not_wired_provider_payloads(project_path: str | Path, seen_sources: set[str] | None = None) -> list[ProviderPayload]:
    """Return generated placeholder payloads for required provider sources not collected elsewhere."""
    project = Path(project_path).expanduser()
    seen = seen_sources if seen_sources is not None else set()
    return [
        ProviderPayload(
            source=source,
            project_path=project,
            status="not-wired",
            message="Provider not wired yet.",
        )
        for source in PLANNED_PROVIDER_SOURCES
        if source not in seen
    ]


def collect_default(project_path: str | Path) -> list[ProviderPayload]:
    """Collect concrete providers plus explicit not-wired placeholders for planned sources."""
    project = Path(project_path).expanduser()
    payloads: list[ProviderPayload] = []
    for provider in default_providers():
        try:
            payloads.append(provider.collect(project))
        except Exception as exc:  # noqa: BLE001
            payloads.append(ProviderPayload.error(source=provider.name, project_path=project, message=str(exc)))
    seen = {payload.source for payload in payloads}
    payloads.extend(not_wired_provider_payloads(project, seen))
    return payloads
