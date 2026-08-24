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


def test_python_dep_install_cmd_present_even_with_no_package_manager(monkeypatch):
    """review finding: on an OS with no DETECTED package manager (unrecognized distro etc.),
    `_install_cmd_for` used to bail out to None unconditionally before ever checking dep.kind —
    silently dropping the install command (and therefore the PEP-668 fallback) for python deps
    that never needed a package manager in the first place, install.sh's own `rig doctor` promise
    notwithstanding."""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    import sys

    report = doctor.diagnose(OsInfo("linux", None, "Unknown Linux"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.install_cmd == [sys.executable, "-m", "pip", "install", "--user", "textual"]
    # and a binary dep with a real package mapping still correctly gets no command — there is
    # genuinely no way to install `gh` without a KNOWN package manager.
    gh = next(s for s in report.statuses if s.dep.name == "gh")
    assert gh.install_cmd is None


def test_pep668_fallback_present_even_with_no_package_manager(monkeypatch):
    """The PEP-668 fallback itself must survive the same no-manager edge case, since it's gated
    on `install_cmd is not None` — this is the exact scenario the review finding flagged."""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "externally_managed", lambda: True)
    report = doctor.diagnose(OsInfo("linux", None, "Unknown Linux"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.pep668_fallback is not None
    assert "--break-system-packages" in textual.pep668_fallback


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


def test_pep668_fallback_offered_when_externally_managed(monkeypatch):
    """The one-liner `_python_install_command` produces (uv/pip into THIS interpreter) silently
    refuses on a PEP-668 externally-managed Python — a local dev checkout install (install.sh,
    not pipx) hits exactly this. `diagnose()` must surface an explicit --break-system-packages
    fallback for that shape, not leave the user with only the command that will fail."""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "externally_managed", lambda: True)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.pep668_fallback == [
        __import__("sys").executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--break-system-packages",
        "textual",
    ]
    # a dep that DOES have a real system package (lefthook on brew) never gets this fallback —
    # it isn't installing into this interpreter at all, so PEP-668 is irrelevant to it.
    lefthook = next(s for s in report.statuses if s.dep.name == "lefthook")
    assert lefthook.pep668_fallback is None


def test_no_pep668_fallback_when_managed_normally(monkeypatch):
    """On a normal (non-externally-managed) interpreter, the plain install_cmd already works —
    no fallback should be offered (nothing to bypass)."""
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "externally_managed", lambda: False)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert textual.pep668_fallback is None


def test_break_system_packages_command_inserts_flag_before_package(monkeypatch):
    import sys

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # force the pip fallback branch
    cmd = doctor.break_system_packages_command("textual")
    assert cmd == [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "textual"]


def test_break_system_packages_command_for_multiple_packages(monkeypatch):
    import sys

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    cmd = doctor.break_system_packages_command_for(["textual", "rich"])
    assert cmd == [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "textual", "rich"]
    assert doctor.break_system_packages_command_for([]) == []


def test_break_system_packages_command_with_uv(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None
    )
    import sys

    cmd = doctor.break_system_packages_command("textual")
    assert cmd == ["uv", "pip", "install", "--python", sys.executable, "--break-system-packages", "textual"]


def test_externally_managed_detects_marker_file(tmp_path, monkeypatch):
    marker_dir = tmp_path / "lib" / "python3.99"
    marker_dir.mkdir(parents=True)
    (marker_dir / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    monkeypatch.setattr(
        doctor.sysconfig, "get_paths", lambda: {"stdlib": str(marker_dir)}
    )
    # pin "not in a venv" explicitly — this test targets the marker check itself, not the venv
    # guard (covered separately below); running pytest itself from an activated venv must not
    # flip this test's outcome.
    monkeypatch.setattr(doctor.sys, "prefix", "/same/python")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/same/python")
    assert doctor.externally_managed() is True


def test_externally_managed_false_without_marker(tmp_path, monkeypatch):
    marker_dir = tmp_path / "lib" / "python3.99"
    marker_dir.mkdir(parents=True)
    monkeypatch.setattr(
        doctor.sysconfig, "get_paths", lambda: {"stdlib": str(marker_dir)}
    )
    monkeypatch.setattr(doctor.sys, "prefix", "/same/python")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/same/python")
    assert doctor.externally_managed() is False


def test_externally_managed_false_inside_a_venv_even_with_marker(tmp_path, monkeypatch):
    """A venv's sysconfig stdlib path resolves to the BASE interpreter's stdlib (venvs never
    copy the stdlib) — so a marker there does NOT mean PEP 668 applies to THIS (venv)
    interpreter. `pipx install rig-cli` / `uv tool install rig-cli` are both venvs; without this
    guard a marker-carrying base Python would misdiagnose rig's own recommended install shape
    as PEP-668-blocked and offer a `--break-system-packages --user` fallback that fails inside
    a venv anyway (`--user` is rejected there). sys.prefix != sys.base_prefix is the standard
    venv-detection idiom — simulate it directly rather than spawning a real venv."""
    marker_dir = tmp_path / "lib" / "python3.99"
    marker_dir.mkdir(parents=True)
    (marker_dir / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    monkeypatch.setattr(doctor.sysconfig, "get_paths", lambda: {"stdlib": str(marker_dir)})
    monkeypatch.setattr(doctor.sys, "prefix", "/fake/venv")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/fake/base-python")
    assert doctor.externally_managed() is False


def test_externally_managed_still_true_outside_a_venv_with_marker(tmp_path, monkeypatch):
    """Sibling of the venv-false test above: the SAME marker, but sys.prefix == sys.base_prefix
    (not in a venv — the local install.sh dev-checkout shape) still reports True."""
    marker_dir = tmp_path / "lib" / "python3.99"
    marker_dir.mkdir(parents=True)
    (marker_dir / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    monkeypatch.setattr(doctor.sysconfig, "get_paths", lambda: {"stdlib": str(marker_dir)})
    monkeypatch.setattr(doctor.sys, "prefix", "/same/python")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/same/python")
    assert doctor.externally_managed() is True


def test_externally_managed_false_when_sysconfig_lacks_stdlib_key(monkeypatch):
    """`sysconfig.get_paths()` without a "stdlib" key (unusual, but `.get` handles it) must fail
    open — no marker path to check means no marker found, not an exception."""
    monkeypatch.setattr(doctor.sysconfig, "get_paths", lambda: {})
    monkeypatch.setattr(doctor.sys, "prefix", "/same/python")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/same/python")
    assert doctor.externally_managed() is False


def test_print_dep_statuses_shows_pep668_fallback(monkeypatch, capsys):
    """`rig doctor`'s (no --yes) listing must surface the --break-system-packages fallback line
    for a dep that has one, not just the plain install_cmd that's known to fail there."""
    from riglib import cli

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor, "externally_managed", lambda: True)
    report = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    cli._print_dep_statuses(report)
    out = capsys.readouterr().out
    assert "PEP-668 externally-managed" in out
    assert "pipx install rig-cli" in out
    assert "--break-system-packages" in out


def test_cmd_doctor_yes_shows_pep668_fallback_on_failed_install(monkeypatch, capsys):
    """`rig doctor --yes`, after the plain install command it ran fails (rc != 0) on a
    PEP-668 interpreter, must print the actionable fallback — not leave the user with a bare
    "rc=1" and no path forward."""
    import argparse

    from riglib import cli
    from riglib.doctor import DoctorReport

    monkeypatch.setattr(cli, "_handle_core_bare", lambda do_fix: False)
    monkeypatch.setattr(cli, "_scan_missing_targets", lambda settings_paths=None: [])
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor, "externally_managed", lambda: True)

    real = doctor.diagnose(OsInfo("darwin", "brew", "macOS"))
    report = DoctorReport(os=real.os, statuses=real.statuses)
    import riglib.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "diagnose", lambda: report)

    class _FailRes:
        returncode = 1

    monkeypatch.setattr(doctor_mod.subprocess, "run", lambda cmd, timeout=None: _FailRes())

    args = argparse.Namespace(yes=True, optional=False, fix=False)
    rc = cli.cmd_doctor(args)
    out = capsys.readouterr().out
    assert rc != 0
    assert "if this interpreter refused it (PEP-668)" in out
    assert "--break-system-packages" in out


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
    # first line is the editor schema modeline, followed by the policy/ownership contract.
    assert text.startswith("# yaml-language-server: $schema=schema/rig.schema.json")
    assert "# rig.yaml" in text
    assert "COMMITTED BY DEFAULT" in text
    assert "global defaults in ~/.config/rig/config.yaml" in text
    assert "rig lint rules" in text
    assert "rig config set <dot.path> <value>" in text
    assert "--commit" in text
    assert "Generated/carried target files are outputs" in text
