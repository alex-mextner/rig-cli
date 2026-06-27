"""Doctor — detect (and optionally install) the tools rig + agent-tools need.

A "dependency" here is an external CLI binary that agent-tools content relies on
(gitleaks for secret-scan, gh for ship/CI, git always, etc.) plus rig's own optional
runtime bits (pyyaml, textual, rich). For each, doctor reports present/absent and — when the
OS package manager is known — the exact install command. In ``--yes`` mode it runs the
install commands non-interactively; otherwise it only prints them (never a destructive
install without confirmation).

Package name varies per manager, so each dependency carries a per-manager name map.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from .detect import OsInfo, detect_os, install_command


@dataclass
class Dependency:
    name: str  # the binary / module to probe
    why: str  # one-line "what needs it"
    kind: str = "binary"  # "binary" | "python"
    required: bool = False  # required vs optional (optional = nice-to-have)
    # per-manager package name; falls back to ``name`` when a manager is absent here
    pkg: dict[str, str] = field(default_factory=dict)


# The dependency surface for rig + the agent-tools content it applies.
DEPENDENCIES: list[Dependency] = [
    Dependency("git", "version control + all git-hooks / dispatcher", required=True),
    Dependency("python3", "rig runtime", required=True),
    Dependency(
        "pyyaml",
        "parse/serialize rig.yaml (config cascade)",
        kind="python",
        required=True,
        pkg={"brew": "", "apt": "python3-yaml", "dnf": "python3-pyyaml", "pacman": "python-yaml"},
    ),
    Dependency(
        "gh",
        "CI gates (ship, review-threads, screenshots) + repo ops",
        pkg={"brew": "gh", "apt": "gh", "dnf": "gh", "pacman": "github-cli", "zypper": "gh"},
    ),
    Dependency(
        "gitleaks",
        "secret-scan CI gate + the secret-scan git-hook fragment",
        pkg={"brew": "gitleaks", "apt": "gitleaks", "dnf": "gitleaks", "pacman": "gitleaks"},
    ),
    Dependency(
        "lefthook",
        "per-repo git-hook templates (committed, team-wide mechanism)",
        pkg={"brew": "lefthook", "apt": "lefthook", "pacman": "lefthook"},
    ),
    Dependency(
        "textual",
        "the interactive setup wizard (rig init TUI)",
        kind="python",
        pkg={"brew": "", "apt": "", "dnf": "", "pacman": "python-textual"},
    ),
    Dependency(
        "rich",
        "the `rig stats show --format tui` report (degrades to plain text when absent)",
        kind="python",
        pkg={"brew": "", "apt": "python3-rich", "dnf": "python3-rich", "pacman": "python-rich"},
    ),
    # The daily model-freshness schedule (models:) is provisioned via the platform-native
    # scheduler: launchd (launchctl) on macOS, crontab on Linux. Both ship with the OS; this
    # entry surfaces the one rig will actually use so a stripped container without crontab is
    # flagged. The probe is for the scheduler binary the CURRENT platform uses.
    Dependency(
        "launchctl" if sys.platform == "darwin" else "crontab",
        "model-freshness daily schedule (models:) — launchd on macOS, crontab on Linux",
    ),
]


@dataclass
class DepStatus:
    dep: Dependency
    present: bool
    location: str | None
    install_cmd: list[str] | None  # the command to install it (None when unknown)


@dataclass
class DoctorReport:
    os: OsInfo
    statuses: list[DepStatus] = field(default_factory=list)

    @property
    def missing_required(self) -> list[DepStatus]:
        return [s for s in self.statuses if not s.present and s.dep.required]

    @property
    def missing_optional(self) -> list[DepStatus]:
        return [s for s in self.statuses if not s.present and not s.dep.required]


def _python_present(module: str) -> bool:
    # pyyaml's import name is "yaml"
    import_name = {"pyyaml": "yaml"}.get(module, module)
    return importlib.util.find_spec(import_name) is not None


def diagnose(os_info: OsInfo | None = None) -> DoctorReport:
    os_info = os_info or detect_os()
    report = DoctorReport(os=os_info)
    for dep in DEPENDENCIES:
        if dep.kind == "python":
            present = _python_present(dep.name)
            location = "importable" if present else None
        else:
            loc = shutil.which(dep.name)
            present = loc is not None
            location = loc
        report.statuses.append(
            DepStatus(
                dep=dep,
                present=present,
                location=location,
                install_cmd=None if present else _install_cmd_for(dep, os_info),
            )
        )
    return report


def _python_install_command(package: str) -> list[str]:
    """Install ``package`` into rig's OWN interpreter (``sys.executable``).

    Prefers ``uv pip install`` — the toolchain rig users standardize on — targeting the exact
    interpreter rig runs under so the wizard's ``textual`` lands where rig will import it. This
    installs cleanly when that interpreter is a uv-/pipx-managed venv (the recommended install
    shape). It does NOT magically bypass PEP-668: on an externally-managed *system* Python both
    uv and a bare pip refuse — there the real fix is to install rig itself into a managed env
    (see ``riglib.cli._tui_install_hint``), not to force a system-wide install. Falls back to
    ``python -m pip install --user`` only when ``uv`` is absent — no worse than before.
    """
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, package]
    return [sys.executable, "-m", "pip", "install", "--user", package]


def _install_cmd_for(dep: Dependency, os_info: OsInfo) -> list[str] | None:
    mgr = os_info.package_manager
    if not mgr:
        return None
    # python deps that have no system package → install into THIS interpreter (sys.executable),
    # not a bare `python3` that may be a different runtime than the one rig runs under. Prefer uv
    # (the user's toolchain) over a bare `pip` — see _python_install_command for the PEP-668 caveat.
    if dep.kind == "python":
        pkg = dep.pkg.get(mgr)
        if not pkg:  # empty string means "no system package, use uv/pip into this interpreter"
            return _python_install_command(dep.name)
        return install_command(mgr, pkg)
    pkg = dep.pkg.get(mgr, dep.name)
    if not pkg:
        return None
    return install_command(mgr, pkg)


def bootstrap(report: DoctorReport, *, assume_yes: bool, include_optional: bool = False) -> list[tuple[str, int]]:
    """Run install commands for missing deps. Returns (dep_name, returncode) pairs.

    Only runs when ``assume_yes`` is True (the caller gates interactive confirmation).
    """
    results: list[tuple[str, int]] = []
    targets = list(report.missing_required)
    if include_optional:
        targets += report.missing_optional
    for status in targets:
        if not status.install_cmd:
            results.append((status.dep.name, 127))
            continue
        if not assume_yes:
            results.append((status.dep.name, -1))  # -1 = not run (needs confirmation)
            continue
        try:
            res = subprocess.run(status.install_cmd, timeout=600)
            results.append((status.dep.name, res.returncode))
        except (OSError, subprocess.SubprocessError):
            results.append((status.dep.name, 1))
    return results
