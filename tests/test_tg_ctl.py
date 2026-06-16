"""tg-ctl boot provisioning — config, pure plist rendering, install, drift.

rig provisions the ``tg-ctl`` inbound control daemon (tg-cli's long-poll/inject/voice daemon)
as a macOS LaunchAgent so it auto-starts at login/boot — exactly like the tmux boot service.
This module mirrors :mod:`test_tmux`: stdlib-only pure render tests for the byte-exact plist,
HOME-isolated install/idempotency/conflict tests that NEVER touch the real
``~/Library/LaunchAgents`` or run a real ``launchctl`` (the gui-domain seams are stubbed per
test), the stale-predecessor (``com.ultra.codex-tg-bot``) teardown, and drift detection.

HARD ISOLATION (CTO): every install/drift test points ``Path.home()`` at a tmp dir AND stubs
``_launchctl_bootstrap``/``_launchctl_bootout``/``_launchctl_gui_loaded`` so no test can mutate
the real launchd domain or write the real LaunchAgents dir. The one test that reads the live
machine plist is READ-ONLY and skips when the file is absent.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from riglib import drift as driftmod
from riglib import tg_ctl
from riglib.actions import runner
from riglib.config import ConfigError, validate

# captured at import (before any monkeypatch runs) — the genuine handler + drift check.
_REAL_PROVISION = runner._do_provision_tg_ctl
_REAL_CHECK = driftmod._check_tg_ctl


@pytest.fixture(autouse=True)
def _real_tg_ctl(monkeypatch):
    """Restore the REAL provision_tg_ctl handler + drift check for THIS module's tests.

    conftest's autouse ``_isolate_scheduler`` stubs ``_do_provision_tg_ctl`` (+ the ``_HANDLERS``
    entry) and ``drift._check_tg_ctl`` to no-ops so e2e tests can't touch the host launchd. These
    dedicated tests EXERCISE the real install + drift logic — with HOME-isolated tmp dirs +
    stubbed launchctl seams — so they restore the real implementations. conftest still stubs the
    gui-domain ``_launchctl*`` seams to safe no-ops; tests that assert on launchctl calls re-stub
    them with a spy, and drift tests override ``_on_darwin``/``_launchctl_gui_loaded`` explicitly.
    """
    monkeypatch.setattr(runner, "_do_provision_tg_ctl", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_tg_ctl", _REAL_PROVISION)
    monkeypatch.setattr(driftmod, "_check_tg_ctl", _REAL_CHECK)
    # These tests assert on the LIVE (non-dry) provisioning path by default. A leaked ambient
    # RIG_TG_CTL_DRY_RUN (e.g. exported by tests/smoke.sh when it shells out to pytest) would
    # silently route them through the dry-run branch — so clear it; the one dry-run test sets it.
    monkeypatch.delenv("RIG_TG_CTL_DRY_RUN", raising=False)


def _force_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _isolate_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ── config validation ───────────────────────────────────────────────────────────────────
def test_tg_ctl_block_accepted():
    validate({"version": 1, "tg_ctl": {"enabled": True}})


def test_tg_ctl_block_empty_ok():
    validate({"version": 1, "tg_ctl": {}})


def test_tg_ctl_full_block_accepted():
    validate(
        {
            "version": 1,
            "tg_ctl": {
                "enabled": True,
                "boot": True,
                "label": "ai.hyperide.tg-ctl",
                "bun_path": "/Users/u/.bun/bin/bun",
                "tg_ctl_path": "~/.files/bin/tg-ctl",
                "config_dir": "~/.config/tg-cli",
            },
        }
    )


def test_tg_ctl_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tg_ctl": {"nope": 1}})


def test_tg_ctl_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tg_ctl": {"enabled": "yes"}})


def test_tg_ctl_boot_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "tg_ctl": {"boot": "yes"}})


@pytest.mark.parametrize("key", ["label", "bun_path", "tg_ctl_path", "config_dir"])
def test_tg_ctl_string_keys_reject_non_string(key):
    with pytest.raises(ConfigError):
        validate({"version": 1, "tg_ctl": {key: 123}})


def test_tg_ctl_enabled_null_is_accepted():
    """`enabled: null` (explicit None) is valid and is NOT treated as disabled (default-on)."""
    validate({"version": 1, "tg_ctl": {"enabled": None}})


# ── pure plist rendering ─────────────────────────────────────────────────────────────────
def _plan(home="/Users/ultra", **over):
    """A TgCtlPlan with an explicit bun_path so render is deterministic (no `which bun`)."""
    over.setdefault("bun_path", str(Path(home) / ".bun" / "bin" / "bun"))
    return tg_ctl.build_tg_ctl(home=Path(home), **over)


# The exact, hand-created + WORKING plist shape rig must match byte-for-byte (from the prompt).
_EXPECTED_LIVE_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>ai.hyperide.tg-ctl</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/Users/ultra/.bun/bin/bun</string>
\t\t<string>/Users/ultra/.files/bin/tg-ctl</string>
\t\t<string>run</string>
\t</array>
\t<key>WorkingDirectory</key>
\t<string>/Users/ultra/.files/bin</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>/Users/ultra/.bun/bin:/opt/homebrew/bin:/Users/ultra/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
\t\t<key>HOME</key>
\t\t<string>/Users/ultra</string>
\t</dict>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>KeepAlive</key>
\t<true/>
\t<key>ThrottleInterval</key>
\t<integer>10</integer>
\t<key>StandardOutPath</key>
\t<string>/Users/ultra/.config/tg-cli/launchd.tg-ctl.out.log</string>
\t<key>StandardErrorPath</key>
\t<string>/Users/ultra/.config/tg-cli/launchd.tg-ctl.err.log</string>
</dict>
</plist>
"""


def test_render_matches_exact_expected_plist():
    """The render must equal the hand-created WORKING plist BYTE-FOR-BYTE (the no-op contract).

    Key ORDER is load-bearing (plistlib defaults to sorting keys, which would reorder Label /
    ProgramArguments / EnvironmentVariables and force a spurious rewrite every apply). The
    render pins ``sort_keys=False`` to preserve insertion order.
    """
    got = _plan(home="/Users/ultra").render_plist()
    assert got == _EXPECTED_LIVE_PLIST


def test_render_is_well_formed_and_labelled():
    p = _plan()
    parsed = plistlib.loads(p.render_plist().encode("utf-8"))
    assert parsed["Label"] == p.boot_label
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ThrottleInterval"] == 10
    assert parsed["ProgramArguments"] == [str(p.bun_path), str(p.tg_ctl_path), "run"]


def test_render_preserves_key_order_not_sorted():
    """Regression guard: the keys must NOT be alphabetically sorted (Label must precede
    EnvironmentVariables; PATH must precede HOME) — the byte-exact-vs-live contract."""
    body = _plan().render_plist()
    assert body.index("<key>Label</key>") < body.index("<key>EnvironmentVariables</key>")
    assert body.index("<key>PATH</key>") < body.index("<key>HOME</key>")
    assert body.index("<key>RunAtLoad</key>") < body.index("<key>StandardOutPath</key>")


def test_render_is_deterministic():
    assert _plan().render_plist() == _plan().render_plist()


def test_label_is_configurable():
    p = _plan(boot_label="com.me.tg")
    assert p.boot_label == "com.me.tg"
    assert "com.me.tg" in p.render_plist()
    assert p.plist_path.name == "com.me.tg.plist"


def test_paths_resolve_against_injected_home():
    p = _plan(home="/home/u")
    assert p.tg_ctl_path == Path("/home/u/.files/bin/tg-ctl")
    assert p.config_dir == Path("/home/u/.config/tg-cli")
    assert p.out_log_path == Path("/home/u/.config/tg-cli/launchd.tg-ctl.out.log")
    assert p.plist_path == Path("/home/u/Library/LaunchAgents/ai.hyperide.tg-ctl.plist")
    assert p.working_directory == Path("/home/u/.files/bin")


def test_bun_path_falls_back_to_home_bun_when_not_on_path(monkeypatch):
    """When bun isn't on PATH and no explicit bun_path is given, fall back to ~/.bun/bin/bun
    expanded against the INJECTED home (never the real ~/.bun)."""
    monkeypatch.setattr(tg_ctl.shutil, "which", lambda name: None)
    monkeypatch.setattr(tg_ctl.Path, "exists", lambda self: False)
    p = tg_ctl.build_tg_ctl(home=Path("/home/u"))
    assert p.bun_path == Path("/home/u/.bun/bin/bun")


def test_bun_path_prefers_path_resolution(monkeypatch):
    monkeypatch.setattr(tg_ctl.shutil, "which", lambda name: "/opt/homebrew/bin/bun")
    p = tg_ctl.build_tg_ctl(home=Path("/home/u"))
    assert p.bun_path == Path("/opt/homebrew/bin/bun")
    # the daemon PATH carries the resolved bun DIR first so the daemon can find bun.
    assert p.daemon_path_env.startswith("/opt/homebrew/bin:")


def test_explicit_config_dir_is_honored():
    p = _plan(config_dir="/var/tg")
    assert p.config_dir == Path("/var/tg")
    assert p.out_log_path == Path("/var/tg/launchd.tg-ctl.out.log")


# ── install (runner) — write + (re)load, idempotent, conflict, stale-predecessor ─────────
def _action(home, **over):
    from riglib.plan import Action

    options = {
        "boot": True,
        "label": None,
        "bun_path": str(home / ".bun" / "bin" / "bun"),  # deterministic (no `which bun`)
        "tg_ctl_path": None,
        "config_dir": None,
    }
    options.update(over)
    return Action(
        kind="provision_tg_ctl",
        category="tg_ctl",
        item="boot",
        source=home,
        target=Path("ai.hyperide.tg-ctl"),
        options=options,
    )


def _stub_launchctl(monkeypatch, *, loaded=False, spy=None):
    """Stub the gui-domain launchctl seams. `loaded` is the gui-loaded state; `spy` (a list)
    records (verb, plist) calls so a test can assert bootout/bootstrap fired — WITHOUT ever
    touching the real launchd domain."""
    def _bootout(plist):
        if spy is not None:
            spy.append(("bootout", plist))
        return 0

    def _bootstrap(plist):
        if spy is not None:
            spy.append(("bootstrap", plist))
        return 0

    monkeypatch.setattr(runner, "_launchctl_bootout", _bootout)
    monkeypatch.setattr(runner, "_launchctl_bootstrap", _bootstrap)
    monkeypatch.setattr(runner, "_launchctl_gui_loaded", lambda label: loaded)


def test_apply_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)

    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res.status in ("created", "backed_up")
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    assert plist.is_file()
    parsed = plistlib.loads(plist.read_bytes())
    assert parsed["Label"] == "ai.hyperide.tg-ctl"
    # the log dir (config dir) was created so launchd can open the logs.
    assert (home / ".config" / "tg-cli").is_dir()
    # the agent was (re)loaded via the gui domain: bootout then bootstrap.
    assert ("bootstrap", str(plist)) in spy


def test_apply_is_idempotent_when_loaded(monkeypatch, tmp_path):
    """A re-apply against the byte-identical plist with the agent already loaded is a no-op."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    runner._do_provision_tg_ctl(_action(home), "backup")  # install
    # now the agent is loaded → a second apply must be a `skipped` no-op (no rewrite, no reload).
    _stub_launchctl(monkeypatch, loaded=True)
    res2 = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res2.status == "skipped", res2.detail


def test_reapply_against_identical_plist_does_not_rewrite(monkeypatch, tmp_path):
    """Idempotency at the byte level: a re-apply must not change the plist mtime-content."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=True)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    desired = runner.tg_ctl_plan_from_action(_action(home)).render_plist()
    plist.write_text(desired, encoding="utf-8")
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res.status == "skipped"
    assert plist.read_text(encoding="utf-8") == desired  # untouched


def test_apply_reloads_when_plist_present_but_not_loaded(monkeypatch, tmp_path):
    """Plist byte-identical but the agent isn't loaded → loading IT is the change (not skipped)."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(runner.tg_ctl_plan_from_action(_action(home)).render_plist(), encoding="utf-8")
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res.status != "skipped"
    assert ("bootstrap", str(plist)) in spy


def test_apply_backs_up_a_differing_plist(monkeypatch, tmp_path):
    """on_conflict=backup: an existing DIFFERING plist is backed up (timestamped) before rewrite."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist>OLD HAND-EDITED</plist>\n", encoding="utf-8")
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res.status == "backed_up"
    baks = list(plist.parent.glob("ai.hyperide.tg-ctl.plist.*"))
    assert baks, "a differing plist must be backed up before rewrite"
    assert "OLD HAND-EDITED" in baks[0].read_text()
    assert "ai.hyperide.tg-ctl" in plist.read_text() and "OLD HAND-EDITED" not in plist.read_text()


def test_apply_skip_does_not_load_a_differing_plist(monkeypatch, tmp_path):
    """on_conflict=skip + a differing existing plist → leave it untouched AND do NOT bootstrap
    the stale config (that would load the wrong agent). Surface unresolved drift instead."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist>USER OWNED</plist>\n", encoding="utf-8")
    res = runner._do_provision_tg_ctl(_action(home), "skip")
    assert res.status == "skipped"
    assert plist.read_text() == "<plist>USER OWNED</plist>\n"  # untouched
    assert "on_conflict=skip" in res.detail
    assert not any(verb == "bootstrap" for verb, _ in spy)  # never loaded the stale plist


def test_apply_disabled_boot_does_not_write_or_load(monkeypatch, tmp_path):
    """tg_ctl.boot=false: no plist written, no launchctl call. A `skipped` no-op."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    res = runner._do_provision_tg_ctl(_action(home, boot=False), "backup")
    assert res.status == "skipped"
    assert not (home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist").exists()
    assert spy == []


def test_apply_off_darwin_is_skipped(monkeypatch, tmp_path):
    """Off macOS (no launchd) tg-ctl provisioning is a no-op — no plist, no launchctl."""
    monkeypatch.setattr(sys, "platform", "linux")
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert res.status == "skipped"
    assert not (home / "Library" / "LaunchAgents").exists()
    assert spy == []


def test_dry_run_writes_plist_but_skips_launchctl(monkeypatch, tmp_path):
    """RIG_TG_CTL_DRY_RUN: the plist lands but no launchctl bootstrap/bootout fires."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TG_CTL_DRY_RUN", "1")
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert (home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist").is_file()
    assert spy == []  # NO real launchd mutation
    assert "RIG_TG_CTL_DRY_RUN" in res.detail


def test_dry_run_does_not_remove_stale_predecessor(monkeypatch, tmp_path):
    """REGRESSION (review): under dry-run the stale-predecessor teardown must touch NOTHING — no
    bootout, no backup file, and the predecessor plist must STAY on disk (the message said 'would
    remove' while the code actually removed it). Dry-run only reports what it would do."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RIG_TG_CTL_DRY_RUN", "1")
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    stale = home / "Library" / "LaunchAgents" / "com.ultra.codex-tg-bot.plist"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("<plist>DEAD</plist>\n", encoding="utf-8")
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    assert stale.is_file(), "dry-run must NOT delete the stale predecessor plist"
    assert stale.read_text() == "<plist>DEAD</plist>\n"  # untouched
    assert not list(stale.parent.glob("com.ultra.codex-tg-bot.plist.rig-bak-*"))  # no backup written
    assert spy == []  # no bootout
    assert "would boot out" in res.detail.lower()


# ── stale predecessor (com.ultra.codex-tg-bot) teardown ──────────────────────────────────
def test_apply_removes_stale_predecessor(monkeypatch, tmp_path):
    """The dead codex-tg-bot LaunchAgent is booted out + backed up + removed on reconcile."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    stale = home / "Library" / "LaunchAgents" / "com.ultra.codex-tg-bot.plist"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("<plist>DEAD PREDECESSOR</plist>\n", encoding="utf-8")
    res = runner._do_provision_tg_ctl(_action(home), "backup")
    # the stale plist is gone, backed up, and booted out of launchd.
    assert not stale.exists()
    baks = list(stale.parent.glob("com.ultra.codex-tg-bot.plist.rig-bak-*"))
    assert baks and "DEAD PREDECESSOR" in baks[0].read_text()
    assert ("bootout", str(stale)) in spy
    assert "stale predecessor" in res.detail.lower()
    # the new tg-ctl agent is still installed in the same run.
    assert (home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist").is_file()


def test_apply_no_stale_predecessor_is_clean(monkeypatch, tmp_path):
    """No stale predecessor present → no bootout for it, just the normal tg-ctl install."""
    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    spy: list = []
    _stub_launchctl(monkeypatch, loaded=False, spy=spy)
    runner._do_provision_tg_ctl(_action(home), "backup")
    stale = home / "Library" / "LaunchAgents" / "com.ultra.codex-tg-bot.plist"
    assert ("bootout", str(stale)) not in spy


# ── full apply path through run_plan ─────────────────────────────────────────────────────
def test_run_plan_provisions_tg_ctl(monkeypatch, tmp_path):
    from riglib.actions.runner import run_plan
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=False)
    report = run_plan(InstallPlan(actions=[_action(home)]))
    assert not report.errors
    assert report.changed == 1


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_tg_ctl_missing(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    report = detect(InstallPlan(actions=[_action(tmp_path / "home")]))
    drift = [d for d in report.items if d.category == "tg_ctl"]
    assert drift and drift[0].direction == "missing"


def test_drift_tg_ctl_in_sync_after_apply(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _force_darwin(monkeypatch)
    home = _isolate_home(monkeypatch, tmp_path)
    _stub_launchctl(monkeypatch, loaded=True)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    runner._do_provision_tg_ctl(_action(home), "backup")
    report = detect(InstallPlan(actions=[_action(home)]))
    assert not [d for d in report.items if d.category == "tg_ctl"]


def test_drift_tg_ctl_modified(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist>HAND EDITED, DIFFERENT</plist>\n", encoding="utf-8")
    report = detect(InstallPlan(actions=[_action(home)]))
    drift = [d for d in report.items if d.category == "tg_ctl"]
    assert drift and drift[0].direction == "modified"


def test_drift_tg_ctl_not_loaded(monkeypatch, tmp_path):
    """Plist present + identical but the agent isn't loaded in the gui domain → missing drift."""
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: False)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(runner.tg_ctl_plan_from_action(_action(home)).render_plist(), encoding="utf-8")
    report = detect(InstallPlan(actions=[_action(home)]))
    drift = [d for d in report.items if d.category == "tg_ctl"]
    assert drift and drift[0].direction == "missing"
    assert "not loaded" in drift[0].detail


def test_drift_flags_stale_predecessor(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    monkeypatch.setattr("riglib.drift._launchctl_gui_loaded", lambda label: True)
    # the tg-ctl plist itself is in sync, but the stale predecessor lingers.
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(runner.tg_ctl_plan_from_action(_action(home)).render_plist(), encoding="utf-8")
    stale = home / "Library" / "LaunchAgents" / "com.ultra.codex-tg-bot.plist"
    stale.write_text("<plist>DEAD</plist>\n", encoding="utf-8")
    report = detect(InstallPlan(actions=[_action(home)]))
    extra = [d for d in report.items if d.category == "tg_ctl" and d.direction == "extra"]
    assert extra and "predecessor" in extra[0].detail.lower()


def test_drift_boot_disabled_flags_leftover_plist(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: True)
    plist = home / "Library" / "LaunchAgents" / "ai.hyperide.tg-ctl.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>\n", encoding="utf-8")
    report = detect(InstallPlan(actions=[_action(home, boot=False)]))
    extra = [d for d in report.items if d.category == "tg_ctl" and d.direction == "extra"]
    assert extra and "boot is disabled" in extra[0].detail


def test_drift_off_darwin_is_silent(monkeypatch, tmp_path):
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: False)
    report = detect(InstallPlan(actions=[_action(tmp_path / "home")]))
    assert not [d for d in report.items if d.category == "tg_ctl"]


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


def test_plan_provisions_tg_ctl_by_default(fake_agent_tools, tmp_path):
    """tg_ctl is default-on: an ABSENT block still emits a provision_tg_ctl action."""
    plan = _build({}, tmp_path, fake_agent_tools)
    assert [a for a in plan.actions if a.kind == "provision_tg_ctl"]


def test_plan_no_tg_ctl_when_disabled(fake_agent_tools, tmp_path):
    plan = _build({"tg_ctl": {"enabled": False}}, tmp_path, fake_agent_tools)
    assert not [a for a in plan.actions if a.kind == "provision_tg_ctl"]


def test_plan_emits_single_tg_ctl_action(fake_agent_tools, tmp_path):
    plan = _build({"tg_ctl": {"enabled": True}}, tmp_path, fake_agent_tools)
    acts = [a for a in plan.actions if a.kind == "provision_tg_ctl"]
    assert len(acts) == 1
    assert acts[0].category == "tg_ctl" and acts[0].item == "boot"


def test_plan_carries_knobs(fake_agent_tools, tmp_path):
    plan = _build(
        {"tg_ctl": {"enabled": True, "boot": False, "label": "com.me.tg",
                    "config_dir": "/var/tg"}},
        tmp_path, fake_agent_tools,
    )
    a = next(a for a in plan.actions if a.kind == "provision_tg_ctl")
    assert a.options["boot"] is False
    assert a.options["label"] == "com.me.tg"
    assert a.options["config_dir"] == "/var/tg"


def test_plan_boot_null_resolves_to_enabled(fake_agent_tools, tmp_path):
    """REGRESSION (codex P1): `boot: null` (YAML `boot:` with no value) must default to TRUE, not
    `bool(None)`=False — the resolved plan must keep the boot agent ENABLED."""
    plan = _build({"tg_ctl": {"boot": None}}, tmp_path, fake_agent_tools)
    a = next(a for a in plan.actions if a.kind == "provision_tg_ctl")
    tg = runner.tg_ctl_plan_from_action(a)
    assert tg.boot_enabled is True


def test_plan_label_null_resolves_to_default(fake_agent_tools, tmp_path):
    """REGRESSION (review): `label: null` must resolve to the default label, never the literal
    string 'None' (neither in the action target nor the resolved plan)."""
    plan = _build({"tg_ctl": {"label": None}}, tmp_path, fake_agent_tools)
    a = next(a for a in plan.actions if a.kind == "provision_tg_ctl")
    assert a.target.name != "None"
    assert tg_ctl.DEFAULT_BOOT_LABEL in str(a.target)
    tg = runner.tg_ctl_plan_from_action(a)
    assert tg.boot_label == tg_ctl.DEFAULT_BOOT_LABEL


# ── status line (GLOBAL section) ─────────────────────────────────────────────────────────
def _status_line(monkeypatch, tmp_path, *, darwin, drift_items=None, options_over=None):
    """Render the tg-ctl GLOBAL status line for a one-action plan, with `_on_darwin` forced."""
    from riglib import cli
    from riglib.drift import DriftReport
    from riglib.plan import InstallPlan

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr("riglib.drift._on_darwin", lambda: darwin)
    action = _action(home, **(options_over or {}))
    report = DriftReport(items=list(drift_items or []))
    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(" ".join(str(x) for x in a)))
    cli._print_tg_ctl_status(InstallPlan(actions=[action]), report)
    return "\n".join(captured)


def test_status_off_darwin_says_unsupported(monkeypatch, tmp_path):
    """REGRESSION (codex P2): off macOS, status must say 'unsupported' — NOT a misleading
    'installed' (apply is a no-op off darwin)."""
    line = _status_line(monkeypatch, tmp_path, darwin=False)
    assert "unsupported" in line
    assert "installed" not in line


def test_status_on_darwin_in_sync_says_installed(monkeypatch, tmp_path):
    line = _status_line(monkeypatch, tmp_path, darwin=True, drift_items=[])
    assert "installed" in line


def test_status_boot_disabled_says_disabled(monkeypatch, tmp_path):
    line = _status_line(monkeypatch, tmp_path, darwin=True, options_over={"boot": False})
    assert "disabled" in line


# ── the live machine plist (read-only idempotency proof) ─────────────────────────────────
def test_render_matches_live_machine_plist_if_present():
    """If THIS machine has the hand-created working plist, the render must equal it byte-for-byte
    (so `rig apply` is a true no-op against the live file). READ-ONLY: skips when absent (CI /
    another machine). This is the proof the no-op contract holds against the real artifact."""
    live = Path("/Users/ultra/Library/LaunchAgents/ai.hyperide.tg-ctl.plist")
    if not live.is_file():
        pytest.skip("live ai.hyperide.tg-ctl.plist not present on this machine")
    got = tg_ctl.build_tg_ctl(home=Path("/Users/ultra")).render_plist()
    assert got == live.read_text(encoding="utf-8")
