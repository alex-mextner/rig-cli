"""Model-freshness daily-schedule provisioning — config, plan, install, drift.

Cross-platform is exercised on either host via the ``RIG_SCHEDULE_PLATFORM`` test seam
(``riglib.schedule.detect_platform``). launchd writes go to a tmp HOME; crontab reads/writes
are mocked at the ``crontab``/``launchctl`` subprocess seam so the suite never touches the
real user crontab or launchd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib import config as cfgmod
from riglib import drift as driftmod
from riglib import schedule as sched
from riglib.actions import runner
from riglib.actions.runner import ApplyReport, run_plan
from riglib.catalog import Catalog
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.plan import build


# captured at import (before any monkeypatch runs) — the genuine handler.
_REAL_PROVISION = runner._do_provision_schedule


@pytest.fixture(autouse=True)
def _real_scheduler(monkeypatch):
    """Restore the REAL provision_schedule handler for THIS module's tests.

    conftest's autouse `_isolate_scheduler` stubs `_do_provision_schedule` (+ the `_HANDLERS`
    entry) to a no-op so e2e tests can't touch the host scheduler. These dedicated tests
    EXERCISE the real install logic — with their own HOME-isolated tmp dirs + stubbed daemon
    seams — so they restore the real handler. (conftest still stubs the daemon `_launchctl*`
    /`_*_crontab` seams; these tests re-stub them per-test as needed.)
    """
    monkeypatch.setattr(runner, "_do_provision_schedule", _REAL_PROVISION)
    monkeypatch.setitem(runner._HANDLERS, "provision_schedule", _REAL_PROVISION)


# ── config validation ───────────────────────────────────────────────────────────────────
def test_models_block_accepted():
    validate({"version": 1, "models": {"enabled": True, "schedule": {"time": "12:00"}}})


def test_models_block_empty_ok():
    validate({"version": 1, "models": {}})


def test_models_unknown_key_rejected():
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"nope": 1}})


def test_models_enabled_must_be_bool():
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"enabled": "yes"}})


@pytest.mark.parametrize("bad", ["noon", "25:00", "12:60", "12", "12:00:00", "-1:00"])
def test_models_schedule_time_rejected(bad):
    with pytest.raises(ConfigError):
        validate({"version": 1, "models": {"schedule": {"time": bad}}})


@pytest.mark.parametrize("good", ["00:00", "12:00", "23:59", "09:30"])
def test_models_schedule_time_accepted(good):
    validate({"version": 1, "models": {"schedule": {"time": good}}})


def test_parse_hhmm():
    assert cfgmod.parse_hhmm("12:00") == (12, 0)
    assert cfgmod.parse_hhmm("09:30") == (9, 30)


# ── schedule artifact rendering (pure) ───────────────────────────────────────────────────
def test_build_schedule_launchd(monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "launchd")
    s = sched.build_schedule(checker_path=Path("/checkout/lib/checker/model_freshness.py"))
    assert s.platform == "launchd"
    assert s.hour == 12 and s.minute == 0
    assert s.plist_path is not None and s.plist_path.name == f"{sched.DEFAULT_LABEL}.plist"
    xml = s.plist_xml()
    assert sched.DEFAULT_LABEL in xml
    assert "<key>Hour</key>" in xml and "<integer>12</integer>" in xml
    assert "model_freshness.py" in xml


def test_build_schedule_crontab(monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "crontab")
    s = sched.build_schedule(checker_path=Path("/checkout/lib/checker/model_freshness.py"), hour=9, minute=30)
    assert s.platform == "crontab"
    lines = s.crontab_lines()
    assert lines[0] == f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}"
    assert lines[1].startswith("30 9 * * *")
    assert "model_freshness.py" in lines[1]


def test_default_checker_path():
    p = sched.default_checker_path("/some/agent-tools")
    assert p == Path("/some/agent-tools/lib/checker/model_freshness.py")
    assert sched.default_checker_path(None) is None


# ── plan building ────────────────────────────────────────────────────────────────────────
def _cfg(data: dict, repo_root: Path) -> LoadedConfig:
    return LoadedConfig(data=data, repo_root=repo_root)


def test_plan_emits_schedule_action(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "launchd")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools), "models": {"enabled": True, "schedule": {"time": "12:00"}},
         "skills": {"enabled": False}, "agent_hooks": {"enabled": False}, "ci": {"enabled": False},
         "mcp": {"enabled": False}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    sched_actions = [a for a in plan.actions if a.kind == "provision_schedule"]
    assert len(sched_actions) == 1
    a = sched_actions[0]
    assert a.category == "models" and a.item == "model-freshness"
    assert a.options["hour"] == 12 and a.options["minute"] == 0
    assert a.options["platform"] == "launchd"
    assert a.options["checker_path"].endswith("lib/checker/model_freshness.py")


def test_plan_no_schedule_when_absent(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"agent_tools_source": str(fake_agent_tools)}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]


def test_plan_no_schedule_when_disabled(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"agent_tools_source": str(fake_agent_tools), "models": {"enabled": False}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]


def test_plan_custom_time(fake_agent_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_PLATFORM", "crontab")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools), "models": {"schedule": {"time": "06:15"}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    a = next(a for a in plan.actions if a.kind == "provision_schedule")
    assert a.options["hour"] == 6 and a.options["minute"] == 15


def test_plan_explicit_checker_path(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    custom = tmp_path / "custom" / "checker.py"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools),
         "models": {"checker_path": str(custom)}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    a = next(a for a in plan.actions if a.kind == "provision_schedule")
    assert a.options["checker_path"] == str(custom)


def test_plan_skips_when_checker_missing(fake_agent_tools, tmp_path):
    """A checker_path that doesn't exist on disk → no action, a plan note (not a silent cron)."""
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"agent_tools_source": str(fake_agent_tools),
         "models": {"checker_path": str(tmp_path / "nope" / "missing.py")}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.kind == "provision_schedule"]
    assert any("checker not found" in n for n in plan.notes)


# ── install (launchd) — idempotent install-if-missing ───────────────────────────────────
def _launchd_action(tmp_home: Path):
    """A provision_schedule action whose launchd plist lands under tmp_home."""
    from riglib.plan import Action

    label = sched.DEFAULT_LABEL
    return Action(
        kind="provision_schedule",
        category="models",
        item="model-freshness",
        source=tmp_home,
        target=tmp_home / "Library" / "LaunchAgents" / f"{label}.plist",
        options={
            "platform": "launchd",
            "label": label,
            "hour": 12,
            "minute": 0,
            "checker_path": "/checkout/lib/checker/model_freshness.py",
        },
    )


def test_launchd_install_then_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    loaded = {"v": False}
    calls = []

    def fake_launchctl(verb, arg):
        calls.append((verb, arg))
        if verb == "load":
            loaded["v"] = True
        elif verb == "unload":
            loaded["v"] = False
        return 0

    monkeypatch.setattr(runner, "_launchctl", fake_launchctl)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: loaded["v"])

    action = _launchd_action(home)
    res1 = runner._do_provision_schedule(action, "backup")
    assert res1.status == "created"
    plist = home / "Library" / "LaunchAgents" / f"{sched.DEFAULT_LABEL}.plist"
    assert plist.is_file()
    assert ("load", str(plist)) in calls

    # re-apply: present + current + loaded → no-op
    calls.clear()
    res2 = runner._do_provision_schedule(action, "backup")
    assert res2.status == "skipped"
    assert calls == []  # no launchctl churn on the idempotent path


def test_launchd_reinstall_when_unloaded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0)
    # plist exists & current, but launchd says NOT loaded → must reload (not skip)
    action = _launchd_action(home)
    plist = action.target
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(runner.schedule_plan_from_action(action).plist_xml(), encoding="utf-8")
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    res = runner._do_provision_schedule(action, "backup")
    assert res.status in ("created", "updated")


def test_launchd_conflict_skip_does_not_touch_daemon(tmp_path, monkeypatch):
    """on_conflict=skip + a plist that DIFFERS → report 'skipped', never (re)load launchd.

    The desired schedule was not written (write_file skipped on the conflict), so unloading
    /loading the stale plist would mutate launchd with the WRONG schedule and reporting
    'updated' would mask the unresolved drift. We must surface 'skipped' and leave the daemon
    untouched.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    calls = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: calls.append((verb, arg)) or 0)
    # not loaded — so without the conflict-skip guard the code would proceed to (re)load.
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)

    action = _launchd_action(home)
    plist = action.target
    plist.parent.mkdir(parents=True, exist_ok=True)
    # an EXISTING plist that differs from desired (a stale, hand-edited schedule).
    stale = runner.schedule_plan_from_action(action).plist_xml().replace("<integer>12</integer>", "<integer>6</integer>")
    plist.write_text(stale, encoding="utf-8")

    res = runner._do_provision_schedule(action, "skip")
    assert res.status == "skipped", res.detail
    assert calls == []  # daemon never mutated
    # the stale plist on disk is left exactly as-is (drift NOT silently reconciled).
    assert plist.read_text(encoding="utf-8") == stale


# ── install (crontab) — idempotent install-if-missing ───────────────────────────────────
def _crontab_action(repo_root: Path):
    from riglib.plan import Action

    return Action(
        kind="provision_schedule",
        category="models",
        item="model-freshness",
        source=repo_root,
        target=repo_root,
        options={
            "platform": "crontab",
            "label": sched.DEFAULT_LABEL,
            "hour": 12,
            "minute": 0,
            "checker_path": "/checkout/lib/checker/model_freshness.py",
        },
    )


def test_crontab_install_then_idempotent(tmp_path, monkeypatch):
    store = {"has": False, "content": ""}

    def fake_read():
        return store["has"], store["content"]

    def fake_write(contents):
        store["has"] = True
        store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_read_crontab", fake_read)
    monkeypatch.setattr(runner, "_write_crontab", fake_write)

    action = _crontab_action(tmp_path)
    res1 = runner._do_provision_schedule(action, "backup")
    assert res1.status == "created"
    assert sched.DEFAULT_LABEL in store["content"]
    assert "0 12 * * *" in store["content"]  # minute=0 hour=12 → daily at noon

    # re-apply: line already present → skipped, no rewrite
    written_before = store["content"]
    res2 = runner._do_provision_schedule(action, "backup")
    assert res2.status == "skipped"
    assert store["content"] == written_before


def test_crontab_preserves_user_lines(tmp_path, monkeypatch):
    store = {"has": True, "content": "0 3 * * * /usr/bin/backup.sh\n"}
    monkeypatch.setattr(runner, "_read_crontab", lambda: (store["has"], store["content"]))

    def fake_write(contents):
        store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_write_crontab", fake_write)
    runner._do_provision_schedule(_crontab_action(tmp_path), "backup")
    assert "/usr/bin/backup.sh" in store["content"]  # user line kept
    assert sched.DEFAULT_LABEL in store["content"]  # ours added


def test_crontab_without_managed_strips_pair():
    content = (
        "0 3 * * * backup\n"
        f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n"
        "12 12 * * * python3 checker.py\n"
        "30 4 * * * other\n"
    )
    out = runner._crontab_without_managed(content, sched.DEFAULT_LABEL)
    assert "backup" in "\n".join(out)
    assert "other" in "\n".join(out)
    assert sched.DEFAULT_LABEL not in "\n".join(out)
    assert "checker.py" not in "\n".join(out)


def test_crontab_position_preserving_no_spurious_reorder(tmp_path, monkeypatch):
    """A user line AFTER rig's block must NOT cause drift / a reorder on re-apply (Opus #3)."""
    action = _crontab_action(tmp_path)
    pair = runner.schedule_plan_from_action(action).crontab_lines()
    # rig's block is in the MIDDLE; a user line follows it.
    current = "0 1 * * * before\n" + "\n".join(pair) + "\n0 9 * * * after\n"

    # crontab_with_managed returns None → no change needed (idempotent, position preserved).
    assert runner.crontab_with_managed(current, sched.DEFAULT_LABEL, pair) is None

    # and drift reports NO models drift for that exact crontab.
    monkeypatch.setattr(driftmod, "_read_crontab", lambda: (True, current))
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "models"]


def test_crontab_with_managed_updates_in_place(tmp_path):
    """A stale time updates the cron line WHERE IT SITS, keeping following user lines."""
    action = _crontab_action(tmp_path)
    pair = runner.schedule_plan_from_action(action).crontab_lines()
    stale = (
        "0 1 * * * before\n"
        f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n"
        "0 6 * * * python3 /old/checker.py\n"   # stale time + path
        "0 9 * * * after\n"
    )
    out = runner.crontab_with_managed(stale, sched.DEFAULT_LABEL, pair)
    assert out is not None
    joined = "\n".join(out)
    assert "before" in joined and "after" in joined  # both user lines kept
    assert pair[1] in joined  # the new cron line present
    assert "/old/checker.py" not in joined  # stale line gone
    # position preserved: 'after' still comes after our block
    assert out.index("0 9 * * * after") > out.index(pair[1])


def test_sh_quote_escapes_percent():
    """`%` is special in crontab; a checker path with `%` must be quoted+escaped, never bare."""
    s = sched.build_schedule(checker_path=Path("/weird/pa%th/model_freshness.py"),
                             platform="crontab")
    cron_line = s.crontab_lines()[1]
    # the raw `%` must not appear unescaped (it would truncate the command in crontab)
    assert "pa%th" not in cron_line
    assert "\\%" in cron_line


# ── drift ────────────────────────────────────────────────────────────────────────────────
def test_drift_launchd_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # drift.py imports the helpers by name → patch the names in the drift namespace.
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    from riglib.plan import InstallPlan

    plan = InstallPlan(actions=[_launchd_action(home)])
    report = detect(plan)
    sched_drift = [d for d in report.items if d.category == "models"]
    assert sched_drift and sched_drift[0].direction == "missing"


def test_drift_launchd_in_sync(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: True)
    action = _launchd_action(home)
    action.target.parent.mkdir(parents=True, exist_ok=True)
    action.target.write_text(runner.schedule_plan_from_action(action).plist_xml(), encoding="utf-8")
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "models"]


def test_drift_crontab_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(driftmod, "_read_crontab", lambda: (False, ""))
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[_crontab_action(tmp_path)]))
    drift = [d for d in report.items if d.category == "models"]
    assert drift and drift[0].direction == "missing"


def test_drift_crontab_modified(tmp_path, monkeypatch):
    # installed but at a DIFFERENT time → modified
    stale = (
        f"{sched.CRON_SENTINEL_PREFIX} {sched.DEFAULT_LABEL}\n"
        "0 6 * * * python3 /checkout/lib/checker/model_freshness.py\n"
    )
    monkeypatch.setattr(driftmod, "_read_crontab", lambda: (True, stale))
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[_crontab_action(tmp_path)]))
    drift = [d for d in report.items if d.category == "models"]
    assert drift and drift[0].direction == "modified"


def test_drift_crontab_in_sync(tmp_path, monkeypatch):
    action = _crontab_action(tmp_path)
    good = "\n".join(runner.schedule_plan_from_action(action).crontab_lines()) + "\n"
    monkeypatch.setattr(driftmod, "_read_crontab", lambda: (True, good))
    from riglib.plan import InstallPlan

    report = detect(InstallPlan(actions=[action]))
    assert not [d for d in report.items if d.category == "models"]


def test_schedule_dry_run_skips_daemon_launchd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("RIG_SCHEDULE_DRY_RUN", "1")
    called = []
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: called.append(verb) or 0)
    res = runner._do_provision_schedule(_launchd_action(home), "backup")
    assert res.status in ("created", "updated")
    assert called == []  # the daemon was never touched
    assert (home / "Library" / "LaunchAgents" / f"{sched.DEFAULT_LABEL}.plist").is_file()


def test_schedule_dry_run_skips_daemon_crontab(tmp_path, monkeypatch):
    monkeypatch.setenv("RIG_SCHEDULE_DRY_RUN", "1")
    monkeypatch.setattr(runner, "_read_crontab", lambda: (False, ""))
    wrote = []
    monkeypatch.setattr(runner, "_write_crontab", lambda c: wrote.append(c) or 0)
    res = runner._do_provision_schedule(_crontab_action(tmp_path), "backup")
    assert res.status == "created"
    assert wrote == []  # crontab never written


# ── full apply path through run_plan (crontab, mocked) ──────────────────────────────────
def test_run_plan_provisions_schedule(tmp_path, monkeypatch):
    store = {"has": False, "content": ""}
    monkeypatch.setattr(runner, "_read_crontab", lambda: (store["has"], store["content"]))

    def fake_write(contents):
        store["has"] = True
        store["content"] = contents
        return 0

    monkeypatch.setattr(runner, "_write_crontab", fake_write)
    from riglib.plan import InstallPlan

    report: ApplyReport = run_plan(InstallPlan(actions=[_crontab_action(tmp_path)]))
    assert not report.errors
    assert report.changed == 1
