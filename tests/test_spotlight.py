"""Tests for the Spotlight-exclude provisioner (sweep + launchd re-sweep agent + plan wiring)."""

from __future__ import annotations

from pathlib import Path

from riglib import spotlight
from riglib.actions.runner import _do_provision_spotlight
from riglib.plan import Action


def _make_tree(root: Path) -> None:
    """A project tree with dependency dirs at several depths + a nested node_modules."""
    (root / "proj/node_modules/pkg").mkdir(parents=True)
    (root / "proj/dist").mkdir(parents=True)
    (root / "proj/src").mkdir(parents=True)
    (root / "proj/packages/inner/node_modules").mkdir(parents=True)
    (root / "api/target/debug").mkdir(parents=True)
    (root / "api/.venv/lib").mkdir(parents=True)
    (root / "api/app").mkdir(parents=True)


def test_iter_target_dirs_finds_and_prunes(tmp_path):
    root = tmp_path / "work"
    _make_tree(root)
    found = spotlight.iter_target_dirs((root,), frozenset(spotlight.DEFAULT_DENY))
    names = sorted(p.name for p in found)
    # matched: two node_modules (top + nested), dist, target, .venv
    assert names == [".venv", "dist", "node_modules", "node_modules", "target"]
    # pruning: nothing UNDER a matched node_modules is yielded (pkg/ is not a match anyway, but
    # the walk must not have descended — assert no path contains node_modules/pkg style descent).
    assert not any("node_modules" in str(p.parent) for p in found)


def test_perform_sweep_drops_sentinels_idempotent(tmp_path):
    root = tmp_path / "xp"
    _make_tree(root)
    deny = frozenset(spotlight.DEFAULT_DENY)
    res1 = spotlight.perform_sweep((root,), deny)
    assert res1.matched == 5
    assert len(res1.created) == 5 and len(res1.existing) == 0
    for sentinel in res1.created:
        assert sentinel.name == spotlight.SENTINEL_NAME
        assert sentinel.is_file()
    # re-sweep is a no-op: everything already covered.
    res2 = spotlight.perform_sweep((root,), deny)
    assert len(res2.created) == 0 and len(res2.existing) == 5


def test_perform_sweep_records_missing_root(tmp_path):
    present = tmp_path / "work"
    (present / "proj/node_modules").mkdir(parents=True)
    absent = tmp_path / "nope"
    res = spotlight.perform_sweep((present, absent), frozenset(spotlight.DEFAULT_DENY))
    assert present in res.roots_scanned
    assert absent in res.roots_missing


def test_resolve_deny_replace_and_extra():
    assert spotlight.resolve_deny(["a", "b"], None) == frozenset({"a", "b"})  # replace
    got = spotlight.resolve_deny(None, ["zz"])
    assert "zz" in got and "node_modules" in got  # extra adds to default
    assert spotlight.resolve_deny(None, None) == frozenset(spotlight.DEFAULT_DENY)


def test_plist_xml_has_runatload_and_logging():
    plan = spotlight.build_spotlight(
        roots=(Path("/x"),), deny=frozenset({"node_modules"}),
        sweep_cmd=("/usr/bin/python3", "-m", "riglib", "spotlight-sweep"),
    )
    xml = plan.plist_xml()
    assert "<key>RunAtLoad</key>" in xml and "<true/>" in xml
    assert "StandardOutPath" in xml and "StandardErrorPath" in xml
    assert "StartCalendarInterval" in xml
    assert "spotlight-sweep" in xml


def test_do_provision_spotlight_sweeps_and_writes_plist(tmp_path, monkeypatch):
    root = tmp_path / "work"
    _make_tree(root)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")  # write the plist, skip live launchctl
    action = Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=tmp_path, target=Path("ai.hyperide.spotlight-exclude"),
        options={
            "roots": [str(root)],
            "deny": sorted(spotlight.DEFAULT_DENY),
            "label": "ai.hyperide.spotlight-exclude",
            "max_depth": 8,
            "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"],
        },
    )
    res = _do_provision_spotlight(action, "backup")
    assert res.ok
    # sentinels dropped
    assert (root / "proj/node_modules/.metadata_never_index").is_file()
    assert (root / "api/target/.metadata_never_index").is_file()
    # plist written (non-darwin hosts skip the plist — guard the assertion)
    import sys

    if sys.platform == "darwin":
        plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
        assert plist.is_file()
        assert "spotlight-sweep" in plist.read_text()


def test_plan_emits_spotlight_action_only_when_enabled(tmp_path):
    from riglib.config import LoadedConfig
    from riglib.plan import _build_spotlight, InstallPlan

    def _cfg(data):
        return LoadedConfig(data=data, repo_root=tmp_path)

    plan = InstallPlan()
    _build_spotlight(_cfg({}), plan)  # absent block → no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {"enabled": False}}), plan)  # disabled → no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {}}), plan)  # present-but-empty → default OFF, no action
    assert not plan.actions
    plan = InstallPlan()
    _build_spotlight(_cfg({"spotlight": {"enabled": True, "roots": ["~/w"], "extra": ["foo"]}}), plan)
    assert len(plan.actions) == 1
    act = plan.actions[0]
    assert act.kind == "provision_spotlight"
    assert "foo" in act.options["deny"]


def test_do_provision_spotlight_launchctl_load_failure_is_error(tmp_path, monkeypatch):
    import sys
    if sys.platform != "darwin":
        import pytest
        pytest.skip("launchd branch is macOS-only")
    from riglib.actions import runner

    root = tmp_path / "work"
    (root / "proj/node_modules").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the real load path
    monkeypatch.setattr(runner, "_launchctl", lambda verb, arg: 0 if verb == "unload" else 1)
    monkeypatch.setattr(runner, "_launchctl_loaded", lambda label: False)
    action = Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=tmp_path, target=Path("ai.hyperide.spotlight-exclude"),
        options={"roots": [str(root)], "deny": sorted(spotlight.DEFAULT_DENY),
                 "label": "ai.hyperide.spotlight-exclude", "max_depth": 8,
                 "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"]},
    )
    res = _do_provision_spotlight(action, "backup")
    assert res.status == "error"
    assert "launchctl load` failed" in res.detail
    # sentinel still dropped despite the load failure (the sweep ran first).
    assert (root / "proj/node_modules/.metadata_never_index").is_file()


def _spotlight_action(root: Path, label: str = "ai.hyperide.spotlight-exclude") -> Action:
    return Action(
        kind="provision_spotlight", category="spotlight", item="exclude",
        source=root, target=Path(label),
        options={
            "roots": [str(root)],
            "deny": sorted(spotlight.DEFAULT_DENY),
            "label": label,
            "max_depth": 8,
            "sweep_cmd": ["/usr/bin/python3", "-m", "riglib", "spotlight-sweep"],
        },
    )


def _apply_spotlight(root: Path, home: Path, monkeypatch) -> Action:
    """Apply the provisioner (dry-run launchd) so sentinels + plist land, and return the action."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RIG_SPOTLIGHT_DRY_RUN", "1")  # write plist, skip live launchctl
    action = _spotlight_action(root)
    _do_provision_spotlight(action, "backup")
    return action


def _spotlight_darwin_applied(tmp_path, monkeypatch, platform: str = "darwin") -> tuple[Action, Path, Path]:
    """Pin the platform, build a project tree, apply (sentinels + plist), pin ``Path.home``.

    The shared preamble of the spotlight drift tests — returns (action, root, home) so each test
    only writes its own perturbation + assertion. ``platform`` pins ``sys.platform`` at BOTH apply
    and detect time (``"linux"`` exercises the non-darwin no-plist path on every host).
    """
    import sys

    monkeypatch.setattr(sys, "platform", platform)
    root = tmp_path / "work"
    _make_tree(root)
    home = tmp_path / "home"
    home.mkdir()
    action = _apply_spotlight(root, home, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return action, root, home


def _spotlight_drift(action: Action) -> list:
    from riglib.drift import detect
    from riglib.plan import InstallPlan

    return [d for d in detect(InstallPlan(actions=[action])).items if d.category == "spotlight"]


def test_drift_spotlight_in_sync_after_apply(tmp_path, monkeypatch):
    """Right after apply, `rig status` sees NO spotlight drift (sentinels + plist match config)."""
    action, _root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    assert not _spotlight_drift(action)


def test_drift_spotlight_missing_sentinel(tmp_path, monkeypatch):
    """A matched dir that lost its sentinel (or a new project appeared) is surfaced as drift."""
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    # a fresh project appears without a sentinel (simulates a new checkout after the last sweep)
    (root / "fresh/node_modules").mkdir(parents=True)
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and spotlight.SENTINEL_NAME in d.detail for d in drift)


def test_drift_spotlight_scans_full_set_not_just_sample(tmp_path, monkeypatch):
    """Drift must catch an uncovered project even when it sorts PAST the first SAMPLE_LIMIT dirs.

    Verify's post-apply spot-check samples the head of the matched list; reusing that bound for
    drift would silently miss a freshly-added project whenever it lands beyond the sample —
    defeating the check's stated purpose. This pins the fresh dir at the tail of a >SAMPLE_LIMIT
    list so the old first-N slice would have skipped it.
    """
    action, root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    covered = []
    for i in range(spotlight.SAMPLE_LIMIT + 5):
        d = root / f"covered{i:03d}/node_modules"
        d.mkdir(parents=True)
        spotlight.sentinel_path(d).touch()  # already covered
        covered.append(d)
    fresh = root / "zzz_fresh/node_modules"
    fresh.mkdir(parents=True)  # a new project appeared, no sentinel — must be flagged
    # Deterministic order: the uncovered dir is LAST, beyond the first-N window the old code sampled.
    monkeypatch.setattr(spotlight, "iter_target_dirs", lambda *a, **k: [*covered, fresh])
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and spotlight.SENTINEL_NAME in d.detail for d in drift)


def test_drift_spotlight_missing_plist(tmp_path, monkeypatch):
    """The launchd re-sweep agent plist vanishing is drift (new projects would stop being covered)."""
    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    assert plist.is_file()
    plist.unlink()
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and "plist" in d.detail for d in drift)


def test_drift_spotlight_modified_plist(tmp_path, monkeypatch):
    """A hand-edited re-sweep plist (drifted schedule/argv) is surfaced as modified."""
    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    plist.write_text("<plist>tampered</plist>", encoding="utf-8")
    drift = _spotlight_drift(action)
    assert any(d.direction == "modified" for d in drift)


def test_drift_spotlight_agent_not_loaded(tmp_path, monkeypatch):
    """With a correct plist on disk but the launchd agent NOT loaded (real load path, not dry-run),
    status flags 'not loaded' — the branch every other drift test short-circuits via dry-run."""
    from riglib import drift as driftmod

    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    assert plist.is_file()  # plist matches config; only the loaded-state differs
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the live loaded-probe
    # explicit here (the autouse fixture already stubs this False) — this is the one test whose
    # branch depends on the loaded-probe outcome, so pin it locally rather than rely on the fixture.
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    drift = _spotlight_drift(action)
    assert any(d.direction == "missing" and "not loaded" in d.detail for d in drift)


def test_drift_spotlight_modified_and_not_loaded_both_surface(tmp_path, monkeypatch):
    """A content-drifted plist whose agent is ALSO not loaded surfaces BOTH facts — the modified
    check must not short-circuit the (independent) loaded probe."""
    from riglib import drift as driftmod

    action, _root, home = _spotlight_darwin_applied(tmp_path, monkeypatch)
    plist = home / "Library/LaunchAgents/ai.hyperide.spotlight-exclude.plist"
    plist.write_text("<plist>tampered</plist>", encoding="utf-8")  # content drift
    monkeypatch.delenv("RIG_SPOTLIGHT_DRY_RUN", raising=False)  # exercise the live loaded-probe
    monkeypatch.setattr(driftmod, "_launchctl_loaded", lambda label: False)
    drift = _spotlight_drift(action)
    assert any(d.direction == "modified" for d in drift)
    assert any(d.direction == "missing" and "not loaded" in d.detail for d in drift)


def test_drift_spotlight_skipped_on_non_darwin(tmp_path, monkeypatch):
    """On a non-macOS host the provisioner writes no plist, so status must NOT flag a phantom one."""
    action, _root, _home = _spotlight_darwin_applied(tmp_path, monkeypatch, platform="linux")
    # sentinels are present (sweep is cross-platform), and there is no launchd plist to miss.
    assert not [d for d in _spotlight_drift(action) if "plist" in d.detail]


def test_iter_target_dirs_respects_max_depth_boundary(tmp_path):
    # a matched dir exactly AT the cap is excluded; one level shallower is found — guards the
    # off-by-one in `len(here.parts) - root_depth >= max_depth`.
    root = tmp_path / "work"
    (root / "a/node_modules").mkdir(parents=True)          # depth 1 under root (a/ then match)
    (root / "a/b/c/node_modules").mkdir(parents=True)      # deeper
    deny = frozenset({"node_modules"})
    shallow = spotlight.iter_target_dirs((root,), deny, max_depth=2)
    assert (root / "a/node_modules") in shallow
    assert (root / "a/b/c/node_modules") not in shallow    # beyond the depth-2 cap → pruned
    deep = spotlight.iter_target_dirs((root,), deny, max_depth=8)
    assert (root / "a/b/c/node_modules") in deep


def test_cmd_spotlight_sweep_reads_deny_from_config(tmp_path, monkeypatch, capsys):
    # the sweep command honors a custom `deny` from the merged config (not just `roots`).
    from riglib.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    (work / "proj/weirddir").mkdir(parents=True)
    (work / "proj/node_modules").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "rig.yaml"
    cfg.write_text(
        f"version: 1\nspotlight:\n  enabled: true\n  roots:\n    - {work}\n"
        "  deny:\n    - weirddir\n",
        encoding="utf-8",
    )
    rc = main(["spotlight-sweep", "-C", str(repo), "--config", str(cfg)])
    assert rc == 0
    # custom deny took effect; the default node_modules is NOT swept (deny REPLACES the default).
    assert (work / "proj/weirddir/.metadata_never_index").is_file()
    assert not (work / "proj/node_modules/.metadata_never_index").exists()
