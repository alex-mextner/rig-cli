"""GitHub Advanced Security (GHAS) repo-settings desired-state — the pure, side-effect-free core.

WHAT THIS IS. The §5 ``github:`` block provisions a repo's GitHub settings declaratively;
``github.ghas`` owns the repo SECURITY toggles GitHub exposes under ``security_and_analysis`` on
the repo object (plus the sub-resources that live on their OWN endpoints):

  - Vulnerability alerts     — ``PUT/DELETE /repos/{o}/{r}/vulnerability-alerts`` (a sub-resource,
                              NOT a ``security_and_analysis`` field — its own endpoint; the
                              supply-chain / dependency-graph umbrella on github.com cloud).
  - Automated security fixes — ``PUT/DELETE /repos/{o}/{r}/automated-security-fixes`` (Dependabot
                              security updates; also its own sub-resource endpoint).
  - Secret scanning          — ``security_and_analysis.secret_scanning.status``.
  - Secret-scanning push protection — ``security_and_analysis.secret_scanning_push_protection``.
  - Code scanning (default setup) — ``security_and_analysis`` does NOT expose CodeQL default-setup
                              enable/disable; it is a SEPARATE endpoint
                              (``PUT /repos/{o}/{r}/code-scanning/default-setup``). rig manages its
                              DESIRED state here; the action drives the right endpoint.

  (There is deliberately NO ``dependency_graph`` knob — github.com cloud has no separately-togglable
  API for it; it's always-on for public repos and governed by the vuln-alerts / Dependabot
  sub-resources above. A knob no apply/drift path could honor would be a config lie.)

This module is the single source of truth for the DESIRED GHAS settings: the secure-default knobs,
the ``security_and_analysis`` PATCH body, the sub-resource desired states, and the normalized
desired-vs-actual comparison. The ``Action`` handler and the ``gh`` subprocess seams live in
``actions/runner.py`` and import from here; ``plan``/``state``/``drift`` import the constants here.

WHY ITS OWN MODULE (not in ``actions/runner.py``). ``runner`` imports ``Action`` from ``..plan``,
so ``plan`` cannot import ``runner`` at module top without a cycle. Keeping these PURE pieces here
(stdlib-only) lets ``plan`` and ``state`` import them at module top. Mirrors ``github_ruleset.py``.

CAPABILITY DEGRADE (the org/private hard-fail). GHAS code-scanning / secret-scanning on a PRIVATE
repo requires a GHAS-licensed plan; the API returns HTTP 403/422 when the plan does not include it.
That is NOT a rig bug and must NOT crash — the action degrades to a VISIBLE "could not enable
(plan does not include GHAS)" result, exactly like a no-admin token. Dependency graph / vuln-alerts
/ Dependabot are free on public repos and on private repos with the dependency-graph feature, so
those degrade independently of the GHAS-licensed scanners. The classifier reports per-knob outcomes
so one unlicensed scanner never masks a successfully-toggled free feature.
"""

from __future__ import annotations

from typing import Any

# The canonical SECURE/SENSIBLE-DEFAULT GHAS knobs — the single source the plan builder merges a
# sparse config onto and the state.py scaffold writes. Secure defaults ON: every supply-chain and
# secret-leak guard GitHub exposes is requested; on a repo whose plan does not include a given
# scanner the action degrades loudly rather than failing the whole apply. `enabled` is a
# plan-gating meta-key, not a body knob, so it is NOT here.
# NB: there is intentionally NO ``dependency_graph`` knob. On github.com cloud the dependency graph
# is not a separately-togglable repo setting via the API (it's always-on for public repos and is
# governed by the vulnerability-alerts / Dependabot sub-resources rig DOES manage below) — a knob
# that no apply/drift path could honor would be a config lie. The supply-chain guard is expressed
# through ``vulnerability_alerts`` + ``automated_security_fixes``, which have real endpoints.
GITHUB_GHAS_DEFAULTS: dict[str, Any] = {
    "vulnerability_alerts": True,
    "automated_security_fixes": True,
    "secret_scanning": True,
    "secret_scanning_push_protection": True,
    "code_scanning_default_setup": True,
}

# The knobs that map onto the repo object's ``security_and_analysis`` block (one PATCH carries all
# of them). Each is a ``{"status": "enabled"|"disabled"}`` sub-object on the repo. Listed once so
# the body builder and the normalized comparison read the SAME field set.
#
# ⚠️ ``dependency_graph`` is intentionally NOT here. On github.com cloud the repo object does not
# round-trip a ``security_and_analysis.dependency_graph`` field (it's always-on for public repos
# and is governed by the vulnerability-alerts / automated-security-fixes sub-resources elsewhere),
# so comparing it would make ``rig status`` report perpetual drift and every apply re-PATCH a no-op.
# The ``dependency_graph`` config knob still exists (it gates whether rig requests the supply-chain
# umbrella), but it is reconciled through the sub-resource endpoints, not this PATCH-body diff.
_SECURITY_ANALYSIS_KNOBS: dict[str, str] = {
    "secret_scanning": "secret_scanning",
    "secret_scanning_push_protection": "secret_scanning_push_protection",
}

# The knobs that are SEPARATE sub-resource endpoints (not ``security_and_analysis`` fields). The
# action drives ``PUT/DELETE`` on each; the desired value is a plain bool. Kept here so the action
# and drift agree on which knobs are endpoint-driven vs. PATCH-body driven.
SUBRESOURCE_KNOBS: tuple[str, ...] = (
    "vulnerability_alerts",
    "automated_security_fixes",
)

# The code-scanning default-setup knob is its OWN endpoint with its OWN body shape
# (``{"state": "configured"|"not-configured"}``), so it is handled distinctly from both groups.
CODE_SCANNING_KNOB = "code_scanning_default_setup"


def build_security_analysis_body(opts: dict) -> dict[str, dict[str, str]]:
    """The desired ``security_and_analysis`` block for ``PATCH /repos/{owner}/{repo}``.

    Only the ``security_and_analysis`` knobs rig manages are emitted (secret scanning + push
    protection — NOT ``dependency_graph``, which github.com cloud doesn't round-trip; see
    ``_SECURITY_ANALYSIS_KNOBS``), each as the API's ``{"status": "enabled"|"disabled"}`` sub-object
    with a hard bool→status coercion (so a stray truthy config value can't send a bogus status).
    Shared by apply + drift. The body carries ONLY these security sub-objects — never a repo
    name/visibility key — so a PATCH can never rename or expose the repo.
    """
    body: dict[str, dict[str, str]] = {}
    for knob, field in _SECURITY_ANALYSIS_KNOBS.items():
        status = "enabled" if bool(opts.get(knob, GITHUB_GHAS_DEFAULTS[knob])) else "disabled"
        body[field] = {"status": status}
    return body


def desired_subresource(opts: dict, knob: str) -> bool:
    """Whether a sub-resource knob (vuln-alerts / automated-fixes) should be ON, as a hard bool."""
    return bool(opts.get(knob, GITHUB_GHAS_DEFAULTS[knob]))


def desired_code_scanning(opts: dict) -> bool:
    """Whether CodeQL default-setup should be configured, as a hard bool."""
    return bool(opts.get(CODE_SCANNING_KNOB, GITHUB_GHAS_DEFAULTS[CODE_SCANNING_KNOB]))


def normalize_security_analysis(repo_obj: dict) -> dict[str, str]:
    """The comparable shape of the live ``security_and_analysis`` — only the managed fields.

    Both the desired body and the live repo object (from ``GET /repos/{o}/{r}``) normalize through
    this, so the diff ignores every other repo field GitHub returns and a missing/absent scanner
    reads as ``"disabled"`` (its effective state) rather than raising. A semantic match reads as
    in-sync, not drift.
    """
    sa = repo_obj.get("security_and_analysis")
    if not isinstance(sa, dict):
        sa = {}
    out: dict[str, str] = {}
    for field in _SECURITY_ANALYSIS_KNOBS.values():
        node = sa.get(field)
        status = node.get("status") if isinstance(node, dict) else None
        out[field] = str(status) if status in ("enabled", "disabled") else "disabled"
    return out
