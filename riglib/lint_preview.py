"""Best-effort before/after lint finding statistics for ``rig apply``.

The preview is deliberately non-blocking: missing Oxlint, an invalid pre-existing config, or a
repository that cannot currently lint must never prevent Rig from provisioning the requested
policy. When both sides can run, Rig reports the warning/error delta before it writes the new
configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FindingCounts:
    errors: int
    warnings: int


@dataclass(frozen=True)
class LintImpact:
    current: FindingCounts | None
    desired: FindingCounts | None
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.current is not None and self.desired is not None

    def render(self) -> str:
        if not self.available:
            return self.detail or "lint finding preview unavailable"
        assert self.current is not None and self.desired is not None
        de = self.desired.errors - self.current.errors
        dw = self.desired.warnings - self.current.warnings
        return (
            "lint findings: "
            f"errors {self.current.errors}→{self.desired.errors} ({de:+d}), "
            f"warnings {self.current.warnings}→{self.desired.warnings} ({dw:+d})"
        )


def _oxlint_executable(repo_root: Path) -> str | None:
    local = repo_root / "node_modules" / ".bin" / "oxlint"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("oxlint")


def _counts_from_json(text: str) -> FindingCounts:
    payload: Any = json.loads(text)
    diagnostics = payload.get("diagnostics", []) if isinstance(payload, dict) else []
    errors = 0
    warnings = 0
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        severity = str(diagnostic.get("severity", "")).lower()
        if severity == "error":
            errors += 1
        elif severity in {"warning", "warn"}:
            warnings += 1
    return FindingCounts(errors=errors, warnings=warnings)


def _run(repo_root: Path, executable: str, config: Path) -> FindingCounts:
    proc = subprocess.run(
        [executable, "--config", str(config), "--format", "json", "."],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    # Lint errors intentionally produce a non-zero exit status; JSON output is still authoritative.
    if not proc.stdout.strip():
        raise ValueError(proc.stderr.strip() or f"oxlint exited {proc.returncode} without JSON output")
    return _counts_from_json(proc.stdout)


def preview_oxlint_policy(repo_root: Path, desired_content: str, rel_path: str = "oxlint.config.ts") -> LintImpact:
    """Compare findings under the current config and a not-yet-written desired config."""
    executable = _oxlint_executable(repo_root)
    if executable is None:
        return LintImpact(None, None, "lint finding preview skipped: local oxlint is not installed")

    current_path = repo_root / rel_path
    if not current_path.is_file():
        current = FindingCounts(0, 0)
    else:
        try:
            current = _run(repo_root, executable, current_path)
        except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
            return LintImpact(None, None, f"lint finding preview skipped: current config cannot run ({exc})")

    fd, tmp_name = tempfile.mkstemp(
        dir=repo_root,
        prefix=".rig-oxlint-preview-",
        suffix=".config.ts",
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(desired_content)
        try:
            desired = _run(repo_root, executable, tmp)
        except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
            return LintImpact(None, None, f"lint finding preview skipped: desired config cannot run ({exc})")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    return LintImpact(current=current, desired=desired)
