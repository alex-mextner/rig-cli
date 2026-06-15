"""GitHub repository ruleset provisioning — the ``github.ruleset`` block.

Covers the desired-body assembly (and the footgun guard: the ``update`` rule is structurally
unreachable), the create / update / skip apply outcomes, drift parity (apply and drift switch
on the SAME :func:`github_ruleset_state`), the no-github-remote no-op, and fail-closed config
validation. Every test is deterministic: the ``gh`` subprocess and the git-remote resolution
are fully monkeypatched, so nothing here hits the network or a real repo.
"""

from __future__ import annotations

import json

import pytest

from riglib.actions import runner
from riglib.actions.runner import github_owner_repo, github_ruleset_state
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.github_ruleset import (
    GITHUB_RULESET_DEFAULTS,
    build_ruleset_body,
    build_ruleset_rules,
    find_managed_ruleset,
    parse_github_remote,
)
from riglib.plan import Action, InstallPlan, build

# ── helpers ────────────────────────────────────────────────────────────────────────
# Use the SAME canonical defaults the action/plan use, so a default drift breaks the tests too.
_DEFAULT_OPTS = dict(GITHUB_RULESET_DEFAULTS)


def _action(repo, **overrides) -> Action:
    opts = {**_DEFAULT_OPTS, **overrides}
    return Action(
        kind="provision_github_ruleset",
        category="github",
        item="ruleset",
        source=repo,
        target=repo,
        options=opts,
    )


def _rule_types(rules: list[dict]) -> set[str]:
    return {r["type"] for r in rules}


class _GhRecorder:
    """A monkeypatch double for ``runner._gh_api`` that records calls and replays canned GETs.

    ``rulesets`` is the list returned for ``repos/.../rulesets``; ``ruleset_by_id`` maps an id →
    the full ruleset GET response. POST/PUT calls are recorded (``self.posts`` / ``self.puts``)
    and return a success rc — so a test asserts WHAT would be sent without a real API.
    """

    def __init__(self, rulesets=None, ruleset_by_id=None):
        self.rulesets = rulesets if rulesets is not None else []
        self.ruleset_by_id = ruleset_by_id or {}
        self.posts: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, args, *, input_text=None):
        self.calls.append(args)
        if "--method" in args:
            method = args[args.index("--method") + 1]
            path = args[args.index("--method") + 2]
            if method == "POST":
                self.posts.append(input_text or "")
                return 0, json.dumps({"id": 999, "name": "rig-managed"}), ""
            if method == "PUT":
                self.puts.append((path, input_text or ""))
                return 0, json.dumps({"id": path.rsplit("/", 1)[-1]}), ""
            raise AssertionError(f"unexpected gh method {method}")
        # GET requests — strip any ?query (the list call passes ?includes_parents=false).
        path = args[0].split("?", 1)[0]
        if path.endswith("/rulesets"):
            return 0, json.dumps(self.rulesets), ""
        rs_id = path.rsplit("/", 1)[-1]
        body = self.ruleset_by_id.get(rs_id) or self.ruleset_by_id.get(int(rs_id) if rs_id.isdigit() else rs_id)
        if body is None:
            return 1, "", "not found"
        return 0, json.dumps(body), ""


@pytest.fixture
def gh_repo(monkeypatch):
    """Pretend the repo has a github origin remote (no real git needed)."""
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: ("acme", "widget"))


def _install_gh(monkeypatch, recorder: _GhRecorder) -> None:
    monkeypatch.setattr(runner, "_gh_api", recorder)


# ── desired body assembly + the footgun guard ──────────────────────────────────────
def test_default_body_has_pr_force_push_deletion_and_admin_bypass():
    body = build_ruleset_body(_DEFAULT_OPTS)
    types = _rule_types(body["rules"])
    assert "pull_request" in types
    assert "non_fast_forward" in types  # block_force_push
    assert "deletion" in types  # restrict_deletion
    # admin bypass actor present (repo Admin role id 5, RepositoryRole, always)
    assert body["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]
    # targets the default branch via the ~DEFAULT_BRANCH token, active enforcement
    assert body["enforcement"] == "active"
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_update_rule_is_never_emitted_for_any_config():
    # The footgun: an `update` ("Restrict updates") rule + zero bypass actors locks out every
    # merge. It must be structurally impossible — for EVERY combination of knobs.
    import itertools

    bools = [True, False]
    for pr, fp, dele, lin, sig, adm in itertools.product(bools, repeat=6):
        opts = {
            **_DEFAULT_OPTS,
            "require_pull_request": pr,
            "block_force_push": fp,
            "restrict_deletion": dele,
            "require_linear_history": lin,
            "require_signatures": sig,
            "admin_bypass": adm,
            "required_status_checks": ["ci"] if pr else [],
        }
        types = _rule_types(build_ruleset_rules(opts))
        assert "update" not in types
        assert "required_deployments" not in types


def test_admin_bypass_false_omits_bypass_actors():
    body = build_ruleset_body({**_DEFAULT_OPTS, "admin_bypass": False})
    assert body["bypass_actors"] == []


def test_required_status_checks_populates_rule_only_when_non_empty():
    empty = build_ruleset_rules({**_DEFAULT_OPTS, "required_status_checks": []})
    assert "required_status_checks" not in _rule_types(empty)

    populated = build_ruleset_rules(
        {**_DEFAULT_OPTS, "required_status_checks": ["build", "test"]}
    )
    rule = next(r for r in populated if r["type"] == "required_status_checks")
    contexts = [c["context"] for c in rule["parameters"]["required_status_checks"]]
    assert contexts == ["build", "test"]
    assert rule["parameters"]["strict_required_status_checks_policy"] is False


def test_optional_rules_toggle_on():
    rules = build_ruleset_rules(
        {**_DEFAULT_OPTS, "require_linear_history": True, "require_signatures": True}
    )
    assert "required_linear_history" in _rule_types(rules)
    assert "required_signatures" in _rule_types(rules)


def test_required_reviews_flows_into_pull_request_rule():
    rules = build_ruleset_rules({**_DEFAULT_OPTS, "required_reviews": 2})
    pr = next(r for r in rules if r["type"] == "pull_request")
    assert pr["parameters"]["required_approving_review_count"] == 2


# ── remote URL parsing (owner/repo resolution) ──────────────────────────────────────
_REMOTE_CASES = [
    ("git@github.com:acme/widget.git", ("acme", "widget")),
    ("git@github.com:acme/widget", ("acme", "widget")),
    ("https://github.com/acme/widget.git", ("acme", "widget")),
    ("https://github.com/acme/widget", ("acme", "widget")),
    ("https://github.com/acme/widget/", ("acme", "widget")),
    ("ssh://git@github.com/acme/widget.git", ("acme", "widget")),
    ("ssh://git@github.com:22/acme/widget.git", ("acme", "widget")),  # SSH port handled
    ("git@gitlab.com:acme/widget.git", None),  # non-github → None
    ("https://evil.com/github.com/acme/widget.git", None),  # embedded host → not github
    ("https://notgithub.com/acme/widget", None),
    ("git@github.com:acme/sub/widget.git", None),  # extra path segment rejected
    ("", None),
]


@pytest.mark.parametrize("url,expected", _REMOTE_CASES)
def test_parse_github_remote(url, expected):
    # direct unit test of the pure parser (a regex regression isn't masked by subprocess mocks).
    assert parse_github_remote(url) == expected


@pytest.mark.parametrize("url,expected", _REMOTE_CASES)
def test_github_owner_repo_parses_remote(monkeypatch, tmp_path, url, expected):
    import subprocess as _sp
    from types import SimpleNamespace

    def _fake_run(args, **kwargs):
        rc = 0 if url else 1
        return SimpleNamespace(returncode=rc, stdout=url, stderr="")

    monkeypatch.setattr(_sp, "run", _fake_run)
    assert github_owner_repo(tmp_path) == expected


def test_github_owner_repo_none_when_git_fails(monkeypatch, tmp_path):
    import subprocess as _sp

    def _boom(args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(_sp, "run", _boom)
    assert github_owner_repo(tmp_path) is None


# ── seams: dry-run parsing, gh-missing, find_managed_ruleset defensiveness ───────────
@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("True", True), (" yes ", True),
     ("0", False), ("no", False), ("", False)],
)
def test_gh_dry_run_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("RIG_GH_DRY_RUN", value)
    assert runner._gh_dry_run() is expected


def test_gh_dry_run_unset_is_false(monkeypatch):
    monkeypatch.delenv("RIG_GH_DRY_RUN", raising=False)
    assert runner._gh_dry_run() is False


def test_gh_api_missing_binary(monkeypatch):
    # when `gh` isn't on PATH, the seam returns 127 + a clear message (never raises).
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    rc, out, err = runner._gh_api(["repos/acme/widget/rulesets"])
    assert rc == 127 and "gh CLI not found" in err


def test_find_managed_ruleset_defensive():
    assert find_managed_ruleset("not-a-list", "rig-managed") is None  # type: ignore[arg-type]
    assert find_managed_ruleset([], "rig-managed") is None
    # a ruleset with no `target` field defaults to "branch" (older API shapes) → still matches.
    rs = {"id": 1, "name": "rig-managed"}
    assert find_managed_ruleset([rs], "rig-managed") is rs
    # a non-branch (tag) ruleset with the managed name is NOT matched.
    assert find_managed_ruleset([{"name": "rig-managed", "target": "tag"}], "rig-managed") is None


# ── apply: create / update / skip ───────────────────────────────────────────────────
def test_create_when_absent(monkeypatch, tmp_path, gh_repo):
    rec = _GhRecorder(rulesets=[])  # no managed ruleset on the repo
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "created"
    assert len(rec.posts) == 1 and rec.puts == []
    sent = json.loads(rec.posts[0])
    assert sent["name"] == "rig-managed"
    assert "update" not in _rule_types(sent["rules"])


def test_update_when_drifted(monkeypatch, tmp_path, gh_repo):
    # a managed ruleset exists but with a stale rule set → apply PUTs the desired body.
    existing = {"id": 42, "name": "rig-managed", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "deletion"}]}
    rec = _GhRecorder(rulesets=[{"id": 42, "name": "rig-managed"}], ruleset_by_id={42: existing})
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "updated"
    assert rec.posts == [] and len(rec.puts) == 1
    path, body = rec.puts[0]
    assert path == "repos/acme/widget/rulesets/42"
    assert "pull_request" in _rule_types(json.loads(body)["rules"])


def test_skip_when_matching(monkeypatch, tmp_path, gh_repo):
    # the live ruleset already equals the desired body → no POST/PUT, skipped.
    desired = build_ruleset_body(_DEFAULT_OPTS)
    existing = {"id": 7, "name": "rig-managed", **desired}
    rec = _GhRecorder(rulesets=[{"id": 7, "name": "rig-managed"}], ruleset_by_id={7: existing})
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "skipped"
    assert rec.posts == [] and rec.puts == []


def test_inherited_org_ruleset_with_same_name_is_ignored(monkeypatch, tmp_path, gh_repo):
    # an org/enterprise ruleset named 'rig-managed' (source_type Organization, or a tag-target
    # ruleset) must NOT be treated as the repo branch ruleset — else apply would PUT the wrong
    # id or report a false ok. With no REPO branch ruleset present, the state is `create`.
    rec = _GhRecorder(
        rulesets=[
            {"id": 1, "name": "rig-managed", "target": "branch", "source_type": "Organization"},
            {"id": 2, "name": "rig-managed", "target": "tag", "source_type": "Repository"},
        ]
    )
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "created"  # POSTed a fresh repo branch ruleset
    assert len(rec.posts) == 1 and rec.puts == []


def test_list_call_excludes_inherited_parents_and_paginates(monkeypatch, tmp_path, gh_repo):
    rec = _GhRecorder(rulesets=[])
    _install_gh(monkeypatch, rec)
    runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    list_call = next(c for c in rec.calls if c[0].split("?")[0].endswith("/rulesets"))
    assert "includes_parents=false" in list_call[0]
    assert "per_page=100" in list_call[0]
    assert "--paginate" in list_call  # slurp every page → no duplicate-create on >30 rulesets


def test_managed_ruleset_on_later_page_is_found_not_duplicated(monkeypatch, tmp_path, gh_repo):
    # simulate gh --paginate having concatenated pages: the managed ruleset sits among 30+ others.
    others = [{"id": i, "name": f"other-{i}", "target": "branch", "source_type": "Repository"}
              for i in range(40)]
    managed_stub = {"id": 777, "name": "rig-managed", "target": "branch", "source_type": "Repository"}
    desired = build_ruleset_body(_DEFAULT_OPTS)
    rec = _GhRecorder(rulesets=others + [managed_stub],
                      ruleset_by_id={777: {"id": 777, "name": "rig-managed", **desired}})
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "skipped"  # found + matches → NO duplicate POST
    assert rec.posts == []


def test_pull_request_param_drift_is_detected(monkeypatch, tmp_path, gh_repo):
    # a live ruleset whose pull_request rule flips a managed boolean (require_code_owner_review)
    # must read as drift — not a false ok. Guards the full-parameter normalization.
    desired = build_ruleset_body(_DEFAULT_OPTS)
    drifted = json.loads(json.dumps(desired))  # deep copy
    for rule in drifted["rules"]:
        if rule["type"] == "pull_request":
            rule["parameters"]["require_code_owner_review"] = True
    existing = {"id": 3, "name": "rig-managed", **drifted}
    rec = _GhRecorder(rulesets=[{"id": 3, "name": "rig-managed", "target": "branch", "source_type": "Repository"}],
                      ruleset_by_id={3: existing})
    _install_gh(monkeypatch, rec)
    state, _ = github_ruleset_state(_action(tmp_path))
    assert state == "update"


def test_conditions_order_is_not_drift(monkeypatch, tmp_path, gh_repo):
    # GitHub may return ref_name include/exclude in any order/key-order — a semantic match must
    # read as ok, not churn.
    desired = build_ruleset_body(_DEFAULT_OPTS)
    existing = {"id": 4, "name": "rig-managed", **json.loads(json.dumps(desired))}
    existing["conditions"] = {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}}
    rec = _GhRecorder(rulesets=[{"id": 4, "name": "rig-managed", "target": "branch", "source_type": "Repository"}],
                      ruleset_by_id={4: existing})
    _install_gh(monkeypatch, rec)
    state, _ = github_ruleset_state(_action(tmp_path))
    assert state == "ok"


def test_dry_run_update_path_makes_no_put(monkeypatch, tmp_path, gh_repo):
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    existing = {"id": 6, "name": "rig-managed", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "deletion"}]}
    rec = _GhRecorder(rulesets=[{"id": 6, "name": "rig-managed", "target": "branch", "source_type": "Repository"}],
                      ruleset_by_id={6: existing})
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "updated"  # reports the would-be update
    assert rec.puts == [] and rec.posts == []  # but no mutation


def test_no_github_remote_is_skipped_not_error(monkeypatch, tmp_path):
    def _explode(*a, **k):
        raise AssertionError("gh api must not be called when there is no github remote")

    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    monkeypatch.setattr(runner, "_gh_api", _explode)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "skipped" and "no github" in res.detail.lower()


def test_dry_run_seam_makes_no_mutation(monkeypatch, tmp_path, gh_repo):
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    rec = _GhRecorder(rulesets=[])
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "created"  # reports what WOULD happen
    assert rec.posts == [] and rec.puts == []  # but never mutated


def test_gh_error_surfaces_as_error(monkeypatch, tmp_path, gh_repo):
    def _boom(args, *, input_text=None):
        return 1, "", "HTTP 401: Bad credentials"

    _install_gh(monkeypatch, _boom)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "error" and "401" in res.detail


def test_create_post_failure_is_error(monkeypatch, tmp_path, gh_repo):
    # list succeeds (absent) but the POST itself fails → error result, not a false "created".
    def _list_ok_post_fails(args, *, input_text=None):
        if "--method" in args:
            return 1, "", "HTTP 422: validation failed"
        return 0, json.dumps([]), ""

    _install_gh(monkeypatch, _list_ok_post_fails)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "error" and "create failed" in res.detail and "422" in res.detail


def test_update_put_failure_is_error(monkeypatch, tmp_path, gh_repo):
    # list+get succeed (drifted) but the PUT fails → error result, not a false "updated".
    existing = {"id": 12, "name": "rig-managed", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "deletion"}]}

    def _put_fails(args, *, input_text=None):
        if "--method" in args and args[args.index("--method") + 1] == "PUT":
            return 1, "", "HTTP 500: server error"
        path = args[0].split("?", 1)[0]
        if path.endswith("/rulesets"):
            return 0, json.dumps([{"id": 12, "name": "rig-managed", "target": "branch", "source_type": "Repository"}]), ""
        return 0, json.dumps(existing), ""

    _install_gh(monkeypatch, _put_fails)
    res = runner._do_provision_github_ruleset(_action(tmp_path), "backup")
    assert res.status == "error" and "update failed" in res.detail and "500" in res.detail


def test_second_get_failure_is_gh_error(monkeypatch, tmp_path, gh_repo):
    # the list succeeds but the follow-up GET-by-id fails → gh_error (not a crash, not a false ok).
    rec = _GhRecorder(
        rulesets=[{"id": 99, "name": "rig-managed", "target": "branch", "source_type": "Repository"}],
        ruleset_by_id={},  # no body for id 99 → recorder returns rc=1
    )
    _install_gh(monkeypatch, rec)
    state, info = github_ruleset_state(_action(tmp_path))
    assert state == "gh_error" and "detail" in info


def test_listed_ruleset_without_id_is_gh_error(monkeypatch, tmp_path, gh_repo):
    rec = _GhRecorder(rulesets=[{"name": "rig-managed", "target": "branch", "source_type": "Repository"}])
    _install_gh(monkeypatch, rec)
    state, info = github_ruleset_state(_action(tmp_path))
    assert state == "gh_error" and "no id" in info["detail"]


# ── drift parity ─────────────────────────────────────────────────────────────────────
def _plan_with(repo) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    return plan


def test_drift_missing_when_no_managed_ruleset(monkeypatch, tmp_path, gh_repo):
    _install_gh(monkeypatch, _GhRecorder(rulesets=[]))
    report = detect(_plan_with(tmp_path))
    items = [i for i in report.items if i.category == "github"]
    assert len(items) == 1 and items[0].direction == "missing"


def test_drift_modified_when_ruleset_differs(monkeypatch, tmp_path, gh_repo):
    existing = {"id": 5, "name": "rig-managed", "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "bypass_actors": [], "rules": [{"type": "deletion"}]}
    _install_gh(monkeypatch, _GhRecorder(rulesets=[{"id": 5, "name": "rig-managed"}], ruleset_by_id={5: existing}))
    report = detect(_plan_with(tmp_path))
    items = [i for i in report.items if i.category == "github"]
    assert len(items) == 1 and items[0].direction == "modified"


def test_drift_in_sync_when_matching(monkeypatch, tmp_path, gh_repo):
    desired = build_ruleset_body(_DEFAULT_OPTS)
    existing = {"id": 8, "name": "rig-managed", **desired}
    _install_gh(monkeypatch, _GhRecorder(rulesets=[{"id": 8, "name": "rig-managed"}], ruleset_by_id={8: existing}))
    report = detect(_plan_with(tmp_path))
    assert not [i for i in report.items if i.category == "github"]


def test_drift_no_item_without_github_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    report = detect(_plan_with(tmp_path))
    assert not [i for i in report.items if i.category == "github"]


def test_drift_surfaces_gh_error_not_silent_in_sync(monkeypatch, tmp_path, gh_repo):
    # gh missing / not authed must NOT read as "in sync" — that would mask a real missing/drifted
    # ruleset behind a green status. It surfaces a visible "could not verify" item.
    def _unauthed(args, *, input_text=None):
        return 1, "", "HTTP 401: Bad credentials"

    _install_gh(monkeypatch, _unauthed)
    report = detect(_plan_with(tmp_path))
    items = [i for i in report.items if i.category == "github"]
    assert len(items) == 1
    assert "could not verify" in items[0].detail and "401" in items[0].detail
    assert not report.in_sync  # status is NOT clean when rig couldn't check


def test_apply_and_drift_agree_on_state(monkeypatch, tmp_path, gh_repo):
    # the parity invariant: drift "missing" ⇒ apply "created"; both via github_ruleset_state.
    _install_gh(monkeypatch, _GhRecorder(rulesets=[]))
    state, _ = github_ruleset_state(_action(tmp_path))
    assert state == "create"
    report = detect(_plan_with(tmp_path))
    assert any(i.category == "github" and i.direction == "missing" for i in report.items)


# ── plan gating (default ON, opt-out) ────────────────────────────────────────────────
def _cfg(data: dict, repo) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def test_plan_includes_github_ruleset_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, tmp_path), cat, project_type="cli")
    actions = [a for a in plan.actions if a.kind == "provision_github_ruleset"]
    assert len(actions) == 1
    # the sparse config still gets the safe defaults merged in.
    assert actions[0].options["require_pull_request"] is True
    assert actions[0].options["admin_bypass"] is True


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"github": {"ruleset": {"enabled": False}}}, tmp_path), cat, project_type="cli")
    assert not any(a.kind == "provision_github_ruleset" for a in plan.actions)


def test_plan_explicit_null_knobs_fall_back_to_defaults(fake_agent_tools, tmp_path):
    # an explicit `null` (which validate() tolerates as "absent") must NOT overlay the default
    # with None — that would crash int(None) in build_ruleset_rules and silently disable guards.
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _cfg({"github": {"ruleset": {"required_reviews": None, "name": None,
                                     "require_pull_request": None}}}, tmp_path),
        cat, project_type="cli",
    )
    action = next(a for a in plan.actions if a.kind == "provision_github_ruleset")
    assert action.options["required_reviews"] == 0  # default, not None
    assert action.options["name"] == "rig-managed"
    assert action.options["require_pull_request"] is True
    # and the body builds without crashing, with the guards intact.
    body = build_ruleset_body(action.options)
    assert "pull_request" in {r["type"] for r in body["rules"]}
    assert body["name"] == "rig-managed"


def test_plan_overrides_merge_over_defaults(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _cfg({"github": {"ruleset": {"name": "team-rules", "required_reviews": 1,
                                     "required_status_checks": ["ci"]}}}, tmp_path),
        cat, project_type="cli",
    )
    action = next(a for a in plan.actions if a.kind == "provision_github_ruleset")
    assert action.options["name"] == "team-rules"
    assert action.options["required_reviews"] == 1
    assert action.options["required_status_checks"] == ["ci"]
    # untouched knobs keep their safe defaults
    assert action.options["block_force_push"] is True


# ── config validation (fail-closed) ──────────────────────────────────────────────────
def test_validate_rejects_unknown_github_key():
    with pytest.raises(ConfigError, match="unknown github key"):
        validate({"version": 1, "github": {"rulset": {}}})


def test_validate_rejects_unknown_ruleset_key():
    with pytest.raises(ConfigError, match="unknown github.ruleset key"):
        validate({"version": 1, "github": {"ruleset": {"update": True}}})


def test_validate_rejects_non_bool_knob():
    with pytest.raises(ConfigError, match="github.ruleset.block_force_push must be a bool"):
        validate({"version": 1, "github": {"ruleset": {"block_force_push": "yes"}}})


def test_validate_rejects_negative_required_reviews():
    with pytest.raises(ConfigError, match="required_reviews must be an int >= 0"):
        validate({"version": 1, "github": {"ruleset": {"required_reviews": -1}}})


def test_validate_rejects_bool_required_reviews():
    # bool is an int subclass — `true` must NOT pass as a review count.
    with pytest.raises(ConfigError, match="required_reviews must be an int >= 0"):
        validate({"version": 1, "github": {"ruleset": {"required_reviews": True}}})


def test_validate_rejects_non_string_name():
    with pytest.raises(ConfigError, match="github.ruleset.name must be a string"):
        validate({"version": 1, "github": {"ruleset": {"name": 123}}})


def test_validate_rejects_non_string_status_checks():
    with pytest.raises(ConfigError, match="required_status_checks must be a list of strings"):
        validate({"version": 1, "github": {"ruleset": {"required_status_checks": [1, 2]}}})

    with pytest.raises(ConfigError, match="required_status_checks must be a list of strings"):
        validate({"version": 1, "github": {"ruleset": {"required_status_checks": "ci"}}})


def test_validate_rejects_non_mapping_github():
    with pytest.raises(ConfigError, match="github must be a mapping"):
        validate({"version": 1, "github": ["nope"]})

    with pytest.raises(ConfigError, match="github.ruleset must be a mapping"):
        validate({"version": 1, "github": {"ruleset": ["nope"]}})


def test_validate_accepts_empty_and_valid_block():
    validate({"version": 1, "github": {}})
    validate({"version": 1, "github": {"ruleset": {}}})
    validate(
        {
            "version": 1,
            "github": {
                "ruleset": {
                    "enabled": True,
                    "name": "rig-managed",
                    "require_pull_request": True,
                    "required_reviews": 0,
                    "block_force_push": True,
                    "restrict_deletion": True,
                    "require_linear_history": False,
                    "require_signatures": False,
                    "required_status_checks": ["ci", "lint"],
                    "admin_bypass": True,
                }
            },
        }
    )
