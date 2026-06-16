"""Doctor dependency diagnosis (mocked) + SetupState round-trip."""

from __future__ import annotations

from riglib import doctor
from riglib.detect import OsInfo
from riglib.state import SetupState, default_state


def test_diagnose_marks_missing_with_install_cmd(monkeypatch):
    # pretend nothing is on PATH and no python modules importable
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    os_info = OsInfo(system="linux", package_manager="apt", pretty="Ubuntu")
    report = doctor.diagnose(os_info)
    gh = next(s for s in report.statuses if s.dep.name == "gh")
    assert not gh.present
    assert gh.install_cmd == ["sudo", "apt-get", "install", "-y", "gh"]
    # a required dep is flagged
    assert any(s.dep.name == "git" for s in report.missing_required)


def test_diagnose_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_python_present", lambda name: True)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    assert not report.missing_required
    assert not report.missing_optional


def test_python_dep_no_syspkg_recommends_pip(monkeypatch):
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    # textual has no apt package → pip recommendation into THIS interpreter
    import sys

    report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "textual"]


def test_rich_dep_is_diagnosed_for_stats_tui(monkeypatch):
    """`rig stats show --format tui` needs `rich` (pyproject's [tui] extra ships it), so
    doctor must diagnose/provision it — not just `textual`. (review finding)"""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    rich = next(s for s in report.statuses if s.dep.name == "rich")
    assert not rich.present
    assert not rich.dep.required  # optional, like textual (TUI degrades to plain text)
    # brew has no `rich` formula → pip into THIS interpreter, like textual.
    import sys

    assert rich.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "rich"]
    # and on a manager that DOES package it, the system package is used.
    apt_report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    apt_rich = next(s for s in apt_report.statuses if s.dep.name == "rich")
    assert apt_rich.install_cmd == ["sudo", "apt-get", "install", "-y", "python3-rich"]


def test_bootstrap_not_run_without_yes(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    results = doctor.bootstrap(report, assume_yes=False)
    # rc -1 means "not run, needs confirmation"
    assert all(rc == -1 for _, rc in results)


def test_bootstrap_runs_with_yes(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    calls = []

    class _Res:
        returncode = 0

    def _fake_run(cmd, timeout=None):
        calls.append(cmd)
        return _Res()

    monkeypatch.setattr(doctor.subprocess, "run", _fake_run)
    report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    results = doctor.bootstrap(report, assume_yes=True)
    assert calls  # something was "installed"
    assert all(rc == 0 for _, rc in results if rc != 127)


def test_state_round_trip_yaml():
    data = default_state(agent_tools_source="/x/agent-tools", project_type="cli")
    state = SetupState.from_dict(data)
    text = state.to_yaml()
    import yaml

    reparsed = yaml.safe_load(text)
    assert reparsed["version"] == 1
    assert reparsed["agent_tools_source"] == "/x/agent-tools"
    assert reparsed["skills"]["by_type"]["enable"] == ["cli"]


def test_default_state_is_portable(monkeypatch):
    """The committed default config must not pin machine-specific absolute paths."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    data = default_state(agent_tools_source=None, project_type="cli")
    import yaml

    text = yaml.safe_dump(data)
    assert "agent_tools_source" not in data  # omitted for auto-detected sources
    assert "/Users/" not in text and "/home/" not in text  # no absolute home paths
    disp = data["git_hooks"]["dispatcher"]
    assert disp["dir"].startswith("~/")  # portable ~ path
    assert disp["runner"].startswith("~/")


def test_state_write_has_header(tmp_path):
    state = SetupState.default(project_type="backend")
    out = state.write(tmp_path / "rig.yaml")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# rig.yaml")
    assert "COMMITTED BY DEFAULT" in text
