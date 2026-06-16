"""missing-target heuristic — a config that references a path/binary that's gone on disk.

Motivating case (from the spec): a hook command in the harness ``settings.json`` points at a
script that no longer exists (the dead-rtk-hook case), which otherwise surfaces only as a
generic harness "PreToolUse error" with no hint of the cause. The scanner names the missing
file + how to regenerate it, and `rig status` / `rig doctor` surface it PROACTIVELY (before it
bites at runtime).

All offline; operates on a fake settings.json under a tmp HOME.
"""

from __future__ import annotations

import json
from pathlib import Path

from riglib import errors
from riglib.missing_target import scan_settings_hooks


def _settings(home: Path, hooks: dict) -> Path:
    cfg = home / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return cfg


def test_scan_flags_dead_hook_command(tmp_path):
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "rtk-hook.py"  # never created
    settings = _settings(
        home,
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone}"}]}
            ]
        },
    )
    findings = scan_settings_hooks(settings)
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, errors.MissingTargetError)
    assert f.exit_code == errors.EXIT_MISSING_TARGET
    assert str(gone) in f.what  # names the missing file
    assert "settings.json" in f.why  # says WHERE it's referenced
    assert f.fix  # carries a concrete regenerate/remove hint


def test_scan_ignores_live_hook_command(tmp_path):
    home = tmp_path / "home"
    live = tmp_path / "live-hook.py"
    live.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {live}"}]}]},
    )
    assert scan_settings_hooks(settings) == []


def test_scan_ignores_bare_binary_on_path(tmp_path):
    # a command that's a plain binary name (resolved via PATH), not an absolute script path,
    # must not be flagged as a missing FILE — we only check absolute path args that look like
    # script files.
    home = tmp_path / "home"
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "gitleaks protect"}]}]},
    )
    assert scan_settings_hooks(settings) == []


def test_scan_ignores_nonexistent_output_arg_when_script_lives(tmp_path):
    # a hook whose SCRIPT exists but which names a not-yet-created runtime output file as a
    # later absolute-path arg must NOT be flagged — the output file is created at run time, not
    # a dead reference. Only the invoked script is the "target" we check.
    home = tmp_path / "home"
    live = tmp_path / "live-hook.py"
    live.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    out_file = tmp_path / "run" / "out.json"  # never created — a runtime output path
    settings = _settings(
        home,
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {live} --out {out_file}"}]}
            ]
        },
    )
    assert scan_settings_hooks(settings) == []


def test_scan_flags_dead_script_even_with_trailing_args(tmp_path):
    # the inverse: a DEAD script is still flagged, and trailing args don't change the verdict.
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "hook.py"  # never created — the script itself is missing
    out_file = tmp_path / "run" / "out.json"
    settings = _settings(
        home,
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone} --out {out_file}"}]}
            ]
        },
    )
    findings = scan_settings_hooks(settings)
    assert len(findings) == 1
    assert str(gone) in findings[0].what
    assert str(out_file) not in findings[0].what  # the output arg is not the reported target


def test_scan_flags_dead_script_under_absolute_interpreter(tmp_path):
    # the motivating rtk-hook form on macOS: the interpreter is itself an ABSOLUTE path
    # (/usr/bin/env, /opt/homebrew/bin/python3). The first absolute token is the interpreter
    # (which exists) — the scanner must skip it and check the real (missing) script after it.
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "rtk-hook.py"  # never created
    for cmd in (f"/usr/bin/env python3 {gone}", f"/opt/homebrew/bin/python3 {gone}"):
        settings = _settings(
            home, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}
        )
        findings = scan_settings_hooks(settings)
        assert len(findings) == 1, cmd
        assert str(gone) in findings[0].what, cmd


def test_scan_ignores_inline_c_code(tmp_path):
    # `python3 -c '<code>'` runs no script FILE — there is no path target to verify, so a
    # nonexistent-looking path embedded in inline code must not be flagged.
    home = tmp_path / "home"
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 -c 'import sys; sys.exit(0)'"}]}]},
    )
    assert scan_settings_hooks(settings) == []


def test_scan_flags_dead_script_when_dash_c_is_script_arg(tmp_path):
    # `-c` AFTER the script is the SCRIPT's own argument, not Python's inline-code flag — a dead
    # script must still be flagged. (Guards against suppressing the check on any trailing `-c`.)
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "hook.py"  # the script itself is missing
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone} -c config.json"}]}]},
    )
    findings = scan_settings_hooks(settings)
    assert len(findings) == 1
    assert str(gone) in findings[0].what


def test_scan_missing_settings_file_is_empty(tmp_path):
    # no settings.json at all → nothing to scan, no error
    assert scan_settings_hooks(tmp_path / "home" / ".claude" / "settings.json") == []


def test_scan_malformed_settings_is_empty(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{not json", encoding="utf-8")
    assert scan_settings_hooks(cfg) == []


def test_status_surfaces_dead_hook(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig status` proactively surfaces a dead hook path in the harness settings.json."""
    import subprocess

    home = tmp_path / "home"
    gone = tmp_path / "gone-hook.py"  # referenced but absent
    _settings(
        home,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone}"}]}]},
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\nagents_md: {enabled: false}\n",
        encoding="utf-8",
    )
    main_rc = __import__("riglib.cli", fromlist=["main"]).main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert str(gone) in out  # the dead hook path is named
    assert "missing" in out.lower()
    # a missing-target makes status non-zero (something IS wrong), distinct from clean
    assert main_rc != 0


def test_doctor_surfaces_dead_hook(tmp_path, capsys, monkeypatch):
    """`rig doctor` proactively surfaces a dead hook path with the missing-target exit code."""
    from riglib.cli import main

    home = tmp_path / "home"
    gone = tmp_path / "gone-hook.py"
    _settings(
        home,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {gone}"}]}]},
    )
    monkeypatch.setenv("HOME", str(home))
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert str(gone) in out
    # doctor returns the missing-target class when deps are otherwise fine OR may already be 1
    # if a required dep is missing on the box; either way it's non-zero and names the dead hook.
    assert rc != 0
