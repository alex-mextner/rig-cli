"""Tests for config-web's plan-preview + interactive-apply engine (riglib/config_web_plan.py,
rig-cli#310) — action_key/fingerprint stability, the Global-scope filter, and the ApplyJobStore
running SELECTED actions through the real riglib.actions.runner.run_plan (never a re-implemented
executor), with staleness + concurrency guards.

Only the well-established "skills-only, everything risky off" config
(mirrors tests/test_apply_preview.py's `_small_config`) is ever actually APPLIED here — never
`all: true` against the real catalog, and never a config that could touch tg_ctl/tmux/github for
real.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from riglib import config_web_plan as cwp
from riglib.config_web_scopes import Scope, GLOBAL_SCOPE_ID
from riglib.layers import GLOBAL, layer_for_category


def _small_config(tmp_path: Path, fake_agent_tools: Path) -> str:
    return (
        f"version: 1\nagent_tools_source: {fake_agent_tools}\n"
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}\n"
        "agent_hooks: {enabled: false}\nci: {enabled: false}\nmcp: {enabled: false}\n"
        "git_hooks: {dispatcher: {enabled: false}}\n"
        "harness: {enabled: false}\npermissions: {enabled: false}\n"
        "agents_md: {enabled: false}\ngitignore: {enabled: false}\n"
        "tmux: {enabled: false}\ntg_ctl: {enabled: false}\nmodels: {enabled: false}\n"
    )


def _repo_scope(tmp_path: Path, fake_agent_tools: Path, monkeypatch, *, body: str | None = None) -> Scope:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rig.yaml").write_text(body or _small_config(tmp_path, fake_agent_tools), encoding="utf-8")
    return Scope(id=str(repo.resolve()), label="repo", repo_root=repo.resolve(), is_global=False)


def test_build_scope_plan_repo_has_actions(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    assert scope_plan.plan.actions
    assert any(a.kind == "copy_skill" for a in scope_plan.plan.actions)


def test_global_scope_plan_only_contains_global_layer_actions(tmp_path, fake_agent_tools, monkeypatch):
    """The Global scope's PLAN must match its VIEW exactly (rig-cli#310, found in review twice):
    the narrow WRITABLE-global set (gitignore/spotlight/tg_ctl/tmux/mode) -- never the broader
    status-layer GLOBAL set (which also counts skills/harness/permissions/agent_hooks as global
    machine-wide ARTIFACTS, even though those are written into a REPO rig.yaml and so are invisible
    on the Global tab's own view). "Apply selected" on the Global tab must never execute an action
    the tab gives no way to see or opt out of by editing a setting there.
    """
    from riglib.schema import writable_layer_for_category

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))
    scope = Scope(id=GLOBAL_SCOPE_ID, label="Global", repo_root=None, is_global=True)
    scope_plan = cwp.build_scope_plan(scope)
    for a in scope_plan.plan.actions:
        assert layer_for_category(a.category) == GLOBAL, f"{a.category} leaked into the Global scope"
        assert writable_layer_for_category(a.category) == GLOBAL, (
            f"{a.category} is status-global but NOT writable-global -- it must not appear in the "
            "Global tab's plan (its view can't show/edit it)"
        )
    assert not any(a.category in ("skills", "harness", "agent_hooks", "permissions") for a in scope_plan.plan.actions)


def test_action_key_stable_across_rebuilds(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    plan1 = cwp.build_scope_plan(scope).plan
    plan2 = cwp.build_scope_plan(scope).plan
    keys1 = {cwp.action_key(a) for a in plan1.actions}
    keys2 = {cwp.action_key(a) for a in plan2.actions}
    assert keys1 == keys2
    assert cwp.fingerprint_plan(plan1) == cwp.fingerprint_plan(plan2)


def test_fingerprint_changes_when_config_changes(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    fp_before = cwp.fingerprint_plan(cwp.build_scope_plan(scope).plan)

    # disable the by_type cli bundle -> fewer actions -> different fingerprint
    new_body = _small_config(tmp_path, fake_agent_tools).replace(
        "skills: {universal: {all: true}, by_type: {enable: [cli]}}",
        "skills: {universal: {all: true}, by_type: {enable: []}}",
    )
    (scope.repo_root / "rig.yaml").write_text(new_body, encoding="utf-8")
    fp_after = cwp.fingerprint_plan(cwp.build_scope_plan(scope).plan)
    assert fp_before != fp_after


def test_preview_payload_tags_every_action(tmp_path, fake_agent_tools, monkeypatch):
    from riglib.action_tags import CATEGORIES

    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    payload = cwp.preview_payload(scope_plan)
    assert payload["scope"] == scope.id
    assert payload["fingerprint"] == cwp.fingerprint_plan(scope_plan.plan)
    assert len(payload["actions"]) == len(scope_plan.plan.actions)
    for row in payload["actions"]:
        assert row["tag"]["category"] in CATEGORIES
        assert row["key"]


# ── ApplyJobStore ────────────────────────────────────────────────────────────────────────────


def _wait_for_job(store: cwp.ApplyJobStore, job_id: str, timeout: float = 10.0) -> cwp.ApplyJob:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get(job_id)
        if job is not None and job.done:
            return job
        time.sleep(0.02)
    raise TimeoutError(f"apply job {job_id} did not finish within {timeout}s")


def test_apply_runs_selected_actions_via_run_plan(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)

    store = cwp.ApplyJobStore()
    job = store.start(scope, expected_fingerprint=fp, skip_keys=set())
    done = _wait_for_job(store, job.id)

    assert done.error is None
    statuses = {row.status for row in done.actions}
    assert "queued" not in statuses and "running" not in statuses
    # the real engine actually ran: skills landed on disk under the isolated HOME
    home = scope.repo_root.parent / "home"
    assert (home / ".claude" / "skills").exists()


def test_apply_skip_keys_are_not_run(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)
    all_keys = {cwp.action_key(a) for a in scope_plan.plan.actions}

    store = cwp.ApplyJobStore()
    job = store.start(scope, expected_fingerprint=fp, skip_keys=all_keys)
    done = _wait_for_job(store, job.id)

    assert done.error is None
    assert all(row.status == "skipped" for row in done.actions)
    home = scope.repo_root.parent / "home"
    assert not (home / ".claude" / "skills").exists()


def test_apply_rejects_stale_fingerprint(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    store = cwp.ApplyJobStore()
    with pytest.raises(cwp.PlanStaleError):
        store.start(scope, expected_fingerprint="not-a-real-fingerprint", skip_keys=set())
    home = scope.repo_root.parent / "home"
    assert not (home / ".claude" / "skills").exists()


def test_apply_rejects_unknown_skip_key(tmp_path, fake_agent_tools, monkeypatch):
    """A skip_key matching no action in the current plan must fail closed (409/PlanStaleError),
    never be silently dropped -- that would run an action the caller explicitly tried to skip
    (found in review: the one place in this flow that previously failed OPEN, not closed).
    """
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)

    store = cwp.ApplyJobStore()
    with pytest.raises(cwp.PlanStaleError):
        store.start(scope, expected_fingerprint=fp, skip_keys={"bogus|not-a-real-action|key"})
    home = scope.repo_root.parent / "home"
    assert not (home / ".claude" / "skills").exists(), "nothing must apply when skip_keys is bogus"


def test_apply_busy_rejects_concurrent_start(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)

    release = threading.Event()
    import riglib.actions.runner as runner_mod

    original_run_plan = runner_mod.run_plan

    def _blocking_run_plan(plan, **kwargs):
        release.wait(timeout=5)
        return original_run_plan(plan, **kwargs)

    monkeypatch.setattr(runner_mod, "run_plan", _blocking_run_plan)

    store = cwp.ApplyJobStore()
    job1 = store.start(scope, expected_fingerprint=fp, skip_keys=set())
    try:
        with pytest.raises(cwp.ApplyBusyError):
            store.start(scope, expected_fingerprint=fp, skip_keys=set())
    finally:
        release.set()
    _wait_for_job(store, job1.id)


def test_get_returns_none_for_unknown_job():
    store = cwp.ApplyJobStore()
    assert store.get("does-not-exist") is None


# ── drift ────────────────────────────────────────────────────────────────────────────────────


def test_compute_scope_drift_reports_missing_actions(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    drift = cwp.compute_scope_drift(scope_plan)
    assert drift["scope"] == scope.id
    # nothing applied yet -> every declared skill is "missing" (config->disk) drift
    assert not drift["in_sync"]
    assert any(i["direction"] == "missing" for i in drift["items"])


def test_compute_scope_drift_in_sync_after_apply(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)
    store = cwp.ApplyJobStore()
    job = store.start(scope, expected_fingerprint=fp, skip_keys=set())
    _wait_for_job(store, job.id)

    fresh_plan = cwp.build_scope_plan(scope)
    drift = cwp.compute_scope_drift(fresh_plan)
    assert drift["in_sync"], drift["items"]


# ── fingerprint sensitivity regression (found in review) ───────────────────────────────────────


def test_fingerprint_changes_with_on_conflict_alone(tmp_path):
    from riglib.plan import Action, InstallPlan

    def _plan(on_conflict):
        p = InstallPlan(on_conflict=on_conflict)
        p.actions = [
            Action(kind="copy_skill", category="skills", item="x", source=tmp_path, target=tmp_path / "s")
        ]
        return p

    fp_backup = cwp.fingerprint_plan(_plan("backup"))
    fp_overwrite = cwp.fingerprint_plan(_plan("overwrite"))
    assert fp_backup != fp_overwrite, (
        "the action list is identical -- only on_conflict changed -- so the fingerprint must "
        "still differ, or a stale preview with a safer on_conflict could be applied under a "
        "since-changed destructive one"
    )


def test_fingerprint_changes_with_action_options_alone(tmp_path):
    from riglib.plan import Action, InstallPlan

    def _plan(kind_option):
        p = InstallPlan()
        p.actions = [
            Action(
                kind="apply_harness", category="harness", item="claude-code",
                source=tmp_path, target=tmp_path / "settings.json",
                options={"kind": kind_option},
            )
        ]
        return p

    fp_a = cwp.fingerprint_plan(_plan("claude-code"))
    fp_b = cwp.fingerprint_plan(_plan("opencode"))
    assert fp_a != fp_b, "same action key, different options -- the fingerprint must still differ"


# ── Global-scope drift must never leak repo-local CI scanning (found in review) ────────────────


def test_global_scope_drift_ignores_home_being_a_git_repo(tmp_path, fake_agent_tools, monkeypatch):
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    # make $HOME itself a real git repo with a stray .github/workflows file -- a dotfiles-style
    # setup. The Global tab must NEVER treat this as "the repo" for CI drift scanning.
    subprocess.run(["git", "init", "-q"], cwd=home, check=True)
    workflows = home / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "foo.yml").write_text("name: foo\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))

    scope = Scope(id=GLOBAL_SCOPE_ID, label="Global", repo_root=None, is_global=True)
    scope_plan = cwp.build_scope_plan(scope)
    assert scope_plan.env.is_git_repo is False

    drift = cwp.compute_scope_drift(scope_plan)
    assert not any(
        i["category"] == "ci" and "foo.yml" in i["item"] for i in drift["items"]
    ), f"Global tab leaked $HOME's own git-repo CI dir into drift: {drift['items']}"


def test_global_scope_drift_does_not_flag_installed_skills_as_extra(tmp_path, fake_agent_tools, monkeypatch):
    """A skill some REPO tab installed into ~/.agents/skills must NOT show as drift on the Global
    tab -- the Global plan declares zero skills actions by design (build_scope_plan's docstring),
    so without restrict_scan_categories every real rig user would see permanent false-positive
    "extra" drift there for every installed skill (found in review).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))

    # simulate a REPO tab having already installed a skill into the shared global skills dir
    skill_dir = home / ".agents" / "skills" / "naming"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: naming\n---\n# naming\n", encoding="utf-8")

    scope = Scope(id=GLOBAL_SCOPE_ID, label="Global", repo_root=None, is_global=True)
    scope_plan = cwp.build_scope_plan(scope)
    drift = cwp.compute_scope_drift(scope_plan)

    assert not any(i["category"] == "skills" for i in drift["items"]), drift["items"]


def test_global_scope_compute_scope_drift_returns_well_formed_payload(tmp_path, fake_agent_tools, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("RIG_AGENT_TOOLS_SOURCE", str(fake_agent_tools))

    scope = Scope(id=GLOBAL_SCOPE_ID, label="Global", repo_root=None, is_global=True)
    scope_plan = cwp.build_scope_plan(scope)
    drift = cwp.compute_scope_drift(scope_plan)

    assert drift["scope"] == GLOBAL_SCOPE_ID
    assert isinstance(drift["in_sync"], bool)
    assert isinstance(drift["items"], list)


# ── further regressions found in the second review pass ────────────────────────────────────────


def test_fingerprint_changes_with_action_source_alone(tmp_path):
    from riglib.plan import Action, InstallPlan

    def _plan(source_dir):
        p = InstallPlan()
        p.actions = [
            Action(kind="copy_skill", category="skills", item="x", source=source_dir, target=tmp_path / "s")
        ]
        return p

    fp_a = cwp.fingerprint_plan(_plan(tmp_path / "catalog-a"))
    fp_b = cwp.fingerprint_plan(_plan(tmp_path / "catalog-b"))
    assert fp_a != fp_b, (
        "identical action key, different source (e.g. agent_tools_source repointed at another "
        "checkout) -- the fingerprint must still differ, or a stale preview could apply content "
        "from a source the user never saw"
    )


def test_duplicate_action_key_raises_plan_integrity_error(tmp_path, fake_agent_tools, monkeypatch):
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    # force a collision: two actions with identical kind/category/item/target/no-descriptor
    dup = scope_plan.plan.actions[0]
    scope_plan.plan.actions = [dup, dup]
    with pytest.raises(cwp.PlanIntegrityError):
        cwp.preview_payload(scope_plan)


def test_apply_reports_action_failure_not_silent_success(tmp_path, fake_agent_tools, monkeypatch):
    """A per-action failure must NOT report as a silent success (job.error stays None).

    run_plan() never raises for a per-action failure -- actions/runner.py catches each one into
    an ActionResult(status="error") and keeps going. ApplyJobStore must inspect the returned
    report and set job.error when any action failed (found in review: a fully-failed apply was
    reporting done + error=None, which the JS renders as a green "applied" toast).
    """
    import riglib.actions.runner as runner_mod
    from riglib.actions.runner import ActionResult

    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    scope_plan = cwp.build_scope_plan(scope)
    fp = cwp.fingerprint_plan(scope_plan.plan)
    assert any(a.kind == "copy_skill" for a in scope_plan.plan.actions)

    original_handlers = dict(runner_mod._HANDLERS)

    def _forced_failure(action, on_conflict):
        return ActionResult(action, "error", "forced failure for test")

    patched = dict(original_handlers)
    patched["copy_skill"] = _forced_failure
    monkeypatch.setattr(runner_mod, "_HANDLERS", patched)

    store = cwp.ApplyJobStore()
    job = store.start(scope, expected_fingerprint=fp, skip_keys=set())
    done = _wait_for_job(store, job.id)

    assert done.error is not None, "a failed action must surface as job.error, not a silent success"
    assert "1 action(s) failed" in done.error or "action(s) failed" in done.error


def test_job_store_evicts_oldest_finished_jobs_over_capacity(tmp_path, fake_agent_tools, monkeypatch):
    """Retention is bounded -- a long-lived process must not accumulate jobs forever."""
    scope = _repo_scope(tmp_path, fake_agent_tools, monkeypatch)
    store = cwp.ApplyJobStore()

    job_ids = []
    for _ in range(cwp._MAX_RETAINED_JOBS + 5):
        scope_plan = cwp.build_scope_plan(scope)
        fp = cwp.fingerprint_plan(scope_plan.plan)
        all_keys = {cwp.action_key(a) for a in scope_plan.plan.actions}
        job = store.start(scope, expected_fingerprint=fp, skip_keys=all_keys)  # skip everything: fast
        _wait_for_job(store, job.id)
        job_ids.append(job.id)

    assert len(store._jobs) <= cwp._MAX_RETAINED_JOBS
    assert store.get(job_ids[0]) is None, "the oldest job must have been evicted"
    assert store.get(job_ids[-1]) is not None, "the most recent job must still be retained"


def test_global_only_categories_matches_writable_layer_classification():
    """schema.global_only_categories() (the drift-restriction set) must stay in lockstep with
    writable_layer_for_category() (the view/plan-filter predicate) -- they're two derivations of
    the SAME underlying set; a drift-only test wouldn't catch the two silently diverging.
    """
    from riglib.layers import GLOBAL
    from riglib.schema import global_only_categories, writable_layer_for_category

    categories = global_only_categories()
    assert categories == {"gitignore", "spotlight", "tg_ctl", "tmux", "mode"}
    for cat in categories:
        assert writable_layer_for_category(cat) == GLOBAL
    assert writable_layer_for_category("skills") != GLOBAL
    assert writable_layer_for_category("env") != GLOBAL
