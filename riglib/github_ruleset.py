"""GitHub branch-ruleset desired-state — the pure, side-effect-free core shared everywhere.

WHAT THIS IS. rig provisions a GitHub repository **branch ruleset** (the modern replacement
for branch protection) on a repo's default branch, declaratively and reconciled like every
other category. This module is the single source of truth for the DESIRED ruleset: the
sensible-default knobs, the request-body / rule assembly, the normalized desired-vs-actual
comparison, and the "which ruleset is the managed one" predicate. The ``Action`` handler, the
``gh`` subprocess seams, and the live API classification live in ``actions/runner.py`` and
import from here; the plan builder, the ``state.py`` scaffold, and ``drift.py`` import the
constants/builders from here too — so every consumer reads ONE definition of the desired body.

WHY IT IS ITS OWN MODULE (not in ``actions/runner.py``). ``runner`` imports ``Action`` from
``..plan``, so ``plan`` cannot import ``runner`` at module top without a cycle. Keeping these
PURE pieces here (this module imports only stdlib) lets ``plan`` and ``state`` import them at
module top — no lazy/deferred imports, no cycle.

⚠️ THE FOOTGUN GUARD (canonical statement — referenced, not repeated, elsewhere). A hand-made
ruleset with the ``update`` ("Restrict updates") rule and **zero bypass actors** locks out
*every* merge to the protected default branch: each merge is itself an "update" to the ref, so
GitHub answers ``Cannot update this protected ref`` and only a repo admin using ``--admin`` can
push past it. :func:`build_ruleset_rules` therefore **cannot emit the ``update`` rule** — there
is no config knob and no code path that produces it. It likewise never emits a
``required_deployments`` rule with an empty environment list (a no-op smell that can also
block). And when ``admin_bypass`` is on (the default) the repo Admin role is a bypass actor, so
an active ruleset never locks admins out of merging.
"""

from __future__ import annotations

import re
from typing import Any

# The default-branch token GitHub rulesets use for "the repo's default branch" in a ref-name
# condition — so the ruleset follows a rename of the default branch.
_RULESET_DEFAULT_BRANCH = "~DEFAULT_BRANCH"
# The built-in RepositoryRole id for the repo Admin role (GitHub's documented fixed id). Used
# as the bypass actor so admins can still merge under an active ruleset.
_ADMIN_ROLE_ACTOR_ID = 5
# The ruleset name rig owns/reconciles by default — one constant so the plan builder, the
# state scaffold, the action, and drift never disagree on which ruleset is "the managed one".
DEFAULT_RULESET_NAME = "rig-managed"
# Two anchored forms, so an embedded ``github.com`` path segment (e.g.
# ``https://evil.com/github.com/acme/widget``) can never false-positive:
#  - scp-style SSH:  git@github.com:owner/repo(.git)
#  - URL scheme:     scheme://[user@]github.com[:port]/owner/repo(.git)
# In both, the host must be EXACTLY github.com and owner/repo are the only path segments.
_SCP_REMOTE_RE = re.compile(r"^(?:[^@/]+@)?github\.com:([^/]+)/(.+?)(?:\.git)?/?$")
_URL_REMOTE_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(?:[^@/]+@)?github\.com(?::\d+)?/([^/]+)/(.+?)(?:\.git)?/?$"
)

# The canonical SENSIBLE-DEFAULT ruleset knobs — the single source the plan builder merges a
# sparse config onto and the state.py scaffold writes. Keeps merges WORKING: a PR is required
# (zero reviews), force-push + deletion blocked, admins kept able to merge. No `update` /
# `required_deployments` knob exists — the footgun rules are not expressible (see the module
# docstring + build_ruleset_rules). `enabled` is a plan-gating meta-key, not a body knob, so it
# is NOT here.
GITHUB_RULESET_DEFAULTS: dict[str, Any] = {
    "name": DEFAULT_RULESET_NAME,
    "require_pull_request": True,
    "required_reviews": 0,
    "block_force_push": True,
    "restrict_deletion": True,
    "require_linear_history": False,
    "require_signatures": False,
    "required_status_checks": [],
    "admin_bypass": True,
}

# Map each merge-gating CI gate slot → the GitHub CHECK-RUN context it produces (the job `name:` in
# its workflow, which is the string the required_status_checks rule matches). ROADMAP §5 names these
# two — the PR-checklist gate and the unresolved-review-threads gate — as the required checks rig
# adds to the ruleset, so a PR can't merge until both are green. The KEY (slot) matches the CI
# catalog item name; the VALUE (context) matches the workflow's job name verbatim. One table so the
# plan builder, the scaffold, and the docs never disagree on which check a gate reports as.
#
# ⚠️ THE LOCKOUT GUARD. A required status check whose check-run NEVER appears (the workflow isn't in
# the repo) wedges every PR — GitHub holds the merge waiting for a check that can't report. So the
# plan builder requires a context ONLY when that CI gate is actually enabled and being written for
# the repo (see `_build_github_ruleset`); a repo without the gate gets no required check for it, and
# `admin_bypass` (on by default) keeps an admin able to merge past a stuck check regardless.
CI_GATE_CHECK_CONTEXTS: dict[str, str] = {
    "pr-checklist": "PR Checklist",
    "review-threads": "review-threads",
}


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Parse ``(owner, repo)`` from a github.com remote URL, or None if not github.

    Handles scp-style SSH (``git@github.com:owner/repo.git``) and URL forms
    (``https://github.com/owner/repo``, ``ssh://git@github.com:22/owner/repo.git``), with the
    ``.git`` suffix and a trailing slash optional. Both forms are ANCHORED on the host being
    EXACTLY ``github.com`` — so a non-github URL, an embedded ``github.com`` path segment
    (``https://evil.com/github.com/...``), or an SSH ``host:port`` that isn't github → None.
    Pure (no subprocess), so the subprocess seam in ``runner.github_owner_repo`` and the unit
    tests share one parser.
    """
    m = _SCP_REMOTE_RE.match(url) or _URL_REMOTE_RE.match(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # owner/repo must be single path segments (the URL regex already constrains owner; guard the
    # repo too so a stray extra segment doesn't get swallowed into the repo name).
    if "/" in repo:
        return None
    return owner, repo


def build_ruleset_rules(opts: dict) -> list[dict]:
    """Assemble the ruleset ``rules`` array from the config options (shared by apply + drift).

    Each knob maps to one GitHub ruleset rule. The ``update`` rule is intentionally
    UNREACHABLE — there is no branch that emits it — so a rig-managed ruleset can never lock
    out merges (see the module-docstring footgun guard). ``required_status_checks`` only emits
    its rule when the check list is non-empty (an empty ``required_status_checks`` rule is a
    no-op that GitHub still records as a rule).
    """
    rules: list[dict] = []
    if opts.get("require_pull_request", True):
        rules.append(
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": int(opts.get("required_reviews", 0)),
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            }
        )
    if opts.get("block_force_push", True):
        rules.append({"type": "non_fast_forward"})
    if opts.get("restrict_deletion", True):
        rules.append({"type": "deletion"})
    if opts.get("require_linear_history", False):
        rules.append({"type": "required_linear_history"})
    if opts.get("require_signatures", False):
        rules.append({"type": "required_signatures"})
    checks = list(opts.get("required_status_checks", []) or [])
    if checks:  # an empty list emits NO rule (never a no-op required_status_checks rule)
        rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": c} for c in checks],
                    "strict_required_status_checks_policy": False,
                },
            }
        )
    return rules


def build_ruleset_body(opts: dict) -> dict:
    """The full desired ruleset request body (shared by apply + drift — one source of truth).

    Targets the repo's default branch via the ``~DEFAULT_BRANCH`` ref token. When
    ``admin_bypass`` is on (default) the repo Admin role is added to ``bypass_actors`` so an
    active ruleset never locks admins out of merging.
    """
    bypass_actors: list[dict] = []
    if opts.get("admin_bypass", True):
        bypass_actors.append(
            {
                "actor_id": _ADMIN_ROLE_ACTOR_ID,
                "actor_type": "RepositoryRole",
                "bypass_mode": "always",
            }
        )
    return {
        "name": str(opts.get("name", DEFAULT_RULESET_NAME)),
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": [_RULESET_DEFAULT_BRANCH], "exclude": []},
        },
        "bypass_actors": bypass_actors,
        "rules": build_ruleset_rules(opts),
    }


def normalize_ruleset(body: dict) -> dict:
    """The comparable shape of a ruleset — only the fields rig manages, order-independent.

    Both the desired body and a ruleset fetched from GitHub are normalized through this, so the
    desired-vs-actual diff ignores fields GitHub adds (id, created_at, _links, node_id) and
    ordering. Rules are sorted by type; EVERY managed rule parameter is compared (not just the
    review count / contexts — else a live flip of any other managed param reads as in-sync and
    apply never converges); bypass actors are reduced to their identity tuple; conditions are
    compared order-independently — so a semantic match reads as in-sync, not drift.
    """
    norm_rules = []
    for rule in sorted(body.get("rules", []) or [], key=lambda r: str(r.get("type", ""))):
        rtype = str(rule.get("type", ""))
        params = rule.get("parameters") or {}
        if rtype == "required_status_checks":
            contexts = sorted(
                str(c.get("context", "")) for c in params.get("required_status_checks", []) or []
            )
            norm_rules.append(
                {
                    "type": rtype,
                    "contexts": contexts,
                    "strict_required_status_checks_policy": bool(
                        params.get("strict_required_status_checks_policy", False)
                    ),
                }
            )
        elif rtype == "pull_request":
            # rig MANAGES these four PR sub-params (it always writes them False — see
            # build_ruleset_rules), so they ARE part of the desired state: a hand-flip in the UI
            # is real drift apply should reconcile back, not silently tolerate. They are
            # compared, not configurable — intentional, not an oversight.
            norm_rules.append(
                {
                    "type": rtype,
                    "required_approving_review_count": int(
                        params.get("required_approving_review_count", 0)
                    ),
                    "dismiss_stale_reviews_on_push": bool(
                        params.get("dismiss_stale_reviews_on_push", False)
                    ),
                    "require_code_owner_review": bool(
                        params.get("require_code_owner_review", False)
                    ),
                    "require_last_push_approval": bool(
                        params.get("require_last_push_approval", False)
                    ),
                    "required_review_thread_resolution": bool(
                        params.get("required_review_thread_resolution", False)
                    ),
                }
            )
        else:
            norm_rules.append({"type": rtype})
    # stringify EVERY field of the actor identity tuple (incl. actor_id) so the sort can't hit a
    # None-vs-int TypeError if a future config ever carries multiple bypass actors.
    actors = sorted(
        (str(a.get("actor_type", "")), str(a.get("actor_id", "")), str(a.get("bypass_mode", "")))
        for a in body.get("bypass_actors", []) or []
    )
    return {
        "enforcement": str(body.get("enforcement", "")),
        "conditions": _normalize_conditions(body.get("conditions")),
        "rules": norm_rules,
        "bypass_actors": actors,
    }


def _normalize_conditions(conditions: Any) -> dict:
    """Order-independent shape of the ref_name condition rig manages.

    GitHub may return the ``ref_name`` include/exclude lists in any order (and a different key
    order); compare the SORTED lists so a semantic match isn't reported as drift.
    """
    if not isinstance(conditions, dict):
        return {"include": [], "exclude": []}
    ref = conditions.get("ref_name") or {}
    if not isinstance(ref, dict):
        ref = {}
    return {
        "include": sorted(str(x) for x in ref.get("include", []) or []),
        "exclude": sorted(str(x) for x in ref.get("exclude", []) or []),
    }


def find_managed_ruleset(rulesets: list[dict], name: str) -> dict | None:
    """Return the REPOSITORY branch ruleset whose ``name`` matches, else None.

    rig owns the repo-level branch ruleset with this name. The list endpoint can also surface
    inherited ORG/enterprise rulesets and non-branch (tag/push) rulesets — matching by name
    alone would let an org-level ``rig-managed`` ruleset masquerade as the repo one (wrong id
    on update, false ``ok``). So we additionally require ``target == "branch"`` and, when the
    API reports it, ``source_type == "Repository"`` (the field is present on modern responses;
    absence is tolerated for older shapes since the caller also passes ``includes_parents=false``).
    On the (operator-error) chance of two same-named repo branch rulesets, the FIRST wins.
    """
    if not isinstance(rulesets, list):
        return None
    for rs in rulesets:
        if not isinstance(rs, dict) or str(rs.get("name", "")) != name:
            continue
        if str(rs.get("target", "branch")) != "branch":
            continue
        source_type = rs.get("source_type")
        if source_type is not None and str(source_type) != "Repository":
            continue
        return rs
    return None
