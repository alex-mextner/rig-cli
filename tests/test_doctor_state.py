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
    # nothing on PATH (uv absent too) → pip --user fallback into THIS interpreter.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    # textual has no apt package → pip recommendation into THIS interpreter
    import sys

    report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "textual"]


def test_python_dep_prefers_uv_when_available(monkeypatch):
    """When `uv` is on PATH, a python dep with no system package installs via `uv pip install`
    into THIS interpreter — never a bare `pip install` that fails on PEP-668 externally-managed
    Pythons (the toolchain rig users standardize on)."""
    import sys

    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    # uv present; everything else absent (so the python deps fall to the uv/pip branch).
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None
    )
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.install_cmd == ["uv", "pip", "install", "--python", sys.executable, "textual"]
    # and it is NOT the bare `pip install`/`--user` form that PEP-668 blocks.
    assert textual.install_cmd[:2] == ["uv", "pip"]


def test_rich_dep_is_diagnosed_for_stats_tui(monkeypatch):
    """`rig stats show --format tui` needs `rich` (a core dep in pyproject, shipped with rig), so
    doctor must diagnose/provision it — not just `textual`. (review finding)"""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    rich = next(s for s in report.statuses if s.dep.name == "rich")
    assert not rich.present
    assert rich.dep.required  # CORE dep — ships with rig, required like textual
    # brew has no `rich` formula → pip into THIS interpreter, like textual.
    import sys

    assert rich.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "rich"]
    # ALSO on apt: rich has no system-package entry (empty pkg map) so it ALWAYS installs via
    # pip into rig's own interpreter, not via apt-get — that would install into system Python
    # and leave rig's venv without rich.
    apt_report = doctor.diagnose(OsInfo("linux", "apt", "Ubuntu"))
    apt_rich = next(s for s in apt_report.statuses if s.dep.name == "rich")
    assert apt_rich.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "rich"]


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


def test_default_state_includes_stack_when_provided():
    data = default_state(agent_tools_source=None, project_type="frontend", stack="frontend/ts/react")
    assert data["stack"] == "frontend/ts/react"
    # validation accepts the scaffolded config
    from riglib import config

    config.validate(data)


def test_default_state_omits_stack_when_unset():
    data = default_state(agent_tools_source=None, project_type="unknown")
    assert "stack" not in data  # undetected → absent (soft-require warning), not invented


def test_state_write_has_header(tmp_path):
    state = SetupState.default(project_type="backend")
    out = state.write(tmp_path / "rig.yaml")
    text = out.read_text(encoding="utf-8")
    # first line is the editor schema modeline (completion/validation), then the rig.yaml header
    assert text.startswith("# yaml-language-server: $schema=schema/rig.schema.json")
    assert "# rig.yaml" in text
    assert "COMMITTED BY DEFAULT" in text
