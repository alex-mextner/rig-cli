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
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .probes import ProbeResult

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
        "the interactive setup wizard (rig init TUI) — a CORE dep, ships with rig",
        kind="python",
        required=True,
        pkg={"brew": "", "apt": "", "dnf": "", "pacman": "python-textual"},
    ),
    Dependency(
        "rich",
        "the `rig stats show --format tui` report — a CORE dep, ships with rig",
        kind="python",
        required=True,
        # All entries empty: always install via pip/uv into rig's own interpreter (sys.executable),
        # not via the distro package manager. A distro `python3-rich` installs into the system
        # Python, which is WRONG when rig runs inside a pipx/uv-tool venv.
        pkg={"brew": "", "apt": "", "dnf": "", "pacman": ""},
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
    # set only for a python-kind dep whose install_cmd targets THIS interpreter directly
    # (no system package mapping) AND that interpreter is PEP-668 externally-managed — the
    # exact shape that makes install_cmd above silently fail on a symlink/dev checkout install.
    pep668_fallback: list[str] | None = None


@dataclass
class DoctorReport:
    os: OsInfo
    statuses: list[DepStatus] = field(default_factory=list)
    #: opt-in ACTIVATION probes (``RIG_OMP_PROBE=1``) — filled by the doctor COMMAND
    #: (``diagnose()`` itself stays a pure offline report builder; the probe spawns a real
    #: model turn and must never ride along with every diagnose() caller).
    probes: list["ProbeResult"] = field(default_factory=list)

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
    # computed once, not per-dep — same interpreter for every python-kind dep this call.
    is_ext_managed = externally_managed()
    for dep in DEPENDENCIES:
        if dep.kind == "python":
            present = _python_present(dep.name)
            location = "importable" if present else None
        else:
            loc = shutil.which(dep.name)
            present = loc is not None
            location = loc
        install_cmd = None if present else _install_cmd_for(dep, os_info)
        fallback = None
        # mirrors _install_cmd_for's OWN condition for "targets this interpreter directly"
        # (no OS package mapping for this manager) — checking the same source fact `_install_cmd_for`
        # branches on, rather than re-deriving install_cmd and comparing by value, so this can't
        # silently drift out of sync if that function's command shape ever changes.
        targets_this_interpreter = dep.kind == "python" and not dep.pkg.get(os_info.package_manager or "")
        if not present and is_ext_managed and install_cmd is not None and targets_this_interpreter:
            fallback = break_system_packages_command(dep.name)
        report.statuses.append(
            DepStatus(
                dep=dep,
                present=present,
                location=location,
                install_cmd=install_cmd,
                pep668_fallback=fallback,
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
    (`pipx install rig-cli` / `uv tool install rig-cli`), not to force a system-wide install.
    Falls back to ``python -m pip install --user`` only when ``uv`` is absent — no worse than before.

    ``package`` is always ``dep.name`` from ``DEPENDENCIES`` (never a bare import name) — every
    current python-kind entry's ``name`` already IS its correct pip distribution name (confirmed
    empirically: ``uv pip install --break-system-packages --dry-run`` resolves cleanly for each).
    A future python dep whose import name differs from its distribution name (e.g. Pillow imports
    as ``PIL``) would need its OWN pip name here, not its import name — this function has no way
    to tell the difference on its own.
    """
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, package]
    return [sys.executable, "-m", "pip", "install", "--user", package]


def externally_managed() -> bool:
    """True when THIS interpreter's stdlib carries PEP 668's ``EXTERNALLY-MANAGED`` marker —
    and this interpreter is NOT itself a virtualenv.

    PEP 668 enforcement only applies OUTSIDE a venv (pip's own check exits early under
    ``running_under_virtualenv()`` before ever looking for the marker). A venv's ``sysconfig``
    stdlib path resolves to the BASE interpreter's stdlib (venvs never copy the stdlib), so a
    marker check alone would misfire for rig's own RECOMMENDED install shape — `pipx install
    rig-cli` / `uv tool install rig-cli`, both venvs — whenever the base Python happens to be a
    marker-carrying Homebrew/distro one. That would misdiagnose a genuinely broken pipx install
    as "PEP-668 refuses" and offer a `--break-system-packages --user` fallback that ALSO fails
    inside a venv (`--user` is rejected there), trading a correct diagnosis for a wrong one.
    ``sys.prefix != sys.base_prefix`` is the standard venv-detection idiom (true for venv/virtualenv/
    pipx/uv-tool installs alike) — check it FIRST so a venv always reports False regardless of
    what the base interpreter's stdlib marker says.

    This checks the currently-running interpreter (``sys.executable`` — the same one
    ``_python_install_command`` targets), so no probing of a different ``python3`` is needed.
    """
    if sys.prefix != sys.base_prefix:
        return False
    stdlib = sysconfig.get_paths().get("stdlib")
    if not stdlib:
        return False
    return (Path(stdlib) / "EXTERNALLY-MANAGED").is_file()


def break_system_packages_command(package: str) -> list[str]:
    """The same command ``_python_install_command`` would produce, plus PEP 668's bypass flag.

    Never run automatically (see the docstring above) — only OFFERED as an explicit,
    human-chosen fallback when the plain install is known to fail because this interpreter is
    externally-managed, for someone (like rig's own maintainer) intentionally installing into a
    local dev checkout's system Python rather than switching to a pipx/uv-tool managed install.
    """
    cmd = _python_install_command(package)
    return cmd[:-1] + ["--break-system-packages", cmd[-1]]


def break_system_packages_command_for(packages: list[str]) -> list[str]:
    """Like ``break_system_packages_command``, but installs several packages in ONE command.

    ``uv pip install`` / ``pip install`` both accept multiple trailing package args, so this
    just widens the single-package command's tail instead of duplicating its shape.
    """
    if not packages:
        return []
    cmd = _python_install_command(packages[0])
    return cmd[:-1] + ["--break-system-packages", *packages]


def _install_cmd_for(dep: Dependency, os_info: OsInfo) -> list[str] | None:
    mgr = os_info.package_manager
    # python deps that have no system package → install into THIS interpreter (sys.executable),
    # not a bare `python3` that may be a different runtime than the one rig runs under. Prefer uv
    # (the user's toolchain) over a bare `pip` — see _python_install_command for the PEP-668 caveat.
    # Checked BEFORE the `not mgr` bail-out below: unlike a system-package install, this path
    # never needed a package manager in the first place, so an undetected `mgr` (an unrecognized
    # OS/distro) must not silently swallow it — review finding: it did, on install.sh's own
    # promised "rig doctor shows the exact command" contract for exactly this dep class.
    if dep.kind == "python":
        pkg = dep.pkg.get(mgr or "")
        if not pkg:  # empty string means "no system package, use uv/pip into this interpreter"
            return _python_install_command(dep.name)
        return install_command(mgr, pkg)
    if not mgr:
        return None
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
