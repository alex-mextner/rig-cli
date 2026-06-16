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
    conf = tmux.build_tmux(repo_home=Path("/home/u"), continuum={"save_interval": 7}).render_rig_conf()
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
    assert parsed["ProgramArguments"][-1] == "start-server"


def test_boot_label_is_configurable():
    p = tmux.build_tmux(repo_home=Path("/home/u"), boot={"enabled": True, "label": "com.me.tmux"})
    assert p.boot_label == "com.me.tmux"
    assert "com.me.tmux" in p.render_boot_plist()
    assert p.boot_plist_path.name == "com.me.tmux.plist"


def test_boot_plist_tmux_bin_falls_back_to_existing_path(monkeypatch):
    """When tmux isn't on PATH, the boot plist must point at an EXISTING common location, not a
    blind Apple-silicon hard-code (codex P2)."""
    import plistlib

    monkeypatch.setattr(tmux.shutil, "which", lambda name: None)  # not on PATH
    # only the Intel path "exists"
    monkeypatch.setattr(tmux.Path, "exists", lambda self: str(self) == "/usr/local/bin/tmux")
    p = tmux.build_tmux(repo_home=Path("/home/u"))
    parsed = plistlib.loads(p.render_boot_plist().encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/usr/local/bin/tmux"


def test_boot_plist_passes_f_for_custom_conf_path():
    """A non-default conf_path must reach the login server via `-f`, else it starts WITHOUT
    the managed config (continuum/resurrect never set → no restore) (codex P2)."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"), conf_path="~/.config/tmux/custom.conf")
    args = plistlib.loads(p.render_boot_plist().encode("utf-8"))["ProgramArguments"]
    assert "-f" in args
    assert any(a.endswith("custom.conf") for a in args)
    assert args[-1] == "start-server"


def test_boot_plist_omits_f_for_default_conf_path():
    """The default ~/.tmux.conf is auto-loaded by tmux, so no `-f` is emitted (keeps the
    common-case plist minimal)."""
    import plistlib

    p = tmux.build_tmux(repo_home=Path("/home/u"))  # default conf_path
    args = plistlib.loads(p.render_boot_plist().encode("utf-8"))["ProgramArguments"]
    assert "-f" not in args
    assert args[-1] == "start-server"


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
def test_neutralize_comments_out_only_the_moshi_wipe():
    """import-mode migration NEUTRALIZES ONLY the inline Moshi status wipe (the bug) — and
    leaves every other line, including rig-adjacent options rig does not model, LIVE. (Lesson
    from a real migration: an over-broad neutralize dropped @resurrect-strategy-vim /
    @continuum-boot-options that rig doesn't re-emit.)
    """
    original = (
        "set -g @plugin 'tmux-plugins/tmux-continuum'\n"
        "set -g @continuum-restore 'on'\n"
        "set -g @resurrect-strategy-vim 'session'\n"   # rig does NOT model this — keep live
        "set -g @continuum-boot-options 'iterm'\n"      # rig does NOT model this — keep live
        "run-shell ~/.tmux/plugins/tmux-continuum/continuum.tmux\n"
        "set -g mouse on  # USER LINE\n"
        "if-shell '[ -n \"$MOSHI_CLIENT\" ]' {\n"
        "  set -g status-right ''\n"
        "}\n"
        "run '~/.tmux/plugins/tpm/tpm'\n"
    )
    out = tmux.neutralize_inline_rig_lines(original)
    # the Moshi if-shell wipe block IS neutralized (it's the actual bug)
    live_wipe = [ln for ln in out.splitlines() if "status-right ''" in ln and not ln.lstrip().startswith("#")]
    assert not live_wipe, "the inline Moshi status-right wipe must be neutralized"
    # EVERYTHING else stays live — including options rig does not re-emit (no settings loss).
    for kept in ("@continuum-restore", "@resurrect-strategy-vim", "@continuum-boot-options",
                 "continuum.tmux", "tmux/plugins/tpm/tpm", "set -g mouse on  # USER LINE"):
        live = [ln for ln in out.splitlines() if kept in ln and not ln.lstrip().startswith("#")]
        assert live, f"line wrongly neutralized (settings loss): {kept}"


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
                  "continuum": {"save_interval": 9}}},
        tmp_path, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tmux")
    assert a.options["apply_mode"] == "block"
    assert a.options["moshi"]["enabled"] is True
    assert a.options["continuum"]["save_interval"] == 9


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
    # original backed up to ~/.tmux.conf.rig-bak (never overwriting an existing backup).
    bak = home / ".tmux.conf.rig-bak"
    assert bak.is_file()
    assert "set -g mouse on" in bak.read_text() and "status-right ''" in bak.read_text()
    # the new conf carries the import line.
    new_text = conf.read_text()
    assert "source-file" in new_text
    # the BUGGY Moshi wipe is neutralized (commented); the continuum line + user line stay LIVE
    # (narrow neutralize — no settings loss).
    live_wipe = [ln for ln in new_text.splitlines()
                 if "status-right ''" in ln and not ln.lstrip().startswith("#")]
    assert not live_wipe, "inline Moshi wipe must be neutralized on migration"
    assert "set -g mouse on  # user line" in new_text  # user line preserved live
    live_cont = [ln for ln in new_text.splitlines()
                 if "@continuum-restore" in ln and not ln.lstrip().startswith("#")]
    assert live_cont, "the continuum option line must stay live (narrow neutralize)"


def test_apply_does_not_overwrite_existing_backup(tmp_path, monkeypatch):
    from riglib.actions import runner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".tmux.conf").write_text("set -g @continuum-restore 'on'\nORIGINAL\n", encoding="utf-8")
    bak = home / ".tmux.conf.rig-bak"
    bak.write_text("A-PRECIOUS-EARLIER-BACKUP\n", encoding="utf-8")
    runner._do_provision_tmux(_tmux_action(home), "backup")
    # the pre-existing backup must NOT be clobbered.
    assert bak.read_text() == "A-PRECIOUS-EARLIER-BACKUP\n"


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
    report = detect(InstallPlan(actions=[a]))
    assert not [d for d in report.items if d.category == "tmux"]


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
