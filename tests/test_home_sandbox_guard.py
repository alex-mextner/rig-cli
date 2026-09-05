"""rig-cli#116: a HOME-overridden (sandboxed) run must NEVER reach the real launchd domain.

Every fsutil-based action follows ``Path.home()``, so a test / e2e / manual verification that only
overrides ``$HOME`` looks fully sandboxed — but ``launchctl`` operates on the per-user
``gui/<uid>`` domain, which HOME cannot redirect. On 2026-09-05 such a run bootstrapped the REAL
``ai.hyperide.tg-ctl`` agent from a plist under ``/var/folders/.../T/tmpXXXX/home/...`` (a
nonexistent tg-ctl binary in the scratch HOME), and it crash-looped 23,641 times (exit 78) until
booted out by hand.

The guard: before ANY live launchctl mutation, the runner compares the resolved ``Path.home()``
with the real login home of the current uid (``pwd.getpwuid``). If they differ, the action behaves
exactly like its ``RIG_*_DRY_RUN`` env — writes the artifact, skips the live mutation — and says
so in its detail. ONE shared predicate (``_home_is_sandboxed``) feeds all four launchd paths
(models schedule, tmux boot/autosave, tg_ctl, spotlight).

These tests do NOT stub the ``_launchctl*`` seams (the real ones, captured at import, are
restored) — instead ``subprocess.run`` is patched to FAIL on any ``launchctl`` argv — so a bypass
of the seams would fail too. The inverse tests pin ``_real_login_home`` to the FAKE home (never the
real one: the plist write is real and must stay in the tmp HOME).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from riglib import spotlight
from riglib import tg_ctl as tgmod
from riglib.actions import runner
from riglib import drift as driftmod
from riglib.plan import Action, InstallPlan

# The genuine handlers + launchctl seams, captured at import (before conftest's autouse stubs).
_REAL = {
    name: getattr(runner, name)
    for name in (
        "_do_provision_tg_ctl", "_do_provision_schedule",
        "_launchctl", "_launchctl_loaded", "_launchctl_load_enable",
        "_launchctl_bootstrap", "_launchctl_bootout", "_launchctl_gui_loaded",
    )
}


@pytest.fixture(autouse=True)
def _live_paths(monkeypatch):
    """Restore the real handlers + seams, clear every DRY_RUN env, pin darwin, forbid launchctl."""
    for name, fn in _REAL.items():
        monkeypatch.setattr(runner, name, fn)
    monkeypatch.setitem(runner._HANDLERS, "provision_tg_ctl", _REAL["_do_provision_tg_ctl"])
    monkeypatch.setitem(runner._HANDLERS, "provision_schedule", _REAL["_do_provision_schedule"])
    for var in ("RIG_TG_CTL_DRY_RUN", "RIG_SCHEDULE_DRY_RUN", "RIG_TMUX_DRY_RUN", "RIG_SPOTLIGHT_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    real_run = runner.subprocess.run

    def _guarded_run(argv, *a, **kw):
        if argv and argv[0] == "launchctl":
            raise AssertionError(f"live launchctl reached from a sandboxed HOME: {argv}")
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", _guarded_run)


def _sandbox_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _unsandbox(monkeypatch, home: Path) -> None:
    """Make the FAKE home count as the login home (never point Path.home at the real one)."""
    monkeypatch.setattr(runner, "_real_login_home", lambda: home.resolve())


# ── the predicate ──────────────────────────────────────────────────────────────────────
def test_skip_reason_renders_unknown_login_home_readably(monkeypatch, tmp_path):
    _sandbox_home(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_real_login_home", lambda: None)
    reason = runner._live_skip_reason("RIG_TG_CTL_DRY_RUN", "launchctl bootstrap")
    assert "unknown login home" in reason and "None" not in reason


def test_home_is_sandboxed_when_path_home_differs_from_login_home(monkeypatch, tmp_path):
    _sandbox_home(monkeypatch, tmp_path)
    assert runner._home_is_sandboxed() is True


def test_home_is_not_sandboxed_when_they_match(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    assert runner._home_is_sandboxed() is False


def test_home_comparison_resolves_symlinks(monkeypatch, tmp_path):
    """/var/folders vs /private/var must not read as an override — both sides are resolved."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))
    monkeypatch.setattr(runner, "_real_login_home", lambda: real)
    assert runner._home_is_sandboxed() is False


def test_unknown_login_home_is_sandboxed_fail_closed(monkeypatch, tmp_path):
    _sandbox_home(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_real_login_home", lambda: None)
    assert runner._home_is_sandboxed() is True


def test_real_login_home_comes_from_the_passwd_database():
    pwd = pytest.importorskip("pwd")

    assert runner._real_login_home() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


# ── tg_ctl ─────────────────────────────────────────────────────────────────────────────
def _tg_action(home: Path) -> Action:
    return Action(
        kind="provision_tg_ctl", category="tg_ctl", item="boot", source=home,
        target=Path("ai.hyperide.tg-ctl"),
        options={"boot": True, "label": None, "bun_path": str(home / "bun"),
                 "tg_ctl_path": None, "config_dir": None},
    )


def test_tg_ctl_sandboxed_home_writes_plist_but_never_touches_launchd(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    stale = home / "Library" / "LaunchAgents" / f"{tgmod.STALE_PREDECESSOR_LABEL}.plist"
    stale.parent.mkdir(parents=True)
    stale.write_text("<plist/>", encoding="utf-8")

    res = runner._do_provision_tg_ctl(_tg_action(home), "backup")

    assert res.status == "created"
    assert (home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist").is_file()
    assert stale.is_file()  # the predecessor teardown is a live mutation too — skipped
    assert "HOME is overridden" in res.detail and str(home) in res.detail
    assert "sandboxed run" in res.detail and "skipped live launchctl" in res.detail


def test_tg_ctl_env_dry_run_still_names_the_env_flag(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TG_CTL_DRY_RUN", "1")
    res = runner._do_provision_tg_ctl(_tg_action(home), "backup")
    assert "RIG_TG_CTL_DRY_RUN" in res.detail


def test_tg_ctl_real_home_bootstraps(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    spy: list = []
    monkeypatch.setattr(runner, "_launchctl_bootout", lambda p: spy.append(("bootout", p)) or 0)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", lambda p: spy.append(("bootstrap", p)) or 0)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: False)
    res = runner._do_provision_tg_ctl(_tg_action(home), "backup")
    assert res.status == "created"
    assert [v for v, _ in spy] == ["bootout", "bootstrap"]
    assert "HOME is overridden" not in res.detail


# ── models schedule (launchd) ───────────────────────────────────────────────────────────
def _schedule_action(home: Path) -> Action:
    from riglib import schedule as sched

    return Action(
        kind="provision_schedule", category="models", item="model-freshness", source=home,
        target=home / "Library" / "LaunchAgents" / f"{sched.DEFAULT_LABEL}.plist",
        options={"platform": "launchd", "label": sched.DEFAULT_LABEL, "hour": 12, "minute": 0,
                 "checker_path": "/checkout/lib/checker/model_freshness.py"},
    )


def test_schedule_sandboxed_home_writes_plist_but_never_loads(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    res = runner._do_provision_schedule(_schedule_action(home), "backup")
    assert res.status == "created"
    assert _schedule_action(home).target.is_file()
    assert "HOME is overridden" in res.detail and "sandboxed run" in res.detail


def test_schedule_sandboxed_reapply_with_current_plist_never_probes_launchd(monkeypatch, tmp_path):
    """A re-apply against an already-current plist used to run the loaded-PROBE before the
    dry-run gate — a read of the REAL launchd domain from a sandbox. Now: a current plist is the
    whole no-op (the subprocess tripwire would fail on any launchctl argv)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    assert runner._do_provision_schedule(_schedule_action(home), "backup").status == "created"
    res = runner._do_provision_schedule(_schedule_action(home), "backup")
    assert res.status == "skipped" and "already current" in res.detail
    assert "HOME is overridden" in res.detail


def test_schedule_crontab_sandboxed_home_never_writes(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_read_crontab", lambda: (False, ""))
    monkeypatch.setattr(runner, "_write_crontab", lambda contents: pytest.fail(f"crontab written: {contents!r}"))
    action = _schedule_action(home)
    action.options["platform"] = "crontab"
    res = runner._do_provision_schedule(action, "backup")
    assert res.status == "created"
    assert "HOME is overridden" in res.detail and "crontab write" in res.detail


def test_schedule_real_home_loads(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    calls: list = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: calls.append(verb) or 0)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    res = runner._do_provision_schedule(_schedule_action(home), "backup")
    assert res.status == "created" and "load" in calls


# ── tmux boot / autosave activation ────────────────────────────────────────────────────
def _tmux_action(home: Path) -> Action:
    return Action(
        kind="provision_tmux", category="tmux", item="config", source=home,
        target=home / ".tmux.conf",
        options={"apply_mode": "import", "conf_path": str(home / ".tmux.conf"),
                 "generated_dir": str(home / ".config" / "rig" / "tmux"),
                 "resurrect": {}, "continuum": {}, "moshi": {}, "cc_restore": {},
                 "anti_sprawl": {}, "boot": {"enabled": True}},
    )


def test_tmux_sandboxed_home_writes_artifacts_but_skips_live_activation(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)

    def _no_clone(repo, dest):
        raise AssertionError(f"live plugin clone from a sandboxed HOME: {repo}")

    monkeypatch.setattr(runner, "_git_clone", _no_clone)
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert res.status == "created"  # the sandbox note is a warning, never an error status
    assert (home / ".config" / "rig" / "tmux" / "rig.tmux.conf").is_file()
    assert not (home / ".tmux" / "resurrect").exists()
    assert "HOME is overridden" in res.detail and "sandboxed run" in res.detail


def test_tmux_real_home_runs_the_activation(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    loads: list = []
    monkeypatch.setattr(runner, "_git_clone", lambda repo, dest: Path(dest).mkdir(parents=True, exist_ok=True) or 0)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    monkeypatch.setattr(runner, "_launchctl_load_enable", lambda plist: loads.append(str(plist)) or 0)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: True)
    monkeypatch.setattr(runner, "_launchctl_bootout", lambda p: 0)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", lambda p: 0)
    monkeypatch.setattr(runner, "_tmux_resurrect_save", lambda plan: 0)
    monkeypatch.setattr(runner, "_clean_stale_continuum_boot", lambda plan: False)
    res = runner._do_provision_tmux(_tmux_action(home), "backup")
    assert loads and (home / ".tmux" / "resurrect").is_dir()
    assert "HOME is overridden" not in res.detail


# ── spotlight re-sweep agent ───────────────────────────────────────────────────────────
def _spotlight_action(root: Path) -> Action:
    return Action(
        kind="provision_spotlight", category="spotlight", item="exclude", source=root,
        target=Path(spotlight.DEFAULT_BOOT_LABEL),
        options={"roots": [str(root)], "deny": sorted(spotlight.DEFAULT_DENY),
                 "label": spotlight.DEFAULT_BOOT_LABEL, "max_depth": 8,
                 "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"]},
    )


def test_spotlight_sandboxed_home_writes_plist_but_never_loads(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    root = tmp_path / "work"
    (root / "proj" / "node_modules").mkdir(parents=True)
    res = runner._do_provision_spotlight(_spotlight_action(root), "backup")
    assert res.status == "created"
    assert (home / "Library" / "LaunchAgents" / f"{spotlight.DEFAULT_BOOT_LABEL}.plist").is_file()
    assert "HOME is overridden" in res.detail and "sandboxed run" in res.detail


def test_spotlight_sandboxed_reapply_with_current_plist_never_probes_launchd(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    assert runner._do_provision_spotlight(_spotlight_action(root), "backup").status == "created"
    res = runner._do_provision_spotlight(_spotlight_action(root), "backup")
    assert res.status == "skipped" and "already current" in res.detail
    assert "HOME is overridden" in res.detail
    assert (home / "Library" / "LaunchAgents" / f"{spotlight.DEFAULT_BOOT_LABEL}.plist").is_file()


def test_spotlight_real_home_loads(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    root = tmp_path / "work"
    root.mkdir()
    calls: list = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: calls.append(verb) or 0)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    res = runner._do_provision_spotlight(_spotlight_action(root), "backup")
    assert res.status == "created" and "load" in calls


# ── the crash-fix: Path.home() itself can fail-closed-worthy, not just OSError ─────────
def test_home_is_sandboxed_fails_closed_on_keyerror_from_path_home(monkeypatch, tmp_path):
    """A uid with no passwd entry and no $HOME (e.g. an unmapped-uid container) makes
    Path.home() raise KeyError/RuntimeError, not OSError -- must still read as sandboxed,
    not propagate and crash rig apply/status."""
    monkeypatch.setattr(runner, "_real_login_home", lambda: tmp_path / "somewhere")

    def _boom():
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(KeyError("no passwd entry"))))
    assert runner._home_is_sandboxed() is True


def test_home_is_sandboxed_fails_closed_on_runtimeerror_from_path_home(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_real_login_home", lambda: tmp_path / "somewhere")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("could not find home"))))
    assert runner._home_is_sandboxed() is True


def test_live_skip_reason_never_crashes_when_path_home_raises(monkeypatch, tmp_path):
    """The exact scenario `_home_is_sandboxed` degrades to dry-run for (no $HOME, no passwd
    entry) must not crash the RENDERER either — `_live_skip_reason` is called on every dry-run
    apply path right after the predicate returns True."""
    monkeypatch.setattr(runner, "_real_login_home", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(KeyError("no passwd entry"))))
    monkeypatch.delenv("HOME", raising=False)
    reason = runner._live_skip_reason("RIG_TG_CTL_DRY_RUN", "launchctl bootstrap")
    assert "unset, no passwd entry" in reason and "unknown login home" in reason


def test_tg_ctl_apply_survives_home_becoming_unresolvable_after_plan_build(monkeypatch, tmp_path):
    """Realistic narrower version of the finding: HOME resolves fine for PLAN BUILDING
    (tg_ctl_plan_from_action's own `Path.home()` call succeeds — a genuinely unresolvable HOME
    is a separate, pre-existing tool-wide limitation, not what rig-cli#116 fixes), but a LATER
    `Path.home()` call inside the skip-reason RENDERER must not crash even if resolution has
    since become flaky (e.g. a network home dropping mid-request). Once plan-building has
    captured `plan.home`, only the detail-rendering call is exercised here."""
    home = _sandbox_home(monkeypatch, tmp_path)
    action = _tg_action(home)
    monkeypatch.setattr(runner, "_real_login_home", lambda: None)
    real_home = runner.tg_ctl_plan_from_action(action).home  # capture it BEFORE breaking Path.home()
    assert real_home == home
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(KeyError("no passwd entry"))))
    reason = runner._live_skip_reason("RIG_TG_CTL_DRY_RUN", "launchctl bootstrap")
    assert str(home) in reason  # falls back to $HOME env var, did not raise


# ── rig status must stay convergeable: the drift-side loaded-probe is suppressed too ────
def test_drift_schedule_sandboxed_home_never_probes_launchd(monkeypatch, tmp_path):
    """The sandbox guard's contract is 'apply skips the load, status stays converged' -- so a
    current-but-unloaded plist under a sandboxed HOME must NOT surface as 'not loaded' drift,
    and drift must not even query the real launchd domain to find out."""
    home = _sandbox_home(monkeypatch, tmp_path)
    action = _schedule_action(home)
    assert runner._do_provision_schedule(action, "backup").status == "created"
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: pytest.fail(f"drift probed launchctl: {label}"))
    report = driftmod.detect(InstallPlan(actions=[action]))
    assert not [i for i in report.items if i.category == "models"]


def test_drift_schedule_real_home_still_probes_launchd(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    action = _schedule_action(home)
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    assert runner._do_provision_schedule(action, "backup").status == "created"
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    report = driftmod.detect(InstallPlan(actions=[action]))
    assert any("not loaded" in i.detail for i in report.items if i.category == "models")


def test_drift_tg_ctl_sandboxed_home_never_probes_launchd(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    action = _tg_action(home)
    assert runner._do_provision_tg_ctl(action, "backup").status == "created"
    monkeypatch.setattr(driftmod, "_launchctl_gui_loaded", lambda label: pytest.fail(f"drift probed launchctl: {label}"))
    report = driftmod.detect(InstallPlan(actions=[action]))
    assert not [i for i in report.items if i.category == "tg_ctl"]


def test_drift_schedule_crontab_sandboxed_home_is_not_reported_missing(monkeypatch, tmp_path):
    """Apply skips the crontab WRITE under a sandbox (rig-cli#116); status must not turn that
    into permanent, un-convergeable 'crontab line not installed' drift."""
    home = _sandbox_home(monkeypatch, tmp_path)
    action = _schedule_action(home)
    action.options["platform"] = "crontab"
    monkeypatch.setattr(runner, "_read_crontab", lambda: (False, ""))
    assert runner._do_provision_schedule(action, "backup").status == "created"
    monkeypatch.setattr(driftmod, "_read_crontab", lambda: pytest.fail("drift read the real crontab"))
    report = driftmod.detect(InstallPlan(actions=[action]))
    assert not [i for i in report.items if i.category == "models"]


# ── tg_ctl sandboxed re-apply must name the skip, like schedule/spotlight already do ────
def test_tg_ctl_sandboxed_reapply_with_current_plist_names_the_skip(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    action = _tg_action(home)
    assert runner._do_provision_tg_ctl(action, "backup").status == "created"
    res = runner._do_provision_tg_ctl(action, "backup")
    assert res.status == "skipped"
    assert "HOME is overridden" in res.detail and "sandboxed run" in res.detail


# ── the post-apply verify follows the same predicate ───────────────────────────────────
def test_verify_sandboxed_home_skips_the_loaded_check_and_never_probes(monkeypatch, tmp_path):
    """The verify framework used to keep an env-only copy of the dry-run check: a sandboxed
    apply wrote the plist, skipped the load, then verify probed the REAL domain, reported
    'NOT loaded' and failed `rig apply` (exit 1). Verify must skip on the SAME predicate."""
    from riglib import verify

    home = _sandbox_home(monkeypatch, tmp_path)
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: pytest.fail(f"verify probed launchctl: {a}"))
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    results = verify._verify_launchd_agent(
        "tg_ctl", "boot", "ai.hyperide.tg-ctl", plist, "RIG_TG_CTL_DRY_RUN",
        loaded_check=runner._launchctl_gui_loaded,
    )
    assert [r.passed for r in results] == [True, None]
    assert "sandboxed run" in results[1].evidence and "skipped" in results[1].evidence


def test_verify_real_home_runs_the_loaded_check(monkeypatch, tmp_path):
    from riglib import verify

    home = _sandbox_home(monkeypatch, tmp_path)
    _unsandbox(monkeypatch, home)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    probed: list[str] = []
    results = verify._verify_launchd_agent(
        "tg_ctl", "boot", "ai.hyperide.tg-ctl", plist, "RIG_TG_CTL_DRY_RUN",
        loaded_check=lambda label: probed.append(label) or True,
    )
    assert probed == ["ai.hyperide.tg-ctl"] and [r.passed for r in results] == [True, True]
