"""tmux configuration provisioning — config, pure rendering, plan, install, drift.

rig MANAGES tmux config declaratively from rig.yaml, MIGRATING an existing hand-written
``~/.tmux.conf`` rather than clobbering it. Two apply mechanisms:

- **import** (preferred): rig owns a generated ``~/.config/rig/tmux/rig.tmux.conf`` and
  ``~/.tmux.conf`` carries a single ``source-file <that path>`` import line.
- **block** (fallback): rig splices a managed block between sentinel markers in
  ``~/.tmux.conf``, replacing only between the markers (conda-init style).

The root-cause fix this models: continuum's autosave hook lives in ``status-right``; the
Moshi ``set -g status-right ''`` tweak must run BEFORE continuum init (or never wipe its
hook), and continuum's ``run-shell`` init must be LAST so its hook survives. Because rig
GENERATES the managed region it can GUARANTEE that ordering.

All rendering here is stdlib-only + effect-free (the ``tmux`` module computes WHAT the
artifacts are); the effectful writes live in ``actions/runner.py`` and drift diffs the
desired artifacts against disk in ``drift.py`` — three consumers share one source of truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import tmux
from riglib.config import ConfigError, validate


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tmux_block_accepted():
    validate({"version": 1, "tmux": {"enabled": True}})


def test_tmux_block_empty_ok():
    validate({"version": 1, "tmux": {}})


def test_tmux_full_block_accepted():
    validate(
        {
            "version": 1,
            "tmux": {
                "enabled": True,
                "apply": "import",
                "conf_path": "~/.tmux.conf",
                "generated_dir": "~/.config/rig/tmux",
                "resurrect": {
                    "processes": ["claude", "ssh", "psql"],
                    "capture_pane_contents": True,
                },
                "continuum": {
                    "restore": True,
                    "save_interval": 15,
                    "boot": True,
                },
                "moshi": {"enabled": True},
                "cc_restore": {"enabled": True},
                "anti_sprawl": {"enabled": True, "session": "main"},
                "boot": {"enabled": True},
            },
        }
    )


def test_tmux_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"nope": 1}})


def test_tmux_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"enabled": "yes"}})


def test_tmux_apply_enum_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"apply": "splice"}})


@pytest.mark.parametrize("bad", [None, ["import"], 1, {"x": 1}])
def test_tmux_apply_present_non_enum_rejected(bad):
    """A PRESENT `apply` must be a valid enum string — null (→ str("None")) and unhashable
    values (→ raw TypeError) must fail closed, not slip through (codex P2)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"apply": bad}})


@pytest.mark.parametrize("good", ["import", "block"])
def test_tmux_apply_enum_accepted(good):
    validate({"version": 1, "tmux": {"apply": good}})


def test_tmux_resurrect_processes_must_be_list_of_str():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"resurrect": {"processes": "claude"}}})


def test_tmux_save_interval_must_be_positive_int():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": 0}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": -5}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"continuum": {"save_interval": True}}})


def test_tmux_unknown_nested_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"moshi": {"enable": True}}})  # typo: enable


def test_tmux_boot_label_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"boot": {"label": 123}}})
    validate({"version": 1, "tmux": {"boot": {"label": "com.me.tmux"}}})


def test_tmux_autosave_knobs_validate():
    # enabled must be bool, label must be str, stale_after must be int >= 1 (bool rejected).
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"enabled": "yes"}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"label": 123}}})
    for bad in (0, -1, True):
        with pytest.raises(ConfigError):
            validate({"version": 1, "tmux": {"autosave": {"stale_after": bad}}})
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"autosave": {"nope": 1}}})  # unknown nested key
    validate({"version": 1, "tmux": {"autosave": {"enabled": True, "label": "com.me.save", "stale_after": 30}}})


def test_tmux_login_shell_block_accepted():
    validate({"version": 1, "tmux": {"login_shell": {"enabled": True, "shell": "/bin/zsh"}}})
    validate({"version": 1, "tmux": {"login_shell": {"enabled": False}}})


def test_tmux_login_shell_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"enabled": "yes"}}})


def test_tmux_login_shell_shell_must_be_string():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"shell": 123}}})


def test_tmux_login_shell_shell_must_be_absolute_path():
    """A non-empty shell override must be an ABSOLUTE path to the BINARY with NO args (rig adds
    `-l`). A relative name, OR an absolute path WITH args (`/bin/zsh -l` — the review-caught case
    that passed `startswith('/')` but rendered `'/bin/zsh -l' -l`), is rejected."""
    for bad in ("zsh", "zsh -l", "bin/zsh", "/bin/zsh -l", "/bin/zsh --login", "/opt/My App/zsh"):
        with pytest.raises(ConfigError):
            validate({"version": 1, "tmux": {"login_shell": {"shell": bad}}})
    # empty (use $SHELL) and a bare absolute binary path are both fine.
    validate({"version": 1, "tmux": {"login_shell": {"shell": ""}}})
    validate({"version": 1, "tmux": {"login_shell": {"shell": "/usr/bin/fish"}}})


def test_tmux_login_shell_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {"login_shell": {"bogus": True}}})


def test_tmux_enabled_null_is_accepted_and_provisions():
    """`enabled: null` (explicit None) is valid and, per the docs ('not false' provisions),
    must NOT be treated as disabled."""
    validate({"version": 1, "tmux": {"enabled": None}})


@pytest.mark.parametrize("pathkey", ["conf_path", "generated_dir"])
def test_tmux_null_path_key_rejected(pathkey):
    """A present-but-null path key must FAIL CLOSED — it can't fall back to a default, and the
    plan would otherwise resolve a literal `None` path (codex P2)."""
    with pytest.raises(ConfigError):
        validate({"version": 1, "tmux": {pathkey: None}})


def test_tmux_absent_path_key_is_fine():
    """An ABSENT path key is fine — it uses the documented default."""
    validate({"version": 1, "tmux": {"enabled": True}})


# ── pure rendering: the generated rig.tmux.conf ──────────────────────────────────────────
def _plan(**over):
    """A TmuxPlan with sensible defaults, overridable per-test."""
    return tmux.build_tmux(repo_home=Path("/home/u"), **over)


def test_default_resurrect_processes_excludes_claude_when_cc_restore_on():
    """When cc_restore is ON (default), `claude` must NOT be in @resurrect-processes: resurrect
    would restart the pane as a bare `claude` (a NEW/default session) before cc-restore runs,
    and cc-restore — which (correctly) only resumes a FRESH SHELL — would then skip it, leaving
    the wrong session. cc-restore owns the exact `claude --resume <id>` instead. (codex P2.)
    """
    conf = _plan().render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "claude" not in line  # cc-restore owns claude resume; resurrect brings back the shell
    # cc-restore wiring is what restores claude, by exact id.
    assert "@resurrect-hook-post-restore-all" in conf and "cc-restore.sh" in conf


def test_claude_in_resurrect_processes_when_cc_restore_off():
    """With cc_restore OFF, the user opted out of exact-id resume, so `claude` IS added to
    @resurrect-processes — resurrect's own best-effort restore (bare `claude`) is then the only
    mechanism, which is the documented fallback."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": False}).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "@resurrect-processes" in ln)
    assert "claude" in line


def test_render_capture_pane_contents_on():
    conf = _plan().render_rig_conf()
    assert "set -g @resurrect-capture-pane-contents 'on'" in conf


def test_render_continuum_restore_on():
    conf = _plan().render_rig_conf()
    assert "set -g @continuum-restore 'on'" in conf


def test_render_continuum_boot_is_always_off():
    """rig NEVER emits `@continuum-boot 'on'` — that makes continuum install its OWN unmanaged
    boot artifact (the iTerm-coupled Tmux.Start.plist). rig's launchd agent is the boot path, so
    continuum's boot stays off (codex P2)."""
    # default boot.enabled=True still keeps continuum-boot OFF (rig's launchd agent owns boot).
    assert "set -g @continuum-boot 'off'" in _plan().render_rig_conf()
    assert "set -g @continuum-boot 'on'" not in _plan().render_rig_conf()
    # even with the legacy continuum.boot knob true, it's a no-op — still off.
    conf = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"boot": True}).render_rig_conf()
    assert "set -g @continuum-boot 'off'" in conf and "set -g @continuum-boot 'on'" not in conf


def test_render_save_interval():
    # With the independent autosave agent OFF, continuum keeps its own save at save_interval.
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), continuum={"save_interval": 7}, autosave={"enabled": False}
    ).render_rig_conf()
    assert "set -g @continuum-save-interval '7'" in conf


def test_render_disabled_booleans_emit_explicit_off():
    """A disabled modeled boolean must emit an EXPLICIT 'off' (not just be omitted), so the
    generated tail OVERRIDES a preserved inline `'on'` from a migrated conf (codex P2)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"),
        continuum={"restore": False, "boot": False},
        resurrect={"capture_pane_contents": False},
    ).render_rig_conf()
    assert "set -g @continuum-restore 'off'" in conf
    assert "set -g @continuum-boot 'off'" in conf
    assert "set -g @resurrect-capture-pane-contents 'off'" in conf


def test_cc_save_avoids_ls_head_pipe():
    """cc-save must NOT use `ls -t … | head -n1` (SIGPIPE under pipefail drops the pane) — it
    takes the first line of captured ls output instead (codex P2)."""
    body = _plan().render_cc_save()
    # check the EXECUTABLE lines only (a comment may explain why we avoid the pipe).
    code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("head -n1" in ln for ln in code_lines)
    # the first-line-of-listing parameter expansion replaces the pipe.
    assert any("listing" in ln and "newest=" in ln for ln in code_lines)


# ── DEFECT 2: cc-save must detect claude by the pane's PROCESS TREE, not the command string ──
def test_cc_save_walks_pane_process_tree_not_command_string():
    """DEFECT 2 (the reboot bug): cc shows up in `pane_current_command` as its VERSION string
    (e.g. `2.1.178`), and the real `claude` process is a CHILD of the pane's shell. Filtering on
    `pane_current_command == claude` therefore matched NOTHING → the map was always empty → cc
    never resumed. cc-save must walk the pane's process TREE (pane_pid + descendants) for a
    process whose command is `claude` / `*/claude`."""
    body = _plan().render_cc_save()
    # it must enumerate the pane PID + its descendants (a tree walk), not just read the command.
    assert "pane_pid" in body or "#{pane_pid}" in body
    # the OLD broken filter on the command string must be GONE from the EXECUTABLE code (a
    # comment may still reference it to explain WHY the tree walk replaced it).
    code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("pane_current_command" in ln for ln in code_lines)
    # a process-tree walk: descend children via ppid (ps -o pid,ppid or pgrep -P).
    assert "ppid" in body or "pgrep -P" in body


def test_cc_save_matches_claude_basename_in_the_tree():
    """The matched descendant's command is `claude` or `*/claude` (an absolute path to the
    binary), never a substring like `claudette` — match the basename."""
    body = _plan().render_cc_save()
    assert "claude" in body
    # it must record the pane→cwd→session-id map (the whole point) once a claude descendant is found.
    assert "MAP_FILE" in body and ".jsonl" in body


def test_cc_save_matches_the_versioned_binary_path():
    """The generated match must also cover the versioned-binary install (the 2026-06-17 incident):
    cc launched by its resolved path reports `comm` of `.../claude/versions/<version>`, basename =
    the version, not `claude`. The case-glob must include the `*/claude/versions/*` arm or that
    process is invisible to the tree walk (the map stays empty → cc never resumes)."""
    body = _plan().render_cc_save()
    assert "*/claude/versions/*" in body


def test_cc_save_still_records_cwd_and_session_id():
    """Detection changed (tree walk), but the RECORDED data is unchanged: pane addr, cwd, id."""
    body = _plan().render_cc_save()
    assert "pane_current_path" in body or "#{pane_current_path}" in body
    assert "encode_cwd" in body
    assert "newest_session_id" in body


def _run_pane_has_claude(snapshot: str, root: str) -> int:
    """Extract the REAL `pane_has_claude` BFS from the generated cc-save script and run it against
    a SYNTHETIC ps snapshot — HERMETIC (no tmux/network/real processes), so the tree-walk logic is
    covered even when the e2e is opted out (opus finding: the BFS was only exercised in the e2e).
    The snapshot lines are `<pid> <ppid> <args>` (args = the full command line, as the production
    `ps -eo pid=,ppid=,args=` emits — argv[0] is the executable the matcher keys on).
    Returns the function's exit code (0 = a `claude` descendant of `root` was found)."""
    import shlex
    import subprocess
    import textwrap

    body = _plan().render_cc_save()
    # pull out the pane_has_claude() function definition (from its `pane_has_claude() {` to the
    # matching closing brace at column 0 — the script formats it that way).
    start = body.index("pane_has_claude() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    fn = body[start:end]
    script = textwrap.dedent(f"""\
        set -euo pipefail
        PS_SNAPSHOT={shlex.quote(snapshot)}
        {fn}
        if pane_has_claude {shlex.quote(root)}; then exit 0; else exit 1; fi
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode


def test_pane_has_claude_finds_a_direct_child():
    """The pane's direct child is `claude` → detected (the common case: cc is a child of the shell)."""
    snap = "100 1 bash\n200 100 claude\n300 100 sleep\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_finds_a_deep_descendant():
    """A `claude` two levels down (shell → node → claude) is still found by the BFS."""
    snap = "100 1 bash\n200 100 node\n300 200 claude\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_matches_absolute_path_basename():
    """A descendant reported by its absolute path (`/opt/homebrew/bin/claude`) matches `*/claude`."""
    snap = "100 1 zsh\n200 100 /opt/homebrew/bin/claude\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_matches_versioned_binary_under_claude_versions():
    """THE 2026-06-17 INCIDENT: cc installs as a symlink ~/.local/bin/claude ->
    .../claude/versions/<version>. Launched by the RESOLVED path, `ps comm` reports the full path
    whose basename is the VERSION string (`2.1.179`), not `claude` — so a basename-only match
    missed it and the cc map stayed empty (cc never resumed after a reboot). The tree walk must
    catch a descendant whose path is under `.../claude/versions/`."""
    snap = "100 1 /bin/zsh\n200 100 /Users/u/.local/share/claude/versions/2.1.179\n300 200 sleep\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_versioned_binary_deep_in_tree():
    """The versioned cc binary several levels below the pane shell is still found by the BFS."""
    snap = (
        "100 1 zsh\n"
        "200 100 node\n"
        "300 200 /Users/u/.local/share/claude/versions/3.0.0-beta.1\n"
    )
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_no_false_positive_on_unrelated_versioned_path():
    """A numeric-named process NOT under `claude/versions/` (e.g. a runtime under its own
    `versions/` dir, or a bare version-named binary) must NOT match — the `claude/versions/`
    path segment is required, so the rule can't be tripped by any dotted-numeric basename."""
    snap = (
        "100 1 bash\n"
        "200 100 /opt/node/versions/20.11.0/bin/node\n"   # node, not claude
        "300 100 /usr/local/foo/2.1.179\n"                # bare version, no claude/versions/
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_matches_versioned_binary_with_args():
    """The versioned binary launched WITH arguments (`…/claude/versions/2.1.179 --resume`) still
    matches — the matcher keys on argv[0] (the executable path), ignoring the trailing args."""
    snap = "100 1 /bin/zsh\n200 100 /Users/u/.local/share/claude/versions/2.1.179 --resume\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_ignores_claude_only_in_arguments():
    """`claude` appearing only as an ARGUMENT (not argv[0]) must NOT match — the matcher reads
    argv[0] only, so `vim claude-notes.md` / `grep claude` / `cat ~/claude/x` is not a cc pane."""
    snap = (
        "100 1 bash\n"
        "200 100 /usr/bin/vim claude-notes.md\n"
        "300 100 /usr/bin/grep claude /var/log/x\n"
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_matches_symlink_launch_with_args():
    """The common live case: `claude --resume` (argv[0] == `claude`, launched via the symlink)
    matches even with trailing args — argv[0] basename equality."""
    snap = "100 1 zsh\n200 100 claude --resume\n"
    assert _run_pane_has_claude(snap, "100") == 0


def test_pane_has_claude_does_not_false_match_claude_versions_in_an_argument():
    """REGRESSION GUARD (review finding, 2 models): `claude/versions/` appearing in an ARGUMENT
    (not argv[0]) must NOT mark the pane as cc — else a routine command writes a bogus cc-map entry.
    The matcher keys on argv[0] (the executable) only, so a `grep`/`ls`/`tar` over the versions dir,
    or a `cp` of a `claude` file, is correctly ignored."""
    for argline in (
        "/bin/grep -r foo /home/u/.local/share/claude/versions/",
        "/bin/ls /home/u/.local/share/claude/versions/",
        "/usr/bin/tar czf b.tgz /home/u/.local/share/claude/versions/2.1.179",
        "/bin/cp /opt/claude /tmp/",
        "/usr/bin/find / -path */claude/versions*",
    ):
        snap = f"100 1 bash\n200 100 {argline}\n"
        assert _run_pane_has_claude(snap, "100") != 0, argline


def test_pane_has_claude_no_match_on_notclaude_versions_path():
    """The `*/claude/versions/*` glob requires `claude` to be a real path SEGMENT: a sibling
    project like `/opt/notclaude/versions/2.0.0` (or `myclaude`) must NOT false-match."""
    snap = "100 1 bash\n200 100 /opt/notclaude/versions/2.0.0 --x\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_spaced_install_path_is_a_known_limitation():
    """DOCUMENTED LIMITATION (review finding): an install path containing a SPACE
    (`/Users/J D/.local/share/claude/versions/2.1.179`) is NOT detected — argv[0] cannot be
    isolated from `ps args` when it contains a space, and the whole-line match that would cover it
    reintroduces the argument false-positives above. The default `~/.local/share/claude/versions/`
    path has no space, so this never bites a normal install. This test PINS the accepted behavior
    (not an aspiration): if a future change makes spaced paths match, revisit the false-positive
    trade-off in `pane_has_claude` deliberately."""
    snap = "100 1 /bin/zsh\n200 100 /Users/J D/.local/share/claude/versions/2.1.179 --resume\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_spaced_path_symlink_arm_is_a_known_limitation():
    """Same accepted limitation for the plain `*/claude` symlink arm: a `claude` binary under a
    path with a SPACE (`/Users/J D/bin/claude`) is truncated at the space and NOT detected. Pinned
    so the symlink arm's contract can't silently change either (paired with the versions-arm pin)."""
    snap = "100 1 zsh\n200 100 /Users/J D/bin/claude --resume\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_ignores_wrapper_launch_with_claude_in_argv1():
    """DOCUMENTED LIMITATION: a WRAPPER that rewrites argv[0] (`npx claude`, `node …/cli.js`) puts
    the real claude in argv[1+], so it is NOT detected — matching argv[1+] would resurrect the
    argument false-positives. The canonical installs exec the binary directly (argv[0] = claude),
    so this is an accepted miss. Pinned to make the trade-off explicit."""
    snap = (
        "100 1 bash\n"
        "200 100 /usr/bin/node /home/u/.local/share/claude/cli.js\n"
        "300 100 /usr/bin/npx claude --resume\n"
    )
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_tolerates_empty_or_bracketed_args_lines():
    """A degenerate snapshot line — an empty `args` (kernel/zombie) or a bracketed kernel-thread
    name (`[kthreadd]`) — must neither match nor error out the BFS (it just isn't a cc process)."""
    snap = "100 1 bash\n200 100 [kthreadd]\n300 100 \n"  # 300 has empty args
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_no_match_when_absent():
    """No `claude` anywhere in the tree → not found (exit non-zero)."""
    snap = "100 1 bash\n200 100 vim\n300 100 less\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_does_not_match_substring():
    """A `claudette` process is NOT a `claude` match (basename equality, not substring)."""
    snap = "100 1 bash\n200 100 claudette\n"
    assert _run_pane_has_claude(snap, "100") != 0


def test_pane_has_claude_ignores_claude_outside_the_subtree():
    """A `claude` that is NOT a descendant of the queried pane is ignored (no false positive)."""
    snap = "100 1 bash\n200 100 sleep\n900 1 claude\n"  # 900 is a sibling, not under 100
    assert _run_pane_has_claude(snap, "100") != 0


# ── DEFECT 3: restored panes must be LOGIN shells (so ~/.zprofile / PATH is sourced) ─────────
def test_render_default_command_is_a_login_shell():
    """DEFECT 3 (the reboot bug): resurrect restores panes with a NON-login shell (resurrect's
    `default-command ''`), so `~/.zprofile` (PATH etc.) is NOT sourced → restored panes have a
    broken env. The generated config must set a login-shell default-command."""
    conf = _plan().render_rig_conf()
    assert "set -g default-command" in conf
    # a login shell: the user's $SHELL with -l (so ~/.zprofile / ~/.bash_profile is sourced).
    assert "-l" in conf
    line = next(ln for ln in conf.splitlines() if "default-command" in ln)
    assert "SHELL" in line or "/bin/" in line


def test_login_shell_default_command_is_default_on():
    """The login-shell default-command defaults ON (the safe value); a plan with no login_shell
    block still emits it."""
    assert "set -g default-command" in tmux.build_tmux(repo_home=Path("/home/u")).render_rig_conf()


def test_login_shell_can_be_disabled():
    """Configurable: `login_shell.enabled: false` omits the default-command (the user keeps
    resurrect's default non-login behavior)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), login_shell={"enabled": False}
    ).render_rig_conf()
    assert "set -g default-command" not in conf


def test_login_shell_honors_configured_shell():
    """An explicit `login_shell.shell` is used verbatim (e.g. a non-default shell binary)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), login_shell={"enabled": True, "shell": "/usr/bin/fish"}
    ).render_rig_conf()
    line = next(ln for ln in conf.splitlines() if "default-command" in ln)
    assert "/usr/bin/fish" in line and "-l" in line


def test_plan_resolves_login_shell_to_a_concrete_path(fake_agent_tools, tmp_path, monkeypatch):
    """DETERMINISM (review): the plan resolves an EMPTY login_shell.shell to a CONCRETE absolute
    path at plan time and bakes it — so render does NOT depend on $SHELL/FS at render time."""
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = next(act for act in plan.actions if act.kind == "provision_tmux")
    baked = a.options["login_shell"]["shell"]
    assert baked.startswith("/") and " " not in baked  # a concrete absolute binary path


def test_login_shell_resolves_from_passwd_not_ambient_shell(monkeypatch):
    """The resolver reads the PASSWD database (stable login shell), NOT the volatile $SHELL env —
    so it is identical whatever $SHELL is set to (review P1: $SHELL-based resolve flapped)."""
    import os
    import pwd

    from riglib import tmux as tmod

    real = pwd.getpwuid(os.getuid()).pw_shell
    if not real.startswith("/"):
        pytest.skip("no absolute passwd shell on this host")
    monkeypatch.setenv("SHELL", "/some/other/shell-XYZ")  # $SHELL differs from passwd
    assert tmod.resolve_login_shell() == real  # passwd wins, $SHELL ignored


def test_login_shell_deterministic_across_separate_plans_under_different_shell(
    fake_agent_tools, tmp_path, monkeypatch
):
    """THE real apply→status path (review P1): `apply` and `status` each REBUILD a fresh plan. Two
    independently-built plans under DIFFERENT $SHELL must bake the SAME shell, so a
    `SHELL=/bin/bash rig apply` followed by `SHELL=/usr/bin/fish rig status` does NOT flap drift."""
    from riglib.actions.runner import tmux_plan_from_action

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    plan_apply = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a_apply = next(act for act in plan_apply.actions if act.kind == "provision_tmux")
    render_apply = tmux_plan_from_action(a_apply).render_rig_conf()
    # a SEPARATE plan rebuild (as `rig status` does) under a DIFFERENT $SHELL.
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    plan_status = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a_status = next(act for act in plan_status.actions if act.kind == "provision_tmux")
    render_status = tmux_plan_from_action(a_status).render_rig_conf()
    line_apply = next(ln for ln in render_apply.splitlines() if "default-command" in ln)
    line_status = next(ln for ln in render_status.splitlines() if "default-command" in ln)
    assert line_apply == line_status  # identical despite the two different ambient $SHELLs


def test_render_ordering_continuum_hook_is_last_plugin_init():
    """THE root-cause guarantee: continuum's run-shell init comes AFTER the Moshi status-right
    tweak (and after resurrect), so the Moshi tweak can never wipe continuum's autosave hook.
    """
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": True}).render_rig_conf()
    lines = conf.splitlines()
    continuum_init = next(i for i, ln in enumerate(lines) if "continuum.tmux" in ln)
    # the Moshi status-right wipe, if emitted, must be BEFORE continuum init.
    moshi_idx = [i for i, ln in enumerate(lines) if "status-right ''" in ln or 'status-right ""' in ln]
    assert moshi_idx, "moshi enabled → a status-right tweak must be emitted"
    assert all(i < continuum_init for i in moshi_idx), (
        "every Moshi status-right wipe must precede continuum init so the autosave hook survives"
    )
    # resurrect init also precedes continuum (continuum depends on resurrect being loaded).
    resurrect_init = next(i for i, ln in enumerate(lines) if "resurrect.tmux" in ln)
    assert resurrect_init < continuum_init


def test_render_moshi_off_omits_status_right_wipe():
    """Moshi tweaks are a SEPARATE opt-in toggle — off by default they emit no status-right ''."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": False}).render_rig_conf()
    assert "status-right ''" not in conf and 'status-right ""' not in conf


def test_render_moshi_on_guards_under_moshi_client():
    """The Moshi tweak must be gated on $MOSHI_CLIENT (it only applies on the iOS client)."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), moshi={"enabled": True}).render_rig_conf()
    assert "MOSHI_CLIENT" in conf


def test_render_cc_restore_wires_resurrect_hooks():
    """cc-restore wires cc-save/cc-restore via the resurrect post-save/post-restore hooks."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": True}).render_rig_conf()
    assert "@resurrect-hook-post-save-all" in conf
    assert "@resurrect-hook-post-restore-all" in conf
    assert "cc-save.sh" in conf
    assert "cc-restore.sh" in conf


def test_render_cc_restore_off_omits_hooks():
    conf = tmux.build_tmux(repo_home=Path("/home/u"), cc_restore={"enabled": False}).render_rig_conf()
    assert "@resurrect-hook-post-save-all" not in conf


def test_render_is_deterministic():
    a = _plan().render_rig_conf()
    b = _plan().render_rig_conf()
    assert a == b


def test_render_carries_managed_header():
    """The generated file is rig-owned: a clear 'do not hand-edit' header so a human knows."""
    conf = _plan().render_rig_conf()
    assert "rig" in conf.lower()
    assert "generated" in conf.lower() or "do not edit" in conf.lower() or "managed" in conf.lower()


# ── cc-save / cc-restore script rendering ────────────────────────────────────────────────
def test_cc_save_encodes_cwd_slash_and_dot_to_dash():
    """The cwd→projects-dir encoding (VERIFIED on a real machine): every '/' AND '.' → '-'.
    e.g. /Users/ultra/.files/repos → -Users-ultra--files-repos (note the '--' from '/.').
    """
    body = _plan().render_cc_save()
    # the encoding must be present in the script (a sed/tr/parameter-expansion that maps / and .)
    assert "projects" in body
    # cc-save RECORDS ids; it must never RUN a resume (that's cc-restore's job).
    assert "send-keys" not in body or "claude --resume" not in body
    # cc-save must select the NEWEST session id under the encoded dir.
    assert ".jsonl" in body
    # the encoder maps BOTH '/' and '.' to '-' (the verified projects-dir encoding).
    assert "tr './' '--'" in body or ("tr" in body and "." in body and "/" in body)


def test_cc_restore_resumes_by_exact_id():
    body = _plan().render_cc_restore()
    assert "claude --resume" in body
    # stale/missing id → documented fallback (--continue) or skip, never a hard crash.
    assert "--continue" in body or "skip" in body.lower()


def test_cc_restore_does_not_clobber_running_claude():
    """cc-restore must only resume in a FRESH shell pane, never on top of a running claude."""
    body = _plan().render_cc_restore()
    # it inspects the pane's current command before relaunching.
    assert "pane_current_command" in body or "#{pane_current_command}" in body


def test_cc_restore_skips_missing_and_nonshell_panes():
    """cc-restore must (a) skip a map entry whose pane no longer exists (so `set -e` doesn't
    abort the whole restore), and (b) only send keys into a SHELL pane, never an editor/build
    (so it never types `cd … && claude` into vim). (codex P2.)"""
    body = _plan().render_cc_restore()
    # missing pane → empty current command → skip (guard before send-keys).
    assert '[ -n "$cur" ] || continue' in body
    # whitelist of shells — a non-shell command falls through to the `*) continue` skip.
    assert "sh|bash|zsh|fish" in body
    assert "*) continue" in body


def test_cc_scripts_have_shebang():
    assert _plan().render_cc_save().startswith("#!/")
    assert _plan().render_cc_restore().startswith("#!/")


def test_cc_restore_shell_quotes_runtime_values():
    """A cwd with a space or a metacharacter must be shell-quoted before send-keys — never
    embedded raw (which would break the command or, with a tampered map, inject)."""
    body = _plan().render_cc_restore()
    # the resume command line uses the QUOTED variables, not the raw $cwd/$sid.
    assert "printf '%q'" in body
    assert "claude --resume $qsid" in body and "cd $qcwd" in body
    # the raw unquoted form must NOT be what reaches send-keys.
    assert 'send-keys -t "$addr" "cd $cwd && claude --resume $sid"' not in body


def test_attach_script_shell_quotes_session_name():
    """A configured session name with a space/metachar is shell-quoted at generation time."""
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "my sess; rm -rf /"}).render_attach_script()
    # the dangerous name must be single-quoted (shlex.quote), not bare.
    assert "SESSION='my sess; rm -rf /'" in body


# ── boot launchd plist (#19) ─────────────────────────────────────────────────────────────
def test_boot_plist_is_well_formed_and_labelled():
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    body = p.render_boot_plist()
    parsed = plistlib.loads(body.encode("utf-8"))
    assert parsed["Label"] == p.boot_label
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is False
    # DEFECT 1: the plist must run the BOOT SCRIPT (which `new-session -d` loads the conf →
    # continuum restores), NOT a bare `tmux start-server` (an EMPTY server that loads nothing).
    assert parsed["ProgramArguments"][-1] == str(p.boot_script_path)
    assert parsed["ProgramArguments"][-1].endswith(tmux.BOOT_NAME)


def test_boot_plist_does_not_run_bare_start_server():
    """DEFECT 1 (the reboot bug): `tmux start-server` starts a server WITHOUT loading the conf
    or plugins (tmux loads the conf only on the FIRST session), so continuum-restore never
    fires → empty server. The plist must invoke the boot SCRIPT instead."""
    import plistlib

    args = plistlib.loads(
        tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist().encode("utf-8")
    )["ProgramArguments"]
    assert "start-server" not in args


def test_boot_label_is_configurable():
    p = tmux.build_tmux(repo_home=Path("/home/u"), boot={"enabled": True, "label": "com.me.tmux"})
    assert p.boot_label == "com.me.tmux"
    assert "com.me.tmux" in p.render_boot_plist()
    assert p.boot_plist_path.name == "com.me.tmux.plist"


def test_boot_plist_points_at_the_boot_script():
    """The plist's single program argument is the generated boot script — the one indirection
    that lets the boot bring up a REAL session (loading the conf) instead of an empty server."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    args = plistlib.loads(p.render_boot_plist().encode("utf-8"))["ProgramArguments"]
    assert args == [str(p.boot_script_path)]


def test_boot_plist_sets_homebrew_inclusive_path():
    """DEFECT (the reboot RESTORE bug): the launchd agent runs with a minimal PATH
    (/usr/bin:/bin:/usr/sbin:/sbin) that lacks the Homebrew bin dir. The boot SCRIPT survives it
    (it calls tmux by absolute path), but the tmux SERVER it spawns INHERITS that PATH, and tmux's
    own continuum/resurrect hooks run via `run-shell` and call BARE `tmux` → not found → the
    `@continuum-restore` lookup fails → defaults to `off` → restore is SILENTLY skipped (no
    sessions come back after a reboot). The plist must inject a Homebrew-inclusive PATH so the
    server + every run-shell hook child can resolve tmux."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    env = plistlib.loads(p.render_boot_plist().encode("utf-8"))["EnvironmentVariables"]
    path_dirs = env["PATH"].split(":")
    # the resolved tmux binary's own dir must be on PATH so a bare `tmux` resolves in the hooks.
    assert str(Path(tmux._resolve_tmux_bin()).parent) in path_dirs
    assert "/opt/homebrew/bin" in path_dirs
    assert env["HOME"] == "/home/u"


def test_boot_plist_render_is_deterministic_across_separate_plans(monkeypatch):
    """The real apply→status drift path: apply and status each build a FRESH plan, possibly under
    a different ambient PATH. The plist must be byte-identical anyway. We build two separate plans
    while shutil.which returns DIFFERENT answers — resolution is existence-based (the first EXISTING
    fixed install path wins, ambient-independent), so the rendered plist must not budge.

    HERMETIC: pin ``Path.exists`` so a fixed install path (``/opt/homebrew/bin/tmux``) is the one
    that exists on ANY host — otherwise a runner with NO tmux at a fallback path would fall through
    to the (differing) ``shutil.which`` results and this determinism check would flake (codex P1)."""
    monkeypatch.setattr(tmux.Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/tmux")
    monkeypatch.setattr(tmux.shutil, "which", lambda name: "/interactive/dir/tmux")
    apply_render = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)  # a minimal-PATH `rig status`
    status_render = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    assert apply_render == status_render
    # and the resolved dir is the fixed fallback — shutil.which's differing answers never leak in.
    import plistlib

    path = plistlib.loads(apply_render.encode("utf-8"))["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path.split(":")


def test_boot_plist_preserves_key_order_not_sorted():
    """sort_keys=False is load-bearing: a stable key order keeps re-apply a no-op. Pin it so a
    refactor that drops it (or reorders the payload) fails here instead of flapping silently."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_plist()
    assert body.index("<key>Label</key>") < body.index("<key>EnvironmentVariables</key>")
    assert body.index("<key>PATH</key>") < body.index("<key>HOME</key>")
    assert body.index("<key>RunAtLoad</key>") < body.index("<key>StandardOutPath</key>")


def test_launch_agent_path_has_no_user_writable_dir():
    """Sol #138 hardening: a periodic launchd agent must not resolve executables from a
    user-writable PATH entry (e.g. ~/.local/bin) — an execution-hijack seam. Only fixed system /
    Homebrew dirs plus the resolved tmux dir."""
    path = tmux.build_tmux(repo_home=Path("/home/u")).boot_path_env
    assert "/home/u/.local/bin" not in path.split(":")
    assert "/opt/homebrew/bin" in path.split(":")


# ── independent autosave agent (#138) ─────────────────────────────────────────────────────
def test_autosave_disables_continuum_own_save():
    """With the independent saver ON (default), continuum's own status-right autosave is disabled
    (@continuum-save-interval 0) so there is exactly ONE authoritative saver — no racing writers."""
    conf = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"save_interval": 15}).render_rig_conf()
    assert "set -g @continuum-save-interval '0'" in conf


def test_autosave_off_leaves_continuum_save_active():
    """Opt out of the independent saver → continuum keeps its own save at save_interval (legacy)."""
    conf = tmux.build_tmux(
        repo_home=Path("/home/u"), continuum={"save_interval": 9}, autosave={"enabled": False}
    ).render_rig_conf()
    assert "set -g @continuum-save-interval '9'" in conf


def test_autosave_script_in_managed_scripts_when_enabled():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert any(path == p.autosave_script_path for path, _ in p.managed_scripts())
    off = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"enabled": False})
    assert all(path != off.autosave_script_path for path, _ in off.managed_scripts())


def test_autosave_plist_is_periodic_and_logged():
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"save_interval": 15})
    parsed = plistlib.loads(p.render_autosave_plist().encode("utf-8"))
    assert parsed["Label"] == "ai.hyperide.tmux-autosave"
    assert parsed["StartInterval"] == 15 * 60  # StartInterval = save_interval minutes → seconds
    assert parsed["ProgramArguments"] == [str(p.autosave_script_path)]
    assert parsed["StandardOutPath"] == str(p.autosave_out_log_path)
    # Homebrew-inclusive PATH so the wrapper + resurrect save.sh resolve tmux under launchd.
    assert str(Path(p.tmux_bin).parent) in parsed["EnvironmentVariables"]["PATH"].split(":")


def test_autosave_script_guards_and_saves():
    """The wrapper must: use an absolute tmux path, exit-0 on no server, keep a degenerate-save
    guard, and call resurrect save.sh. These are the robustness properties #138 turns on."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    assert body.startswith("#!/bin/bash")
    assert "has-session" in body                    # no-server guard
    assert "degenerate" in body                     # never clobber a richer prior snapshot
    assert "tmux-resurrect/scripts/save.sh" in body  # the real saver
    assert "mkdir \"$LOCK\"" in body                 # single-flight lock
    assert not body.startswith("#!/usr/bin/env")     # Sol: absolute shebang, not env
    assert "grep -vFx" in body                       # fixed-string session match (no regex misfire)


def test_autosave_stale_after_renders_into_script():
    body = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 90}).render_autosave_script()
    assert "STALE_SECS=5400" in body  # 90 minutes → seconds


def test_autosave_script_reads_mtime_gnu_form_first():
    """The snapshot-mtime read must try `stat -c %Y` (GNU) BEFORE `stat -f %m` (BSD). Under a
    BSD-first chain, GNU `stat -f %m FILE` prints FILE's filesystem block ('File: ...') to STDOUT
    and exits non-zero, so the leaked text lands in the `$(( ))` arithmetic and blows up with
    `File: unbound variable` under `set -u`. GNU-first keeps the failing form stdout-clean on both
    platforms — the exact bug that reddened the launchd-minimal-PATH e2e."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    gnu = body.index("stat -c %Y")
    bsd = body.index("stat -f %m")
    assert gnu < bsd, "GNU `stat -c %Y` must precede BSD `stat -f %m`"
    # the mtime is validated numeric before it feeds `$(( ))` — no leaked text can reach arithmetic
    assert "case \"$mtime\" in" in body


def test_autosave_script_is_valid_bash_under_nounset():
    """`bash -n` (syntax check) the rendered wrapper — it runs under `set -u`, so a stray unbound
    reference or broken arithmetic is a real runtime failure, not a lint nit."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:  # no bash on PATH (unlikely in CI) → nothing to syntax-check
        pytest.skip("bash not available")
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_autosave_script()
    proc = subprocess.run([bash, "-n"], input=body, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_autosave_path_env_matches_boot_path_env():
    """One source for the launch-agent PATH — the autosave agent reuses the boot agent's."""
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert p.autosave_path_env == p.boot_path_env
    assert "/home/u/.local/bin" not in p.autosave_path_env.split(":")


def test_launch_agents_set_utf8_lang():
    """CRITICAL: launchd gives no locale; resurrect's save.sh (awk/sed over TAB-delimited data)
    writes a CORRUPT ~9-byte snapshot under the C locale that then clobbers `last`. Both the boot
    and autosave plists must inject a UTF-8 LANG so the save (and restore parsing) work. Proven
    live: no LANG → 9-byte save; LANG=en_US.UTF-8 → a full snapshot."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    for body in (p.render_boot_plist(), p.render_autosave_plist()):
        env = plistlib.loads(body.encode("utf-8"))["EnvironmentVariables"]
        assert env["LANG"] == "en_US.UTF-8"
        assert "/opt/homebrew/bin" in env["PATH"].split(":")  # PATH still present
        assert env["HOME"] == "/home/u"


# ── autosave freshness healthcheck (the observability half of #138) ─────────────────────────
# The month-long silent death happened because NOBODY owned the question "is the newest save
# fresh?". `assess_autosave_freshness` gives that question an owner: it reads the health-state
# file the wrapper atomically rewrites EVERY run (result=ok|skip|inactive|…) and flags a live
# saver whose last run is older than `stale_after`. It keys off the HEALTH FILE mtime — not the
# snapshot mtime — on purpose: resurrect deletes a byte-identical snapshot, so a healthy quiescent
# server can legitimately have an OLD snapshot but the agent still ran (and refreshed health) on
# schedule. A stale health file means the AGENT stopped running — the exact failure to surface.
def test_autosave_freshness_ok_when_health_is_recent():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=1000.0 - 10 * 60, health_result="ok")
    assert h.state == "ok"
    assert "10m ago" in h.detail


def test_autosave_freshness_stale_when_health_older_than_threshold():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    now = 60_000.0
    health_mtime = now - 60 * 60  # last run 60 minutes ago — past the 45-minute threshold
    h = tmux.assess_autosave_freshness(p, now_epoch=now, health_mtime=health_mtime, health_result="ok")
    assert h.state == "stale"
    assert "60m" in h.detail and "45m" in h.detail  # age and the crossed threshold


@pytest.mark.parametrize("bad", ["error", "busy", "some-future-result"])
def test_autosave_freshness_unhealthy_when_recent_run_failed_to_save(bad):
    """A FRESH health mtime with a non-benign result (error/busy, or any unrecognised value)
    means the agent is firing but cannot save — that must NOT read as `ok` (the exact false-green
    the healthcheck exists to avoid). Allowlisting the benign set keeps this fail-closed."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(
        p, now_epoch=1000.0, health_mtime=1000.0 - 5 * 60, health_result=bad
    )
    assert h.state == "unhealthy"
    assert bad in h.detail


@pytest.mark.parametrize("benign", ["ok", "skip", "inactive", None])
def test_autosave_freshness_ok_for_benign_results(benign):
    """`ok` and the "nothing to save" outcomes (`skip`/`inactive`) — and an ABSENT result (None,
    a partial/non-object write) — are healthy, not failures: stay `ok`."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    h = tmux.assess_autosave_freshness(
        p, now_epoch=1000.0, health_mtime=1000.0 - 5 * 60, health_result=benign
    )
    assert h.state == "ok"


def test_autosave_freshness_stale_outranks_failed_result():
    """A record OLDER than the threshold reports `stale` even when its result is a failure — a
    frozen health file (agent stopped firing entirely) is the more severe, root failure, and the
    last-run outcome is moot once the timer is dead."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"stale_after": 45})
    now = 60_000.0
    h = tmux.assess_autosave_freshness(
        p, now_epoch=now, health_mtime=now - 60 * 60, health_result="error"
    )
    assert h.state == "stale"


def test_autosave_freshness_missing_health_flags_agent_not_running():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=None)
    assert h.state == "missing"


def test_autosave_freshness_disabled_when_saver_off():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"enabled": False})
    h = tmux.assess_autosave_freshness(p, now_epoch=1000.0, health_mtime=None)
    assert h.state == "disabled"


def _tmux_autosave_status_line(monkeypatch, tmp_path, *, autosave=None, health=None, age_min=0, installed=True):
    """Render the `rig status` autosave-freshness line for a one-action tmux plan. Simulates an
    INSTALLED agent (its plist on disk) unless `installed=False`, and writes an optional health file
    (aged `age_min` minutes) into the generated dir the printer reads."""
    import json
    import os as _os
    import time as _time

    from riglib import cli
    from riglib.actions.runner import tmux_plan_from_action
    from riglib.plan import Action, InstallPlan

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    gen = tmp_path / ".config" / "rig" / "tmux"
    gen.mkdir(parents=True)
    opts = {
        "generated_dir": str(gen),
        "conf_path": str(tmp_path / ".tmux.conf"),
        "autosave": {} if autosave is None else autosave,
    }
    action = Action(kind="provision_tmux", category="tmux", item="config",
                    source=tmp_path, target=tmp_path / ".tmux.conf", options=opts)
    plan = InstallPlan(actions=[action])
    if installed:  # the launchd plist on disk = the agent is provisioned here
        tplan = tmux_plan_from_action(action)
        tplan.autosave_plist_path.parent.mkdir(parents=True, exist_ok=True)
        tplan.autosave_plist_path.write_text("<plist/>", encoding="utf-8")
    if health is not None:
        hp = gen / tmux.AUTOSAVE_HEALTH_NAME
        # a str is written VERBATIM (to exercise corrupt / non-object JSON); a dict is serialized.
        hp.write_text(health if isinstance(health, str) else json.dumps(health), encoding="utf-8")
        if age_min:
            when = _time.time() - age_min * 60
            _os.utime(hp, (when, when))
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    cli._print_tmux_autosave_status(plan)
    return "\n".join(captured)


def test_status_autosave_line_fresh_when_health_recent(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health={"result": "ok"}, age_min=5)
    assert "fresh" in line and "autosave agent" in line


def test_status_autosave_line_stale_when_health_old(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(
        monkeypatch, tmp_path, autosave={"stale_after": 45}, health={"result": "ok"}, age_min=90
    )
    assert "STALE" in line


def test_status_autosave_line_unhealthy_when_recent_run_failed(monkeypatch, tmp_path):
    """A fresh health record whose last run failed (result=error) renders a WARNING line, not a
    green `fresh` — a live timer with a broken save must be visible in `rig status`."""
    line = _tmux_autosave_status_line(
        monkeypatch, tmp_path, health={"result": "error"}, age_min=5
    )
    assert "UNHEALTHY" in line and "could not save" in line


def test_status_autosave_line_missing_when_no_health(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health=None)
    assert "no health record" in line


def test_status_autosave_line_silent_when_disabled(monkeypatch, tmp_path):
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, autosave={"enabled": False})
    assert line == ""


def test_status_autosave_line_silent_when_agent_not_installed(monkeypatch, tmp_path):
    """No plist on disk = the agent was never applied here (or off darwin). Don't nag about
    freshness on every `rig status` when there is nothing installed to be fresh."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health=None, installed=False)
    assert line == ""


def test_status_autosave_line_missing_when_health_corrupt(monkeypatch, tmp_path):
    """A corrupt (unparseable) health file is the exact silent-corruption case this feature exists
    to surface — it must read as 'no health record', never crash."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health="not-json{")
    assert "no health record" in line


def test_status_autosave_line_non_dict_health_suppresses_result_note(monkeypatch, tmp_path):
    """Valid JSON that isn't an object (a partial/garbled write) → freshness still assessed from the
    file mtime, but the `result=` note is suppressed (never printed as `result=None`)."""
    line = _tmux_autosave_status_line(monkeypatch, tmp_path, health="[1, 2]", age_min=5)
    assert "fresh" in line and "result=" not in line


def test_autosave_label_configurable():
    p = tmux.build_tmux(repo_home=Path("/home/u"), autosave={"label": "com.me.tmux-save"})
    assert p.autosave_label == "com.me.tmux-save"
    assert p.autosave_plist_path.name == "com.me.tmux-save.plist"
    assert "com.me.tmux-save" in p.render_autosave_plist()


def test_autosave_plist_render_is_deterministic():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    assert p.render_autosave_plist() == p.render_autosave_plist()
    assert p.render_autosave_script() == p.render_autosave_script()


def test_boot_path_env_is_stable_when_tmux_bin_resolution_changes(monkeypatch):
    """tmux_bin is baked at plan time, so a LATER change to ambient PATH (which would make
    _resolve_tmux_bin pick a different dir) must NOT change an already-built plan's rendered
    plist — the determinism contract that avoids apply/status drift flap."""
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    before = p.render_boot_plist()
    monkeypatch.setattr(tmux, "_resolve_tmux_bin", lambda: "/some/other/dir/tmux")
    assert p.render_boot_plist() == before  # baked field wins; no re-resolution at render time


def test_boot_plist_has_logging_paths():
    """Without StandardOutPath/StandardErrorPath the boot agent is a BLACK BOX: a half-working
    boot (server up, restore skipped) leaves zero diagnostics. The plist must log both streams
    into the generated dir alongside the managed scripts."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))
    parsed = plistlib.loads(p.render_boot_plist().encode("utf-8"))
    assert parsed["StandardOutPath"] == str(p.boot_out_log_path)
    assert parsed["StandardErrorPath"] == str(p.boot_err_log_path)
    assert p.boot_out_log_path.parent == p.generated_dir
    assert p.boot_err_log_path.parent == p.generated_dir
    assert p.boot_out_log_path.name.endswith(".log")


# ── boot script (DEFECT 1: new-session -d loads the conf → continuum restores) ──────────────
def test_boot_script_creates_a_session_to_load_the_conf():
    """The boot script must `tmux new-session -d` (which loads ~/.tmux.conf → the sourced
    rig.tmux.conf → continuum), NOT `tmux start-server` (an empty server that loads nothing)."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert body.startswith("#!/")
    assert "new-session -d" in body
    assert "start-server" not in body


def test_boot_script_is_idempotent_attach_or_create():
    """A second login/boot must NOT spawn a duplicate session — the boot script creates the
    canonical session only if it does not already exist (anti-sprawl at boot)."""
    body = tmux.build_tmux(
        repo_home=Path("/home/u"), anti_sprawl={"enabled": True, "session": "main"}
    ).render_boot_script()
    assert "has-session" in body
    assert "main" in body


def test_boot_script_passes_f_for_custom_conf_path():
    """A non-default conf_path must reach the boot session via `-f`, else the session starts
    WITHOUT the managed config (continuum/resurrect never set → no restore)."""
    body = tmux.build_tmux(
        repo_home=Path("/home/u"), conf_path="~/.config/tmux/custom.conf"
    ).render_boot_script()
    assert "-f" in body
    assert "custom.conf" in body


def test_boot_script_omits_f_for_default_conf_path():
    """The default ~/.tmux.conf is auto-loaded by tmux, so no `-f` is emitted."""
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert " -f " not in body


def test_boot_script_tmux_bin_falls_back_to_existing_path(monkeypatch):
    """When tmux isn't on PATH, the boot script must reference an EXISTING common location, not a
    blind Apple-silicon hard-code (codex P2)."""
    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)  # not on PATH
    monkeypatch.setattr(tmux.Path, "exists", lambda self: str(self) == "/usr/local/bin/tmux")
    body = tmux.build_tmux(repo_home=Path("/home/u")).render_boot_script()
    assert "/usr/local/bin/tmux" in body


# ── anti-sprawl attach-or-create ─────────────────────────────────────────────────────────
def test_anti_sprawl_script_is_attach_or_create():
    """The anti-sprawl entry re-attaches the ONE canonical session, never spawns a duplicate."""
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "main"}).render_attach_script()
    assert body.startswith("#!/")
    # attach-or-create: attach the named session, else create it (single canonical session).
    assert "attach" in body and "new-session" in body
    assert "main" in body  # the canonical session name


def test_anti_sprawl_uses_configured_session_name():
    body = tmux.build_tmux(repo_home=Path("/home/u"),
                           anti_sprawl={"enabled": True, "session": "work"}).render_attach_script()
    assert "work" in body


# ── the import line + managed-block sentinels ────────────────────────────────────────────
def test_import_line_sources_the_generated_file():
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    line = p.import_line()
    assert line.startswith("source-file ")
    assert str(p.rig_conf_path) in line


def test_import_line_is_quoted_for_paths_with_spaces():
    """A generated_dir / HOME with a space must be single-quoted so tmux sources ONE path,
    not multiple args (codex P2)."""
    p = tmux.build_tmux(repo_home=Path("/Users/A B"), generated_dir="~/.config/rig/tmux")
    line = p.import_line()
    assert line == f"source-file '{p.rig_conf_path}'"
    assert "/Users/A B/" in line  # the space survives inside the quotes


def test_null_processes_normalizes_to_default():
    """A YAML `processes:` with no value (None) must NOT crash planning — normalize to default."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), resurrect={"processes": None})
    conf = p.render_rig_conf()  # would TypeError on list(None) before the fix
    assert "@resurrect-processes" in conf


def test_null_nested_knobs_normalize_to_default():
    """Present-but-null nested knobs (save_interval/enabled with no value) use the default,
    never crashing int(None)/bool(None) (codex P2)."""
    p = tmux.build_tmux(
        repo_home=Path("/home/u"),
        continuum={"save_interval": None, "restore": None, "boot": None},
        cc_restore={"enabled": None}, moshi={"enabled": None},
        anti_sprawl={"enabled": None, "session": None}, boot={"enabled": None, "label": None},
        autosave={"enabled": False},  # keep continuum's own save so save_interval-default is visible
    )
    conf = p.render_rig_conf()
    assert f"@continuum-save-interval '{tmux.DEFAULT_SAVE_INTERVAL}'" in conf
    assert p.continuum_restore is True and p.cc_restore_enabled is True  # null → default True
    assert p.anti_sprawl_session == tmux.DEFAULT_SESSION  # null → default
    assert p.boot_label == tmux.DEFAULT_BOOT_LABEL


def test_explicit_empty_processes_list_is_honored():
    """An EXPLICIT `processes: []` clears matching — it must NOT silently fall back to the
    default (codex P2)."""
    p = tmux.build_tmux(repo_home=Path("/home/u"), resurrect={"processes": []})
    assert p.resurrect_processes == []
    line = next(ln for ln in p.render_rig_conf().splitlines() if "@resurrect-processes" in ln)
    assert line.strip() == "set -g @resurrect-processes ''"  # empty, not the default list


def test_resurrect_hook_command_is_shell_quoted_for_spaces():
    """The resurrect-hook VALUE is exec'd as a shell command, so a script path with a space
    must be shell-quoted inside it — else resurrect splits it and the hook never runs (codex P2).
    """
    p = tmux.build_tmux(repo_home=Path("/Users/A B"), cc_restore={"enabled": True})
    conf = p.render_rig_conf()
    save_line = next(ln for ln in conf.splitlines() if "post-save-all" in ln)
    # the path with a space must be shell-quoted (single quotes from shlex.quote) inside the value.
    assert "'/Users/A B/" in save_line


def test_managed_block_sentinels_present():
    assert tmux.BLOCK_BEGIN == "# === rig-managed (tmux) BEGIN ==="
    assert tmux.BLOCK_END == "# === rig-managed (tmux) END ==="


# ── splice_managed_block (pure) — replace ONLY between markers ────────────────────────────
def test_splice_appends_block_when_absent():
    original = "# my hand-written tmux\nset -g mouse on\n"
    out = tmux.splice_managed_block(original, "BODY-A\nBODY-B")
    assert "set -g mouse on" in out  # user lines preserved
    assert tmux.BLOCK_BEGIN in out and tmux.BLOCK_END in out
    assert "BODY-A" in out and "BODY-B" in out
    # the begin sentinel comes before the end sentinel and the body sits between.
    assert out.index(tmux.BLOCK_BEGIN) < out.index("BODY-A") < out.index(tmux.BLOCK_END)


def test_splice_replaces_only_between_markers():
    original = (
        "set -g mouse on\n"
        f"{tmux.BLOCK_BEGIN}\n"
        "OLD-BODY\n"
        f"{tmux.BLOCK_END}\n"
        "set -g history-limit 100000\n"  # a user line AFTER the block
    )
    out = tmux.splice_managed_block(original, "NEW-BODY")
    assert "OLD-BODY" not in out
    assert "NEW-BODY" in out
    assert "set -g mouse on" in out  # user line before block kept
    assert "set -g history-limit 100000" in out  # user line after block kept
    # exactly one managed block (no duplication).
    assert out.count(tmux.BLOCK_BEGIN) == 1 and out.count(tmux.BLOCK_END) == 1


def test_splice_is_idempotent():
    original = "set -g mouse on\n"
    once = tmux.splice_managed_block(original, "BODY")
    twice = tmux.splice_managed_block(once, "BODY")
    assert once == twice


# ── neutralizing inline rig-owned lines on migration (the root-cause completion) ──────────
def _live_lines(text):
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _is_neutralized(text, needle):
    """True iff `needle` appears ONLY on neutralized (rig-migrated comment) lines, never live."""
    hits = [ln for ln in text.splitlines() if needle in ln]
    return bool(hits) and all(ln.lstrip().startswith(tmux.NEUTRALIZE_PREFIX) for ln in hits)


def test_neutralize_comments_out_rig_owned_init_and_options():
    """Migration neutralizes every rig-OWNED plugin/continuum/resurrect init line — the three
    rig `@plugin` decls, all `@continuum-*`/`@resurrect-*`, both plugin-init `run-shell`s, and
    tpm's `run` — plus the Moshi wipe. This is the live-machine double-init fix (2026-06-18):
    a hand-written `run-shell …/continuum.tmux` fires continuum-restore BEFORE rig's appended
    source-file sets the login-shell default-command, so the inline init MUST be neutralized.
    """
    original = (
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @continuum-boot 'on'\n"
        "set -g @continuum-boot-options 'iterm'\n"
        "set -g @resurrect-processes 'ssh psql ~rails'\n"
        "set -g @resurrect-strategy-vim 'session'\n"
        "set -g @resurrect-capture-pane-contents 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "  set -g status-right ''\n"
        "}\n"
        "run '~/.tmux/plugins/tpm/tpm'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    # the Moshi if-shell wipe block IS neutralized (the original root-cause line) …
    assert not any("status-right ''" in ln for ln in _live_lines(out)), "Moshi wipe must be off"
    # … and so is EVERY rig-owned init/option/decl line (the double-init completion).
    for needle in (
        "@plugin 'tmux-plugins/tpm'",
        "@plugin 'tmux-plugins/tmux-resurrect'",
        "@plugin 'tmux-plugins/tmux-continuum'",
        "@continuum-restore", "@continuum-boot", "@continuum-boot-options",
        "@resurrect-processes", "@resurrect-strategy-vim", "@resurrect-capture-pane-contents",
        "tmux-resurrect/resurrect.tmux", "tmux-continuum/continuum.tmux",
        "tmux/plugins/tpm/tpm",
    ):
        assert _is_neutralized(out, needle), f"rig-owned line not neutralized: {needle}"


def test_neutralize_preserves_third_party_plugins_and_personal_prefs():
    """Over-neutralization guard: a third-party `@plugin` (rig does NOT own it — rig's tpm loads
    it) and every personal pref stay LIVE. Only the THREE rig-owned plugins are swept.
    """
    original = (
        "set -g mouse on\n"
        "set -g history-limit 100000\n"
        "set -g base-index 1\n"
        "set -g @plugin 'tmux-plugins/tmux-sensible'\n"  # third-party — rig does NOT own it
        "set -g @plugin 'tmux-plugins/tmux-yank'\n"       # third-party — rig does NOT own it
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"  # rig-owned — neutralized
        "set-option -ga update-environment ' MOSHI_CLIENT'\n"  # a pref, NOT the wipe
        "set -g status-right '#{battery}'\n"              # a real value, not the empty wipe
        "bind r source-file ~/.tmux.conf\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = "\n".join(_live_lines(out))
    for kept in (
        "set -g mouse on", "set -g history-limit 100000", "set -g base-index 1",
        "tmux-plugins/tmux-sensible", "tmux-plugins/tmux-yank",
        "update-environment ' MOSHI_CLIENT'", "status-right '#{battery}'",
        "bind r source-file",
    ):
        assert kept in live, f"line wrongly neutralized (over-reach): {kept}"
    # the one rig-owned plugin IS swept.
    assert _is_neutralized(out, "@plugin 'tmux-plugins/tmux-resurrect'")


def test_neutralize_does_not_over_reach_on_lookalikes():
    """Tokens that LOOK rig-owned but aren't an init must stay live: a third-party plugin whose
    name merely CONTAINS a rig plugin name, a fork under the same org, a `source-file` keybinding,
    a `status-right` VALUE that mentions a rig token, a non-set directive carrying the token, and
    a commented-out mention. The `@continuum-`/`@resurrect-` match is anchored to a set directive,
    and `@plugin` to the QUOTED spec — so none of these false-match (review findings 1 + 3)."""
    cases = (
        "set -g @plugin 'someuser/tmux-resurrect-fork'\n",   # not the rig spec
        "set -g @plugin 'tmux-plugins/tmux-resurrect-fork'\n",  # same-org fork — closing quote anchors
        "bind r source-file ~/.tmux.conf\n",                 # a keybinding, not a plugin init
        "set -g status-right 'continuum.tmux rules'\n",       # a real value, not a run-shell init
        "set -g status-right '#(tmux show-option -gv @continuum-save-interval)'\n",  # value prints opt
        "bind r run-shell 'tmux set @continuum-boot on'\n",   # keybind whose VALUE has the token
        "display-message '@resurrect-processes test'\n",      # not a set directive at all
        "# I once used tmux-continuum but stopped\n",         # a comment — never re-touched
    )
    for original in cases:
        assert tmux.neutralize_inline_rig_lines(original) == original, original


def test_neutralize_unquoted_rig_plugin_spec_but_not_fork():
    """An UNQUOTED `set -g @plugin tmux-plugins/tpm` is valid tmux and IS the rig plugin → it
    must be neutralized (the double-init source), while a fork `…/tmux-resurrect-fork` (quoted or
    bare) is matched by EXACT spec value, not a prefix, so it stays live (review findings 2 + 3)."""
    P = tmux.NEUTRALIZE_PREFIX
    for owned in (
        "set -g @plugin tmux-plugins/tpm\n",
        "set -g @plugin tmux-plugins/tmux-resurrect\n",
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(owned).lstrip().startswith(P), owned
    for fork in (
        "set -g @plugin tmux-plugins/tmux-resurrect-fork\n",
        "set -g @plugin 'tmux-plugins/tmux-resurrect-fork'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(fork) == fork, fork


def test_neutralize_run_shell_init_anchored_not_nested_arg():
    """The plugin-init match is on the run-shell/run COMMAND, not a substring: real inits (bare,
    quoted, `-b`, absolute) are neutralized, but a command that merely MENTIONS the plugin path as
    a nested ARGUMENT (a backup/copy) stays live — over-reach would change a line the user did not
    ask to change (review finding 1)."""
    P = tmux.NEUTRALIZE_PREFIX
    for init in (
        "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n",
        "run-shell '~/.tmux/plugins/tmux-continuum/continuum.tmux'\n",
        "run-shell -b ~/.tmux/plugins/tmux-continuum/continuum.tmux\n",
        "run-shell -b '~/.tmux/plugins/tmux-continuum/continuum.tmux'\n",  # -b AND quotes together
        "run '~/.tmux/plugins/tpm/tpm'\n",
        "run-shell /Users/me/.tmux/plugins/tmux-continuum/continuum.tmux\n",
    ):
        assert tmux.neutralize_inline_rig_lines(init).lstrip().startswith(P), init
    for nested in (
        'run-shell "tar czf bak.tgz ~/.tmux/plugins/tmux-continuum/continuum.tmux"\n',
        "run-shell 'cp ~/.tmux/plugins/tmux-continuum/continuum.tmux /tmp'\n",
    ):
        assert tmux.neutralize_inline_rig_lines(nested) == nested, nested


def test_neutralize_skips_moshi_lookalike_inside_managed_block():
    """A `status-right ''` / `@continuum-restore` lookalike INSIDE rig's managed block (block apply
    mode — rig's OWN generated config) must survive untouched: both neutralize passes skip the
    region between BLOCK_BEGIN/BLOCK_END. Guards the block-skip so a future regression that removes
    it (and lets neutralize corrupt rig's own block) fails here, not only via the splice masking."""
    inside = (
        "  set -g status-right ''\n"            # a Moshi-wipe lookalike (rig's own, when moshi on)
        "  set -g @continuum-restore 'off'\n"   # rig's own option line
        "  run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"  # rig's own init
    )
    original = (
        "set -g @continuum-restore 'on'\n"      # USER line above the block — neutralized
        f"{tmux.BLOCK_BEGIN}\n{inside}{tmux.BLOCK_END}\n"
        "set -g mouse on\n"                      # USER pref below the block — live
    )
    out = tmux.neutralize_inline_rig_lines(original)
    b, e = out.index(tmux.BLOCK_BEGIN), out.index(tmux.BLOCK_END)
    assert tmux.NEUTRALIZE_PREFIX not in out[b:e], "rig's own managed block was wrongly neutralized"
    assert tmux.NEUTRALIZE_PREFIX in out[:b], "the USER line above the block must be neutralized"
    assert "set -g mouse on" in out[e:], "the USER pref below the block must stay live"


def test_neutralize_leaves_rig_option_inside_user_brace_block_live():
    """A rig-owned option a user GUARDED inside a non-Moshi `if-shell '…' { … }` conditional stays
    LIVE (multi- and single-line): commenting just the inner line would leave a dangling/empty
    brace body (a parse hazard) and break the user's deliberate condition. Under-reach is preferred
    over over-reach here (review findings 1+2). The Moshi wipe brace is still fully commented."""
    multi = "if-shell '[ -n \"$SOME\" ]' {\n  set -g @continuum-restore 'on'\n}\n"
    assert tmux.neutralize_inline_rig_lines(multi) == multi, "multi-line user brace must stay live"
    single = "if-shell '[ -n \"$SOME\" ]' { set -g @continuum-restore 'on' }\n"
    assert tmux.neutralize_inline_rig_lines(single) == single, "single-line user brace must stay live"
    # contrast: the Moshi wipe brace IS fully commented (braces included → no dangling brace).
    moshi = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n  set -g status-right ''\n}\n"
    out = tmux.neutralize_inline_rig_lines(moshi)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "the Moshi wipe block must be fully commented (no dangling brace)"
    # a top-level rig-owned option AFTER the brace block is still neutralized (brace_depth resets).
    after = multi + "set -g @continuum-restore 'on'\n"
    out2 = tmux.neutralize_inline_rig_lines(after)
    live = [ln for ln in out2.splitlines() if not ln.lstrip().startswith("#")]
    # the guarded one (inside braces) survives; the top-level one (after `}`) is neutralized.
    assert sum(1 for ln in live if "@continuum-restore" in ln) == 1, \
        "the guarded option survives; the top-level one after the brace is neutralized"


def test_neutralize_decorative_brace_in_value_does_not_leak_skip():
    """A literal `{` inside a quoted value (e.g. `status-left "…{…"`) must NOT be treated as a
    block opener — otherwise the NEXT rig-owned line would be wrongly skipped (left live → the
    double-init survives). The brace tracker keys on STRUCTURAL `{`-at-end-of-line, not a raw
    count, so the following @plugin/run-shell init is still neutralized (review finding)."""
    original = (
        "set -g status-left \"session:#S{\"\n"   # decorative brace in a value
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any("status-left" in ln for ln in live), "the status-left value must survive"
    assert not any("tmux-plugins/tmux-continuum'" in ln for ln in live), \
        "the @plugin after a decorative brace must still be neutralized"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the init after a decorative brace must still be neutralized"


def test_neutralize_nested_user_braces_keep_inner_lines_live():
    """NESTED `if-shell '…' { … if-shell '…' { … } … }` conditionals: an INNER `}` must not
    re-expose the OUTER block (a boolean in-brace flag had that bug). Every rig-owned line at any
    depth > 0 stays live (no dangling brace); a line after the OUTERMOST `}` is neutralized."""
    original = (
        "if-shell '[ -n \"$A\" ]' {\n"
        "  if-shell '[ -n \"$B\" ]' {\n"
        "    set -g @continuum-restore 'on'\n"   # depth 2 — live
        "  }\n"
        "  set -g @continuum-restore 'on'\n"     # depth 1 (after inner close) — live
        "}\n"
        "set -g @continuum-restore 'on'\n"       # depth 0 — neutralized
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert sum(1 for ln in live if "@continuum-restore" in ln) == 2, \
        "both nested (depth>0) lines stay live; only the top-level one is neutralized"
    assert tmux.NEUTRALIZE_PREFIX in out, "the top-level line must be neutralized"


def test_neutralize_closing_brace_with_trailing_text_decrements_depth():
    """A closing brace line that is not a BARE `}` — `} # end` or `} <directive>` — must still
    close the conditional, so a rig-owned init AFTER it is neutralized (else the double-init the
    fix targets survives silently). A `}` inside a quoted VALUE never false-closes (it starts with
    the set verb, not `}`)."""
    original = (
        "if-shell '[ -n \"$X\" ]' {\n"
        "  set -g @continuum-restore 'on'\n"   # inside — live
        "} # end conditional\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"  # after close — neutralized
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any("@continuum-restore" in ln for ln in live), "inside-brace line stays live"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the init after `} # end` must be neutralized (depth decremented past the tailed close)"
    # a literal `}` inside a value is not a closer.
    value = "set -g status-right \"}\"\nset -g @plugin 'tmux-plugins/tpm'\n"
    out2 = tmux.neutralize_inline_rig_lines(value)
    assert tmux.NEUTRALIZE_PREFIX in out2, "the @plugin after a value-`}` is still neutralized"


def test_neutralize_bare_wipe_inside_user_brace_stays_live():
    """A bare `set -g status-right ''` a user GUARDED inside their OWN non-Moshi `{ … }` stays
    live — commenting just it would dangle the brace. A bare wipe at TOP level is still
    neutralized, and the Moshi-guarded wipe block is still commented whole."""
    guarded = "if-shell '[ -n \"$X\" ]' {\n  set -g status-right ''\n}\n"
    assert tmux.neutralize_inline_rig_lines(guarded) == guarded, "guarded bare wipe must stay live"
    assert tmux.neutralize_inline_rig_lines("set -g status-right ''\n").lstrip().startswith(
        tmux.NEUTRALIZE_PREFIX
    ), "a top-level bare wipe is still neutralized"


def test_neutralize_moshi_block_with_literal_brace_in_value_does_not_overconsume():
    """The multi-line Moshi-block scan uses STRUCTURAL depth (end-of-line `{` / first-token `}`),
    so a literal `{` inside a value WITHIN the block (`status-left \"x{\"`) does not over-consume
    to EOF and swallow the lines after the block. The Moshi block is commented whole; the line
    after the closing `}` stays live."""
    original = (
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "  set -g status-left \"x{\"\n"   # a literal brace in a value
        "  set -g status-right ''\n"
        "}\n"
        "set -g mouse on\n"               # MUST stay live (not consumed past the close)
    )
    out = tmux.neutralize_inline_rig_lines(original)
    live = [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")]
    assert any(ln.strip() == "set -g mouse on" for ln in live), "line after the block must stay live"
    assert not any("status-right ''" in ln for ln in live), "the Moshi wipe must be commented"
    assert not any("status-left" in ln for ln in live), "the whole Moshi block must be commented"


def test_neutralize_known_limitation_set_hook_init_stays_live():
    """KNOWN LIMITATION (pinned, not a bug): a continuum init run via an indirection rig does not
    model — `set-hook -g session-created 'run-shell …/continuum.tmux'` — is NOT neutralized (the
    run-shell is a quoted VALUE, not the directive). Documented in _is_rig_owned_legacy_line; this
    test makes any future widening of the matcher a DELIBERATE change, not a silent one."""
    original = "set-hook -g session-created 'run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux'\n"
    assert tmux.neutralize_inline_rig_lines(original) == original


def test_neutralize_known_limitation_xdg_plugin_init_stays_live():
    """KNOWN LIMITATION (pinned, not a bug): rig models only the DEFAULT ~/.tmux/plugins/ location
    (it clones plugins there and inits from that path). An init from a custom/XDG plugin dir
    (~/.config/tmux/plugins/…) is NOT neutralized — but the path-independent @plugin decl + the
    @continuum-*/@resurrect-* options of such a user still ARE. Documented at _PLUGIN_INIT_ENTRYPOINTS."""
    xdg_init = "run-shell ~/.config/tmux/plugins/tmux-continuum/continuum.tmux\n"
    assert tmux.neutralize_inline_rig_lines(xdg_init) == xdg_init
    # the @plugin decl + options are still neutralized (path-independent).
    P = tmux.NEUTRALIZE_PREFIX
    assert tmux.neutralize_inline_rig_lines(
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
    ).lstrip().startswith(P)


def test_neutralize_handles_set_option_and_setw_variants():
    """A rig-owned option set via `set-option` / `set-window-option` / `setw` (not just `set -g`)
    is neutralized too — the matcher anchors on the verb + the option name, not a literal `set -g`."""
    original = (
        "set-option -ga @continuum-restore 'on'\n"
        "set-window-option -g @resurrect-strategy-vim 'session'\n"
        "setw -g @continuum-save-interval '5'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "set-option/setw rig-owned options must be neutralized"


def test_neutralize_handles_indented_and_run_shell_tpm_variants():
    """An INDENTED rig-owned init and tpm launched via `run-shell` (not bare `run`) are both
    neutralized (the matcher works on the stripped line, and tpm accepts either run form)."""
    original = (
        "    run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "run-shell '~/.tmux/plugins/tpm/tpm'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    assert not [ln for ln in out.splitlines() if not ln.lstrip().startswith("#")], \
        "every rig-owned init (indented / run-shell tpm) must be neutralized"


def test_neutralize_bare_single_line_moshi_wipe():
    """A bare `set -g status-right ''` (not wrapped in if-shell) is also neutralized."""
    original = "set -g mouse on\nset -g status-right ''\n"
    out = tmux.neutralize_inline_rig_lines(original)
    assert "set -g mouse on" in out and not any(
        "status-right ''" in ln and not ln.lstrip().startswith("#") for ln in out.splitlines()
    )


def test_neutralize_keeps_plain_user_conf_untouched():
    """A conf with no Moshi wipe has nothing to neutralize — every line stays live."""
    plain = "set -g mouse on\nset -g history-limit 100000\nbind r source-file ~/.tmux.conf\n"
    assert tmux.neutralize_inline_rig_lines(plain) == plain


def test_neutralize_keeps_a_real_status_right_value_live():
    """A status-right with an actual value (not the empty wipe) is a user choice — keep it live."""
    original = "set -g status-right '#{battery}'\nset -g mouse on\n"
    assert tmux.neutralize_inline_rig_lines(original) == original


def test_neutralize_is_idempotent():
    original = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\nset -g mouse on\n"
    once = tmux.neutralize_inline_rig_lines(original)
    twice = tmux.neutralize_inline_rig_lines(once)
    assert once == twice


# ── migration detection: rig-owned settings already inline ───────────────────────────────
def test_detect_inline_rig_settings():
    """First apply detects rig-owned settings already hand-written inline (resurrect/continuum/
    tpm/Moshi) so the migration knows there is something to lift into the import/block.
    """
    handwritten = (
        "set -g @plugin 'tmux-plugins/tpm'\n"
        "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "run '~/.tmux/plugins/tpm/tpm'\n"
    )
    assert tmux.has_inline_rig_settings(handwritten) is True


def test_detect_no_inline_rig_settings():
    plain = "set -g mouse on\nset -g history-limit 100000\n"
    assert tmux.has_inline_rig_settings(plain) is False


def test_detect_bare_moshi_wipe_triggers_backup():
    """A conf whose ONLY rig-relevant line is a bare `set -g status-right ''` must still count as
    'has inline settings' — migration will neutralize it, so the backup contract requires a
    backup first (codex P2)."""
    bare = "set -g mouse on\nset -g status-right ''\n"
    assert tmux.has_inline_rig_settings(bare) is True


def test_detect_update_environment_moshi_client_does_not_trigger_backup():
    """`set-option -ga update-environment ' MOSHI_CLIENT'` is a PERSONAL pref migration keeps live
    (NOT the wipe). It must NOT count as 'has inline settings' — the OLD substring `MOSHI_CLIENT`
    marker made it trigger a backup on EVERY apply (perpetual churn). `has_inline_rig_settings` is
    now defined AS 'neutralization changes the region', so the backup gate stays consistent with
    what neutralization does — this pins that consistency directly (not just transitively)."""
    pref = "set -g mouse on\nset-option -ga update-environment ' MOSHI_CLIENT'\n"
    assert tmux.has_inline_rig_settings(pref) is False
    # a fully-migrated import conf (commented legacy + the pref live) is likewise NOT migratable.
    migrated = tmux.neutralize_inline_rig_lines(
        "set -g @plugin 'tmux-plugins/tpm'\nset-option -ga update-environment ' MOSHI_CLIENT'\n"
    )
    assert tmux.has_inline_rig_settings(migrated) is False


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data, repo_root):
    from riglib.config import LoadedConfig

    return LoadedConfig(data=data, repo_root=repo_root)


def _build(data, repo_root, fake_agent_tools):
    from riglib.catalog import Catalog
    from riglib.plan import build

    data = {"agent_tools_source": str(fake_agent_tools), **data}
    cat = Catalog.scan(str(fake_agent_tools))
    return build(_cfg(data, repo_root), cat, project_type="unknown")


def test_plan_no_tmux_when_absent(fake_agent_tools, tmp_path):
    plan = _build({}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_no_tmux_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_emits_tmux_action(fake_agent_tools, tmp_path):
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tmux"]
    assert len(acts) == 1
    a = acts[0]
    assert a.category == "tmux" and a.item == "config"
    assert a.options["apply_mode"] == "import"


def test_plan_disables_autosave_off_darwin(fake_agent_tools, tmp_path, monkeypatch):
    """The autosave LaunchAgent is macOS-only — on a non-darwin host the plan forces
    autosave.enabled False so the generated config keeps continuum's own save (never disabling it
    without a replacement runner → the user would lose autosave entirely). codex P1."""
    import riglib.plan as plan_mod

    monkeypatch.setattr(plan_mod.sys, "platform", "linux")
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["autosave"]["enabled"] is False


def test_plan_keeps_autosave_on_darwin(fake_agent_tools, tmp_path, monkeypatch):
    """On darwin the plan does NOT force autosave off — the default (knob absent → on) stands."""
    import riglib.plan as plan_mod

    monkeypatch.setattr(plan_mod.sys, "platform", "darwin")
    plan = _build({"tmux": {"enabled": True}}, tmp_path, fake_agent_tools)
    a = [a for a in plan.actions if a.kind == "provision_tmux"][0]
    assert a.options["autosave"].get("enabled") is not False


def test_plan_provisions_when_enabled_is_null(fake_agent_tools, tmp_path):
    """`enabled: null` is 'not false' → provision (a present-but-empty-ish block opts in)."""
    plan = _build({"tmux": {"enabled": None, "apply": "import"}}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_provisions_for_empty_tmux_block(fake_agent_tools, tmp_path):
    """An explicit empty mapping `tmux: {}` is a PRESENT block → it opts in with all defaults
    (codex P2: it must not be treated as absent)."""
    plan = _build({"tmux": {}}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tmux"]


def test_plan_resolves_relative_paths_against_repo_root(fake_agent_tools, tmp_path):
    """A relative tmux.conf_path / generated_dir must resolve against the -C REPO ROOT, not the
    process CWD — so `rig apply -C /repo` from elsewhere never writes outside the repo. (codex P2.)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _build(
        {"tmux": {"enabled": True, "conf_path": "sub/.tmux.conf",
                  "generated_dir": "sub/gen"}},
        repo, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tmux")
    # the options carry ABSOLUTE, repo-anchored paths (not the bare relative strings).
    assert a.options["conf_path"] == str(repo / "sub" / ".tmux.conf")
    assert a.options["generated_dir"] == str(repo / "sub" / "gen")


def test_plan_carries_resolved_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tmux": {"enabled": True, "apply": "block", "moshi": {"enabled": True},
                  "continuum": {"save_interval": 9},
                  "login_shell": {"enabled": False, "shell": "/bin/zsh"}}},
        tmp_path, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tmux")
    assert a.options["apply_mode"] == "block"
    assert a.options["moshi"]["enabled"] is True
    assert a.options["continuum"]["save_interval"] == 9
    assert a.options["login_shell"] == {"enabled": False, "shell": "/bin/zsh"}


def test_apply_login_shell_default_command_lands_in_generated_conf(tmp_path, monkeypatch):
    """The login-shell default-command (DEFECT 3) reaches the generated rig.tmux.conf on apply."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g default-command" in conf and "-l" in conf


def test_apply_login_shell_disabled_omits_default_command(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, login_shell={"enabled": False}), "backup")
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g default-command" not in conf


# ── install (runner) — import mode, idempotent, migration + backup ───────────────────────
def _tmux_action(home, **over):
    """A provision_tmux action whose artifacts land under a tmp HOME."""
    from riglib.plan import Action

    options = {
        "apply_mode": "import",
        "conf_path": str(home / ".tmux.conf"),
        "generated_dir": str(home / ".config" / "rig" / "tmux"),
        "resurrect": {},
        "continuum": {},
        "moshi": {},
        "cc_restore": {},
        "anti_sprawl": {},
        "boot": {"enabled": False},  # tests never touch launchd by default
    }
    options.update(over)
    return Action(
        kind="provision_tmux",
        category="tmux",
        item="config",
        source=home,
        target=home / ".tmux.conf",
        options=options,
    )


def _stage_live_tmux_state(home):
    """Create the LIVE activation state (the resurrect dir + complete plugin checkouts) a real
    apply would. Drift now checks these (codex finding), but the unit suite applies under
    RIG_TMUX_DRY_RUN (no live activation), so an in-sync drift test must stage them itself —
    otherwise drift correctly reports 'no plugins / no resurrect dir'. Uses the REAL plugin
    entrypoints from tmux.PLUGINS (NOT `<dir>.tmux` — resurrect ships `resurrect.tmux`)."""
    (home / ".tmux" / "resurrect").mkdir(parents=True, exist_ok=True)
    plugins = home / ".tmux" / "plugins"
    for name, (_repo, entry) in tmux.PLUGINS.items():
        (plugins / name).mkdir(parents=True, exist_ok=True)
        (plugins / name / entry).write_text("#!/usr/bin/env bash\n", encoding="utf-8")


def test_apply_import_writes_generated_file_and_import_line(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "updated", "backed_up")
    rig_conf = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    assert rig_conf.is_file()
    assert "@resurrect-processes" in rig_conf.read_text()
    conf = (home / ".tmux.conf").read_text()
    assert f"source-file '{rig_conf}'" in conf


def test_apply_installs_cc_scripts_executable(tmp_path, monkeypatch):
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")
    gdir = home / ".config" / "rig" / "tmux"
    for name in ("cc-save.sh", "cc-restore.sh"):
        p = gdir / name
        assert p.is_file()
        assert os.access(p, os.X_OK), f"{name} must be executable"


def test_apply_installs_attach_script_when_anti_sprawl_on(tmp_path, monkeypatch):
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": True, "session": "main"}), "backup")
    p = home / ".config" / "rig" / "tmux" / "tmux-attach.sh"
    assert p.is_file() and os.access(p, os.X_OK)
    assert "attach-session" in p.read_text() and "new-session" in p.read_text()


def test_apply_omits_attach_script_when_anti_sprawl_off(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": False}), "backup")
    assert not (home / ".config" / "rig" / "tmux" / "tmux-attach.sh").exists()


def test_apply_import_is_idempotent(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    res2 = runner._do_provision_tmux(a, "backup")
    assert res2.status == "skipped", res2.detail


def test_apply_migrates_and_backs_up_handwritten_conf(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # a hand-written conf with rig-owned settings inline, the BUGGY Moshi wipe, AND a user line.
    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
        "set -g mouse on  # user line\n",
        encoding="utf-8",
    )
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "updated", "backed_up")
    # original backed up to a UNIQUE timestamped ~/.tmux.conf.rig-bak-<UTC> (never clobbering).
    baks = list(home.glob(".tmux.conf.rig-bak-*"))
    assert len(baks) == 1, f"expected one timestamped backup, got {baks}"
    bak_text = baks[0].read_text()
    assert "set -g mouse on" in bak_text and "status-right ''" in bak_text
    # the new conf carries the import line exactly once.
    new_text = conf.read_text()
    assert sum(1 for ln in new_text.splitlines() if ln.strip().startswith("source-file")) == 1
    # the BUGGY Moshi wipe AND the rig-owned plugin/continuum init are neutralized (commented);
    # the user's own line stays LIVE.
    live = [ln for ln in new_text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("status-right ''" in ln for ln in live), "Moshi wipe must be neutralized"
    assert not any("@continuum-restore" in ln for ln in live), \
        "rig-owned @continuum option must be neutralized (the double-init fix)"
    assert not any("continuum.tmux" in ln for ln in live), \
        "the rig-owned continuum init run-shell must be neutralized (the double-init bug)"
    assert "set -g mouse on  # user line" in new_text  # user line preserved live


_FULL_LEGACY_CONF = (
    "# --- personal prefs ---\n"
    "set -g mouse on\n"
    "set -g history-limit 100000\n"
    "set -g base-index 1\n"
    "set -g @plugin 'tmux-plugins/tmux-sensible'\n"   # third-party — must survive
    "set-option -ga update-environment ' MOSHI_CLIENT'\n"
    "# --- the old hand-written tpm/resurrect/continuum init (rig-owned) ---\n"
    "set -g @plugin 'tmux-plugins/tpm'\n"
    "set -g @plugin 'tmux-plugins/tmux-resurrect'\n"
    "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
    "set -g @resurrect-processes 'ssh psql ~rails'\n"
    "set -g @resurrect-capture-pane-contents 'on'\n"
    "set -g @continuum-restore 'on'\n"
    "set -g @continuum-boot 'on'\n"
    "set -g @continuum-boot-options 'iterm'\n"
    "set -g @continuum-save-interval '5'\n"
    "run-shell ~/.tmux/plugins/tmux-resurrect/resurrect.tmux\n"
    "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
    "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
    "run '~/.tmux/plugins/tpm/tpm'\n"
)

# the rig-owned legacy lines that MUST end up neutralized (commented) after migration.
_RIG_OWNED_NEEDLES = (
    "@plugin 'tmux-plugins/tpm'",
    "@plugin 'tmux-plugins/tmux-resurrect'",
    "@plugin 'tmux-plugins/tmux-continuum'",
    "@resurrect-processes", "@resurrect-capture-pane-contents",
    "@continuum-restore", "@continuum-boot", "@continuum-boot-options", "@continuum-save-interval",
    "tmux-resurrect/resurrect.tmux", "tmux-continuum/continuum.tmux",
    "tmux/plugins/tpm/tpm", "status-right ''",
)
# personal prefs + the third-party plugin that MUST stay live.
_PRESERVED_NEEDLES = (
    "set -g mouse on", "set -g history-limit 100000", "set -g base-index 1",
    "@plugin 'tmux-plugins/tmux-sensible'", "update-environment ' MOSHI_CLIENT'",
)


def _user_region_lines(new_text):
    """Live (non-comment) lines OUTSIDE rig's managed block. In block mode rig's OWN generated
    block legitimately carries live `@plugin`/`run-shell` lines (that IS the managed config) —
    the migration contract is only about the USER's hand-written region, so the assertions look
    there. In import mode there is no block, so this is every live line."""
    out, in_block = [], False
    for ln in new_text.splitlines():
        s = ln.strip()
        if s == tmux.BLOCK_BEGIN:
            in_block = True
            continue
        if s == tmux.BLOCK_END:
            in_block = False
            continue
        if not in_block and not s.startswith("#"):
            out.append(ln)
    return out


def _assert_full_migration(new_text):
    live_joined = "\n".join(_user_region_lines(new_text))
    # (a) every rig-owned init/option/decl line in the USER's region is neutralized (none live).
    for needle in _RIG_OWNED_NEEDLES:
        assert needle not in live_joined, f"rig-owned user line still LIVE (double-init): {needle}"
    # (b) third-party plugin + personal prefs survive uncommented.
    for needle in _PRESERVED_NEEDLES:
        assert needle in live_joined, f"preserved line wrongly neutralized: {needle}"


@pytest.mark.parametrize("apply_mode", ["import", "block"])
def test_apply_full_legacy_init_neutralized_both_modes(tmp_path, monkeypatch, apply_mode):
    """The live-machine bug fix end-to-end (2026-06-18): a hand-written conf with the FULL old
    tpm/resurrect/continuum init + personal prefs + a third-party plugin. After migration in
    BOTH apply modes: (a) every rig-owned init line is commented, (b) tmux-sensible + prefs stay
    live, (c) the source-file import / managed block is present exactly once, (d) re-applying is
    a no-op (no double-comment), and the original is backed up first.
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text(_FULL_LEGACY_CONF, encoding="utf-8")

    res = runner._do_provision_tmux(_tmux_action(home, apply_mode=apply_mode), "backup")
    assert res.status in ("created", "updated", "backed_up")

    # the original (with the live legacy init) is backed up before the rewrite.
    baks = list(home.glob(".tmux.conf.rig-bak-*"))
    assert len(baks) == 1, f"expected one timestamped backup, got {baks}"
    assert "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux" in baks[0].read_text()

    migrated = conf.read_text()
    _assert_full_migration(migrated)
    # (c) the managed region is present exactly once.
    if apply_mode == "import":
        n = sum(1 for ln in migrated.splitlines() if ln.strip().startswith("source-file '"))
        assert n == 1, f"expected exactly one source-file import, got {n}"
    else:
        assert migrated.count(tmux.BLOCK_BEGIN) == 1 and migrated.count(tmux.BLOCK_END) == 1

    # (d) re-applying the already-migrated conf is a no-op: no double-comment, no extra backup,
    # byte-identical result.
    runner._do_provision_tmux(_tmux_action(home, apply_mode=apply_mode), "backup")
    reapplied = conf.read_text()
    assert reapplied == migrated, "re-apply must be byte-identical (idempotent migration)"
    assert reapplied.count(tmux.NEUTRALIZE_PREFIX + tmux.NEUTRALIZE_PREFIX) == 0, "no double-comment"
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 1, "clean re-apply must not re-backup"


def test_apply_reusering_a_readded_rig_option_settles_to_idempotent(tmp_path, monkeypatch):
    """The re-add path is HONEST + bounded (review finding 1): if the user re-adds a live rig-owned
    option (rig owns the surface — a bare re-add inside ~/.tmux.conf is re-neutralized by design),
    the next apply backs the conf up ONCE and neutralizes it, then SETTLES — it does NOT re-backup
    or re-comment on every subsequent apply (that would break the idempotency contract)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\nset -g @continuum-boot-options 'iterm'\n", encoding="utf-8")

    runner._do_provision_tmux(_tmux_action(home), "backup")  # migrate
    migrated = conf.read_text()
    # clean re-applies never re-backup (already-migrated is stable).
    runner._do_provision_tmux(_tmux_action(home), "backup")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert conf.read_text() == migrated
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 1, "clean re-apply must not re-backup"

    # the user RE-ADDS the option live → exactly ONE more backup, then settle.
    conf.write_text(conf.read_text() + "set -g @continuum-boot-options 'iterm'\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    settled = conf.read_text()
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 2, "a live re-add backs up once"
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert conf.read_text() == settled, "after the re-add is neutralized, apply settles (idempotent)"
    assert len(list(home.glob(".tmux.conf.rig-bak-*"))) == 2, "settled re-apply must not re-backup"


def test_apply_block_mode_preserves_user_line_after_block_end(tmp_path, monkeypatch):
    """Block mode (review finding 3): a personal line the user placed AFTER the managed BLOCK_END
    survives migration uncommented (it's outside rig's region), and a rig-owned line the user put
    after BLOCK_END is still neutralized (rig owns that surface everywhere in ~/.tmux.conf)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    # first migrate a legacy conf in block mode so a managed block exists.
    conf.write_text(
        "set -g @continuum-restore 'on'\n"
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n",
        encoding="utf-8",
    )
    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    # the user appends, AFTER the managed block, a personal pref + a stray rig-owned option.
    migrated = conf.read_text()
    conf.write_text(migrated + "set -g mouse on\nset -g @continuum-save-interval '99'\n", encoding="utf-8")

    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    final = conf.read_text()
    all_lines = final.splitlines()
    end_idx = next(i for i, ln in enumerate(all_lines) if ln.strip() == tmux.BLOCK_END)
    after_end = all_lines[end_idx + 1:]  # the lines the user appended AFTER the managed block
    live_after = [ln for ln in after_end if not ln.lstrip().startswith("#")]
    # the personal pref the user put after the block stays live, IN that after-block region …
    assert any(ln.strip() == "set -g mouse on" for ln in live_after), \
        "user pref after BLOCK_END must survive live"
    # … and the rig-owned line the user put after the block is neutralized (rig owns that surface
    # everywhere in ~/.tmux.conf, not only above the block).
    assert not any(
        "@continuum-save-interval '99'" in ln for ln in all_lines if not ln.lstrip().startswith("#")
    ), "a rig-owned line is neutralized wherever it sits in ~/.tmux.conf"
    assert any("@continuum-save-interval '99'" in ln for ln in after_end), \
        "the neutralized rig-owned line stays (as a comment) in the after-block region"
    assert final.count(tmux.BLOCK_BEGIN) == 1 and final.count(tmux.BLOCK_END) == 1


def test_apply_creates_a_fresh_timestamped_backup_each_migration(tmp_path, monkeypatch):
    """A second apply, after the user hand-edited ~/.tmux.conf back into a migrating state, must
    snapshot the NEW content under its OWN timestamped backup — not skip because an earlier
    backup exists (which would lose the in-between edits). Earlier backups stay intact. (CTO)
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    # a precious backup from an earlier apply must be preserved untouched.
    precious = home / ".tmux.conf.rig-bak-20000101T000000_000000Z"
    precious.write_text("A-PRECIOUS-EARLIER-BACKUP\n", encoding="utf-8")

    moshi_wipe = "if-shell '[ -n \"$MOSHI_CLIENT\" ]' { set -g status-right '' }\n"
    # first migrating apply
    conf.write_text(moshi_wipe + "FIRST-EDIT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    # user hand-edits the conf back into a migrating state (re-adds the wipe + new content)
    conf.write_text(moshi_wipe + "SECOND-EDIT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")

    baks = sorted(home.glob(".tmux.conf.rig-bak-*"))
    contents = "".join(b.read_text() for b in baks)
    # the precious earlier backup is intact, AND each migrating apply added its own snapshot —
    # nothing lost.
    assert precious.read_text() == "A-PRECIOUS-EARLIER-BACKUP\n"
    assert "A-PRECIOUS-EARLIER-BACKUP" in contents
    assert "FIRST-EDIT" in contents and "SECOND-EDIT" in contents
    assert len(baks) >= 3


def test_apply_honors_on_conflict_skip_for_user_conf(tmp_path, monkeypatch):
    """on_conflict=skip + an existing differing ~/.tmux.conf → leave it UNWIRED (codex P2).

    The generated artifacts honor skip via fsutil; the user's own file must too — a direct
    rewrite that ignores skip violates rig's conflict contract.
    """
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    # the user's conf is left exactly as-is (no import line spliced in).
    assert conf.read_text() == "set -g mouse on\n"
    assert "source-file" not in conf.read_text()
    assert "on_conflict=skip" in res.detail


def test_apply_skip_does_not_chmod_a_conflicting_script(tmp_path, monkeypatch):
    """on_conflict=skip + a pre-existing DIFFERING managed script (not +x) → leave it untouched,
    including its mode (don't mutate a user file under skip) (codex P2)."""
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    foreign = gdir / "cc-save.sh"
    foreign.write_text("#!/usr/bin/env bash\n# the user's own thing\n", encoding="utf-8")
    foreign.chmod(0o644)  # not executable
    runner._do_provision_tmux(_tmux_action(home), "skip")
    # the differing file is left as-is — NOT chmod'd to 0755 (skip honored on mode too).
    assert not os.access(foreign, os.X_OK)
    assert foreign.read_text() == "#!/usr/bin/env bash\n# the user's own thing\n"


def test_apply_heals_exec_bit_on_identical_script(tmp_path, monkeypatch):
    """identical-skip IS ours — a stripped +x on rig's own (identical) script is healed AND the
    repair is REPORTED (not hidden behind 'already current') (codex P3)."""
    import os

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "backup")  # install
    cc = home / ".config" / "rig" / "tmux" / "cc-save.sh"
    cc.chmod(0o644)  # strip +x on rig's own (now-identical) script
    res = runner._do_provision_tmux(_tmux_action(home), "backup")  # re-apply heals it
    assert os.access(cc, os.X_OK)
    assert res.status != "skipped" and "restored +x" in res.detail  # the repair is surfaced


def test_apply_surfaces_conflict_skipped_script_not_wired(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING pre-existing managed script → surface that the resurrect
    hook points at an UNMANAGED file (not silently 'already current') (codex P2)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    (gdir / "cc-save.sh").write_text("#!/usr/bin/env bash\n# foreign\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    assert "NOT applied" in res.detail and "cc-save.sh" in res.detail


def test_apply_surfaces_conflict_skipped_generated_conf(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING (stale) rig.tmux.conf → surface that tmux still sources the
    STALE config (e.g. an upgrade still carrying @continuum-boot 'on'), not silently 'current'
    (codex P2)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    # pre-seed a STALE generated config that differs from the new render.
    (gdir / "rig.tmux.conf").write_text("# stale\nset -g @continuum-boot 'on'\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "skip")
    assert "rig.tmux.conf differs" in res.detail and "STALE" in res.detail


def test_apply_conflict_skip_only_reports_skipped_not_created(tmp_path, monkeypatch):
    """A conflict-skipped script while everything else is already current must report `skipped`
    (NOT `created`) — nothing was written, so a re-apply stays idempotent and doesn't inflate
    ApplyReport.changed every run (codex P2)."""
    from riglib.actions import runner
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # everything current
    # now make ONE script differ (a user-owned foreign file at rig's path).
    (home / ".config" / "rig" / "tmux" / "cc-save.sh").write_text("# foreign\n", encoding="utf-8")
    res = runner._do_provision_tmux(a, "skip")
    assert res.status == "skipped"  # unresolved drift, reported — not a 'created' change
    assert "NOT applied" in res.detail
    # and ApplyReport.changed is NOT incremented for this no-write apply.
    report = run_plan(InstallPlan(actions=[a], on_conflict="skip"))
    assert report.changed == 0


def test_apply_creates_conf_when_absent_even_under_skip(tmp_path, monkeypatch):
    """skip only protects an EXISTING file — an absent ~/.tmux.conf is still created."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner._do_provision_tmux(_tmux_action(home), "skip")
    assert (home / ".tmux.conf").is_file()
    assert "source-file" in (home / ".tmux.conf").read_text()


def test_apply_block_mode_splices_only_managed_region(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on  # mine\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, apply_mode="block"), "backup")
    text = conf.read_text()
    assert tmux.BLOCK_BEGIN in text and tmux.BLOCK_END in text
    assert "set -g mouse on  # mine" in text  # user line preserved
    assert "@resurrect-processes" in text  # managed body present inline (no import)
    assert "source-file" not in text  # block mode does NOT add an import


def test_apply_preserves_user_lines_in_plain_conf(tmp_path, monkeypatch):
    """import mode into a conf with NO rig settings: keep every user line, add only the import."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("set -g mouse on\nbind r source-file ~/.tmux.conf\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    text = conf.read_text()
    assert "set -g mouse on" in text
    assert "bind r source-file ~/.tmux.conf" in text
    assert (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").is_file()


def test_apply_writes_boot_plist_on_darwin_only(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # darwin → plist written
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    assert plist.is_file()
    import plistlib
    assert plistlib.loads(plist.read_bytes())["Label"] == "ai.hyperide.tmux-boot"


def test_apply_skips_boot_plist_off_darwin(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "linux")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist").exists()


def test_apply_writes_autosave_plist_and_script_on_darwin(tmp_path, monkeypatch):
    """The independent autosave agent (#138): apply writes the launchd plist AND the wrapper
    script (executable), and continuum's own save is disabled in the generated conf."""
    import os as _os
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist"
    script = home / ".config" / "rig" / "tmux" / "tmux-autosave.sh"
    assert plist.is_file() and script.is_file()
    assert _os.access(script, _os.X_OK)  # the launchd agent runs it by path
    conf = (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").read_text()
    assert "set -g @continuum-save-interval '0'" in conf  # one authoritative saver


def test_apply_omits_autosave_when_disabled(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": False}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").exists()
    assert not (home / ".config" / "rig" / "tmux" / "tmux-autosave.sh").exists()


def test_drift_tmux_autosave_plist(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").unlink()
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("autosave launchd plist" in d.detail and d.direction == "missing" for d in drift)


def test_apply_resolves_unexpanded_tilde_conf_path(tmp_path, monkeypatch):
    """The plan→action→runner path with an UNEXPANDED ~/.tmux.conf must resolve against HOME."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home, conf_path="~/.tmux.conf",
                     generated_dir="~/.config/rig/tmux")
    runner._do_provision_tmux(a, "backup")
    assert (home / ".tmux.conf").is_file()
    assert f"source-file '{home / '.config' / 'rig' / 'tmux' / 'rig.tmux.conf'}'" in (home / ".tmux.conf").read_text()


def test_apply_removes_stale_import_pointing_at_old_dir(tmp_path, monkeypatch):
    """A pre-existing rig import from an OLD generated_dir is dropped (one current import only)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text(
        "set -g mouse on\n"
        "source-file /old/place/rig.tmux.conf\n"        # stale rig import (different dir)
        "bind r run-shell 'echo rig.tmux.conf'\n",       # a user binding that merely names it
        encoding="utf-8",
    )
    runner._do_provision_tmux(_tmux_action(home), "backup")
    text = conf.read_text()
    assert "source-file /old/place/rig.tmux.conf" not in text  # stale import removed
    assert text.count("rig.tmux.conf") >= 1
    assert "bind r run-shell 'echo rig.tmux.conf'" in text  # the user binding is preserved
    # exactly one current import line.
    cur = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    assert text.count(f"source-file '{cur}'") == 1


def test_apply_does_not_drop_user_line_mentioning_rig_conf(tmp_path, monkeypatch):
    """A user comment/binding that merely MENTIONS rig.tmux.conf is NOT treated as the import."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    conf = home / ".tmux.conf"
    conf.write_text("# I source rig.tmux.conf below\nset -g mouse on\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert "# I source rig.tmux.conf below" in conf.read_text()  # comment preserved


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tmux(tmp_path, monkeypatch):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    report = run_plan(InstallPlan(actions=[_tmux_action(home)]))
    assert not report.errors
    assert report.changed == 1


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_tmux_missing(tmp_path, monkeypatch):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    report = detect(InstallPlan(actions=[_tmux_action(home)]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "missing"


def test_drift_tmux_in_sync_after_apply(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    report = detect(InstallPlan(actions=[a]))
    assert not [d for d in report.items if d.category == "tmux"]


def test_drift_tmux_live_state_missing(tmp_path, monkeypatch):
    """DEFECTS 4/6 drift: a missing resurrect dir / plugin checkout is surfaced by `rig status`
    so a clean machine doesn't read as in-sync while apply still has live work to do (codex
    finding). Here the live state is NOT staged → drift reports both."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # writes the config artifacts (dry-run: no live activation)
    # status checks the LIVE state only on a real machine (under RIG_TMUX_DRY_RUN apply skips the
    # live activation, so status correctly suppresses the matching live drift to stay consistent).
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("resurrect" in d.detail and d.direction == "missing" for d in drift)
    assert any("plugin" in d.detail and "tpm" in str(d.target) for d in drift)


def test_drift_tmux_live_state_suppressed_under_dry_run(tmp_path, monkeypatch):
    """Under RIG_TMUX_DRY_RUN apply skips the LIVE activation (no plugin clone, no resurrect
    dir), so status MUST suppress the matching live-state drift — else status would report drift
    apply deliberately won't converge (apply/status disagree, and status could never read in-sync
    under the flag, e.g. CI/smoke). The file-artifact drift stays checked; only the live half is
    gated. (The autouse _isolate_tmux_activation fixture already sets the flag for this test.)"""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")  # writes config artifacts; dry-run skips live activation
    # the resurrect dir + plugin checkouts are absent (dry-run never created them), but with the
    # flag set status must NOT flag them — only the live half is gated, file artifacts stay in-sync.
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert not any("resurrect" in d.detail or "plugin" in d.detail for d in drift), drift


def test_drift_tmux_modified_generated_file(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    # tamper with the generated file → drift on the managed region.
    rig_conf = home / ".config" / "rig" / "tmux" / "rig.tmux.conf"
    rig_conf.write_text("HAND EDITED\n", encoding="utf-8")
    report = detect(InstallPlan(actions=[a]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "modified"


def test_drift_tmux_missing_import_line(tmp_path, monkeypatch):
    """The generated file is correct but ~/.tmux.conf lost its import line → drift."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    (home / ".tmux.conf").write_text("set -g mouse on\n", encoding="utf-8")  # import dropped
    report = detect(InstallPlan(actions=[a]))
    drift = [d for d in report.items if d.category == "tmux"]
    assert drift and drift[0].direction == "missing"


def test_drift_tmux_flags_stale_import_alongside_current(tmp_path, monkeypatch):
    """The current import is present BUT an old `source-file …/rig.tmux.conf` (from a moved
    generated_dir) is ALSO live → modified drift (apply removes the stale one) (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    conf = home / ".tmux.conf"
    # append a STALE rig import (old dir) on top of the correct current one.
    conf.write_text(conf.read_text() + "source-file /old/place/rig.tmux.conf\n", encoding="utf-8")
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any(d.direction == "modified" and "STALE" in d.detail for d in drift)


def test_drift_tmux_flags_line_after_the_import(tmp_path, monkeypatch):
    """A user line AFTER rig's import undoes the ordering guarantee (it runs after rig.tmux.conf)
    → modified drift; apply re-appends the import last (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    conf = home / ".tmux.conf"
    # a trailing executable line AFTER the rig import (the classic re-breaks-the-bug case).
    conf.write_text(conf.read_text() + "set -g status-right ''\n", encoding="utf-8")
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any(d.direction == "modified" and "LAST line" in d.detail for d in drift)


def test_drift_tmux_block_mode_missing_and_modified(tmp_path, monkeypatch):
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home, apply_mode="block")
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    # in sync right after apply
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    # tamper INSIDE the managed block → 'modified' (content compared, not just sentinels)
    conf = home / ".tmux.conf"
    text = conf.read_text().replace("@resurrect-processes", "@resurrect-TAMPERED", 1)
    conf.write_text(text, encoding="utf-8")
    d1 = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux" and d.target == conf]
    assert d1 and d1[0].direction == "modified"
    # remove the block entirely → 'missing'
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    d2 = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux" and d.target == conf]
    assert d2 and d2[0].direction == "missing"


def test_drift_tmux_boot_plist(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    a = _tmux_action(home, boot={"enabled": True})
    runner._do_provision_tmux(a, "backup")
    _stage_live_tmux_state(home)  # a real apply would have created plugins + resurrect dir
    assert not [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    # delete the boot plist → drift surfaces it (it was previously unchecked).
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist").unlink()
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("boot launchd plist" in d.detail and d.direction == "missing" for d in drift)


def test_drift_tmux_flags_stripped_exec_bit_on_scripts(tmp_path, monkeypatch):
    """A cc script that lost its +x is drift — the resurrect hook invokes it by path (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    cc = home / ".config" / "rig" / "tmux" / "cc-save.sh"
    cc.chmod(0o644)  # strip the exec bit (content unchanged)
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    assert any("not executable" in d.detail and d.target == cc for d in drift)


def test_drift_tmux_flags_stale_boot_plist_when_disabled(tmp_path, monkeypatch):
    """boot disabled + a leftover plist on disk → reported as a disk→config extra (codex P2)."""
    import sys as _sys

    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    # apply WITH boot on → plist written
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist"
    assert plist.is_file()
    # now the config DISABLES boot — apply doesn't delete it, so drift must flag the leftover.
    a_off = _tmux_action(home, boot={"enabled": False})
    drift = [d for d in detect(InstallPlan(actions=[a_off])).items if d.category == "tmux"]
    assert any(d.direction == "extra" and "boot" in d.detail.lower() and d.target == plist for d in drift)


def test_drift_tmux_flags_stale_attach_script_when_disabled(tmp_path, monkeypatch):
    """anti_sprawl disabled + a leftover tmux-attach.sh on disk → reported as extra (codex P2)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # apply WITH anti-sprawl on → tmux-attach.sh written
    runner._do_provision_tmux(_tmux_action(home, anti_sprawl={"enabled": True}), "backup")
    attach = home / ".config" / "rig" / "tmux" / "tmux-attach.sh"
    assert attach.is_file()
    # now DISABLE anti-sprawl — apply wouldn't delete it, so drift must flag the leftover.
    a_off = _tmux_action(home, anti_sprawl={"enabled": False})
    drift = [d for d in detect(InstallPlan(actions=[a_off])).items if d.category == "tmux"]
    assert any(d.direction == "extra" and d.target == attach for d in drift)


def test_apply_surfaces_generated_file_backups(tmp_path, monkeypatch):
    """on_conflict=backup that moves a differing generated file must surface the .rig-bak-* path
    in the result (the 'backup-noted' contract), not silently drop it (codex P3)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gdir = home / ".config" / "rig" / "tmux"
    gdir.mkdir(parents=True)
    # pre-seed a DIFFERING rig.tmux.conf so apply backs it up under on_conflict=backup.
    (gdir / "rig.tmux.conf").write_text("OLD HAND-EDITED CONFIG\n", encoding="utf-8")
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status == "backed_up"
    assert res.backup is not None and ".rig-bak" in res.backup.name
    assert "backups:" in res.detail


# ── DEFECTS 1/4/5/6: live activation (plugins, resurrect dir, first save, launchctl, cleanup) ──
def _activation_seams(monkeypatch, *, plugins_present=False, launchctl_loaded=False, autosave_loaded=True):
    """Stub the live seams an activation touches and return a record of the calls made.

    Records git clones, launchctl verbs, tmux `resurrect save` runs, and continuum boot-cleanup
    runs — so a test can assert WHICH side effects an activation performed without any real
    network / daemon / tmux-server access. ``autosave_loaded`` defaults True so the independent
    autosave agent (#138) is a steady-state no-op unless a test opts to see its first bootstrap.
    """
    from riglib.actions import runner

    rec = {"clones": [], "launchctl": [], "load_w": [], "saves": 0, "cleanups": 0,
           "autosave_bootstrap": [], "autosave_bootout": []}

    def _clone(repo, dest):
        rec["clones"].append((repo, str(dest)))
        Path(dest).mkdir(parents=True, exist_ok=True)
        return 0

    def _load_w(plist):
        rec["load_w"].append(str(plist))
        return 0

    def _cleanup(plan):
        rec["cleanups"] += 1
        return True

    monkeypatch.setattr(runner, "_git_clone", _clone)
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: rec["launchctl"].append((verb, arg)) or 0)
    monkeypatch.setattr(runner, "_launchctl_load_enable", _load_w)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: launchctl_loaded)
    monkeypatch.setattr(runner, "_tmux_resurrect_save", lambda plan: rec.update(saves=rec["saves"] + 1) or 0)
    monkeypatch.setattr(runner, "_clean_stale_continuum_boot", _cleanup)
    # the independent autosave agent (#138) uses the gui-domain launchctl verbs.
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: autosave_loaded)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", lambda p: rec["autosave_bootstrap"].append(str(p)) or 0)
    monkeypatch.setattr(runner, "_launchctl_bootout", lambda p: rec["autosave_bootout"].append(str(p)) or 0)
    return rec


def test_activation_creates_resurrect_dir(tmp_path, monkeypatch):
    """DEFECT 4: ~/.tmux/resurrect must EXIST after apply, else resurrect never writes a
    snapshot (tmux_resurrect_*.txt) → nothing to restore on reboot."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)  # run the real activation
    _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert (home / ".tmux" / "resurrect").is_dir()


def test_activation_clones_missing_plugins(tmp_path, monkeypatch):
    """DEFECT 6: on a CLEAN machine ~/.tmux/plugins is empty, so the @plugin declarations don't
    resolve. Activation must clone tpm + resurrect + continuum."""
    from riglib.actions import runner
    from riglib import tmux as tmod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    cloned = {repo for repo, _dest in rec["clones"]}
    assert cloned == {url for url, _entry in tmod.PLUGINS.values()}


def test_activation_skips_already_present_plugins(tmp_path, monkeypatch):
    """Idempotent: a COMPLETE plugin checkout that already exists is NOT re-cloned."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    # pre-create a COMPLETE tpm checkout (its `tpm` entrypoint present) so it's "already installed".
    tpm = home / ".tmux" / "plugins" / "tpm"
    tpm.mkdir(parents=True)
    (tpm / "tpm").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    cloned_dests = {dest for _repo, dest in rec["clones"]}
    assert not any(d.endswith("/tpm") for d in cloned_dests)  # tpm not re-cloned
    assert any(d.endswith("/tmux-resurrect") for d in cloned_dests)  # the missing ones still cloned


def test_activation_recloned_partial_plugin_dir(tmp_path, monkeypatch):
    """A partial/broken plugin dir (from a failed clone) is NOT treated as installed — it is
    cleared and re-cloned, so the 'offline retries next apply' contract holds (codex finding)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    # a tpm dir that EXISTS but is missing its `tpm` entrypoint → partial → must be re-cloned.
    partial = home / ".tmux" / "plugins" / "tpm"
    partial.mkdir(parents=True)
    (partial / "stray.txt").write_text("leftover from a failed clone\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert any(d.endswith("/tpm") for _r, d in rec["clones"]), "partial tpm was not re-cloned"


def test_activation_launchctl_loads_the_boot_agent_with_w(tmp_path, monkeypatch):
    """DEFECT 1: `rig apply` must `launchctl load -w` the boot agent (it previously wrote the
    plist but NEVER loaded it → the agent didn't fire). `-w` enables it across reboots."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    # a `launchctl load -w <plist>` must have been issued for the boot plist (via the dedicated
    # _launchctl_load_enable helper, which builds `launchctl load -w <plist>` with -w as its own
    # token — see the helper's docstring for why it is separate from the 2-arg _launchctl).
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-boot.plist")
    assert rec["load_w"] == [plist], rec["load_w"]


def test_activation_bootstraps_autosave_agent(tmp_path, monkeypatch):
    """The independent autosave agent (#138) is a stateless periodic daemon → activation loads it
    NOW (gui-domain bootstrap) so autosave starts immediately, even for the current server —
    unlike the boot agent which only fires at the next login. Not-loaded → bootstrap."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, autosave_loaded=False)  # not yet loaded → first bootstrap
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist")
    assert rec["autosave_bootstrap"] == [plist], rec["autosave_bootstrap"]


def _autosave_activation_home(tmp_path, monkeypatch):
    import sys as _sys

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    return home


def test_activation_reloads_autosave_when_plist_changed_and_loaded(tmp_path, monkeypatch):
    """Loaded already + the plist was (re)written this apply → bootout THEN bootstrap (refresh)."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    rec = _activation_seams(monkeypatch, autosave_loaded=True)  # already loaded
    # first apply writes the plist (a change) → since loaded, it must reload (bootout+bootstrap).
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    plist = str(home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist")
    assert rec["autosave_bootout"] == [plist]
    assert rec["autosave_bootstrap"] == [plist]


def test_activation_autosave_steady_state_is_noop(tmp_path, monkeypatch):
    """Loaded + the plist unchanged (a second apply) → neither bootout nor bootstrap is called."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    _activation_seams(monkeypatch, autosave_loaded=True)
    runner._do_provision_tmux(a, "backup")  # first apply writes + reloads
    rec = _activation_seams(monkeypatch, autosave_loaded=True)  # fresh recorder for the 2nd apply
    runner._do_provision_tmux(a, "backup")  # plist identical now → no launchctl churn
    assert rec["autosave_bootout"] == [] and rec["autosave_bootstrap"] == []


def test_activation_suppresses_autosave_load_when_script_conflict_skipped(tmp_path, monkeypatch):
    """A differing tmux-autosave.sh under on_conflict=skip (stale/unmanaged) suppresses the load —
    we never bootstrap an agent whose wrapper script rig didn't write."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a user-owned foreign wrapper at rig's path → conflict-skip on the next apply.
    (home / ".config" / "rig" / "tmux" / "tmux-autosave.sh").write_text("# foreign\n", encoding="utf-8")
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale wrapper never loaded


def test_activation_suppresses_autosave_load_when_plist_conflict_skipped(tmp_path, monkeypatch):
    """A differing autosave PLIST under on_conflict=skip suppresses the load (autosave_plist_conflicted
    → autosave_load_safe False), same as a conflict-skipped script."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a differing plist at rig's path → conflict-skip on the next apply.
    (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").write_text(
        "<plist>foreign</plist>", encoding="utf-8"
    )
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale plist never loaded


def test_activation_suppresses_autosave_load_when_rig_conf_conflict_skipped(tmp_path, monkeypatch):
    """A conflict-skipped rig.tmux.conf may still carry the OLD nonzero @continuum-save-interval;
    bootstrapping the autosave agent while continuum is also saving = the two-writer race this
    feature removes. So a stale generated config must also suppress the autosave load (codex P2)."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    a = _tmux_action(home, autosave={"enabled": True})
    runner._do_provision_tmux(a, "backup")  # everything current
    # a differing generated rig.tmux.conf at rig's path → conflict-skip on the next apply.
    (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").write_text(
        "# stale\nset -g @continuum-save-interval '15'\n", encoding="utf-8"
    )
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    runner._do_provision_tmux(a, "skip")
    assert rec["autosave_bootstrap"] == []  # suppressed — stale config could keep continuum saving


def test_activation_autosave_bootstrap_failure_is_a_warning(tmp_path, monkeypatch):
    """A non-zero `launchctl bootstrap` is surfaced as a warning, never a silent success."""
    from riglib.actions import runner

    home = _autosave_activation_home(tmp_path, monkeypatch)
    rec = _activation_seams(monkeypatch, autosave_loaded=False)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", lambda p: rec["autosave_bootstrap"].append(str(p)) or 1)
    res = runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    assert "autosave agent NOT loaded" in res.detail


def test_apply_skips_autosave_plist_off_darwin(tmp_path, monkeypatch):
    import sys as _sys

    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "linux")
    runner._do_provision_tmux(_tmux_action(home, autosave={"enabled": True}), "backup")
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tmux-autosave.plist").exists()


def test_activation_takes_a_first_resurrect_save(tmp_path, monkeypatch):
    """DEFECT 6: after apply, take a first `resurrect save` so there is a snapshot to restore."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert rec["saves"] >= 1


def test_activation_cleans_stale_continuum_boot(tmp_path, monkeypatch):
    """DEFECT 5: continuum's own osx_iterm/terminal_start_tmux.sh register as macOS Login Items
    and compete with rig's launchd agent. Activation must clean them (osx_disable.sh / bootout)."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert rec["cleanups"] >= 1


def test_activation_suppresses_boot_load_when_plist_conflict_skipped(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING boot plist on disk: activation must NOT launchctl-load it
    (loading a stale/unmanaged boot path despite skip semantics — codex finding)."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # pre-seed a DIFFERING boot plist so on_conflict=skip leaves it stale (conflict-skipped).
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "ai.hyperide.tmux-boot.plist").write_text("<plist>STALE</plist>\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "skip")
    assert rec["load_w"] == []  # the stale boot agent was NOT loaded
    # AND the stale-continuum-boot cleanup must NOT run: rig has not loaded its replacement, so
    # stripping continuum's own autostart now would leave NO tmux autostart at all (review finding).
    assert rec["cleanups"] == 0


def test_activation_does_not_reload_already_loaded_unchanged_agent(tmp_path, monkeypatch):
    """A steady-state re-apply (agent already loaded, plist unchanged) must NOT unload/reload the
    boot agent — so it never restarts it / re-spawns `main` every apply, and a transient load
    failure can't disable a working agent (review Medium/Low). The launchctl load is NOT called."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, launchctl_loaded=True)
    # first apply writes the plist (changed) and would load; then everything is current + loaded.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    rec["load_w"].clear()
    rec["launchctl"].clear()
    # second apply: plist unchanged + agent loaded → NO load, NO unload.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert rec["load_w"] == []  # not (re)loaded
    assert not any(v == "unload" for v, _a in rec["launchctl"])  # not unloaded


def test_activation_reloads_when_plist_changed_and_agent_loaded(tmp_path, monkeypatch):
    """When the plist is REWRITTEN (e.g. an upgrade changes the boot script path) AND the agent is
    already loaded, activation unloads + load -w's it so launchd picks up the new definition."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch, launchctl_loaded=True)
    # pre-seed a DIFFERING plist so this apply REWRITES it (boot_plist_changed) under backup.
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "ai.hyperide.tmux-boot.plist").write_text("<plist>OLD</plist>\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    plist = str(la / "ai.hyperide.tmux-boot.plist")
    assert rec["load_w"] == [plist]  # reloaded the changed plist
    assert ("unload", plist) in rec["launchctl"]  # unloaded first to refresh the definition


def test_failed_clone_warning_surfaced_on_changed_apply(tmp_path, monkeypatch):
    """A clone failure on a FIRST (config-writing → changed) apply must still surface the
    'plugin NOT installed' warning in the result detail, not be swallowed (review Low-4)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch)
    monkeypatch.setattr(runner, "_git_clone", lambda repo, dest: 1)  # offline: every clone fails
    # a FIRST apply (no config yet) → config is written → status is created/changed.
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status in ("created", "backed_up")
    assert "NOT installed" in res.detail  # the offline-clone warning reached the user


def test_activation_suppresses_boot_load_when_boot_script_conflict_skipped(tmp_path, monkeypatch):
    """on_conflict=skip + a DIFFERING tmux-boot.sh on disk: activation must NOT launchctl-load the
    agent (the plist runs THAT script, so loading would run a stale boot script — review P1)."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # pre-seed a DIFFERING boot script so on_conflict=skip leaves it stale (conflict-skipped).
    gen = home / ".config" / "rig" / "tmux"
    gen.mkdir(parents=True)
    (gen / "tmux-boot.sh").write_text("#!/usr/bin/env bash\necho STALE BOOT\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "skip")
    assert rec["load_w"] == []  # the agent that runs the stale boot script was NOT loaded


def test_activation_does_not_clean_continuum_boot_when_rig_boot_disabled(tmp_path, monkeypatch):
    """The stale-boot cleanup is gated on rig owning boot: if the user opted OUT of rig boot
    (boot.enabled false) but relies on continuum's own autostart, activation must NOT nuke it
    (opus finding)."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": False}), "backup")
    assert rec["cleanups"] == 0  # boot disabled → the user's own autostart is left alone


def test_activation_re_apply_is_a_noop(tmp_path, monkeypatch):
    """Idempotency (opus/codex finding): a second real activation with everything already present
    (plugins complete, boot agent loaded, a snapshot on disk) makes NO changes → the apply is a
    `skipped` no-op, never a spurious `created` on every run."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch, launchctl_loaded=True)  # agent already loaded → no re-load change
    # nothing stale to clean on a settled machine → cleanup is a no-op (not a change).
    monkeypatch.setattr(runner, "_clean_stale_continuum_boot", lambda plan: False)
    # pre-stage a fully-activated machine: complete plugin checkouts + a resurrect snapshot.
    _stage_live_tmux_state(home)
    (home / ".tmux" / "resurrect" / "tmux_resurrect_20260101T000000.txt").write_text(
        "snap\n", encoding="utf-8")
    # first apply writes the config artifacts; second must be a pure no-op.
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    res2 = runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    assert res2.status == "skipped", res2.detail


def test_activation_skips_first_save_when_snapshot_exists(tmp_path, monkeypatch):
    """The FIRST save fires ONLY when no snapshot exists — a re-apply must not re-save and risk
    clobbering a good snapshot with an empty/partial one (opus finding). Exercises the REAL
    _tmux_resurrect_save guard (not the seam stub)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    rec = _activation_seams(monkeypatch)
    # a snapshot already exists on disk.
    resurrect = home / ".tmux" / "resurrect"
    resurrect.mkdir(parents=True)
    (resurrect / "tmux_resurrect_20260101T000000.txt").write_text("snap\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    assert rec["saves"] == 0  # snapshot present → no re-save


def test_failed_clone_is_a_warning_not_a_change(tmp_path, monkeypatch):
    """A failed plugin clone (offline) is surfaced as a WARNING but must NOT mark the apply
    `changed` — else every re-apply falsely reports `created` (codex/opus idempotency finding).
    With nothing else to do (snapshot present, plugins the only gap), a clone-failure-only run is
    `skipped`, and the warning is in the detail."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("RIG_TMUX_DRY_RUN", raising=False)
    _activation_seams(monkeypatch)  # stub launchctl/cleanup/save; the clone is overridden below
    # make every clone fail (offline) …
    monkeypatch.setattr(runner, "_git_clone", lambda repo, dest: 1)
    # nothing stale to clean (so a clone-failure is the ONLY activation outcome).
    monkeypatch.setattr(runner, "_clean_stale_continuum_boot", lambda plan: False)
    # … and pre-stage the resurrect dir + a snapshot so NOTHING else is a change.
    resurrect = home / ".tmux" / "resurrect"
    resurrect.mkdir(parents=True)
    (resurrect / "tmux_resurrect_20260101T000000.txt").write_text("snap\n", encoding="utf-8")
    # second apply (config already current from a first) so only activation could change anything.
    runner._do_provision_tmux(_tmux_action(home), "backup")
    res2 = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res2.status == "skipped", res2.detail  # a failed clone did NOT inflate `changed`
    assert "NOT installed" in res2.detail  # the warning is still surfaced


def test_resurrect_save_does_not_start_a_server(tmp_path, monkeypatch):
    """_tmux_resurrect_save must NOT start a server just to snapshot it (it would save an empty
    pre-restore state). With no live server it returns non-zero and runs no save (opus finding)."""
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # install a real save.sh so the function gets past its existence check.
    save = home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    save.parent.mkdir(parents=True)
    save.write_text("#!/usr/bin/env bash\ntouch ${SAVED_MARKER:-/dev/null}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        import subprocess as sp
        # `tmux list-sessions` → non-zero (no server). Anything else shouldn't be reached.
        return sp.CompletedProcess(cmd, 1, stdout="", stderr="no server running")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
    plan = tmux.build_tmux(repo_home=home)
    rc = runner._tmux_resurrect_save(plan)
    assert rc != 0  # no live server → no save
    # it probed for a live server (list-sessions) but NEVER ran save.sh and NEVER started a server.
    assert any("list-sessions" in " ".join(c) for c in calls)
    assert not any("save.sh" in " ".join(c) for c in calls)
    assert not any("new-session" in " ".join(c) for c in calls)


def test_dry_run_skips_all_live_activation(tmp_path, monkeypatch):
    """RIG_TMUX_DRY_RUN=1 writes the on-disk artifacts but performs NO live effect (clone /
    launchctl / save / cleanup) — the seam the unit suite + CI rely on."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setenv("RIG_TMUX_DRY_RUN", "1")
    rec = _activation_seams(monkeypatch)
    runner._do_provision_tmux(_tmux_action(home, boot={"enabled": True}), "backup")
    # the files still land …
    assert (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").is_file()
    # … but NOTHING live ran.
    assert rec["clones"] == [] and rec["launchctl"] == []
    assert rec["saves"] == 0 and rec["cleanups"] == 0


def test_clean_continuum_boot_is_idempotent_noop_when_absent(tmp_path, monkeypatch):
    """The cleanup is idempotent: with no stale continuum boot present it does nothing and
    never errors (a clean machine has nothing to clean)."""
    import sys as _sys
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_sys, "platform", "darwin")
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0)
    plan = tmux.build_tmux(repo_home=home)
    # must not raise even though no Tmux.Start.plist / osx_disable.sh exists.
    runner._clean_stale_continuum_boot(plan)


def test_drift_tmux_reports_all_drifted_regions(tmp_path, monkeypatch):
    """Two regions drift at once → BOTH reported (no early-return masking the second)."""
    from riglib.actions import runner
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    a = _tmux_action(home)
    runner._do_provision_tmux(a, "backup")
    gdir = home / ".config" / "rig" / "tmux"
    (gdir / "rig.tmux.conf").write_text("TAMPERED\n", encoding="utf-8")   # region 1
    (gdir / "cc-save.sh").unlink()                                          # region 2
    drift = [d for d in detect(InstallPlan(actions=[a])).items if d.category == "tmux"]
    targets = {d.target.name for d in drift}
    assert "rig.tmux.conf" in targets and "cc-save.sh" in targets
