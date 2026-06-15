"""OS + package-manager detection (mocked) and project/stack heuristics."""

from __future__ import annotations

import json
from pathlib import Path

from riglib import detect


def _which_factory(present):
    present = set(present)
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_detect_package_manager_preference_order():
    # apt wins over dnf when both present (apt is first in preference)
    which = _which_factory({"apt", "dnf"})
    assert detect.detect_package_manager(which) == "apt"


def test_detect_package_manager_pacman_only():
    assert detect.detect_package_manager(_which_factory({"pacman"})) == "pacman"


def test_detect_package_manager_none():
    assert detect.detect_package_manager(_which_factory(set())) is None


def test_detect_os_darwin_brew(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Darwin")
    info = detect.detect_os(_which_factory({"brew"}))
    assert info.system == "darwin"
    assert info.package_manager == "brew"


def test_detect_os_darwin_no_brew(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Darwin")
    info = detect.detect_os(_which_factory(set()))
    assert info.package_manager is None


def test_detect_os_linux_dnf(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Linux")
    info = detect.detect_os(_which_factory({"dnf"}))
    assert info.system == "linux"
    assert info.package_manager == "dnf"


def test_install_command_per_manager():
    assert detect.install_command("brew", "gh") == ["brew", "install", "gh"]
    assert detect.install_command("apt", "gh") == ["sudo", "apt-get", "install", "-y", "gh"]
    assert detect.install_command("pacman", "gh") == ["sudo", "pacman", "-S", "--noconfirm", "gh"]
    assert detect.install_command("dnf", "gh") == ["sudo", "dnf", "install", "-y", "gh"]
    assert detect.install_command("zypper", "gh") == ["sudo", "zypper", "--non-interactive", "install", "gh"]


def test_detect_stack(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect.detect_stack(tmp_path) == "bun-node"
    (tmp_path / "package.json").unlink()
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert detect.detect_stack(tmp_path) == "go"


def test_detect_project_type_frontend(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19"}}), encoding="utf-8"
    )
    assert detect.detect_project_type(tmp_path, "bun-node") == "frontend"


def test_detect_project_type_cli_from_bin(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"bin": {"x": "./x"}}), encoding="utf-8")
    assert detect.detect_project_type(tmp_path, "bun-node") == "cli"


def test_detect_project_type_monorepo(tmp_path: Path):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n", encoding="utf-8")
    assert detect.detect_project_type(tmp_path, "bun-node") == "monorepo"


def test_detect_python_ignores_venv_main(tmp_path: Path):
    """A __main__.py inside .venv must NOT classify a library as a CLI."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='lib'\n", encoding="utf-8")
    venv_pkg = tmp_path / ".venv" / "lib" / "python3.11" / "site-packages" / "dep"
    venv_pkg.mkdir(parents=True)
    (venv_pkg / "__main__.py").write_text("", encoding="utf-8")
    assert detect.detect_project_type(tmp_path, "python-uv") != "cli"


def test_detect_python_cli_from_project_main(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tool'\n", encoding="utf-8")
    pkg = tmp_path / "tool"
    pkg.mkdir()
    (pkg / "__main__.py").write_text("", encoding="utf-8")
    assert detect.detect_project_type(tmp_path, "python-uv") == "cli"
