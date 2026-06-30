"""Rig provider for `rig evolve`.

Accessed via: provider collection so the portal can show repo-local rig config and project-tool
provisioning health next to git/tool data.

Assumptions: reading rig config must stay stdlib-only here; this provider records lightweight
metadata and raw refs, leaving full YAML interpretation to rig's existing config engine later.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import ProviderPayload


class RigProvider:
    name = "rig"

    def collect(self, project_path: str | Path) -> ProviderPayload:
        project = Path(project_path).expanduser()
        rig_yaml = project / "rig.yaml"
        if not rig_yaml.exists():
            return ProviderPayload.warning(
                source=self.name,
                project_path=project,
                message="rig.yaml not found",
                data={"rig_yaml": {"present": False, "path": str(_resolve(rig_yaml))}},
            )
        try:
            text = rig_yaml.read_text(encoding="utf-8")
            stat = rig_yaml.stat()
        except OSError as exc:
            return ProviderPayload.error(source=self.name, project_path=project, message=str(exc), raw_ref=str(rig_yaml))
        data = {
            "rig_yaml": {
                "present": True,
                "path": str(_resolve(rig_yaml)),
                "bytes": stat.st_size,
            },
            "project_tools": {
                "heuristic": True,
                "mentioned": _yaml_key_present(text, "project_tools", indent=0),
                "haft": _yaml_key_present(text, "haft"),
                "serena": _yaml_key_present(text, "serena"),
                "sverklo": _yaml_key_present(text, "sverklo"),
            },
        }
        return ProviderPayload.ok(source=self.name, project_path=project, data=data, raw_ref=str(_resolve(rig_yaml)))


def _yaml_key_present(text: str, key: str, *, indent: int | None = None) -> bool:
    """Best-effort key detection without importing YAML in the provider hot path."""

    if indent is None:
        pattern = rf"^[ \t]+{re.escape(key)}\s*:"
    else:
        pattern = rf"^[ ]{{{indent}}}{re.escape(key)}\s*:"
    return re.search(pattern, text, flags=re.MULTILINE) is not None


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()
