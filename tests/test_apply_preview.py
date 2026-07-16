"""`rig apply` preview-by-default surface (info/commit) + the plan/notes UX.

The CTO redesign: a bare `rig apply` is a DRY-RUN alias for `rig apply info` (build + print
the plan, mutate NOTHING, point at `rig apply commit`); `rig apply commit` is the subcommand
that actually executes today's apply behavior. `--yes` (bare) is read as commit intent for
back-compat with automation. Both `info` and `commit` share the SAME plan engine — only
`commit` calls `actions.run_plan`.
"""

from __future__ import annotations

from pathlib import Path

from riglib.cli import main


def _small_config(tmp_path: Path, fake_agent_tools: Path) -> Path:
    """A minimal, real-applyable config: a couple of universal skills, everything else off."""
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "harness: {enabled: false}\npermissions: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n"
        "tmux: {enabled: false}\ntg_ctl: {enabled: false}\nmodels: {enabled: false}\n",
        encoding="utf-8",
    )
    return cfg


def _isolate_home(tmp_path, monkeypatch, fake_agent_tools) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    return home


def test_bare_apply_is_preview_mutates_nothing(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """Bare `rig apply` builds + prints the plan but writes NOTHING under HOME, and says so."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Plan:" in out
    # it announces it is a preview alias for `rig apply info` and pointed at the commit step
    assert "rig apply info" in out
    assert "rig apply commit" in out
    # NOTHING was applied: no results Summary, no skills copied under HOME
    assert "Summary:" not in out
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".agents" / "skills").exists()


def test_apply_info_equals_bare_preview(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig apply info` is the explicit preview — same plan, still mutates nothing."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "info", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Plan:" in out
    assert "rig apply commit" in out
    assert "Summary:" not in out
    assert not (home / ".claude" / "skills").exists()


def test_apply_commit_executes(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig apply commit` actually runs the plan and reports what changed."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "commit", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    # a real results Summary + the completion line, and skills actually installed under HOME
    assert "Summary:" in out
    assert "applied" in out  # the ✓ applied N actions completion line
    assert (home / ".claude" / "skills").exists()


def test_apply_yes_is_commit_intent(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig apply --yes` (bare + --yes) executes — the back-compat automation path."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "--yes", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Summary:" in out
    assert (home / ".claude" / "skills").exists()


def test_apply_commit_yes_headless(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig apply commit --yes` executes headless (the CI/script form)."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "commit", "--yes", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Summary:" in out
    assert (home / ".claude" / "skills").exists()


def test_apply_commit_is_idempotent_completion_line(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A second `rig apply commit` changes nothing → completion line says 0 changed."""
    _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    main(["apply", "commit", "-C", str(tmp_path), "--config", str(cfg)])
    capsys.readouterr()
    rc = main(["apply", "commit", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    # second run: everything already in sync → 0 changed. No-op rows are collapsed by default.
    assert "0 changed" in out


def test_apply_dry_run_still_previews(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`rig apply --dry-run` remains a preview (back-compat) even with an explicit commit."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "commit", "--dry-run", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Plan:" in out
    assert "Summary:" not in out
    assert not (home / ".claude" / "skills").exists()


def test_apply_still_refuses_without_config(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The no-config fail-closed guard fires BEFORE the info/commit split, on both paths."""
    _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["apply", "-C", str(repo)], ["apply", "commit", "-C", str(repo)]):
        rc = main(argv)
        out = capsys.readouterr().out
        assert rc == 2
        assert "no rig.yaml" in out


def test_notes_collapse_and_elevate_gap(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """Informational notes collapse behind a count; a real-gap note is elevated to a ⚠ line."""
    from riglib.cli import _print_notes

    notes = [
        "autonomous mode: review/fix until 2026-01-01 (max 5 iterations)",
        "autonomous mode: escalation framework skill 'foo'",
        "hook_bridge: skipped — agent_hooks disabled, so no descriptors to dispatch",
    ]
    _print_notes(notes, expand=False)
    out = capsys.readouterr().out
    # the gap note is elevated (visible, with a ⚠ marker)
    assert "hook_bridge: skipped" in out
    assert "⚠" in out
    # the two informational notes are collapsed behind a one-line count, not printed verbatim
    assert "escalation framework skill" not in out
    assert "2 informational" in out
    assert "--notes" in out


def test_apply_commit_writes_full_log_file(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """Every commit writes a full apply log to ~/.cache/rig/apply-<UTC>.log and prints its path."""
    home = _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "commit", "-C", str(tmp_path), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "full log:" in out

    logs = list((home / ".cache" / "rig").glob("apply-*.log"))
    assert len(logs) == 1, f"expected exactly one apply log, got {logs}"
    body = logs[0].read_text(encoding="utf-8")
    # the log carries the full record regardless of console verbosity: plan, results, summary
    assert "PLAN:" in body and "RESULTS:" in body and "SUMMARY:" in body
    assert "skills/" in body  # the actual actions are recorded


def test_apply_info_hint_preserves_config_and_only(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """The `rig apply commit` hint carries the preview's own -C/--config/--only, not a bare cmd."""
    _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = _small_config(tmp_path, fake_agent_tools)

    rc = main(["apply", "-C", str(tmp_path), "--config", str(cfg), "--only", "skills"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rig apply commit" in out
    assert f"--config {cfg}" in out
    assert "--only skills" in out


def test_apply_commit_verify_failure_downgrades_completion(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """A failed post-apply verify flips the completion marker to ✗ (never a green ✓ before exit 1)."""
    from riglib import verify

    _isolate_home(tmp_path, monkeypatch, fake_agent_tools)
    cfg = tmp_path / "rig.yaml"
    cfg.write_text(
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {enabled: false}\nagent_hooks: {enabled: false}\n"
        "ci: {enabled: false}\nmcp: {enabled: false}\ngit_hooks: {dispatcher: {enabled: false}}\n",
        encoding="utf-8",
    )

    @verify.register_verifier("provision_permissions")
    def _always_fail(action):
        return [verify.VerifyResult("permissions", "test", False, "forced failure for test")]

    try:
        rc = main(["apply", "commit", "-C", str(tmp_path), "--config", str(cfg)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "verify FAILED" in out
        # the completion line must NOT claim a clean ✓ apply when the run exits non-zero
        assert "✓ applied" not in out
    finally:
        verify._VERIFIERS.pop("provision_permissions", None)


def test_notes_expand_shows_all(tmp_path, capsys, fake_agent_tools, monkeypatch):
    """`--notes` expands the full list, informational notes included."""
    from riglib.cli import _print_notes

    notes = [
        "autonomous mode: escalation framework skill 'foo'",
        "hook_bridge: skipped — agent_hooks disabled, so no descriptors to dispatch",
    ]
    _print_notes(notes, expand=True)
    out = capsys.readouterr().out
    assert "escalation framework skill" in out
    assert "hook_bridge: skipped" in out
