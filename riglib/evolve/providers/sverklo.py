"""Sverklo provider for `rig evolve`.

Accessed via: provider collection to show registry/index availability and project registration
health in the portal.

Assumptions: Sverklo is optional and may be locked, absent, or stale; all such cases return
provider health payloads rather than blocking the rest of the evolve view.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..model import ProviderPayload


class SverkloProvider:
    name = "sverklo"

    def collect(self, project_path: str | Path) -> ProviderPayload:
        project = Path(project_path).expanduser()
        exe = shutil.which("sverklo")
        if not exe:
            return ProviderPayload.error(
                source=self.name,
                project_path=project,
                message="sverklo CLI not found on PATH",
            )
        try:
            proc = subprocess.run([exe, "list"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return ProviderPayload.error(source=self.name, project_path=project, message=f"sverklo list failed: {exc}")
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip() or f"sverklo list exited {proc.returncode}"
            return ProviderPayload.error(source=self.name, project_path=project, message=message)

        entries = _entries(proc.stdout)
        target = _resolve(project)
        registered = any(entry["path"] == str(target) for entry in entries)
        data = {"registered": registered, "entries": entries}
        if not registered:
            return ProviderPayload.warning(
                source=self.name,
                project_path=project,
                message="project is not registered in sverklo",
                data=data,
            )
        return ProviderPayload.ok(source=self.name, project_path=project, data=data)


def _entries(output: str) -> list[dict[str, Any]]:
    from ..projects import _parse_sverklo_entries

    entries: list[dict[str, Any]] = []
    for entry in _parse_sverklo_entries(output):
        path = _resolve(entry["path"])
        entries.append(
            {
                "name": entry.get("name") or path.name,
                "path": str(path),
                "exists": path.exists(),
            }
        )
    return entries


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()
