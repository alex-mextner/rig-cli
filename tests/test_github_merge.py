"""GitHub repository MERGE-button-policy provisioning — the ``github.merge`` block.

Covers the desired-body assembly (squash-only + secure defaults), the managed-field set, the
update / skip apply outcomes (a repo always has merge settings, so there is no ``create``), drift
parity (apply and drift switch on the SAME :func:`github_merge_state`), the no-github-remote
no-op, the capability-degrade error surfacing (no admin / not authed → 403, never a silent ok),
the RIG_GH_DRY_RUN no-mutation guard, the scaffold round-trip, and fail-closed config validation.
Every test is deterministic: the ``gh`` subprocess and the git-remote resolution are fully
monkeypatched, so nothing here hits the network or a real repo.
"""

from __future__ import annotations

import json

import pytest

from riglib.actions import runner
from riglib.actions.runner import github_merge_state
from riglib.config import ConfigError, LoadedConfig, validate
from riglib.drift import detect
from riglib.github_merge import (
    GITHUB_MERGE_DEFAULTS,
    MANAGED_MERGE_API_FIELDS,
    build_merge_body,
    normalize_merge,
)
from riglib.plan import Action, InstallPlan, build

# ── helpers ────────────────────────────────────────────────────────────────────────
# Use the SAME canonical defaults the action/plan use, so a default drift breaks the tests too.
_DEFAULT_OPTS = dict(GITHUB_MERGE_DEFAULTS)


def _action(repo, **overrides) -> Action:
    opts = {**_DEFAULT_OPTS, **overrides}
    return Action(
        kind="provision_github_merge",
        category="github",
        item="merge",
        source=repo,
        target=repo,
        options=opts,
    )


class _GhRepoRecorder:
    """A monkeypatch double for ``runner._gh_api`` — replays a canned repo GET, records PATCHes.

    ``repo_obj`` is the JSON object returned for ``GET repos/{owner}/{repo}``. PATCH calls are
    recorded (``self.patches`` as ``(path, body)``) and return success — so a test asserts WHAT
    would be sent without a real API.
    """

    def __init__(self, repo_obj=None):
        self.repo_obj = repo_obj if repo_obj is not None else {}
        self.patches: list[tuple[str, str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, args, *, input_text=None):
        self.calls.append(args)
        if "--method" in args:
            method = args[args.index("--method") + 1]
            path = args[args.index("--method") + 2]
            if method == "PATCH":
                self.patches.append((path, input_text or ""))
                return 0, json.dumps({"full_name": path.split("/", 1)[-1]}), ""
            raise AssertionError(f"unexpected gh method {method}")
        # GET repos/{owner}/{repo}
        return 0, json.dumps(self.repo_obj), ""


@pytest.fixture
def gh_repo(monkeypatch):
    """Pretend the repo has a github origin remote (no real git needed)."""
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: ("acme", "widget"))


def _install_gh(monkeypatch, recorder) -> None:
    monkeypatch.setattr(runner, "_gh_api", recorder)


def _live_repo(**flags) -> dict:
    """A live repo object whose managed flags default to the desired secure state, plus noise.

    Starts from the desired body (so absent overrides read as in-sync) and lets a test flip any
    field. Extra unrelated fields (id/name/…) are present to prove they are ignored by the diff.
    """
    obj = {"id": 1, "name": "widget", "full_name": "acme/widget", "private": False}
    obj.update(build_merge_body(_DEFAULT_OPTS))
    obj.update(flags)
    return obj


# ── desired body assembly + managed-field set ───────────────────────────────────────
def test_default_body_is_squash_only_with_secure_flags():
    body = build_merge_body(_DEFAULT_OPTS)
    assert body == {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "delete_branch_on_merge": True,
        "allow_auto_merge": True,
        "allow_update_branch": True,
    }


def test_body_carries_only_merge_fields_never_repo_identity():
    # a PATCH must never rename/expose the repo: the body holds ONLY the managed merge flags.
    body = build_merge_body(_DEFAULT_OPTS)
    assert set(body) == set(MANAGED_MERGE_API_FIELDS)
    for forbidden in ("name", "description", "private", "visibility", "default_branch"):
        assert forbidden not in body


def test_body_coerces_to_hard_bools():
    # a stray truthy/falsy config value must be sent as a real bool, not 1/""/None.
    body = build_merge_body({**_DEFAULT_OPTS, "squash_merge": 1, "merge_commit": 0})
    assert body["allow_squash_merge"] is True
    assert body["allow_merge_commit"] is False


def test_overrides_flow_into_body():
    body = build_merge_body({**_DEFAULT_OPTS, "merge_commit": True, "allow_auto_merge": False})
    assert body["allow_merge_commit"] is True
    assert body["allow_auto_merge"] is False
    assert body["allow_squash_merge"] is True  # untouched knob keeps its default


def test_normalize_ignores_unrelated_fields_and_missing_reads_false():
    norm = normalize_merge({"id": 9, "allow_squash_merge": True})
    assert norm == {f: (f == "allow_squash_merge") for f in MANAGED_MERGE_API_FIELDS}


# ── apply: update / skip ─────────────────────────────────────────────────────────────
def test_skip_when_matching(monkeypatch, tmp_path, gh_repo):
    rec = _GhRepoRecorder(repo_obj=_live_repo())
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "skipped"
    assert rec.patches == []


def test_update_when_drifted(monkeypatch, tmp_path, gh_repo):
    # the live repo allows merge commits (drift from squash-only) → apply PATCHes the desired body.
    rec = _GhRepoRecorder(repo_obj=_live_repo(allow_merge_commit=True, delete_branch_on_merge=False))
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "updated"
    assert len(rec.patches) == 1
    path, body = rec.patches[0]
    assert path == "repos/acme/widget"
    sent = json.loads(body)
    assert sent["allow_merge_commit"] is False  # converged back to squash-only
    assert sent["delete_branch_on_merge"] is True


def test_no_github_remote_is_skipped_not_error(monkeypatch, tmp_path):
    def _explode(*a, **k):
        raise AssertionError("gh api must not be called when there is no github remote")

    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    monkeypatch.setattr(runner, "_gh_api", _explode)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "skipped" and "no github" in res.detail.lower()


def test_dry_run_makes_no_patch(monkeypatch, tmp_path, gh_repo):
    monkeypatch.setenv("RIG_GH_DRY_RUN", "1")
    rec = _GhRepoRecorder(repo_obj=_live_repo(allow_merge_commit=True))
    _install_gh(monkeypatch, rec)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "updated"  # reports what WOULD happen
    assert rec.patches == []  # but never mutated


# ── capability degrade: gh errors surface, never a silent ok ─────────────────────────
def test_gh_error_surfaces_as_error(monkeypatch, tmp_path, gh_repo):
    # a token without admin on the repo → 403; must surface, not read as a false "skipped".
    def _forbidden(args, *, input_text=None):
        return 1, "", "HTTP 403: Resource not accessible by integration"

    _install_gh(monkeypatch, _forbidden)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "error" and "403" in res.detail


def test_patch_failure_is_error(monkeypatch, tmp_path, gh_repo):
    # GET succeeds (drifted) but the PATCH fails → error result, not a false "updated".
    def _patch_fails(args, *, input_text=None):
        if "--method" in args:
            return 1, "", "HTTP 422: validation failed"
        return 0, json.dumps(_live_repo(allow_merge_commit=True)), ""

    _install_gh(monkeypatch, _patch_fails)
    res = runner._do_provision_github_merge(_action(tmp_path), "backup")
    assert res.status == "error" and "update failed" in res.detail and "422" in res.detail


def test_non_json_repo_is_gh_error(monkeypatch, tmp_path, gh_repo):
    def _garbage(args, *, input_text=None):
        return 0, "not json", ""

    _install_gh(monkeypatch, _garbage)
    state, info = github_merge_state(_action(tmp_path))
    assert state == "gh_error" and "non-JSON" in info["detail"]


def test_non_object_repo_is_gh_error(monkeypatch, tmp_path, gh_repo):
    def _list_resp(args, *, input_text=None):
        return 0, json.dumps([1, 2, 3]), ""

    _install_gh(monkeypatch, _list_resp)
    state, info = github_merge_state(_action(tmp_path))
    assert state == "gh_error" and "non-object" in info["detail"]


def test_gh_error_detail_falls_back_to_stdout_then_generic(monkeypatch, tmp_path, gh_repo):
    # some `gh` failures write to stdout, not stderr; and a bare non-zero rc with neither must
    # still produce a non-empty detail. Exercise the `err or out or "gh api exited {rc}"` chain.
    def _err_empty_out_set(args, *, input_text=None):
        return 1, "boom on stdout", ""  # err empty, out populated

    _install_gh(monkeypatch, _err_empty_out_set)
    state, info = github_merge_state(_action(tmp_path))
    assert state == "gh_error" and info["detail"] == "boom on stdout"

    def _both_empty(args, *, input_text=None):
        return 7, "", ""  # neither stream → generic message carries the rc

    _install_gh(monkeypatch, _both_empty)
    state, info = github_merge_state(_action(tmp_path))
    assert state == "gh_error" and "gh api exited 7" in info["detail"]


# ── drift parity ─────────────────────────────────────────────────────────────────────
def _plan_with(repo) -> InstallPlan:
    plan = InstallPlan()
    plan.actions.append(_action(repo))
    return plan


def test_drift_modified_when_policy_differs(monkeypatch, tmp_path, gh_repo):
    _install_gh(monkeypatch, _GhRepoRecorder(repo_obj=_live_repo(allow_merge_commit=True)))
    report = detect(_plan_with(tmp_path))
    items = [i for i in report.items if i.category == "github" and i.item == "merge"]
    assert len(items) == 1 and items[0].direction == "modified"


def test_drift_in_sync_when_matching(monkeypatch, tmp_path, gh_repo):
    _install_gh(monkeypatch, _GhRepoRecorder(repo_obj=_live_repo()))
    report = detect(_plan_with(tmp_path))
    assert not [i for i in report.items if i.category == "github" and i.item == "merge"]


def test_drift_no_item_without_github_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "github_owner_repo", lambda repo_root: None)
    report = detect(_plan_with(tmp_path))
    assert not [i for i in report.items if i.category == "github" and i.item == "merge"]


def test_drift_surfaces_gh_error_not_silent_in_sync(monkeypatch, tmp_path, gh_repo):
    def _unauthed(args, *, input_text=None):
        return 1, "", "HTTP 401: Bad credentials"

    _install_gh(monkeypatch, _unauthed)
    report = detect(_plan_with(tmp_path))
    items = [i for i in report.items if i.category == "github" and i.item == "merge"]
    assert len(items) == 1
    assert "could not verify" in items[0].detail and "401" in items[0].detail
    assert not report.in_sync  # status is NOT clean when rig couldn't check


def test_apply_and_drift_agree_on_state(monkeypatch, tmp_path, gh_repo):
    # the parity invariant: drift "modified" ⇒ apply "updated"; both via github_merge_state.
    _install_gh(monkeypatch, _GhRepoRecorder(repo_obj=_live_repo(allow_merge_commit=True)))
    state, _ = github_merge_state(_action(tmp_path))
    assert state == "update"
    report = detect(_plan_with(tmp_path))
    assert any(i.category == "github" and i.item == "merge" and i.direction == "modified"
               for i in report.items)


# ── plan gating (default ON, opt-out) ────────────────────────────────────────────────
def _cfg(data: dict, repo) -> LoadedConfig:
    return LoadedConfig(data={"version": 1, **data}, repo_root=repo)


def test_plan_includes_github_merge_by_default(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"skills": {"enabled": False}}, tmp_path), cat, project_type="cli")
    actions = [a for a in plan.actions if a.kind == "provision_github_merge"]
    assert len(actions) == 1
    assert actions[0].options["squash_merge"] is True
    assert actions[0].options["merge_commit"] is False
    assert actions[0].options["delete_branch_on_merge"] is True


def test_plan_opt_out(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(_cfg({"github": {"merge": {"enabled": False}}}, tmp_path), cat, project_type="cli")
    assert not any(a.kind == "provision_github_merge" for a in plan.actions)
    # opting out of merge must NOT disable the ruleset (independent sub-blocks).
    assert any(a.kind == "provision_github_ruleset" for a in plan.actions)


def test_plan_explicit_null_knobs_fall_back_to_defaults(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _cfg({"github": {"merge": {"squash_merge": None, "delete_branch_on_merge": None}}}, tmp_path),
        cat, project_type="cli",
    )
    action = next(a for a in plan.actions if a.kind == "provision_github_merge")
    assert action.options["squash_merge"] is True  # default, not None
    assert action.options["delete_branch_on_merge"] is True
    body = build_merge_body(action.options)
    assert body["allow_squash_merge"] is True


def test_plan_overrides_merge_over_defaults(fake_agent_tools, tmp_path):
    from riglib.catalog import Catalog

    cat = Catalog.scan(str(fake_agent_tools))
    plan = build(
        _cfg({"github": {"merge": {"merge_commit": True, "allow_auto_merge": False}}}, tmp_path),
        cat, project_type="cli",
    )
    action = next(a for a in plan.actions if a.kind == "provision_github_merge")
    assert action.options["merge_commit"] is True
    assert action.options["allow_auto_merge"] is False
    assert action.options["squash_merge"] is True  # untouched knob keeps its default


# ── config validation (fail-closed) ──────────────────────────────────────────────────
def test_validate_rejects_unknown_github_key():
    with pytest.raises(ConfigError, match="unknown github key"):
        validate({"version": 1, "github": {"merg": {}}})


def test_validate_rejects_unknown_merge_key():
    with pytest.raises(ConfigError, match="unknown github.merge key"):
        validate({"version": 1, "github": {"merge": {"squash": True}}})


def test_validate_rejects_non_bool_merge_knob():
    with pytest.raises(ConfigError, match="github.merge.squash_merge must be a bool"):
        validate({"version": 1, "github": {"merge": {"squash_merge": "yes"}}})


def test_validate_rejects_non_mapping_merge():
    with pytest.raises(ConfigError, match="github.merge must be a mapping"):
        validate({"version": 1, "github": {"merge": ["nope"]}})


def test_validate_accepts_empty_and_valid_merge_block():
    validate({"version": 1, "github": {"merge": {}}})
    validate(
        {
            "version": 1,
            "github": {
                "merge": {
                    "enabled": True,
                    "squash_merge": True,
                    "merge_commit": False,
                    "rebase_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_auto_merge": True,
                    "allow_update_branch": True,
                }
            },
        }
    )


def test_validate_accepts_enabled_false():
    # the plan-gating opt-out value must pass validate() directly, not only indirectly via build().
    validate({"version": 1, "github": {"merge": {"enabled": False}}})


def test_validate_accepts_ruleset_and_merge_together():
    validate({"version": 1, "github": {"ruleset": {"required_reviews": 1}, "merge": {"merge_commit": True}}})


# ── scaffold round-trip: rig init writes a merge block that validates ─────────────────
def test_scaffold_includes_merge_block_and_round_trips():
    from riglib.state import default_state

    data = default_state(project_type="cli")
    merge = data["github"]["merge"]
    assert merge["enabled"] is True
    assert merge["squash_merge"] is True
    assert merge["merge_commit"] is False
    # the generated scaffold must validate against its own validator (no init-then-apply surprise).
    validate(data)
