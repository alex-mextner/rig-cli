"""Git provider for `rig evolve`.

Accessed via: provider collection for project health, current HEAD/version keys, and future git
events/relationships.

Assumptions: git may be missing, the project may not be a worktree, and commands must be bounded;
all those cases return error payloads instead of raising.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..model import ProviderPayload


class GitProvider:
    name = "git"

    def collect(self, project_path: str | Path) -> ProviderPayload:
        project = Path(project_path).expanduser()
        try:
            inside = _run_git(project, "rev-parse", "--is-inside-work-tree")
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                message = (inside.stderr or inside.stdout).strip() or "not a git worktree"
                return ProviderPayload.error(source=self.name, project_path=project, message=message)
            head = _run_git(project, "rev-parse", "HEAD")
            branch = _run_git(project, "rev-parse", "--abbrev-ref", "HEAD")
            status = _run_git(project, "status", "--short")
        except (OSError, subprocess.SubprocessError) as exc:
            return ProviderPayload.error(source=self.name, project_path=project, message=str(exc))

        dirty_paths = [line[3:] for line in status.stdout.splitlines() if len(line) > 3] if status.returncode == 0 else []
        data = {
            "inside_work_tree": True,
            "head": head.stdout.strip() if head.returncode == 0 else "",
            "branch": branch.stdout.strip() if branch.returncode == 0 else "",
            "dirty_count": len(dirty_paths),
            "dirty_paths": dirty_paths[:200],
        }
        links = [{"rel": "worktree", "href": str(_resolve(project))}]
        return ProviderPayload.ok(source=self.name, project_path=project, data=data, links=links)


def _run_git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()
