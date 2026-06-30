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


class Provider(Protocol):
    name: str

    def collect(self, project_path: str | Path) -> ProviderPayload:
        ...


def default_providers() -> list[Provider]:
    return [GitProvider(), RigProvider(), SverkloProvider()]


def collect_default(project_path: str | Path) -> list[ProviderPayload]:
    project = Path(project_path).expanduser()
    payloads: list[ProviderPayload] = []
    for provider in default_providers():
        try:
            payloads.append(provider.collect(project))
        except Exception as exc:  # noqa: BLE001
            payloads.append(ProviderPayload.error(source=provider.name, project_path=project, message=str(exc)))
    return payloads
