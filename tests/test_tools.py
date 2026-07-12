"""Personal CLI ecosystem provisioning — config, spec resolution, install, idempotency, drift.

rig's PRIMARY purpose: install + advertise the tool ecosystem (tg/review/task/draw/…) at apply, by
running each tool's OWN install.sh. This module mirrors :mod:`test_tg_ctl`: stdlib-only validation
tests, HOME-isolated install/idempotency/conflict tests that NEVER touch the real ~/.local/bin or
~/.agents/skills and NEVER run a real tool's install.sh (a fake, fully-controlled install.sh writes
into the throwaway HOME), and drift detection.

HARD ISOLATION: every install/drift test points the tool's bin_dir + the skills dir at tmp dirs and
uses a fake install.sh, so no test can mutate the host's PATH dir, skills dir, or run a real
installer. conftest's autouse ``_isolate_scheduler`` stubs ``_do_provision_tools`` suite-wide; this
module restores the REAL handler (the dedicated-test override pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import codex_update
from riglib import drift as driftmod
from riglib import errors
from riglib import tools
from riglib.actions import runner
from riglib.config import ConfigError, validate
from riglib.plan import Action

# captured at import (before any monkeypatch) — the genuine handler.
_REAL_PROVISION = runner._do_provision_tools


@pytest.fixture(autouse=True)
def _real_tools(monkeypatch):
    """Restore the REAL provision_tools handler for THIS module (conftest stubs it to a no-op)."""
    monkeypatch.setattr(runner, "_do_provision_tools", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tools", _REAL_PROVISION)
    # clear any leaked dry-run flag so these tests exercise the live install path by default.
    monkeypatch.delenv("RIG_TOOLS_DRY_RUN", raising=False)


# ── config validation ─────────────────────────────────────────────────────────────────────
def test_tools_block_accepted():
    validate({"version": 1, "tools": {"enabled": True, "items": {"tg": {"repo": "~/x/tg"}}}})


def test_tools_block_empty_ok():
    validate({"version": 1, "tools": {}})


def test_tools_block_absent_ok():
    validate({"version": 1})


def test_tools_enabled_must_be_bool():
    with pytest.raises(ConfigError, match="tools.enabled"):
        validate({"version": 1, "tools": {"enabled": "yes"}})


def test_tools_target_must_be_str():
    with pytest.raises(ConfigError, match="tools.target"):
        validate({"version": 1, "tools": {"target": 7}})


def test_tools_unknown_top_key_rejected():
    with pytest.raises(ConfigError, match="tools"):
        validate({"version": 1, "tools": {"enabld": True}})


def test_tools_items_must_be_mapping():
    with pytest.raises(ConfigError, match="tools.items"):
        validate({"version": 1, "tools": {"items": ["tg"]}})


def test_tools_item_must_be_mapping():
    with pytest.raises(ConfigError, match="tools.items.tg"):
        validate({"version": 1, "tools": {"items": {"tg": "nope"}}})


def test_tools_item_unknown_key_rejected():
    with pytest.raises(ConfigError, match="tools.items.tg"):
        validate({"version": 1, "tools": {"items": {"tg": {"rebo": "/x"}}}})


def test_tools_item_repo_must_be_str():
    with pytest.raises(ConfigError, match="tools.items.tg.repo"):
        validate({"version": 1, "tools": {"items": {"tg": {"repo": 5}}}})


def test_tools_item_enabled_must_be_bool():
    with pytest.raises(ConfigError, match="tools.items.tg.enabled"):
        validate({"version": 1, "tools": {"items": {"tg": {"enabled": "yes"}}}})


def test_tools_item_bin_dir_must_be_str():
    with pytest.raises(ConfigError, match="tools.items.tg.bin_dir"):
        validate({"version": 1, "tools": {"items": {"tg": {"bin_dir": 9}}}})


def test_resolve_specs_default_target_when_no_target():
    specs = tools.resolve_tool_specs({"enabled": True, "items": {"tg": {"repo": "/r/tg"}}})
    assert specs[0].bin_dir == Path.home() / ".local" / "bin"


# ── spec resolution ───────────────────────────────────────────────────────────────────────
def test_resolve_specs_empty_when_disabled():
    assert tools.resolve_tool_specs({"enabled": False, "items": {"tg": {}}}) == []


def test_resolve_specs_empty_when_items_listed_but_enabled_omitted():
    # REGRESSION: a block that lists items but FORGETS `enabled: true` must provision NOTHING
    # (default-OFF). `enabled` absent → falsy → no specs. A `block.get("enabled") is False` check
    # would WRONGLY treat the missing key as enabled and surprise-install on the next apply.
    assert tools.resolve_tool_specs({"items": {"tg": {"repo": "/r/tg"}}}) == []


def test_resolve_specs_empty_when_absent():
    assert tools.resolve_tool_specs(None) == []
    assert tools.resolve_tool_specs({}) == []


def test_resolve_specs_default_repo_layout():
    specs = tools.resolve_tool_specs({"enabled": True, "items": {"draw": {}}})
    assert len(specs) == 1
    assert specs[0].name == "draw"
    assert specs[0].repo == Path.home() / "xp" / "draw-cli"
    assert specs[0].bin_dir == Path.home() / ".local" / "bin"


def test_resolve_specs_honors_repo_and_target_and_per_item_bindir():
    specs = tools.resolve_tool_specs(
        {
            "enabled": True,
            "target": "/opt/bin",
            "items": {
                "tg": {"repo": "/r/tg-cli"},
                "review": {"repo": "/r/review-cli", "bin_dir": "/usr/local/bin"},
            },
        }
    )
    by_name = {s.name: s for s in specs}
    assert by_name["tg"].repo == Path("/r/tg-cli")
    assert by_name["tg"].bin_dir == Path("/opt/bin")
    assert by_name["review"].bin_dir == Path("/usr/local/bin")


def test_resolve_specs_skips_disabled_item():
    specs = tools.resolve_tool_specs(
        {"enabled": True, "items": {"tg": {}, "draw": {"enabled": False}}}
    )
    assert [s.name for s in specs] == ["tg"]


# ── install handler ───────────────────────────────────────────────────────────────────────
def _fake_install_sh(repo: Path, bin_dir: Path, blurb_file: Path, marker: Path) -> None:
    """Write a fake install.sh into ``repo`` that mimics a real tool's install:

    symlink an entry into ``bin_dir``, write the skill blurb (advertise), and touch ``marker`` so a
    test can assert the script actually RAN (and count how many times). All paths are absolute so the
    script is HOME-independent.
    """
    repo.mkdir(parents=True, exist_ok=True)
    entry = repo / "entry"
    entry.write_text("#!/bin/sh\necho hi\n")
    entry.chmod(0o755)
    script = repo / "install.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'mkdir -p "{bin_dir}" "{blurb_file.parent}"\n'
        f'ln -sfn "{entry}" "{bin_dir / "demo"}"\n'
        f'printf "demo blurb\\n" > "{blurb_file}"\n'
        f'printf "x" >> "{marker}"\n'
    )
    script.chmod(0o755)


def _write_deploy_sh(repo: Path, *, sentinel: Path | None = None, exit_code: int = 0) -> None:
    """Write a fake ``scripts/deploy.sh`` into ``repo`` (the freshness hook rig runs on apply).

    Appends ``x`` to ``sentinel`` when given (so a test can count how many times deploy ran) and then
    exits ``exit_code`` — a non-zero code simulates the offline/dirty/diverged tree the real deploy.sh
    refuses on, so a test can assert that failure is downgraded to a warning, not an apply error.
    """
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    body = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if sentinel is not None:
        body += f'printf "x" >> "{sentinel}"\n'
    body += f"exit {exit_code}\n"
    script = scripts / "deploy.sh"
    script.write_text(body)
    script.chmod(0o755)


def _spec_for(tmp_path: Path, name: str = "demo") -> tuple[tools.ToolSpec, Path, Path]:
    """A ToolSpec wired into tmp dirs, with a fake install.sh + a run-marker. Returns (spec, blurb, marker)."""
    repo = tmp_path / f"{name}-cli"
    bin_dir = tmp_path / "bin"
    skills = tmp_path / "skills"
    blurb = skills / tools.BLURBS_SUBDIR / f"{name}.md"
    marker = tmp_path / "ran-marker"
    spec = tools.ToolSpec(name=name, repo=repo, bin_dir=bin_dir)
    _fake_install_sh(repo, bin_dir, blurb, marker)
    # point the spec's blurb file at the tmp skills dir (blurb_file derives from SKILLS_DIR=~/.agents).
    return spec, blurb, marker


def _run(spec: tools.ToolSpec, monkeypatch) -> runner.ActionResult:
    """Build a provision_tools action for one spec and run the real handler."""
    action = Action(
        kind="provision_tools",
        category="tools",
        item="ecosystem",
        source=spec.repo,
        target=spec.bin_dir,
        options={"specs": [tools.spec_to_option(spec)]},
    )
    return runner._do_provision_tools(action, "backup")


def _isolate_blurb(monkeypatch, blurb: Path) -> None:
    """Force ToolSpec.blurb_file to point at our tmp blurb path (it normally derives from ~/.agents)."""
    monkeypatch.setattr(
        tools.ToolSpec, "blurb_file", property(lambda self: blurb), raising=True
    )


def test_apply_installs_a_tool(tmp_path, monkeypatch):
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    res = _run(spec, monkeypatch)
    assert res.status == "created", res.detail
    assert (spec.bin_dir / "demo").exists()  # symlinked the bin
    assert blurb.is_file()  # advertised the skill
    assert marker.read_text() == "x"  # install.sh ran exactly once


def test_reapply_is_a_noop_when_current(tmp_path, monkeypatch):
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    first = _run(spec, monkeypatch)
    assert first.status == "created"
    second = _run(spec, monkeypatch)
    assert second.status == "skipped", second.detail
    # install.sh ran ONLY on the first apply (marker not appended a second time).
    assert marker.read_text() == "x"


def test_reapply_runs_deploy_script_when_present(tmp_path, monkeypatch):
    """A tool whose repo ships scripts/deploy.sh gets it run on EVERY apply — including when the
    tool is already installed — so a provisioned checkout is kept fresh. Contrast
    test_reapply_is_a_noop_when_current above (no deploy.sh → the reapply is a true no-op).
    """
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    sentinel = tmp_path / "deploy-ran"
    _write_deploy_sh(spec.repo, sentinel=sentinel)  # rc 0; touches the sentinel each run
    first = _run(spec, monkeypatch)
    assert first.status == "created", first.detail
    second = _run(spec, monkeypatch)
    assert second.status == "skipped", second.detail  # tool already installed → install short-circuits
    assert marker.read_text() == "x"  # install.sh ran ONCE (first apply only)
    assert sentinel.read_text() == "xx"  # deploy.sh ran on BOTH applies (freshness even when installed)
    assert "deploy" in second.detail.lower()  # the deploy result is folded into the reported message


def test_freshness_skipped_when_no_deploy_script(tmp_path, monkeypatch):
    """No scripts/deploy.sh in the repo → no deploy attempted, apply still succeeds (freshness is
    opt-in per tool: a tool wires it up by ADDING a deploy.sh)."""
    spec, blurb, marker = _spec_for(tmp_path)  # writes install.sh but NOT scripts/deploy.sh
    _isolate_blurb(monkeypatch, blurb)
    assert not spec.deploy_script.exists()
    res = _run(spec, monkeypatch)
    assert res.status == "created", res.detail
    assert marker.read_text() == "x"  # install ran
    assert "deploy" not in res.detail.lower()  # no deploy note when the tool has no deploy.sh


def test_deploy_failure_is_non_fatal(tmp_path, monkeypatch):
    """A non-zero deploy.sh exit (offline/dirty/diverged) is downgraded to a warning, NOT an error —
    the tool's install/skip outcome is unchanged. Mirrors the offline-safe _git_clone discipline."""
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    sentinel = tmp_path / "deploy-ran"
    _write_deploy_sh(spec.repo, sentinel=sentinel, exit_code=1)  # runs, then fails
    first = _run(spec, monkeypatch)
    assert first.status == "created", first.detail  # install succeeded; failing deploy did NOT fold to error
    second = _run(spec, monkeypatch)
    assert second.status == "skipped", second.detail  # already installed; failing deploy stays non-fatal
    assert sentinel.read_text() == "xx"  # deploy ran on both applies despite exiting non-zero
    assert "deploy" in second.detail.lower()  # the warning is surfaced, not silently swallowed


def test_deploy_not_run_when_install_script_absent(tmp_path, monkeypatch):
    """A repo with scripts/deploy.sh but NO install.sh is 'repo missing' (repo_present keys off
    install.sh) → it errors WITHOUT running deploy.sh. The missing-repo guard runs before freshness,
    so no script fires against a malformed/foreign repo path before the no-install.sh error."""
    repo = tmp_path / "half-cli"
    repo.mkdir()
    sentinel = tmp_path / "deploy-ran"
    _write_deploy_sh(repo, sentinel=sentinel)  # deploy.sh present…
    assert not (repo / "install.sh").exists()  # …but install.sh absent → repo_present is False
    spec = tools.ToolSpec(name="half", repo=repo, bin_dir=tmp_path / "bin")
    _isolate_blurb(monkeypatch, tmp_path / "skills" / "half.md")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    res = _run(spec, monkeypatch)
    assert res.status == "error"
    assert "no install.sh" in res.detail
    assert not sentinel.exists()  # deploy.sh must NOT have run before the missing-repo error


def test_already_installed_elsewhere_is_not_clobbered(tmp_path, monkeypatch):
    """A tool whose bin resolves on PATH into ITS OWN repo + already advertised → no-op.

    Simulates a Homebrew/.files `review`: shutil.which finds a path that real-paths back into the
    declared checkout, so rig must NOT re-run install.sh nor overwrite anything.
    """
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    # pre-advertise (skill blurb present) and make the bin resolve via PATH (into the repo's entry),
    # NOT the managed symlink — mirroring a Homebrew shim that points back at the checkout.
    blurb.parent.mkdir(parents=True, exist_ok=True)
    blurb.write_text("pre-existing blurb\n")
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(spec.repo / "entry"))
    res = _run(spec, monkeypatch)
    assert res.status == "skipped", res.detail
    assert not (spec.bin_dir / "demo").exists()  # rig did NOT create a managed symlink
    assert not marker.exists()  # install.sh never ran


def test_foreign_same_named_binary_does_not_count_as_installed(tmp_path, monkeypatch):
    """A stranger's `task`/`draw` on PATH (not into our repo) must NOT read as installed.

    With only a foreign binary present (and no managed symlink, no advertised skill), the tool is
    NOT installed — apply RE-installs it. This is the guard against a Taskwarrior `task` masquerading
    as our `task` and silently suppressing the install.
    """
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    foreign = tmp_path / "elsewhere" / "demo"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\n")
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(foreign))
    assert tools.tool_status(spec).bin_resolves is False  # foreign binary doesn't count
    res = _run(spec, monkeypatch)
    assert res.status == "created", res.detail  # rig installed it (didn't trust the stranger)
    assert marker.read_text() == "x"


def test_missing_repo_is_an_error(tmp_path, monkeypatch):
    spec = tools.ToolSpec(name="ghost", repo=tmp_path / "absent", bin_dir=tmp_path / "bin")
    _isolate_blurb(monkeypatch, tmp_path / "skills" / "ghost.md")
    res = _run(spec, monkeypatch)
    assert res.status == "error"
    assert "no install.sh" in res.detail


def test_dry_run_reports_without_running(tmp_path, monkeypatch):
    spec, blurb, marker = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    monkeypatch.setenv("RIG_TOOLS_DRY_RUN", "1")
    res = _run(spec, monkeypatch)
    assert res.status == "created"
    assert "dry-run" in res.detail
    assert not marker.exists()  # install.sh NOT run under dry-run


def test_install_sh_failure_is_an_error(tmp_path, monkeypatch):
    """A non-zero install.sh exit → error, with the failing tail surfaced in the detail."""
    repo = tmp_path / "demo-cli"
    repo.mkdir()
    (repo / "install.sh").write_text("#!/usr/bin/env bash\necho 'boom: dep missing' >&2\nexit 3\n")
    (repo / "install.sh").chmod(0o755)
    spec = tools.ToolSpec(name="demo", repo=repo, bin_dir=tmp_path / "bin")
    _isolate_blurb(monkeypatch, tmp_path / "skills" / "demo.md")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    res = _run(spec, monkeypatch)
    assert res.status == "error"
    assert "exited 3" in res.detail
    assert "boom: dep missing" in res.detail  # the tail of the script's output


def test_mixed_outcomes_fold_to_error_but_report_each(tmp_path, monkeypatch):
    """One tool installs, one fails → overall error, but both outcomes are in the detail."""
    good, good_blurb, good_marker = _spec_for(tmp_path, name="good")
    bad_repo = tmp_path / "bad-cli"
    bad_repo.mkdir()
    (bad_repo / "install.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bad_repo / "install.sh").chmod(0o755)
    bad = tools.ToolSpec(name="bad", repo=bad_repo, bin_dir=tmp_path / "bin")
    # both blurbs absent so neither is pre-installed; which() finds nothing.
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        tools.ToolSpec, "blurb_file",
        property(lambda self: tmp_path / "skills" / ".blurbs" / f"{self.name}.md"),
    )
    action = Action(
        kind="provision_tools", category="tools", item="ecosystem",
        source=tmp_path, target=tmp_path / "bin",
        options={"specs": [tools.spec_to_option(good), tools.spec_to_option(bad)]},
    )
    res = runner._do_provision_tools(action, "backup")
    assert res.status == "error"  # any failure folds to error
    assert "installed: good" in res.detail
    assert "FAILED" in res.detail and "bad" in res.detail
    assert good_marker.read_text() == "x"  # the good one still installed


def test_install_timeout_parse_is_crash_safe(monkeypatch):
    """A junk RIG_TOOL_INSTALL_TIMEOUT_S must NOT crash — it falls back to the 300s default."""
    monkeypatch.setenv("RIG_TOOL_INSTALL_TIMEOUT_S", "not-a-number")
    assert runner._tool_install_timeout_s() == 300
    monkeypatch.setenv("RIG_TOOL_INSTALL_TIMEOUT_S", "-5")
    assert runner._tool_install_timeout_s() == 300  # non-positive ignored
    monkeypatch.setenv("RIG_TOOL_INSTALL_TIMEOUT_S", "42")
    assert runner._tool_install_timeout_s() == 42
    monkeypatch.delenv("RIG_TOOL_INSTALL_TIMEOUT_S", raising=False)
    assert runner._tool_install_timeout_s() == 300


def test_no_specs_is_skipped(tmp_path):
    action = Action(
        kind="provision_tools", category="tools", item="ecosystem",
        source=tmp_path, target=tmp_path, options={"specs": []},
    )
    res = runner._do_provision_tools(action, "backup")
    assert res.status == "skipped"


# ── Codex safe update ─────────────────────────────────────────────────────────────
def _write_fake_codex(path: Path, version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        f"  --version) echo 'codex-cli {version}' ;;\n"
        "  --help) echo 'Codex CLI help' ;;\n"
        "  completion) echo '#compdef codex' ;;\n"
        "  *) echo 'ok' ;;\n"
        "esac\n"
    )
    path.chmod(0o755)


def _write_hanging_codex(path: Path) -> None:
    path.write_text("#!/bin/sh\nsleep 30\n")
    path.chmod(0o755)


def _write_failing_codex(path: Path) -> None:
    path.write_text("#!/bin/sh\necho 'broken candidate' >&2\nexit 42\n")
    path.chmod(0o755)


def _write_marker_dependent_codex(path: Path, marker: Path, version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f'test -f "{marker}" || exit 77\n'
        "case \"${1:-}\" in\n"
        f"  --version) echo 'codex-cli {version}' ;;\n"
        "  --help) echo 'Codex CLI help' ;;\n"
        "  completion) echo '#compdef codex' ;;\n"
        "  *) echo 'ok' ;;\n"
        "esac\n"
    )
    path.chmod(0o755)


def test_safe_codex_update_rolls_back_hanging_candidate(tmp_path):
    good = tmp_path / "codex-good"
    hanging = tmp_path / "codex-hangs"
    live = tmp_path / "bin" / "codex"
    backup_dir = tmp_path / "backups"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    live.symlink_to(good)

    update = tmp_path / "replace-with-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"ln -sfn {hanging} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=backup_dir,
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.is_symlink()
    assert live.resolve() == good
    assert codex_update.probe_codex(live, timeout_s=1.0).ok
    assert "timed out" in result.message


def test_safe_codex_update_rolls_back_nonzero_candidate(tmp_path):
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-fails"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)

    update = tmp_path / "replace-with-failing.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"ln -sfn {failing} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert "exited 42" in result.message


def test_safe_codex_update_restores_missing_original_symlink_target(tmp_path):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    live.symlink_to(good)

    update = tmp_path / "remove-old-and-replace-with-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"rm -f {good}\n"
        f"ln -sfn {hanging} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert good.is_file()
    assert codex_update.probe_codex(live, timeout_s=1.0).ok


def test_safe_codex_update_preserves_intermediate_symlink_on_rollback(tmp_path):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    shim = tmp_path / "opt" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    shim.symlink_to(good)
    live.symlink_to(shim)

    update = tmp_path / "remove-old-and-replace-top-link-with-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"rm -f {good}\n"
        f"ln -sfn {hanging} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.is_symlink()
    assert live.readlink() == shim
    assert shim.is_symlink()
    assert shim.readlink() == good
    assert good.is_file()
    assert codex_update.probe_codex(live, timeout_s=1.0).ok


def test_safe_codex_update_restores_retargeted_intermediate_symlink(tmp_path):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    hanging = tmp_path / "caskroom" / "0.144.1" / "codex"
    shim = tmp_path / "opt" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    hanging.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_hanging_codex(hanging)
    shim.symlink_to(good)
    live.symlink_to(shim)

    update = tmp_path / "retarget-intermediate-to-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"ln -sfn {hanging} {shim}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.is_symlink()
    assert live.readlink() == shim
    assert shim.is_symlink()
    assert shim.readlink() == good
    assert live.resolve() == good
    assert codex_update.probe_codex(live, timeout_s=1.0).ok


def test_safe_codex_update_restores_overwritten_original_symlink_target(tmp_path):
    good = tmp_path / "caskroom" / "0.142.4" / "codex"
    live = tmp_path / "bin" / "codex"
    good.parent.mkdir(parents=True)
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    live.symlink_to(good)

    update = tmp_path / "overwrite-old-target-with-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cat > {good} <<'SH'\n"
        "#!/usr/bin/env bash\n"
        "sleep 30\n"
        "SH\n"
        f"chmod +x {good}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert live.resolve() == good
    assert codex_update.probe_codex(live, timeout_s=1.0).ok
    assert "codex-cli 0.142.4" in good.read_text(encoding="utf-8")


def test_safe_codex_update_rolls_back_plain_binary(tmp_path):
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(live, "0.142.4")
    live.chmod(0o700)

    update = tmp_path / "overwrite-plain-binary-with-hang.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cat > {live} <<'SH'\n"
        "#!/usr/bin/env bash\n"
        "sleep 30\n"
        "SH\n"
        f"chmod +x {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "rolled_back", result.message
    assert not live.is_symlink()
    assert codex_update.probe_codex(live, timeout_s=1.0).ok
    assert live.stat().st_mode & 0o777 == 0o700
    assert result.backup_path is not None
    assert result.backup_path.stat().st_mode & 0o777 == 0o700


def test_safe_codex_update_errors_when_restored_codex_is_unhealthy(tmp_path):
    marker = tmp_path / "runtime-marker"
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-failing"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    marker.write_text("present", encoding="utf-8")
    _write_marker_dependent_codex(good, marker, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)

    update = tmp_path / "break-runtime-and-replace.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"rm -f {marker}\n"
        f"ln -sfn {failing} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "error"
    assert "rollback restored files but codex is still unhealthy" in result.message
    assert result.exit_code == errors.EXIT_CODEX_UPDATE


def test_safe_codex_update_reports_current_unhealthy_before_update(tmp_path):
    live = tmp_path / "bin" / "codex"
    marker = tmp_path / "update-ran"
    live.parent.mkdir()
    _write_hanging_codex(live)

    update = tmp_path / "update.sh"
    update.write_text(f"#!/usr/bin/env bash\nprintf x > {marker}\n")
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=0.1,
    )

    assert result.status == "error"
    assert result.exit_code == errors.EXIT_CODEX_UPDATE
    assert "current codex is not healthy" in result.message
    assert not marker.exists()


def test_safe_codex_update_reports_rollback_failure(tmp_path, monkeypatch):
    good = tmp_path / "codex-good"
    failing = tmp_path / "codex-fails"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(good, "0.142.4")
    _write_failing_codex(failing)
    live.symlink_to(good)

    update = tmp_path / "replace-with-failing.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"ln -sfn {failing} {live}\n"
    )
    update.chmod(0o755)
    monkeypatch.setattr(codex_update, "restore_snapshot", lambda snapshot: "permission denied")

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "error"
    assert result.exit_code == errors.EXIT_CODEX_UPDATE
    assert "rollback FAILED: permission denied" in result.message


def test_safe_codex_update_reports_backup_failure_before_update(tmp_path):
    live = tmp_path / "bin" / "codex"
    backup_file = tmp_path / "backup-file"
    marker = tmp_path / "update-ran"
    live.parent.mkdir()
    _write_fake_codex(live, "0.142.4")
    backup_file.write_text("not a directory", encoding="utf-8")

    update = tmp_path / "update.sh"
    update.write_text(f"#!/usr/bin/env bash\nprintf x > {marker}\n")
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=backup_file,
        probe_timeout_s=1.0,
    )

    assert result.status == "error"
    assert "could not back up current codex" in result.message
    assert not marker.exists()


def test_safe_codex_update_keeps_successful_candidate(tmp_path):
    old = tmp_path / "codex-old"
    new = tmp_path / "codex-new"
    live = tmp_path / "bin" / "codex"
    live.parent.mkdir()
    _write_fake_codex(old, "0.142.4")
    _write_fake_codex(new, "0.144.1")
    live.symlink_to(old)

    update = tmp_path / "replace-with-new.sh"
    update.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"ln -sfn {new} {live}\n"
    )
    update.chmod(0o755)

    result = codex_update.safe_update(
        codex_path=live,
        update_command=[str(update)],
        backup_dir=tmp_path / "backups",
        probe_timeout_s=1.0,
    )

    assert result.status == "updated", result.message
    assert live.resolve() == new
    assert "0.144.1" in result.message


def test_default_codex_update_command_uses_brew_only_for_brew_cask_codex(tmp_path, monkeypatch):
    brew = tmp_path / "homebrew" / "bin" / "brew"
    brew.parent.mkdir(parents=True)
    brew.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"--prefix\" ]; then dirname \"$(dirname \"$0\")\"; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 0; fi\n",
        encoding="utf-8",
    )
    brew.chmod(0o755)
    monkeypatch.setattr(codex_update.shutil, "which", lambda name: str(brew) if name == "brew" else None)

    cask_codex = tmp_path / "homebrew" / "Caskroom" / "codex" / "0.144.1" / "codex"
    brew_codex = tmp_path / "homebrew" / "bin" / "codex"
    other_codex = tmp_path / "other" / "codex"
    cask_codex.parent.mkdir(parents=True)
    brew_codex.symlink_to(cask_codex)

    assert codex_update.default_update_command(brew_codex) == [str(brew), "upgrade", "--cask", "codex"]
    assert codex_update.default_update_command(other_codex) == [str(other_codex), "update"]


def test_default_codex_update_command_does_not_treat_brew_prefix_binary_as_cask(tmp_path, monkeypatch):
    brew = tmp_path / "homebrew" / "bin" / "brew"
    codex = tmp_path / "homebrew" / "bin" / "codex"
    brew.parent.mkdir(parents=True)
    brew.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = \"--prefix\" ]; then dirname \"$(dirname \"$0\")\"; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 1; fi\n",
        encoding="utf-8",
    )
    brew.chmod(0o755)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(codex_update.shutil, "which", lambda name: str(brew) if name == "brew" else None)

    assert codex_update.default_update_command(codex) == [str(codex), "update"]


def test_default_codex_update_command_handles_intel_homebrew_symlink(tmp_path, monkeypatch):
    prefix = tmp_path / "usr-local"
    real_brew = prefix / "Homebrew" / "bin" / "brew"
    brew_link = prefix / "bin" / "brew"
    cask_codex = prefix / "Caskroom" / "codex" / "0.144.1" / "codex"
    codex = prefix / "bin" / "codex"
    real_brew.parent.mkdir(parents=True)
    brew_link.parent.mkdir(parents=True)
    cask_codex.parent.mkdir(parents=True)
    real_brew.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ \"${{1:-}}\" = \"--prefix\" ]; then printf '%s\\n' {prefix}; exit 0; fi\n"
        "if [ \"${1:-}\" = \"list\" ]; then exit 0; fi\n",
        encoding="utf-8",
    )
    real_brew.chmod(0o755)
    brew_link.symlink_to(real_brew)
    codex.symlink_to(cask_codex)
    monkeypatch.setattr(codex_update.shutil, "which", lambda name: str(brew_link) if name == "brew" else None)

    assert codex_update.default_update_command(codex) == [str(brew_link), "upgrade", "--cask", "codex"]


def test_run_bounded_returns_after_timeout_with_detached_pipe_holder(tmp_path):
    script = tmp_path / "detached-pipe-holder.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import os, time; os.setsid(); time.sleep(2)' &\n"
        "sleep 30\n"
    )
    script.chmod(0o755)

    result = codex_update.run_bounded([str(script)], timeout_s=0.1)

    assert result.timed_out


# ── drift ─────────────────────────────────────────────────────────────────────────────────
def _drift_action(spec: tools.ToolSpec) -> Action:
    return Action(
        kind="provision_tools", category="tools", item="ecosystem",
        source=spec.repo, target=spec.bin_dir,
        options={"specs": [tools.spec_to_option(spec)]},
    )


def test_drift_flags_uninstalled_tool_missing(tmp_path, monkeypatch):
    spec, blurb, _ = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)  # not on PATH either
    report = driftmod.DriftReport()
    driftmod._check_tools(_drift_action(spec), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "missing"
    assert "demo" in report.items[0].detail


def test_drift_flags_unadvertised_tool_modified(tmp_path, monkeypatch):
    spec, blurb, _ = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    # bin resolves (managed symlink present) but the skill blurb is absent → modified.
    spec.bin_dir.mkdir(parents=True, exist_ok=True)
    (spec.bin_dir / "demo").symlink_to(spec.repo / "entry")
    report = driftmod.DriftReport()
    driftmod._check_tools(_drift_action(spec), report)
    assert len(report.items) == 1
    assert report.items[0].direction == "modified"


def test_drift_clean_when_installed_and_advertised(tmp_path, monkeypatch):
    spec, blurb, _ = _spec_for(tmp_path)
    _isolate_blurb(monkeypatch, blurb)
    spec.bin_dir.mkdir(parents=True, exist_ok=True)
    (spec.bin_dir / "demo").symlink_to(spec.repo / "entry")
    blurb.parent.mkdir(parents=True, exist_ok=True)
    blurb.write_text("blurb\n")
    report = driftmod.DriftReport()
    driftmod._check_tools(_drift_action(spec), report)
    assert report.items == []


# ── plan ──────────────────────────────────────────────────────────────────────────────────
def test_plan_emits_no_action_when_default_off():
    from riglib import plan as planmod

    class _Cfg:
        data: dict = {}
        repo_root = Path("/tmp")

    p = planmod.InstallPlan()
    planmod._build_tools(_Cfg(), p)
    assert [a for a in p.actions if a.kind == "provision_tools"] == []


def test_plan_emits_one_action_when_enabled():
    from riglib import plan as planmod

    class _Cfg:
        data = {"tools": {"enabled": True, "items": {"tg": {"repo": "/r/tg"}}}}
        repo_root = Path("/tmp")

    p = planmod.InstallPlan()
    planmod._build_tools(_Cfg(), p)
    acts = [a for a in p.actions if a.kind == "provision_tools"]
    assert len(acts) == 1
    assert acts[0].options["specs"][0]["name"] == "tg"
