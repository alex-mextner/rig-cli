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
    # `_python_present` mocked False simulates "nothing installed" for the presence gate --
    # but `_versioned_install_spec` does its OWN independent `textual_too_old()` read (a
    # review finding: pin it explicitly here too, or this test only passes by coincidence
    # on a machine whose REAL textual happens to satisfy the floor -- these tests verify
    # the COMMAND SHAPE, not the version-constraint logic, which is covered separately).
    monkeypatch.setattr(doctor, "_python_present", lambda name: False)
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)
    # nothing on PATH (uv absent too) → pip --user fallback into THIS interpreter. Pin
    # "not a venv" explicitly (review finding): `_python_install_command` drops `--user`
    # inside a venv, so this command-shape assertion would otherwise only pass by
    # coincidence on whichever interpreter happens to run pytest.
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)
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
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)  # see the sibling test above
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — see the sibling test above
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
    # Pin for consistency with the sibling tests, even though today's assertions (below)
    # are already version/venv-independent so this couldn't flake yet (a review finding,
    # GLM, round 23) — without this, the first person to strengthen the assertion to an
    # exact command shape would silently reintroduce the host-dependence the siblings were
    # fixed for. See test_python_dep_no_syspkg_recommends_pip.
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)
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
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)  # see test_python_dep_no_syspkg_recommends_pip
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
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — see test_python_dep_no_syspkg_recommends_pip
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
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)  # see test_python_dep_no_syspkg_recommends_pip
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — ditto
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

    # pin explicitly (see test_python_dep_no_syspkg_recommends_pip) — this test verifies the
    # COMMAND SHAPE for a genuinely-new-enough textual, not the version-constraint logic.
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — ditto
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # force the pip fallback branch
    cmd = doctor.break_system_packages_command("textual")
    assert cmd == [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "textual"]


def test_break_system_packages_command_for_multiple_packages(monkeypatch):
    import sys

    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)  # see the sibling test above
    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — ditto
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    cmd = doctor.break_system_packages_command_for(["textual", "rich"])
    assert cmd == [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "textual", "rich"]
    assert doctor.break_system_packages_command_for([]) == []


def test_break_system_packages_command_with_uv(monkeypatch):
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)  # see the sibling tests above
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

    from riglib import drift as _drift

    monkeypatch.setattr(cli, "_handle_core_bare", lambda do_fix: False)
    monkeypatch.setattr(_drift, "scan_missing_targets", lambda settings_paths=None: [])
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


def test_textual_too_old_true_below_floor(monkeypatch):
    """Regression for rig-cli#292: a PRESENT textual older than the wizard's real floor (0.66
    — `refresh_bindings()` needs 0.63, `Button(tooltip=...)` needs 0.66, the LATER of the
    two) must be reported too-old, not treated as fine just because it's importable."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55.0")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_at_and_above_floor(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "8.2.8")
    assert doctor.textual_too_old() is False


def test_textual_too_old_true_when_package_not_found(monkeypatch):
    """Regression for a review finding: every REAL caller of `textual_too_old()`
    (`_python_present`, `_tui_importable`, `_missing_tui_deps`) checks `find_spec` FIRST and
    only calls this function once that already succeeded -- so a `PackageNotFoundError` here
    never means "genuinely absent" (that's `find_spec`'s job to catch); it means "importable
    via something unusual (a `.pth`-injected package, a raw source checkout on `PYTHONPATH`)
    with no readable dist-info" -- which can't be vouched for either. The first version of
    this check treated that the same as "fine, launch the wizard" (False); that let an
    importable-but-metadata-less textual bypass the version gate entirely. Must fail CLOSED
    (True), never itself raise -- `textual_too_old()` must never throw, matching
    `_tui_importable`'s own "must never throw" contract."""
    import importlib.metadata

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", _raise)
    assert doctor.textual_too_old() is True


def test_textual_too_old_true_on_other_metadata_read_failures(monkeypatch):
    """Regression for a review finding: the first version of this check caught only
    `PackageNotFoundError`, so a present `textual` with a corrupt/non-UTF-8 `METADATA` file
    (raising `UnicodeDecodeError`/`OSError` from `importlib.metadata.version()`) would
    propagate straight through `textual_too_old()` -> `_tui_importable()`, crashing `rig
    init` with a raw traceback instead of degrading to the documented non-destructive
    preview. Every metadata-reading failure must fail closed, not just the one exception
    type this used to special-case."""

    def _raise(name):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated corrupt METADATA")

    monkeypatch.setattr("importlib.metadata.version", _raise)
    assert doctor.textual_too_old() is True


def test_textual_too_old_true_on_unparseable_version(monkeypatch):
    """Regression for a review finding: a version string with no leading digits at all can't
    be vouched for either way. The first version of this check treated that the same as
    "genuinely not installed" (False -- fine, launch the wizard); that is backwards -- a
    present-but-unparseable version must fail CLOSED (True -- refuse to vouch for it), since
    launching a wizard that then crashes mid-Apply is a far more expensive mistake than
    showing a preview instead."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "not-a-version")
    assert doctor.textual_too_old() is True


def test_textual_too_old_prerelease_below_floor_is_too_old(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55.0rc1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_prerelease_above_floor_is_not_too_old(monkeypatch):
    """A pre-release of a version STRICTLY above the floor's (major, minor) is unambiguous
    either way -- PEP 440 orders any 0.70-anything above any 0.66-anything, pre-release or
    not -- so this doesn't need the floor-exact strictness check below."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.70.0rc1")
    assert doctor.textual_too_old() is False


def test_textual_too_old_prerelease_at_exact_floor_is_too_old(monkeypatch):
    """Regression for a review finding: a naive digit-only parse extracts "66" from "66rc1"
    and treats it as EQUAL to the floor -- but PEP 440 orders a pre-release strictly BEFORE
    its final release (`0.66.0rc1 < 0.66.0`), so it may not yet contain a feature (like
    `Button(tooltip=...)`) added late in that release's cycle. Only a version confidently
    confirmed >= the exact floor release may pass; a pre/dev/post-release tagged AT the
    floor's (major, minor) can't be vouched for and must fail closed."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0rc1")
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66rc1")
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0.dev0")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_for_a_prerelease_of_a_micro_above_the_floor(monkeypatch):
    """Regression for a review finding: truncating the parse to (major, minor) discarded
    the MICRO component entirely -- so a pre-release of a micro version ABOVE the floor
    (e.g. "0.66.1rc1", cut from code already past the 0.66.0 release) collided with the
    exact-floor strictness check and got incorrectly rejected, even though its release
    segment (0, 66, 1) is already strictly greater than the floor's (0, 66) regardless of
    any pre-release suffix -- a real "0.66.1rc1" necessarily already contains everything
    the 0.66.0 release does. Must NOT fail closed here; only a pre-release tagged AT the
    exact floor micro (e.g. "0.66.0rc1", covered above) is genuinely ambiguous."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.1rc1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.2b1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.1.dev0")
    assert doctor.textual_too_old() is False
    # a pre-release of a micro version BELOW the exact floor micro is unaffected by this --
    # 0.66.0 IS the floor, so there is no "micro below the floor" case to test; the floor
    # is defined at (0, 66) which pads to (0, 66, 0), the lowest possible micro.


def test_textual_too_old_false_for_the_exact_final_floor_release(monkeypatch):
    """The plain final release AT the floor (no pre/dev/post suffix anywhere) is the one
    case that must NOT fail closed -- this is the exact version `textual>=0.66` resolves to
    for a fresh install."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.2")
    assert doctor.textual_too_old() is False


def test_textual_too_old_false_for_a_post_release_at_the_exact_floor(monkeypatch):
    """Regression for a review finding: an earlier version of the floor-exact strictness
    check rejected ANY non-digit suffix alike, including a PEP 440 POST-release
    (`0.66.post1`), which sorts AFTER its base release -- not before, like a pre-release.
    Rejecting it created a non-remediable loop: `textual>=0.66` is already satisfied by
    `0.66.post1`, so the printed "upgrade" command is a permanent no-op while the wizard
    keeps refusing to launch."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.post1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0.post1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66-post2")
    assert doctor.textual_too_old() is False
    # a post-release of a version BELOW the floor is still too old
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55.post1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_for_a_local_version_at_the_exact_floor(monkeypatch):
    """Regression for a review finding (independently raised by two reviewers in the same
    round): a PEP 440 LOCAL version segment (`+<label>`, e.g. a from-source/vendor-patched
    build tagged `0.66+vendor.1`) sorts AFTER its base public version -- same reasoning as
    the post-release case above -- but the floor-exact strictness check rejected it the
    same as a pre-release, reopening the exact non-remediable loop this mechanism exists to
    prevent: `textual>=0.66` is already satisfied by `0.66+vendor.1`, so the printed
    "upgrade" command is a permanent no-op while the wizard keeps refusing to launch."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66+vendor.1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0+vendor.1")
    assert doctor.textual_too_old() is False
    # a local version segment combined with a post-release: the local segment (always the
    # LAST part of the string) must be stripped before the post-release check runs.
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.post1+vendor.1")
    assert doctor.textual_too_old() is False
    # a local version segment on a genuine PRE-release is still too old -- only post/local
    # suffixes sort after their base; pre-release still sorts before.
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0rc1+vendor.1")
    assert doctor.textual_too_old() is True
    # a local version of a version BELOW the floor is still too old
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55+vendor.1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_for_a_hyphenated_local_version_label(monkeypatch):
    """Regression for a review finding: PEP 440 allows ".", "-", and "_" as separators
    WITHIN a local version label, not just "." -- an earlier version of the strip regex
    only accepted ".", so un-normalized metadata like "0.66+ubuntu-20.04" (a real-world
    distro-patched-build labeling style) still failed the floor-exact check and reopened
    the remediation loop, even though it's a valid PEP 440 local version that DOES satisfy
    `textual>=0.66`."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66+ubuntu-20.04")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66+deb_11")
    assert doctor.textual_too_old() is False


def test_textual_too_old_rejects_malformed_suffix_even_above_the_floor(monkeypatch):
    """Regression for a review finding: an earlier version only validated the SUFFIX (what
    comes after the release segment) when the release segment was EXACTLY the floor -- a
    genuinely malformed/adversarial version whose leading digit run happened to compare
    ABOVE the floor sailed through with no suffix validation at all, e.g. a hand-crafted or
    corrupt `Version:` metadata field that isn't a real PEP 440 version at all. The suffix
    must be validated as a RECOGNIZED PEP 440 modifier (or empty) unconditionally, not just
    at the exact floor."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "999not-a-version")
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.67+bad?")
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.1garbage")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_for_legacy_post_release_spellings(monkeypatch):
    """PEP 440 accepts "r"/"rev" as legacy aliases for "post" (both normalize to ".postN"
    and sort AFTER their base release) -- previously documented as a deliberately-accepted
    gap since real PyPI packages essentially never use them, closed for free by the
    suffix-grammar redesign that also fixed the malformed-suffix-above-floor bug."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66r1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66rev1")
    assert doctor.textual_too_old() is False
    # a legacy post-release spelling of a version BELOW the floor is still too old
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55r1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_false_for_a_dev_release_of_a_post_release(monkeypatch):
    """Regression for a review finding (Codex + k3, round 16, independently): PEP 440's
    canonical grammar orders modifiers release[pre][post][dev][local] -- post BEFORE dev --
    and `packaging.version.Version` accepts "0.66.post1.dev1" (a dev release of a post
    release) in exactly that order. An earlier version of `_TEXTUAL_VERSION_SUFFIX_RE`
    anchored the groups pre?dev?post?, so this well-formed string failed `fullmatch`
    entirely and fell through to fail-closed on a version that both satisfies the floor
    AND is not remediable (the printed `textual>=0.66` upgrade command is already
    satisfied -- a permanent non-remediable loop, the exact class this whole mechanism
    exists to close).

    A dev-of-post also sorts differently from a BARE dev release: PEP 440 precedence is
    release < release.postN.devM < release.postN -- a dev-of-post is already PAST the base
    release doorway even before the post fully resolves, unlike a bare dev PREVIEW of the
    base release (which sorts BEFORE it). `textual_too_old` must track that distinction,
    not treat every dev marker identically regardless of an accompanying post."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.post1.dev1")
    assert doctor.textual_too_old() is False
    # a bare dev preview (no post) at the exact floor is still NOT confirmed new enough
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.dev1")
    assert doctor.textual_too_old() is True
    # a dev-of-post BELOW the floor is still too old — the post/dev combo doesn't override
    # the release-segment comparison itself
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55.post1.dev1")
    assert doctor.textual_too_old() is True
    # a dev-of-a-pre-release (both pre and dev, no post) still sorts strictly BEFORE —
    # `pre` alone is enough to fail closed regardless of the refined dev/post interaction
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66a1.dev1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_local_label_matching_a_prerelease_word_is_not_misdetected(monkeypatch):
    """Regression for a review finding on the suffix-grammar redesign ITSELF (caught before
    it ever shipped, via adversarial pre-commit testing): naively `re.search`-ing the raw
    suffix for a pre-release marker false-flags a LOCAL version label that merely CONTAINS
    one of the single-letter markers as a substring -- e.g. "+ubuntu-20.04" contains "b",
    and "beta" is itself a valid (if unusual) local-version label. The pre/dev-release
    check must read from the STRUCTURED regex match's named groups, not re-search the raw
    string."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0+beta")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66+ubuntu-20.04")
    assert doctor.textual_too_old() is False


def test_textual_too_old_accepts_a_leading_v_prefix(monkeypatch):
    """Regression for a review finding (Codex + k3, round 17, independently): PEP 440
    permits an optional leading "v"/"V" on the public version identifier itself
    (`packaging.version.Version("v0.66.0")` normalizes to "0.66.0"). Real PyPI/setuptools
    dist-info essentially never carries this prefix, but a hand-rolled or very old
    toolchain's `Version:` header could -- and rejecting it recreates the exact
    non-remediable loop this parser exists to close elsewhere (post-releases, local
    versions, dev-of-post, implicit posts): the printed `textual>=0.66` upgrade command
    is already satisfied, so the "too old" verdict could never self-resolve."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "v0.66.0")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "V0.66")
    assert doctor.textual_too_old() is False  # case-insensitive, per PEP 440
    monkeypatch.setattr("importlib.metadata.version", lambda name: "v0.65.9")
    assert doctor.textual_too_old() is True  # below the floor, prefix stripped either way
    monkeypatch.setattr("importlib.metadata.version", lambda name: "v1!0.1")
    assert doctor.textual_too_old() is False  # v-prefix composes with epoch, not just release
    # a "v" that ISN'T actually a version prefix (garbage) still fails closed, not crashes
    monkeypatch.setattr("importlib.metadata.version", lambda name: "version-string")
    assert doctor.textual_too_old() is True


def test_textual_too_old_tolerates_surrounding_whitespace(monkeypatch):
    """Regression for a review finding (Codex + GLM, round 21, independently):
    `packaging.version.Version` (what pip/uv actually use to decide whether
    `textual>=0.66` is satisfied) tolerates surrounding whitespace in the version string.
    An email-parsed `Version:` header can preserve a stray leading/trailing space (a
    hand-edited or non-standard-toolchain METADATA file) -- without stripping it first,
    every regex below fails on the unexpected whitespace and this reports "too old" for a
    version that both satisfies the floor AND is not remediable (the printed
    `textual>=0.66` is already satisfied per pip) -- the same packaging-tolerance
    divergence class the v-prefix fix closes, just a different form of it."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66 ")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: " 0.66")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "\t0.66\n")
    assert doctor.textual_too_old() is False
    # whitespace-padding doesn't grant a free pass for a version genuinely below the floor
    monkeypatch.setattr("importlib.metadata.version", lambda name: " 0.65.9 ")
    assert doctor.textual_too_old() is True


def test_textual_too_old_epoch_wins_unconditionally(monkeypatch):
    """Regression for a review finding: PEP 440's EPOCH prefix ("N!", e.g. "1!0.1") sorts
    BEFORE and ABSOLUTELY above everything else -- any non-zero epoch outranks the floor's
    implicit epoch 0, regardless of how small the release segment looks afterward. An
    earlier, less-strict version of this parser got this right BY ACCIDENT (an unrecognized
    "!" fell through to "strictly above floor" before suffix validation existed); making
    the suffix validation unconditional (to close the malformed-garbage-above-floor gap)
    silently broke that accident -- epoch needs its own explicit handling."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1!0.1")
    assert doctor.textual_too_old() is False  # epoch 1 wins even though "0.1" looks tiny
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1!0.66")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "2!0.1.dev0")
    assert doctor.textual_too_old() is False  # epoch wins regardless of suffix too
    # explicit epoch 0 is semantically identical to no epoch -- doesn't get a free pass
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0!0.66")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0!0.55")
    assert doctor.textual_too_old() is True
    # a garbage (non-numeric) epoch prefix still fails closed
    monkeypatch.setattr("importlib.metadata.version", lambda name: "abc!0.66")
    assert doctor.textual_too_old() is True


def test_textual_too_old_malformed_tail_after_a_nonzero_epoch_still_fails_closed(monkeypatch):
    """Regression for a review finding (Codex, round 16): a non-zero epoch must NOT short-
    circuit to "new enough" before the rest of the string is confirmed well-formed -- a
    corrupt header like "1!0.1garbage" has a valid-looking epoch digit but an unparseable
    remainder, and the fail-closed contract this whole function documents (every malformed
    input returns True) must still hold even though the epoch alone would otherwise win."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1!0.1garbage")
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1!not-a-version")
    assert doctor.textual_too_old() is True
    # a genuinely well-formed tail after the epoch still wins, unaffected by the fix above
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1!0.1")
    assert doctor.textual_too_old() is False


def test_textual_too_old_false_for_an_implicit_post_release_at_the_exact_floor(monkeypatch):
    """Regression for a review finding: PEP 440 normalizes a bare trailing "-N" (no literal
    "post") TO ".postN" -- some packaging tools write the un-normalized metadata form
    directly (e.g. "0.66.0-1"). It sorts AFTER its base release exactly like the explicit
    ".postN" form, but the strip regex required the literal word "post", so this form still
    got rejected -- the same non-remediable loop, a third time."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0-1")
    assert doctor.textual_too_old() is False
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66-2")
    assert doctor.textual_too_old() is False
    # a hyphenated PRE-release marker (not a bare digit) must still fail closed
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0-rc1")
    assert doctor.textual_too_old() is True
    # an implicit post-release of a version BELOW the floor is still too old
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.55.0-1")
    assert doctor.textual_too_old() is True


def test_textual_too_old_rejects_non_ascii_digits(monkeypatch):
    """Regression for a review finding: Python's bare `\\d` (and `int()`) accept non-ASCII
    Unicode decimal digits, so a hand-crafted/corrupt metadata value using e.g. Arabic-Indic
    digits for "63" could otherwise smuggle a passing comparison past this gate. Must fail
    closed on anything outside ASCII 0-9."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.٦٣")  # "0.٦٣"
    assert doctor.textual_too_old() is True


def test_textual_too_old_rejects_non_ascii_digits_in_post_release_suffix(monkeypatch):
    """Regression for a review finding: the leading-digit extraction was hardened to
    ASCII-only `[0-9]+`, but the SIBLING post-release/implicit-post-release strip regexes
    still used the bare Unicode-permissive `\\d` -- silently reopening the exact
    non-ASCII-digit-smuggling class the leading-digit fix closed. A version like
    "0.66.post١" (an Arabic-Indic "1") would strip via `\\d*` and then pass as a clean
    "0.66", not too old, even though it is not a confidently-parseable version."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.post١")  # Arabic-Indic 1
    assert doctor.textual_too_old() is True
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.66.0-١")  # implicit post
    assert doctor.textual_too_old() is True


def test_textual_too_old_handles_oversized_numeric_component(monkeypatch):
    """Regression for a review finding: an earlier version of this function wrapped only the
    `importlib.metadata.version()` call in a try/except, leaving the parse loop's `int()`
    call unguarded. Python rejects integer-string conversions past a length limit (the
    CVE-2020-10735 fix, default 4300 digits) -- a maliciously/corruptly oversized all-digit
    version component would otherwise raise an uncaught ValueError straight out of this
    "must never throw" predicate, crashing `rig init`/`rig doctor` instead of degrading to
    the documented non-destructive preview."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0." + "6" * 5000)
    assert doctor.textual_too_old() is True


def test_textual_too_old_handles_none_version(monkeypatch):
    """Regression for a review finding: `Distribution.version` can return `None` (a corrupt
    dist-info with no `Version:` header) instead of raising -- `None.split(".")` would
    otherwise crash with an uncaught AttributeError, violating `_tui_importable()`'s own
    "must never throw" contract."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: None)
    assert doctor.textual_too_old() is True  # can't verify -> fail closed, same as unparseable


def test_versioned_install_spec_adds_floor_for_too_old_textual(monkeypatch):
    """Regression for the "remediation loop" review finding: a bare `pip install textual` /
    `uv pip install textual` against an already-installed-but-old textual reports "already
    satisfied" and installs nothing -- the printed command must carry a version floor so
    pip/uv actually resolves and installs a newer release."""
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    assert doctor._versioned_install_spec("textual") == "textual>=0.66"


def test_versioned_install_spec_bare_name_otherwise(monkeypatch):
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)
    assert doctor._versioned_install_spec("textual") == "textual"
    # a non-textual dep is never version-constrained, regardless of textual_too_old()
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    assert doctor._versioned_install_spec("rich") == "rich"


def test_textual_upgrade_command_targets_this_interpreter_with_the_floor(monkeypatch):
    """Regression for a review finding: the non-PEP-668 `rig init` degrade path used to
    only ever suggest reinstalling rig (`pipx install rig-cli`), never a direct upgrade of
    an already-too-old textual in the CURRENT interpreter -- this is the command that
    closes that gap. Must NOT carry `--break-system-packages` (that's
    `break_system_packages_command`'s job, for the externally-managed case only)."""
    import sys

    monkeypatch.setattr(doctor.sys, "base_prefix", doctor.sys.prefix)  # pin "not a venv" — see below
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # force the pip fallback shape
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    cmd = doctor.textual_upgrade_command()
    assert cmd == [sys.executable, "-m", "pip", "install", "--user", "textual>=0.66"]
    assert "--break-system-packages" not in cmd


def test_textual_upgrade_command_omits_user_flag_inside_a_venv(monkeypatch):
    """Review finding (Codex, round 16): `textual_upgrade_command()` is the DIRECT remediation
    printed for a present-but-too-old textual — and rig's own RECOMMENDED install shape
    (`pipx install rig-cli` / `uv tool install rig-cli`) is itself a venv. `pip install --user`
    hard-refuses inside a venv ("User site-packages are not visible in this virtualenv"), so
    the previous unconditional `--user` handed exactly that audience a command that fails
    outright whenever `uv` isn't ALSO on PATH — the same remediation-loop class this whole
    mechanism exists to close, reopened by the fallback branch itself."""
    import sys

    monkeypatch.setattr(doctor.sys, "prefix", "/some/venv")
    monkeypatch.setattr(doctor.sys, "base_prefix", "/usr")  # prefix != base_prefix ⇒ inside a venv
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # force the pip fallback shape
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    cmd = doctor.textual_upgrade_command()
    assert cmd == [sys.executable, "-m", "pip", "install", "textual>=0.66"]
    assert "--user" not in cmd


def test_break_system_packages_command_for_upgrades_too_old_textual(monkeypatch):
    """End-to-end through the actual command builder callers use: the printed/executed
    fallback command must contain the version-constrained spec, not a bare "textual" that
    would silently no-op against an already-installed old version."""
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    cmd = doctor.break_system_packages_command_for(["textual"])
    assert "textual>=0.66" in cmd
    assert "textual" not in cmd  # never the bare, unconstrained form
    assert "--break-system-packages" in cmd


def test_python_present_textual_false_when_too_old(monkeypatch):
    """`_python_present("textual")` must fold the version check in -- a present-but-too-old
    textual reports as absent, so `diagnose()`'s existing MISSING -> install-command path is
    what the user acts on (rather than a silently-wrong "present" status)."""
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    assert doctor._python_present("textual") is False
    # a non-textual dep is unaffected by the textual-specific version gate
    assert doctor._python_present("rich") is True


def test_install_cmd_bypasses_pacman_when_textual_is_too_old(monkeypatch):
    """Regression for the "remediation loop" review finding reopening on Arch: `pacman -S
    python-textual` only touches SYSTEM site-packages, so it does nothing when rig runs from
    a pipx/uv venv (or when a stale venv shadows a perfectly current system package). A
    CONFIRMED-present-but-too-old textual (`find_spec` succeeds -- explicitly mocked here,
    not relying on this test machine's own textual install) must route through the
    interpreter-targeted install unconditionally, bypassing the `pacman` -> `python-textual`
    mapping entirely -- otherwise `pacman -S python-textual` "succeeds" (rc=0) while the
    RUNNING interpreter's old textual, and the "missing" report, are both untouched."""
    import sys

    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    textual_dep = next(d for d in doctor.DEPENDENCIES if d.name == "textual")
    cmd = doctor._install_cmd_for(textual_dep, OsInfo("linux", "pacman", "Arch"))
    assert cmd is not None
    assert "python-textual" not in cmd  # never the pacman system-package form
    assert "textual>=0.66" in cmd
    assert sys.executable in cmd  # targets THIS interpreter, not the system one


def test_install_cmd_uses_pacman_when_textual_is_genuinely_absent(monkeypatch):
    """Regression for a review finding: an EARLIER version of the too-old bypass fired on
    `textual_too_old()` alone, which ALSO returns True for a genuinely absent textual
    (`PackageNotFoundError` fails closed) -- so it silently dead-coded the pacman mapping for
    absent textual too, regressing Arch users from a working `pacman -S python-textual` to an
    interpreter-targeted install that fails on a PEP-668-marked system Python. `find_spec`
    returning None (genuinely absent, not merely "too old") must keep using the OS mapping
    REGARDLESS of what `textual_too_old()` reports -- this test forces `textual_too_old` to
    its REAL-WORLD value for the absent case (True, via `PackageNotFoundError`) to prove the
    `find_spec` check is what actually gates the bypass, not a mocked shortcut."""
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    textual_dep = next(d for d in doctor.DEPENDENCIES if d.name == "textual")
    cmd = doctor._install_cmd_for(textual_dep, OsInfo("linux", "pacman", "Arch"))
    assert cmd == ["sudo", "pacman", "-S", "--noconfirm", "python-textual"]


def test_diagnose_offers_pep668_fallback_for_too_old_textual_on_pacman(monkeypatch):
    """Regression for a review finding: `diagnose()`'s PEP-668-fallback gate
    (`targets_this_interpreter`) used to be a SEPARATELY re-derived condition from
    `_install_cmd_for`'s own branch -- when the too-old-textual bypass was added to the
    latter, the former was never updated to match, so on an externally-managed
    interpreter `rig doctor` printed an install command that refuses under PEP 668 with NO
    `--break-system-packages` alternative shown (the "remediation loop" resurrected in
    exactly the one case this whole mechanism exists to close). Both must go through the
    SAME `_targets_this_interpreter` helper now."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_python_present", lambda name: name != "textual")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor, "textual_too_old", lambda: True)
    monkeypatch.setattr(doctor, "externally_managed", lambda: True)
    report = doctor.diagnose(OsInfo("linux", "pacman", "Arch"))
    textual = next(s for s in report.statuses if s.dep.name == "textual")
    assert not textual.present
    assert textual.install_cmd is not None
    assert "python-textual" not in textual.install_cmd  # interpreter-targeted, not pacman
    assert textual.pep668_fallback is not None, (
        "PEP-668 fallback must be offered for the interpreter-targeted install"
    )
    assert "--break-system-packages" in textual.pep668_fallback
    assert "textual>=0.66" in textual.pep668_fallback


def test_python_present_textual_true_when_new_enough(monkeypatch):
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor, "textual_too_old", lambda: False)
    assert doctor._python_present("textual") is True
