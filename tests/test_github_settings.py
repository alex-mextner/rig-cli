"""The full ``github:`` repo-settings area — merge / ghas / actions / browser + the #4136.1 auth gate.

Companion to ``test_github_ruleset.py`` (the branch ruleset). Covers the remaining repo-settings
backends rig reconciles autonomously:

  - ``github.merge``   — the squash-only merge-button policy via ``PATCH /repos`` (folded in from #37).
  - ``github.ghas``    — GitHub Advanced Security (dep-graph / vuln-alerts / Dependabot / secret-
                         scanning / CodeQL), incl. the private-repo-unlicensed LOUD degrade.
  - ``github.actions`` — Actions permissions (two PUTs), least-privilege GITHUB_TOKEN default.
  - ``github.browser`` — the agent-browser backend for API-unreachable settings (command plan +
                         the RIG_GH_BROWSER apply gate + the auth gate).
  - the auth gate     — CTO #4136.1: not-authed → notify (tg) + WAIT/poll for login → resume; the
                         autonomous (0-budget) path degrades loudly instead of hanging.

Every test is deterministic: the ``gh``/``agent-browser`` subprocess seams, the git-remote
resolution, and the auth probe/notify/sleep seams are all monkeypatched — nothing here hits the
network, a real repo, a real browser, or the user's phone. RIG_GH_DRY_RUN is NOT relied on for the
mutation tests (they assert the real PATCH/PUT body via the recorder), but a dedicated test proves
the dry-run no-mutation seam.
"""

from __future__ import annotations

import json

import pytest

from riglib.actions import runner
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib import github_auth
from riglib.github_actions import (
    GITHUB_ACTIONS_DEFAULTS,
    build_permissions_body,
    build_workflow_permissions_body,
    normalize_permissions,
)
from riglib.github_browser import (
    UI_ONLY_TOGGLES,
    build_command_plan,
    desired_toggles,
    settings_url,
)
from riglib.github_ghas import (
    GITHUB_GHAS_DEFAULTS,
    build_security_analysis_body,
    normalize_security_analysis,
)
from riglib.github_merge import (
    GITHUB_MERGE_DEFAULTS,
    MANAGED_MERGE_API_FIELDS,
    build_merge_body,
)
from riglib.plan import Action, build


# ── shared helpers ───────────────────────────────────────────────────────────────────
@pytest.fixture
def gh_repo(monkeypatch):
    """Pretend the repo has a github origin remote (no real git needed)."""
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: ("acme", "widget"))


@pytest.fixture(autouse=True)
def _authed(monkeypatch):
    """Default every test to an already-authed gate — the gate has its OWN tests below.

    Autouse so the mutation tests don't each have to stub it; the gate-specific tests override it.
    Also resets the process-level per-apply auth-gate dedup set so one test's timed-out kind can't
    leak into the next (the real reset happens in run_plan; tests call handlers directly).
    """
    github_auth.reset_auth_gate()
    monkeypatch.setattr(runner, "ensure_gh_auth", lambda **kw: github_auth.AuthOutcome("ok"))
    monkeypatch.setattr(runner, "ensure_browser_auth", lambda **kw: github_auth.AuthOutcome("ok"))


def _action(kind, item, repo, opts) -> Action:
    return Action(kind=kind, category="github", item=item, source=repo, target=repo, options=opts)


def _merge_action(repo, **overrides):
    return _action("provision_github_merge", "merge", repo, {**GITHUB_MERGE_DEFAULTS, **overrides})


def _ghas_action(repo, **overrides):
    return _action("provision_github_ghas", "ghas", repo, {**GITHUB_GHAS_DEFAULTS, **overrides})


def _actions_action(repo, **overrides):
    return _action("provision_github_actions", "actions", repo, {**GITHUB_ACTIONS_DEFAULTS, **overrides})


def _browser_action(repo, **overrides):
    base = {k: v["default"] for k, v in UI_ONLY_TOGGLES.items()}
    return _action("provision_github_browser", "browser", repo, {**base, **overrides})


def _cfg(data, repo_root):
    return LoadedConfig(data=data, repo_root=repo_root)


# ════════════════════════════════════════════════════════════════════════════════════
# github.merge — squash-only merge-button policy (PATCH /repos)
# ════════════════════════════════════════════════════════════════════════════════════
def test_merge_body_is_squash_only_secure_default():
    body = build_merge_body(GITHUB_MERGE_DEFAULTS)
    assert body == {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "delete_branch_on_merge": True,
        "allow_auto_merge": True,
        "allow_update_branch": True,
    }


def test_merge_body_carries_only_managed_fields_never_repo_identity():
    # the PATCH body must never carry a name/visibility key — a guard against renaming/exposing.
    body = build_merge_body({**GITHUB_MERGE_DEFAULTS, "merge_commit": True})
    assert set(body) == set(MANAGED_MERGE_API_FIELDS)
    assert not any(k in body for k in ("name", "private", "visibility", "description"))


def test_merge_body_hard_bools():
    # a stray truthy/falsy config value is coerced to a real bool (never sends 1 / "").
    body = build_merge_body({**GITHUB_MERGE_DEFAULTS, "squash_merge": 1, "merge_commit": ""})
    assert body["allow_squash_merge"] is True and body["allow_merge_commit"] is False


def test_merge_state_update_then_ok(monkeypatch, gh_repo, tmp_path):
    # live repo currently allows merge commits → update; after we report it matches → ok.
    live = {"allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
            "delete_branch_on_merge": False, "allow_auto_merge": False, "allow_update_branch": False}
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    state, _ = runner.github_merge_state(_merge_action(tmp_path))
    assert state == "update"

    matching = dict(build_merge_body(GITHUB_MERGE_DEFAULTS))
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(matching), ""))
    state, _ = runner.github_merge_state(_merge_action(tmp_path))
    assert state == "ok"


def test_merge_apply_patches_only_managed_fields(monkeypatch, gh_repo, tmp_path):
    live = {"allow_squash_merge": False}  # differs → update
    patched: dict = {}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH":
            patched["body"] = json.loads(input_text)
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "updated"
    assert patched["body"] == build_merge_body(GITHUB_MERGE_DEFAULTS)
    assert "name" not in patched["body"] and "private" not in patched["body"]


def test_merge_no_remote_is_skip_not_error(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "skipped" and "no github origin" in res.detail


def test_merge_gh_error_degrades_loud(monkeypatch, gh_repo, tmp_path):
    # a no-admin token / not-authed read → 403 → loud error, never a silent green.
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: Forbidden"))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


def test_merge_dry_run_makes_no_patch(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    live = {"allow_squash_merge": False}
    calls: list = []

    def fake(args, *, input_text=None):
        calls.append(args)
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "updated" and "RIG_GH_DRY_RUN" in res.detail
    assert not any("--method" in c for c in calls)  # no PATCH issued


def test_merge_drift_parity(monkeypatch, gh_repo, tmp_path):
    # apply and drift switch on the SAME github_merge_state.
    live = {"allow_squash_merge": True, "allow_merge_commit": True}
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    plan_obj = type("P", (), {"actions": [_merge_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    merge_items = [i for i in report.items if i.item == "merge"]
    assert merge_items and merge_items[0].direction == "modified"


# ════════════════════════════════════════════════════════════════════════════════════
# github.ghas — GitHub Advanced Security
# ════════════════════════════════════════════════════════════════════════════════════
def test_ghas_security_analysis_body_secure_default():
    body = build_security_analysis_body(GITHUB_GHAS_DEFAULTS)
    # dependency_graph is NOT in the PATCH-body diff (github.com doesn't round-trip it); only the
    # secret-scanning fields are compared/PATCHed via security_and_analysis.
    assert body == {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
    }
    assert "dependency_graph" not in body


def test_ghas_body_disabled_knob_sets_disabled_status():
    body = build_security_analysis_body({**GITHUB_GHAS_DEFAULTS, "secret_scanning": False})
    assert body["secret_scanning"] == {"status": "disabled"}


def test_ghas_normalize_missing_block_reads_disabled():
    assert normalize_security_analysis({}) == {
        "secret_scanning": "disabled",
        "secret_scanning_push_protection": "disabled",
    }


def test_ghas_state_update_when_scanner_off(monkeypatch, gh_repo, tmp_path):
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"},
                                      "dependency_graph": {"status": "enabled"},
                                      "secret_scanning_push_protection": {"status": "enabled"}}}
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (0, json.dumps(live), ""))
    state, _ = runner.github_ghas_state(_ghas_action(tmp_path))
    assert state == "update"


def test_ghas_apply_patches_sub_resources_and_codeql(monkeypatch, gh_repo, tmp_path):
    # everything is currently OFF, so apply must drive the security PATCH, both sub-resource PUTs,
    # and the CodeQL PATCH (read-then-act: the GETs report off, so the mutations fire).
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}
    seen: list[tuple[str, str]] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            method = args[args.index("--method") + 1]
            path = args[args.index("--method") + 2]
            seen.append((method, path))
            return 0, "{}", ""
        # GETs: repo object, the sub-resource presence reads (404 = off), CodeQL state (off).
        if args[0].endswith("/vulnerability-alerts") or args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"  # disabled
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "not-configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    methods = {(m, p.split("/")[-1]) for m, p in seen}
    assert ("PATCH", "widget") in methods  # PATCH /repos/acme/widget (security_and_analysis)
    assert ("PUT", "vulnerability-alerts") in methods
    assert ("PUT", "automated-security-fixes") in methods
    assert ("PATCH", "default-setup") in methods


def test_ghas_second_apply_is_idempotent_noop(monkeypatch, gh_repo, tmp_path):
    # everything already in the desired state → state ok, NO mutations, reports skipped.
    sa = {f: {"status": s} for f, s in {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}.items()}
    live = {"security_and_analysis": sa}
    mutated: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            mutated.append(args)
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""  # 204 → enabled (desired)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "skipped" and not mutated  # a true no-op second apply


def test_ghas_private_unlicensed_degrades_loud_not_crash(monkeypatch, gh_repo, tmp_path):
    # a private repo without a GHAS plan → 403/422 on the scanners; the apply degrades LOUDLY but
    # does not crash and does not return a silent green.
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH" and args[args.index("--method") + 2] == "repos/acme/widget":
            return 1, "", "HTTP 422: Advanced Security is not available for this repository"
        if "--method" in args:
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"  # the free features still applied
    assert "DEGRADED" in res.detail and "GHAS" in res.detail


def test_ghas_plan_gated_scanner_unreadable_still_applies_free_features(monkeypatch, gh_repo, tmp_path):
    """Review-finding #2: a code-scanning endpoint that can't even be READ (plan-gated 403 on a
    private repo) must NOT collapse the whole apply to an error. The free features (vuln-alerts /
    Dependabot) must still be applied, and the unreadable scanner degraded LOUDLY — not a bare error
    that strands everything (the old behavior returned `gh_error` from the classifier and bailed)."""
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/default-setup"):
                return 1, "", "HTTP 403: Advanced Security is not available for this repository"
            puts.append(path.split("/")[-1])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts") or args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will enable them (free features)
        if args[0].endswith("/default-setup"):
            return 1, "", "HTTP 403: Advanced Security is not available for this repository"  # UNREADABLE
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    # the FREE features were still applied despite the plan-gated scanner being unreadable
    assert "vulnerability-alerts" in puts and "automated-security-fixes" in puts
    # and the unreadable scanner is surfaced LOUDLY, not silently dropped
    assert "DEGRADED" in res.detail and "code-scanning" in res.detail


def test_ghas_real_auth_error_is_not_swallowed_as_degrade(monkeypatch, gh_repo, tmp_path):
    # a genuine permission error (not a plan/feature limit) must surface as an error, not a degrade.
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PATCH" and args[args.index("--method") + 2] == "repos/acme/widget":
            return 1, "", "HTTP 403: Resource not accessible by integration"
        if "--method" in args:
            return 0, "{}", ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


def test_ghas_sa_block_hard_error_still_applies_free_features(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 8) #1: a GENUINE security_and_analysis PATCH failure must NOT early-out
    and strand the free features — it goes to hard_errors (final status=error) but vuln-alerts /
    Dependabot are still attempted, matching the 'applied independently' design."""
    live = {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}  # SA drifts
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            method, path = args[args.index("--method") + 1], args[args.index("--method") + 2]
            if method == "PATCH" and path == "repos/acme/widget":
                return 1, "", "HTTP 403: Resource not accessible by integration"  # REAL error
            puts.append(path.split("/")[-1])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will PUT it (a free feature)
        if args[0].endswith("/automated-security-fixes"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail and "security_and_analysis" in res.detail
    # the free features were STILL attempted despite the SA-block hard error (no early return)
    assert "vulnerability-alerts" in puts and "automated-security-fixes" in puts


def test_ghas_real_subresource_write_error_is_hard_error_not_silent_degrade(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 3) #2: a GENUINE (non-plan-limit) failure on a sub-resource PUT/DELETE
    must surface as status=error — NOT 'updated (degraded)', which automation checking
    `status != error` would read as a false green. SA block matches; the sub-resource WRITE 403s."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 403: Resource not accessible by integration"  # REAL error, not plan
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → apply will try to PUT it (and that 403s)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail and "FAILED" in res.detail


def test_ghas_plan_limit_classified_on_full_string_not_truncated_prefix(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #2: a verbose gh prefix could push the plan-limit phrase past the
    80-char display truncation. Classification must use the FULL string, so a real plan limit still
    DEGRADES (not a hard error) even when the key phrase sits far into a long message."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    long_prefix = "HTTP 422: Validation Failed. " + ("blah " * 25)  # >100 chars before the phrase
    plan_limit = long_prefix + "GitHub Advanced Security is not available for this repository"

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", plan_limit
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated" and "DEGRADED" in res.detail and "FAILED" not in res.detail


def test_ghas_actions_disabled_collision_is_hard_error_not_unlicensed_degrade(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #3: 'Actions must be enabled for default setup' is a FIXABLE config
    collision (turn Actions on), not a plan limit — it must surface as a hard error, not degrade."""
    live = {"security_and_analysis": {f2: {"status": n["status"]}
                                      for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/default-setup"):
                return 1, "", "HTTP 409: Actions must be enabled for default setup to be configured"
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "not-configured"}), ""  # drift → apply tries to configure
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail and "code-scanning" in res.detail


def test_ghas_transient_service_error_is_hard_error_not_swallowed_as_unlicensed(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 6) #4: a transient '503 Service not available' must NOT be mis-classified
    as a GHAS plan limit (which would degrade to a false green). Only feature/plan wording degrades."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 503: Service not available, try again later"  # transient, NOT a plan limit
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "error" and "FAILED" in res.detail  # not silently degraded


def test_ghas_plan_limited_subresource_write_still_degrades_not_errors(monkeypatch, gh_repo, tmp_path):
    """The flip side: a PLAN/FEATURE limit on a sub-resource write degrades loudly but stays
    `updated` (the free features that DID apply are not thrown away)."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path.endswith("/vulnerability-alerts"):
                return 1, "", "HTTP 422: Dependency graph is not available for this repository"
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated" and "DEGRADED" in res.detail and "FAILED" not in res.detail


def test_ghas_no_remote_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "skipped"


def test_ghas_drift_could_not_verify_on_repo_read_failure(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) test gap: `rig status` ghas leg must surface a VISIBLE 'could not
    verify' item when the repo read fails — never a silent in-sync."""
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    plan_obj = type("P", (), {"actions": [_ghas_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    ghas_items = [i for i in report.items if i.item == "ghas"]
    assert ghas_items and ghas_items[0].direction == "modified" and "could not verify" in ghas_items[0].detail


def test_merge_and_actions_drift_could_not_verify_on_read_failure(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 8) test gap: the merge + actions status legs must ALSO surface a loud
    'could not verify' on a failed read (the key guard against a false-green status under no admin)."""
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    for action, item in ((_merge_action(tmp_path), "merge"), (_actions_action(tmp_path), "actions")):
        plan_obj = type("P", (), {"actions": [action]})()
        report = detect(plan_obj)  # type: ignore[arg-type]
        items = [i for i in report.items if i.item == item]
        assert items and items[0].direction == "modified" and "could not verify" in items[0].detail


def test_ghas_subresource_disable_sends_delete(monkeypatch, gh_repo, tmp_path):
    """want=False for a sub-resource currently ON → DELETE (the off direction, not only enable)."""
    # SA + codeql already match defaults; only vuln-alerts is desired OFF while live is ON.
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    seen: list[tuple[str, str]] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            seen.append((args[args.index("--method") + 1], args[args.index("--method") + 2].split("/")[-1]))
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 0, "", ""  # 204 → enabled (but we want it OFF)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path, vulnerability_alerts=False), "backup")
    assert res.status == "updated"
    assert ("DELETE", "vulnerability-alerts") in seen  # the OFF direction was driven


def test_ghas_unreadable_subresource_not_double_degraded_nor_re_mutated(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 2) #1: a sub-resource the classifier couldn't read (read-only token,
    403) must be degraded EXACTLY ONCE and NOT get a doomed PUT/DELETE that 403s a second time."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    mutations: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            mutations.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 403: Resource not accessible"  # unreadable (not a 404 → None)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    # the unreadable endpoint appears in the degrade summary EXACTLY ONCE
    assert res.detail.count("vulnerability-alerts") == 1
    # and NO mutation was attempted against it (no doomed PUT/DELETE)
    assert not any("vulnerability-alerts" in m for m in mutations)


def test_ghas_sa_block_not_re_patched_when_only_subresource_drifts(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 2) #3: when the security_and_analysis block already matches and only a
    sub-resource drifts, apply must NOT PATCH /repos (a no-op + a misleading 'converged' note)."""
    sa = {f2: n["status"] for f2, n in build_security_analysis_body(GITHUB_GHAS_DEFAULTS).items()}
    live = {"security_and_analysis": {k: {"status": v} for k, v in sa.items()}}
    repo_patched: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            path = args[args.index("--method") + 2]
            if path == "repos/acme/widget":
                repo_patched.append(path)
            return 0, "{}", ""
        if args[0].endswith("/vulnerability-alerts"):
            return 1, "", "HTTP 404: Not Found"  # off → only THIS drifts (a free feature to enable)
        if args[0].endswith("/automated-security-fixes"):
            return 0, json.dumps({"enabled": True}), ""
        if args[0].endswith("/default-setup"):
            return 0, json.dumps({"state": "configured"}), ""
        return 0, json.dumps(live), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_ghas(_ghas_action(tmp_path), "backup")
    assert res.status == "updated"
    assert not repo_patched  # the SA block already matched — no redundant PATCH /repos
    assert "security_and_analysis converged" not in res.detail  # no misleading note


# ════════════════════════════════════════════════════════════════════════════════════
# github.actions — Actions permissions (least privilege)
# ════════════════════════════════════════════════════════════════════════════════════
def test_actions_token_is_read_only_by_default():
    wf = build_workflow_permissions_body(GITHUB_ACTIONS_DEFAULTS)
    assert wf == {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}


def test_actions_permissions_body_omits_allowed_when_disabled():
    body = build_permissions_body({**GITHUB_ACTIONS_DEFAULTS, "actions_enabled": False})
    assert body == {"enabled": False}  # the API rejects allowed_actions with enabled=false


def test_actions_state_update_when_token_is_write(monkeypatch, gh_repo, tmp_path):
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    state, _ = runner.github_actions_state(_actions_action(tmp_path))
    assert state == "update"


def test_actions_apply_puts_both_endpoints(monkeypatch, gh_repo, tmp_path):
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PUT":
            puts.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write"}), ""
        return 0, json.dumps({"enabled": False}), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "updated"
    assert any(p.endswith("/actions/permissions") for p in puts)
    assert any(p.endswith("/permissions/workflow") for p in puts)


def test_actions_already_in_sync_skips(monkeypatch, gh_repo, tmp_path):
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps(build_workflow_permissions_body(GITHUB_ACTIONS_DEFAULTS)), ""
        return 0, json.dumps(normalize_permissions(build_permissions_body(GITHUB_ACTIONS_DEFAULTS))), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "skipped"


def test_actions_disabled_skips_workflow_permissions_put(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 6) #2: `actions_enabled: false` must NOT issue the workflow-permissions
    PUT (GitHub rejects it when Actions is off) — else a legitimate 'disable Actions' config errors."""
    puts: list[str] = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            puts.append(args[args.index("--method") + 2])
            return 0, "{}", ""
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""  # live: enabled → drift

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path, actions_enabled=False), "backup")
    assert res.status == "updated" and "disabled Actions" in res.detail
    assert any(p.endswith("/actions/permissions") for p in puts)
    assert not any(p.endswith("/permissions/workflow") for p in puts)  # the workflow PUT was skipped


def test_actions_disabled_converges_and_is_idempotent(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 7) #1: with `actions_enabled: false`, the classifier must IGNORE the
    workflow-token endpoint (apply skips its PUT) — else it reads perpetual drift and apply reports
    `updated` forever. Live Actions already disabled → state `ok`, a re-apply is a true no-op."""
    wf_reads: list = []

    def fake(args, *, input_text=None):
        if "--method" in args:
            return 0, "{}", ""
        if args[0].endswith("/permissions/workflow"):
            wf_reads.append(args[0])
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": False}), ""  # Actions already disabled (matches desired)

    monkeypatch.setattr(runner, "_gh_api", fake)
    res = runner._do_provision_github_actions(_actions_action(tmp_path, actions_enabled=False), "backup")
    assert res.status == "skipped"  # converged — NOT a phantom `updated`
    assert not wf_reads  # the workflow endpoint was never even read (mirrors the apply skip)


def test_actions_drift_parity(monkeypatch, gh_repo, tmp_path):
    """apply and drift switch on the SAME github_actions_state — a write token reads as drift."""
    def fake(args, *, input_text=None):
        if args[0].endswith("/workflow"):
            return 0, json.dumps({"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}), ""
        return 0, json.dumps({"enabled": True, "allowed_actions": "all"}), ""

    monkeypatch.setattr(runner, "_gh_api", fake)
    plan_obj = type("P", (), {"actions": [_actions_action(tmp_path)]})()
    report = detect(plan_obj)  # type: ignore[arg-type]
    actions_items = [i for i in report.items if i.item == "actions"]
    assert actions_items and actions_items[0].direction == "modified"


def test_actions_no_remote_and_gh_error(monkeypatch, tmp_path):
    """no remote → skipped; a failed read → loud error (not a silent green)."""
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    assert runner._do_provision_github_actions(_actions_action(tmp_path), "backup").status == "skipped"
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: ("acme", "widget"))
    monkeypatch.setattr(runner, "ensure_gh_auth", lambda **kw: github_auth.AuthOutcome("ok"))
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: (1, "", "HTTP 403: forbidden"))
    res = runner._do_provision_github_actions(_actions_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


# ════════════════════════════════════════════════════════════════════════════════════
# github.browser — agent-browser backend for API-unreachable settings
# ════════════════════════════════════════════════════════════════════════════════════
def test_browser_command_plan_is_deterministic():
    desired = desired_toggles({"discussions": True, "projects": False})
    plan = build_command_plan("acme", "widget", desired)
    assert plan[0] == ["open", settings_url("acme", "widget")]
    # one find-role step per managed toggle; the agent-browser contract is
    # `find role <role> <action> --name <label>` (name is an OPTION, never a positional).
    assert ["find", "role", "switch", "check", "--name", "Discussions"] in plan
    assert ["find", "role", "switch", "uncheck", "--name", "Projects"] in plan


def test_browser_gated_off_by_default(monkeypatch, gh_repo, tmp_path):
    monkeypatch.delenv("RIG_GH_BROWSER", raising=False)
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    called: list = []
    monkeypatch.setattr(runner, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "skipped" and "RIG_GH_BROWSER" in res.detail
    assert not called  # no browser spawned


def test_browser_drives_ui_when_enabled(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    steps: list = []
    monkeypatch.setattr(runner, "_agent_browser", lambda args: steps.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "updated"
    assert steps and steps[0][0] == "open"


def test_browser_step_failure_degrades_loud(monkeypatch, gh_repo, tmp_path):
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)

    # `open` and the login probe (`get url`) succeed so the handler reaches the TOGGLE steps; a
    # toggle (`find role switch …`) then fails — the per-step degrade path this test is about. (A
    # blanket-fail stub would trip the earlier `open` failure instead, never exercising it.)
    def _toggle_fails(args):
        if args and args[0] in ("open", "get"):
            return 0, "https://github.com/acme/widget/settings", ""
        return 1, "", "could not find element"

    monkeypatch.setattr(runner, "_agent_browser", _toggle_fails)
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "failed" in res.detail


def test_browser_handler_errors_when_browser_auth_unavailable(monkeypatch, gh_repo, tmp_path):
    """Handler→gate integration (round-5 test gap): when ensure_browser_auth is NOT ok (agent-browser
    absent), the handler returns a loud error and never drives the UI."""
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    monkeypatch.setattr(runner, "ensure_browser_auth",
                        lambda **kw: github_auth.AuthOutcome("unavailable", detail="agent-browser not available"))
    called: list = []
    monkeypatch.setattr(runner, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "agent-browser" in res.detail and not called


def test_browser_login_probe_recognizes_sso_redirect(monkeypatch):
    """guard 2b: a SAML/SSO bounce (/orgs/<org>/sso) reads as not-logged-in (round-5 #2)."""
    monkeypatch.setattr(runner, "_agent_browser", lambda args: (0, "https://github.com/orgs/acme/sso?return_to=x", ""))
    assert runner._browser_on_login_page("acme", "widget") is True
    # a normal org page that is NOT an sso path stays logged-in
    monkeypatch.setattr(runner, "_agent_browser", lambda args: (0, "https://github.com/orgs/acme/people", ""))
    assert runner._browser_on_login_page("acme", "widget") is False


def test_browser_login_probe_uses_path_segment_not_substring(monkeypatch):
    """Review-finding (round 2) #2: the login check must key on the first PATH SEGMENT, so a repo or
    owner literally named `login` (URL `.../login/...`) does NOT false-positive as 'not logged in'."""
    # logged out → GitHub redirected to the /login page (first segment is `login`)
    monkeypatch.setattr(runner, "_agent_browser", lambda args: (0, "https://github.com/login?return_to=x", ""))
    assert runner._browser_on_login_page("acme", "widget") is True
    # a repo named `login` on a settings page — `login` appears in the path but NOT as the first
    # segment, so this is a logged-IN settings page and must NOT be flagged.
    monkeypatch.setattr(runner, "_agent_browser", lambda args: (0, "https://github.com/acme/login/settings", ""))
    assert runner._browser_on_login_page("acme", "login") is False
    # an owner named `login`
    monkeypatch.setattr(runner, "_agent_browser", lambda args: (0, "https://github.com/login/widget/settings", ""))
    assert runner._browser_on_login_page("login", "widget") is False


def test_browser_logged_out_session_degrades_loud(monkeypatch, gh_repo, tmp_path):
    """A present-but-logged-OUT browser session (open succeeds, but URL bounced to /login) degrades
    loudly with an actionable message — never a blind click on the login page."""
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    toggles: list = []

    def _logged_out(args):
        if args[:2] == ["get", "url"]:
            return 0, "https://github.com/login", ""  # session not logged in
        if args and args[0] == "open":
            return 0, "", ""
        toggles.append(args)
        return 0, "", ""

    monkeypatch.setattr(runner, "_agent_browser", _logged_out)
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "error" and "not logged into github.com" in res.detail
    assert not toggles  # no toggle was clicked on the login page


def test_browser_dry_run_enabled_previews_steps_runs_nothing(monkeypatch, gh_repo, tmp_path):
    # backend ENABLED → dry-run previews the would-drive steps but spawns nothing.
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    monkeypatch.setenv("RIG_GH_BROWSER", "1")
    called: list = []
    monkeypatch.setattr(runner, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "updated" and "RIG_GH_DRY_RUN" in res.detail and "would drive" in res.detail and not called


def test_browser_dry_run_disabled_previews_skip_not_drive(monkeypatch, gh_repo, tmp_path):
    """Review-finding (round 3) #4: with the backend OFF (no RIG_GH_BROWSER), a real apply is
    skipped — so the dry-run preview must say 'would be SKIPPED', not 'would drive N steps' (a
    preview an apply in the same env would never honor)."""
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    monkeypatch.delenv("RIG_GH_BROWSER", raising=False)
    called: list = []
    monkeypatch.setattr(runner, "_agent_browser", lambda args: called.append(args) or (0, "", ""))
    res = runner._do_provision_github_browser(_browser_action(tmp_path), "backup")
    assert res.status == "skipped" and "would be SKIPPED" in res.detail and not called


# ════════════════════════════════════════════════════════════════════════════════════
# the #4136.1 auth gate — ASK and WAIT, never silently fail
# ════════════════════════════════════════════════════════════════════════════════════
def test_auth_ok_immediately_when_authed(monkeypatch):
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: True)
    notified: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.ok and not notified  # no notify when already authed


def test_auth_missing_degrades_loud_without_phone_spam_at_zero_budget(monkeypatch):
    # the autonomous default (0 budget) must NOT hang AND must NOT ping the phone: there's no wait to
    # notify about, and an apply runs the gate per-action (~5×) — "loud" is the visible error result,
    # not up to 5 tg pushes. (Review round-5 #1.) So: no notify, no sleep, a loud actionable detail.
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "0")
    notified: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    slept: list = []
    monkeypatch.setattr(github_auth, "_sleep", lambda s: slept.append(s))
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and not notified and not slept
    assert "gh auth login" in out.detail  # actionable: the exact command, in the RESULT (the loud signal)


def test_auth_waits_then_resumes_when_login_appears(monkeypatch):
    # with a budget, the gate polls and returns ok the moment auth appears (the "WAIT then resume").
    states = iter([False, False, True])
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: next(states))
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(github_auth, "_notify", lambda msg: True)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "100")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    # a clock that advances 1s per call but never hits the 100s deadline before auth appears.
    t = iter(range(0, 1000))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.ok and out.notified


def test_auth_unavailable_when_gh_missing(monkeypatch):
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: None)
    out = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert out.state == "unavailable" and "gh CLI not found" in out.detail


def test_auth_gate_dedups_notify_and_wait_across_actions_in_one_run(monkeypatch):
    """Review-finding (round 8) #2: within ONE apply, the gate runs per github.* action. If login
    never appears, only the FIRST action should notify + wait the budget; siblings short-circuit to
    an immediate timed_out (no extra tg pushes, no extra budget×N blocking)."""
    github_auth.reset_auth_gate()
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")
    notifies: list = []
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notifies.append(msg) or True)
    sleeps: list = []
    monkeypatch.setattr(github_auth, "_sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "3")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 1000))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))

    first = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    sleeps_after_first = len(sleeps)
    second = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    third = github_auth.ensure_gh_auth(owner="acme", repo="widget")
    assert first.state == second.state == third.state == "timed_out"
    assert len(notifies) == 1  # only the FIRST action pinged the user
    assert len(sleeps) == sleeps_after_first  # the 2nd/3rd actions did NOT re-wait (no extra sleeps)


def test_browser_auth_ok_when_present(monkeypatch):
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: True)
    assert github_auth.ensure_browser_auth(owner="acme", repo="widget").ok


def test_browser_auth_notifies_and_times_out_when_missing(monkeypatch):
    """ensure_browser_auth mirrors the gh gate: absent + a WAIT budget → notify once, then degrade
    loud when it doesn't appear. (Notify is reserved for the positive-budget wait path — at 0 budget
    it degrades silently to the result, per round-5 #1.)"""
    notified: list = []
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "2")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 100))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    out = github_auth.ensure_browser_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and notified and "agent-browser" in out.detail


def test_browser_auth_no_phone_spam_at_zero_budget(monkeypatch):
    """At the 0 (unattended) budget the browser gate degrades loud WITHOUT a tg ping — same anti-spam
    rule as the gh gate (round-5 #1)."""
    notified: list = []
    monkeypatch.setattr(github_auth, "_browser_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "0")
    out = github_auth.ensure_browser_auth(owner="acme", repo="widget")
    assert out.state == "timed_out" and not notified


def test_gate_blocks_before_any_api_call_when_not_authed(monkeypatch, gh_repo, tmp_path):
    """#4136.1 + review-finding #1: the gate runs BEFORE the first read, not just before the PATCH.

    The realistic no-auth case is "the very first `gh api` read fails because there's no token". If
    the gate only ran before the mutation, that read would short-circuit to a plain `gh_error` and
    the notify-and-wait gate would be dead code. So the gate must fire FIRST — proven here by
    asserting `_gh_api` is NEVER called when the gate is not-ok (the read never even happens).
    """
    monkeypatch.setattr(runner, "ensure_gh_auth",
                        lambda **kw: github_auth.AuthOutcome("timed_out", detail="run `gh auth login`"))
    calls: list = []
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: calls.append(args) or (0, "{}", ""))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "gh auth login" in res.detail
    assert not calls  # neither the read NOR the mutation happened — the gate fired first


def test_unauthed_read_failure_triggers_the_gate_not_a_bare_error(monkeypatch, gh_repo, tmp_path):
    """End-to-end of finding #1: with a REAL not-authed probe, an apply hits the notify-and-wait
    gate (which returns its actionable detail), instead of the first read failing into a bare
    `gh_error`. We stub the auth PROBE (not the runner gate) to logged-out + a recorded notify."""
    notified: list = []
    # Restore the REAL gate (the autouse `_authed` fixture stubbed runner.ensure_gh_auth to ok) so
    # this test exercises the genuine probe→notify→wait path against a logged-out probe.
    monkeypatch.setattr(runner, "ensure_gh_auth", github_auth.ensure_gh_auth)
    monkeypatch.setattr(github_auth, "_gh_auth_ok", lambda: False)
    monkeypatch.setattr(github_auth.shutil, "which", lambda name: "/usr/bin/gh")  # gh present, just logged out
    monkeypatch.setattr(github_auth, "_notify", lambda msg: notified.append(msg) or True)
    monkeypatch.setattr(github_auth, "_sleep", lambda s: None)
    # a positive (bounded) budget so the "ask and WAIT" path runs — notify fires, then it times out.
    monkeypatch.setenv("RIG_GH_AUTH_WAIT", "2")
    monkeypatch.setenv("RIG_GH_AUTH_POLL", "1")
    t = iter(range(0, 100))
    monkeypatch.setattr(github_auth, "_now", lambda: float(next(t)))
    api_calls: list = []
    monkeypatch.setattr(runner, "_gh_api", lambda args, *, input_text=None: api_calls.append(args) or (0, "{}", ""))
    res = runner._do_provision_github_merge(_merge_action(tmp_path), "backup")
    assert res.status == "error" and "gh auth login" in res.detail
    assert notified, "the user must have been pinged (tg) about the missing login — the #4136.1 contract"
    assert not api_calls  # the gate intercepted before any gh api read/mutation


# ════════════════════════════════════════════════════════════════════════════════════
# plan wiring + config validation (the schema is the single source of truth)
# ════════════════════════════════════════════════════════════════════════════════════
def test_plan_emits_all_github_actions_default_on(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({}, tmp_path), cat, project_type="cli")
    kinds = {a.kind for a in plan.actions}
    assert {
        "provision_github_ruleset",
        "provision_github_merge",
        "provision_github_ghas",
        "provision_github_actions",
        "provision_github_browser",
    } <= kinds


def test_plan_opt_out_per_subblock(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"github": {"ghas": {"enabled": False}, "actions": {"enabled": False}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    kinds = {a.kind for a in plan.actions}
    assert "provision_github_ghas" not in kinds and "provision_github_actions" not in kinds
    assert "provision_github_merge" in kinds  # the others stay on


# ════════════════════════════════════════════════════════════════════════════════════
# ruleset required status checks — ROADMAP §5: PR Checklist + review-threads as REQUIRED checks,
# derived from the merge-gating CI gates the repo actually provisions (lockout-safe).
# ════════════════════════════════════════════════════════════════════════════════════
def _ruleset_action(plan):
    return next(a for a in plan.actions if a.kind == "provision_github_ruleset")


# CI items are default-OFF in the catalog, so the auto-default only kicks in for gates the config
# opts into — exactly mirroring the scaffold (riglib/state.py), which enables both merge gates.
_BOTH_GATES = {"ci": {"items": {"pr-checklist": {"enabled": True}, "review-threads": {"enabled": True}}}}


def test_ruleset_requires_enabled_ci_gates_as_status_checks(fake_agent_tools, tmp_path):
    """Both merge-gating gates present+enabled → both contexts become required status checks."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(_BOTH_GATES, tmp_path), cat, project_type="cli")
    checks = _ruleset_action(plan).options["required_status_checks"]
    # The check-run CONTEXT names (job `name:`), not the slot names — matching what GitHub reports.
    assert checks == ["PR Checklist", "review-threads"]


def test_ruleset_omits_status_check_for_a_disabled_gate(fake_agent_tools, tmp_path):
    """Lockout guard: a DISABLED merge-gating gate must NOT be required (its check never runs)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"items": {"pr-checklist": {"enabled": True}, "review-threads": {"enabled": False}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    checks = _ruleset_action(plan).options["required_status_checks"]
    assert checks == ["PR Checklist"]  # review-threads dropped — requiring it would wedge every PR


def test_ruleset_no_required_checks_when_no_gate_enabled(fake_agent_tools, tmp_path):
    """No merge-gating gate enabled → no required checks (else every PR locks out)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({}, tmp_path), cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_when_ci_disabled(fake_agent_tools, tmp_path):
    """CI off entirely → no workflows land → no required checks (else every PR locks out)."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"enabled": False, "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_when_ci_export_only(fake_agent_tools, tmp_path):
    """export-only writes no workflow into the repo, so its checks never run → require none."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"target": "export-only", "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ruleset_no_required_checks_for_custom_non_standard_ci_target(fake_agent_tools, tmp_path):
    """Review-finding (round 4) #1: GitHub only runs workflows from `.github/workflows`. A CUSTOM
    target dir means the check-run never appears, so requiring its context would wedge every PR. The
    auto-default must require nothing for any target other than `.github/workflows`."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"target": ".ci/workflows", "items": {"review-threads": {"enabled": True}}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_ci_gate_check_contexts_match_real_workflow_job_names():
    """Review-finding (round 3) #1 — the LOCKOUT guard's load-bearing fact: the context a ruleset
    requires must equal the check-run name GitHub reports, which for an Actions check is the JOB's
    `name:` (when set), NOT the workflow's top-level `name:`. A wrong context wedges every PR. Assert
    each `CI_GATE_CHECK_CONTEXTS` value equals the real agent-tools workflow's JOB name. Skips when
    the agent-tools checkout isn't beside this repo (so a thin checkout doesn't fail the suite)."""
    import re as _re
    from pathlib import Path as _Path

    from riglib.github_ruleset import CI_GATE_CHECK_CONTEXTS

    at = _Path.home() / "xp" / "agent-tools"
    if not (at / "ci").is_dir():
        pytest.skip("agent-tools checkout not present — context-vs-real-workflow guard skipped")
    for slot, context in CI_GATE_CHECK_CONTEXTS.items():
        wf = at / "ci" / slot / "workflow.yml"
        assert wf.is_file(), f"merge-gating CI slot {slot!r} has no workflow.yml in agent-tools"
        text = wf.read_text()
        # the FIRST `jobs:` entry's `name:` is the check-run context (gh reports a single-job
        # workflow's check by that job name); fall back to the job id when no job `name:`.
        job_block = text.split("jobs:", 1)[1] if "jobs:" in text else ""
        names = _re.findall(r"^\s{4}name:\s*(.+?)\s*$", job_block, _re.MULTILINE)
        job_ids = _re.findall(r"^\s{2}([A-Za-z0-9_-]+):\s*$", job_block, _re.MULTILINE)
        candidates = set(names) | set(job_ids)
        assert context in candidates, (
            f"CI_GATE_CHECK_CONTEXTS[{slot!r}]={context!r} matches no job name/id in {wf} "
            f"(found {sorted(candidates)}) — a stale context would LOCK OUT every PR"
        )


def test_required_checks_match_written_workflow_job_names_in_one_plan(fake_agent_tools, tmp_path):
    """Review-finding (round 4) #2 — the lockout invariant, tested HERMETICALLY in a single build:
    every required_status_checks context the plan emits must equal a JOB NAME in the workflow that
    the SAME plan writes (an `install_ci` action for that slot). This couples "required" to
    "written" without needing the real agent-tools checkout — a context/job-name mismatch (the exact
    lockout footgun) fails here, not silently on a live repo."""
    import re as _re

    from riglib.catalog import Catalog
    from riglib.github_ruleset import CI_GATE_CHECK_CONTEXTS

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg(_BOTH_GATES, tmp_path), cat, project_type="cli")
    required = _ruleset_action(plan).options["required_status_checks"]
    assert required, "expected the auto-default to require the merge gates"
    context_to_slot = {ctx: slot for slot, ctx in CI_GATE_CHECK_CONTEXTS.items()}
    written_ci = {a.item: a for a in plan.actions if a.kind == "install_ci"}
    for context in required:
        slot = context_to_slot[context]
        assert slot in written_ci, f"required context {context!r} but its workflow (slot {slot!r}) is not written"
        text = (written_ci[slot].source / "workflow.yml").read_text()
        job_block = text.split("jobs:", 1)[1] if "jobs:" in text else ""
        names = _re.findall(r"^\s{4}name:\s*(.+?)\s*$", job_block, _re.MULTILINE)
        ids = _re.findall(r"^\s{2}([A-Za-z0-9_-]+):\s*$", job_block, _re.MULTILINE)
        assert context in (set(names) | set(ids)), (
            f"required context {context!r} is not a job name/id in the workflow the plan writes "
            f"for slot {slot!r} (found {sorted(set(names) | set(ids))}) — this would LOCK OUT every PR"
        )


def test_ruleset_required_checks_honor_ci_enable_delta_list(fake_agent_tools, tmp_path):
    """The `enable:` delta list also flips a gate on — its context must be required too."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"ci": {"enable": ["pr-checklist", "review-threads"]}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == ["PR Checklist", "review-threads"]


def test_ruleset_explicit_required_checks_win_verbatim(fake_agent_tools, tmp_path):
    """An explicit config list is honored as-is — the CI-derived auto-default never overrides it."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {**_BOTH_GATES, "github": {"ruleset": {"required_status_checks": ["my-custom-check"]}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == ["my-custom-check"]


def test_ruleset_explicit_empty_required_checks_means_require_none(fake_agent_tools, tmp_path):
    """An explicit `[]` is a deliberate 'require no checks' and survives the auto-default."""
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {**_BOTH_GATES, "github": {"ruleset": {"required_status_checks": []}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="cli")
    assert _ruleset_action(plan).options["required_status_checks"] == []


def test_validate_rejects_unknown_github_subblock_key(tmp_path):
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merg": True}}})  # typo
    assert "github.merge" in str(e.value)


def test_validate_rejects_non_bool_ghas_knob():
    with pytest.raises(ConfigError):
        validate({"github": {"ghas": {"secret_scanning": "yes"}}})


def test_validate_rejects_bad_actions_enum():
    with pytest.raises(ConfigError) as e:
        validate({"github": {"actions": {"allowed_actions": "everything"}}})
    assert "allowed_actions" in str(e.value)


def test_validate_rejects_merge_with_no_model_enabled():
    """All three merge models off → GitHub 422; catch it at config time, not on the live PATCH."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merge": False, "merge_commit": False, "rebase_merge": False}}})
    assert "merge model" in str(e.value) and "github.merge" in str(e.value)


def test_validate_accepts_merge_with_only_rebase_enabled():
    """A non-default single model is fine — only the all-off case is rejected."""
    validate({"github": {"merge": {"squash_merge": False, "merge_commit": False, "rebase_merge": True}}})


def test_validate_rejects_all_off_reachable_via_single_squash_false():
    """All-off is reachable by setting squash_merge:false ALONE (merge_commit/rebase default off) —
    the validator resolves the omitted knobs to their defaults, so this must be rejected too."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": {"squash_merge": False}}})
    assert "merge model" in str(e.value)


def test_validate_allows_all_off_merge_when_block_is_disabled():
    """Review-finding (round 4) #3: an all-off merge block that is `enabled: false` is harmless — the
    plan skips it entirely — so validation must NOT reject it (the model check is for live blocks)."""
    validate({"github": {"merge": {"enabled": False, "squash_merge": False, "merge_commit": False, "rebase_merge": False}}})


def test_validate_rejects_non_mapping_github_subblock():
    """A scalar where a sub-block mapping is expected fails closed with the schema path."""
    with pytest.raises(ConfigError) as e:
        validate({"github": {"merge": "squash"}})
    assert "github.merge must be a mapping" in str(e.value)


def test_validate_merge_null_knob_resolves_to_default_not_all_off():
    """Review-finding (round 6) #3: an explicit `null` resolves to the DEFAULT (matching the plan,
    which drops null overrides), so `squash_merge: null` is squash-ON — NOT an all-off rejection."""
    validate({"github": {"merge": {"squash_merge": None, "merge_commit": False, "rebase_merge": False}}})


def test_validate_accepts_valid_full_github_block():
    # the scaffolded shape (all sub-blocks present, secure defaults) round-trips clean.
    from riglib.state import default_state

    validate(default_state())  # must not raise
