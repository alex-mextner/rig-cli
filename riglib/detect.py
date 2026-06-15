"""Environment, project, OS, and package-manager detection — stdlib only.

Two jobs:

1. **Project/env detection** (``detect_environment``) — what stack and project type the
   current repo is, where the skills/hooks dirs live, whether the global dispatcher is
   installed, ``gh`` auth. Drives wizard defaults and ``rig status``.
2. **OS + package-manager detection** (``detect_os``, ``detect_package_manager``) — for
   ``rig doctor`` dependency bootstrap: pick the right install command per platform
   (brew on mac; apt/dnf/pacman/zypper on linux).

All detection is pure/observational; nothing here mutates the system. ``which`` lookups
use ``shutil.which`` (stdlib). Package-manager probing is injectable (``which_fn``) so the
detection can be unit-tested without the real binaries present.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

WhichFn = Callable[[str], "str | None"]

# Ordered preference per OS family; first present manager wins.
_LINUX_MANAGERS = ("apt", "dnf", "pacman", "zypper")


@dataclass
class OsInfo:
    system: str  # "darwin" | "linux" | other
    package_manager: str | None  # "brew" | "apt" | "dnf" | "pacman" | "zypper" | None
    pretty: str  # human label


@dataclass
class Environment:
    repo_root: Path
    is_git_repo: bool
    stack: str  # bun-node | python-uv | go | unknown
    project_type: str  # backend | frontend | cli | bot | library | monorepo | unknown
    skills_dirs: dict[str, bool]  # candidate skills dir → exists?
    global_hooks_path: str | None  # git config --global core.hooksPath
    dispatcher_installed: bool
    gh_authed: bool
    is_github_repo: bool
    os: OsInfo
    notes: list[str] = field(default_factory=list)


def detect_os(which_fn: WhichFn | None = None) -> OsInfo:
    which = which_fn or shutil.which
    system = platform.system().lower()
    if system == "darwin":
        mgr = "brew" if which("brew") else None
        return OsInfo(system="darwin", package_manager=mgr, pretty="macOS")
    if system == "linux":
        return OsInfo(
            system="linux",
            package_manager=detect_package_manager(which),
            pretty=_linux_pretty(),
        )
    return OsInfo(system=system or "unknown", package_manager=None, pretty=system or "unknown")


def detect_package_manager(which_fn: WhichFn | None = None) -> str | None:
    """First available Linux package manager, by preference order."""
    which = which_fn or shutil.which
    for mgr in _LINUX_MANAGERS:
        if which(mgr):
            return mgr
    return None


def _linux_pretty() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux"


def install_command(manager: str, package: str) -> list[str]:
    """The non-interactive install command for ``package`` under ``manager``.

    Used by ``rig doctor --yes``. In interactive mode the command is shown for
    confirmation; it is never executed without consent.
    """
    table: dict[str, list[str]] = {
        "brew": ["brew", "install", package],
        "apt": ["sudo", "apt-get", "install", "-y", package],
        "dnf": ["sudo", "dnf", "install", "-y", package],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm", package],
        "zypper": ["sudo", "zypper", "--non-interactive", "install", package],
    }
    if manager not in table:
        raise ValueError(f"unknown package manager: {manager}")
    return table[manager] + ([] if package else [])


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def detect_stack(repo_root: Path) -> str:
    if (repo_root / "package.json").is_file():
        return "bun-node"
    if (repo_root / "pyproject.toml").is_file() or (repo_root / "uv.lock").is_file():
        return "python-uv"
    if (repo_root / "go.mod").is_file():
        return "go"
    return "unknown"


def detect_project_type(repo_root: Path, stack: str) -> str:
    """Best-effort project-type guess from manifests/layout. Cheap heuristics only."""
    # monorepo signals
    if (repo_root / "pnpm-workspace.yaml").is_file() or (repo_root / "turbo.json").is_file():
        return "monorepo"
    pkg = repo_root / "package.json"
    if pkg.is_file():
        try:
            import json

            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data.get("workspaces"), (list, dict)):
            return "monorepo"
        if "bin" in data:
            return "cli"
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        if any(k in deps for k in ("react", "next", "vue", "svelte", "@angular/core")):
            return "frontend"
        if any(k in deps for k in ("telegraf", "grammy", "discord.js", "node-telegram-bot-api")):
            return "bot"
        if any(k in deps for k in ("express", "fastify", "hono", "koa", "@nestjs/core")):
            return "backend"
    if stack == "go":
        return "cli" if (repo_root / "cmd").is_dir() else "backend"
    if stack == "python-uv":
        # a top-level bin/ or a PROJECT-OWNED __main__.py = CLI. Don't recurse into
        # virtualenvs / vendored deps (.venv, site-packages, …) — a dependency's
        # __main__.py would misclassify an ordinary library/backend as a CLI (and the deep
        # glob is slow on big environments).
        _skip = {".venv", "venv", "env", "site-packages", "node_modules", ".git", "build", "dist"}
        has_main = False
        if (repo_root / "__main__.py").is_file():
            has_main = True
        else:
            for top in repo_root.iterdir():
                if top.is_dir() and top.name not in _skip and not top.name.startswith("."):
                    if (top / "__main__.py").is_file():
                        has_main = True
                        break
        if (repo_root / "bin").is_dir() or has_main:
            return "cli"
    return "unknown"


def detect_environment(repo_root: Path | None = None, which_fn: WhichFn | None = None) -> Environment:
    repo_root = (repo_root or Path.cwd()).resolve()
    which = which_fn or shutil.which

    is_git = _git(["rev-parse", "--is-inside-work-tree"], repo_root) == "true"
    if is_git:
        top = _git(["rev-parse", "--show-toplevel"], repo_root)
        if top:
            repo_root = Path(top).resolve()

    stack = detect_stack(repo_root)
    ptype = detect_project_type(repo_root, stack)

    home = Path(os.path.expanduser("~"))
    skills_dirs = {
        str(home / ".agents" / "skills"): (home / ".agents" / "skills").is_dir(),
        str(home / ".claude" / "skills"): (home / ".claude" / "skills").is_dir(),
        str(repo_root / ".agents" / "skills"): (repo_root / ".agents" / "skills").is_dir(),
    }

    global_hooks_path = _git(["config", "--global", "core.hooksPath"], repo_root)
    dispatcher_dir = Path(os.path.expanduser("~/.config/git/global-hooks.d"))
    runner = Path(os.path.expanduser("~/.config/git/run-global-hooks"))
    dispatcher_installed = dispatcher_dir.is_dir() and runner.exists()

    gh_authed = False
    is_github = False
    if which("gh"):
        try:
            res = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            )
            gh_authed = res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            gh_authed = False
    remote = _git(["remote", "get-url", "origin"], repo_root) or ""
    is_github = "github.com" in remote

    return Environment(
        repo_root=repo_root,
        is_git_repo=is_git,
        stack=stack,
        project_type=ptype,
        skills_dirs=skills_dirs,
        global_hooks_path=global_hooks_path or None,
        dispatcher_installed=dispatcher_installed,
        gh_authed=gh_authed,
        is_github_repo=is_github,
        os=detect_os(which),
    )
