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


def test_scan_ignores_module_invocation(tmp_path):
    # `python3 -m my_hook --out /tmp/result.json` runs a MODULE, not a script FILE — there is no
    # script path to verify. The later absolute `--out` arg is a runtime output path, not a hook
    # script, so a module-based hook must NOT be flagged (it would cry wolf on a healthy hook).
    home = tmp_path / "home"
    out_file = tmp_path / "run" / "result.json"  # never created — a runtime output path
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"python3 -m my_hook --out {out_file}"}
        ]}]},
    )
    assert scan_settings_hooks(settings) == []


def test_scan_ignores_module_invocation_under_absolute_interpreter(tmp_path):
    # same, but the interpreter is an absolute path (the macOS form) — still no script to verify.
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "x.py"  # an absolute path that doesn't exist, AFTER -m <module>
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"/opt/homebrew/bin/python3 -m pkg.hook {gone}"}
        ]}]},
    )
    assert scan_settings_hooks(settings) == []


def test_scan_flags_dead_script_when_dash_m_is_script_arg(tmp_path):
    # `-m` AFTER the script is the SCRIPT's own argument, not Python's module flag — a dead
    # script must still be flagged (mirror of the `-c`-as-script-arg guard).
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "hook.py"  # the script itself is missing
    settings = _settings(
        home,
        {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"python3 {gone} -m somearg"}
        ]}]},
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


def _hooks_settings(path: Path, command: str) -> None:
    """Write a settings.json at an ARBITRARY path with one PreToolUse hook command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}}
        ),
        encoding="utf-8",
    )


def test_harness_settings_paths_resolves_action_targets(tmp_path):
    """`_harness_settings_paths` reads the settings file from the harness-writing actions.

    The scan must follow where rig ACTUALLY provisions hooks — the apply_harness /
    provision_permissions / register_hook_bridge action targets — not a hardcoded path.
    """
    from riglib.cli import _harness_settings_paths
    from riglib.plan import Action, InstallPlan

    repo_settings = tmp_path / "repo" / ".claude" / "settings.json"  # a NON-default location
    user_settings = tmp_path / "home" / ".claude" / "settings.json"
    bridge_settings = tmp_path / "bridge" / ".claude" / "settings.json"
    cc = {"kind": "claude-code"}
    plan = InstallPlan()
    plan.actions = [
        Action(kind="copy_skill", category="skills", item="x", source=tmp_path, target=tmp_path / "s"),
        Action(kind="apply_harness", category="harness", item="claude-code", source=tmp_path, target=repo_settings, options=cc),
        # a dir-target action: harness_settings_file appends settings.json
        Action(kind="provision_permissions", category="harness", item="claude-code", source=tmp_path, target=user_settings.parent, options=cc),
        # the hook-bridge action also targets settings.json — must be covered
        Action(kind="register_hook_bridge", category="harness", item="hook-bridge", source=tmp_path, target=bridge_settings, options=cc),
    ]
    paths = _harness_settings_paths(plan)
    assert repo_settings in paths
    assert user_settings in paths  # dir target → <dir>/settings.json
    assert bridge_settings in paths  # register_hook_bridge is covered
    # a copy_skill action contributes nothing; only harness-settings writers do
    assert len(paths) == 3


def test_harness_settings_paths_excludes_non_claude_harness(tmp_path):
    """A non-claude-code (opencode) permissions action's target is NOT scanned for claude hooks.

    The allowlist write can target opencode's ``opencode.json`` (a different schema with no
    claude ``hooks`` blocks). Feeding it to the claude-hook scanner would misread it — so
    ``_harness_settings_paths`` must scope to claude-code actions only.
    """
    from riglib.cli import _harness_settings_paths
    from riglib.plan import Action, InstallPlan

    oc_settings = tmp_path / "oc" / "opencode.json"
    cc_settings = tmp_path / "cc" / ".claude" / "settings.json"
    plan = InstallPlan()
    plan.actions = [
        Action(kind="provision_permissions", category="permissions", item="opencode", source=tmp_path, target=oc_settings, options={"kind": "opencode"}),
        Action(kind="apply_harness", category="harness", item="claude-code", source=tmp_path, target=cc_settings, options={"kind": "claude-code"}),
    ]
    paths = _harness_settings_paths(plan)
    assert cc_settings in paths
    assert oc_settings not in paths  # opencode config is NOT a claude hooks file
    assert len(paths) == 1


def test_harness_settings_paths_from_real_plan_builders(fake_agent_tools, tmp_path):
    """`_harness_settings_paths` works against a REAL plan (not hand-built actions).

    The scan's claude-code filter keys off ``options['kind']`` — so the REAL plan builders for
    apply_harness / provision_permissions / register_hook_bridge MUST set it, or a managed
    settings file would silently drop out of the scan (a regression the hand-built unit tests
    can't catch). This builds a real plan with all three enabled and asserts each carries
    ``options['kind'] == 'claude-code'`` AND that its resolved settings path is scanned.
    """
    from riglib.actions.runner import harness_settings_file
    from riglib.catalog import Catalog
    from riglib.cli import _harness_settings_paths
    from riglib.config import LoadedConfig
    from riglib.plan import build

    repo = tmp_path / "repo"
    repo.mkdir()
    settings = repo / ".claude" / "settings.json"
    cfg = LoadedConfig(
        data={
            "agent_tools_source": str(fake_agent_tools),
            "skills": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
            "git_hooks": {"dispatcher": {"enabled": False}}, "agents_md": {"enabled": False},
            # harness write (acceptEdits → repo-local settings), allowlist, AND the hook bridge
            # all target the SAME claude-code settings.json — every one must be scanned.
            "harness": {"kind": "claude-code", "settings_path": str(settings), "mode": "acceptEdits",
                        "hook_bridge": {"enabled": True}},
            "permissions": {"settings_path": str(settings), "tools": ["git"]},
            "agent_hooks": {"enabled": True},  # the bridge only emits when hooks are provisioned
        },
        repo_root=repo,
    )
    plan = build(cfg, Catalog.scan(str(fake_agent_tools)), project_type="unknown")

    writers = [a for a in plan.actions
               if a.kind in {"apply_harness", "provision_permissions", "register_hook_bridge"}]
    assert writers, "real plan emitted no harness-settings-writing actions"
    # the invariant the scan's filter depends on: every such REAL action tags options['kind']
    for a in writers:
        assert a.options.get("kind") == "claude-code", f"{a.kind} did not tag options['kind']"
        assert harness_settings_file(a) == settings, f"{a.kind} resolved an unexpected settings path"
    # and the scan picks up that one real settings path
    assert _harness_settings_paths(plan) == [settings]


def test_scan_finds_dead_hook_in_nondefault_settings_path(tmp_path):
    """A dead hook in a NON-default settings file is flagged when that path is scanned.

    Regression for the hardcoded-``~/.claude/settings.json`` bug: a repo whose harness settings
    live elsewhere had its dead hooks silently missed.
    """
    from riglib.cli import _scan_missing_targets

    gone = tmp_path / "gone" / "rtk-hook.py"  # never created
    nondefault = tmp_path / "elsewhere" / "settings.json"
    _hooks_settings(nondefault, f"python3 {gone}")

    # the OLD hardcoded scan (default path only) sees nothing — the file isn't even there
    assert _scan_missing_targets([tmp_path / "no-such" / "settings.json"]) == []
    # scanning the ACTUAL provisioned path surfaces the dead hook
    findings = _scan_missing_targets([nondefault])
    assert len(findings) == 1
    assert str(gone) in findings[0].what


def test_scan_empty_list_scans_nothing_not_home_default(tmp_path, monkeypatch):
    """An EMPTY settings_paths list means 'nothing to scan' — NOT a fallback to ~/.claude.

    A config that provisions no harness settings file (harness + permissions disabled) yields
    an empty path list. Falling back to the HOME default there would flag a dead hook in a file
    the config doesn't manage — a false positive. Empty must give []; only None falls back.
    """
    from riglib.cli import _scan_missing_targets

    # plant a dead hook in the HOME default — the fallback target the OLD `if not` check hit
    home = tmp_path / "home"
    gone = tmp_path / "gone" / "rtk-hook.py"  # never created
    _hooks_settings(home / ".claude" / "settings.json", f"python3 {gone}")
    monkeypatch.setenv("HOME", str(home))

    # empty list → scan nothing → no findings (must NOT pick up the HOME default's dead hook)
    assert _scan_missing_targets([]) == []
    # None → the doctor fallback → DOES scan the HOME default and finds it
    findings = _scan_missing_targets(None)
    assert len(findings) == 1
    assert str(gone) in findings[0].what


def test_scan_dedupes_same_dead_hook_across_files(tmp_path):
    """The same dead path referenced from two settings files is reported ONCE."""
    from riglib.cli import _scan_missing_targets

    gone = tmp_path / "gone" / "hook.py"
    a = tmp_path / "a" / "settings.json"
    b = tmp_path / "b" / "settings.json"
    _hooks_settings(a, f"python3 {gone}")
    _hooks_settings(b, f"python3 {gone}")
    findings = _scan_missing_targets([a, b])
    assert len(findings) == 1


def test_status_surfaces_dead_hook_in_repo_local_settings(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig status` surfaces a dead hook in a harness settings file at a NON-default path.

    The hook lives in the repo-local settings file the config provisions (via
    ``harness.settings_path``), NOT ``~/.claude/settings.json`` — the hardcoded scan would miss it.
    """
    import subprocess

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # a CLEAN default settings.json under HOME — the old scan would look here and find nothing
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    gone = tmp_path / "gone-hook.py"  # referenced but absent
    # the dead hook lives in the repo-local settings file the harness block points at
    _hooks_settings(repo / "harness" / "settings.json", f"python3 {gone}")
    (repo / "rig.yaml").write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\nci: {enabled: false}\nagents_md: {enabled: false}\n"
        "permissions: {enabled: false}\n"
        "harness: {kind: claude-code, settings_path: harness/settings.json}\n",
        encoding="utf-8",
    )
    main_rc = __import__("riglib.cli", fromlist=["main"]).main(["status", "-C", str(repo)])
    out = capsys.readouterr().out
    assert str(gone) in out  # the dead hook in the NON-default file is named
    assert "missing" in out.lower()
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
