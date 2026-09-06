"""config-web's interactive PLAN engine — preview, confirm/skip, live-progress apply
(rig-cli#310), all through the SAME shared engine ``rig apply``/``rig init`` already use
(:func:`riglib.plan.build` + :func:`riglib.actions.runner.run_plan`). This module never
re-implements plan building or execution — it only adds the web-facing plumbing around them:

- :func:`build_scope_plan` resolves ONE :class:`~riglib.config_web_scopes.Scope` (a repo tab or
  the Global tab) into ``(InstallPlan, LoadedConfig, riglib.detect.Environment)`` — the exact
  ``plan.build()`` call ``rig apply info`` makes, with the Global scope routed through
  ``config.load(..., include_repo=False)`` + the same GLOBAL-action filter
  :func:`riglib.drift.compute_drift_report` uses for a non-git cwd, so it never reads or writes
  any repo rig.yaml.
- :func:`action_key` gives each planned action a STABLE identity (kind + category + item +
  descriptor + target) independent of its position in the list, so a browser can safely skip
  individual actions by key; :func:`fingerprint_plan` hashes the full ordered plan shape (keys +
  source + options + on_conflict) so the server can detect a STALE preview (config changed since
  the browser fetched it) before ever mutating disk.
- :class:`ApplyJobStore` runs a SELECTED sub-plan through the real ``run_plan`` in a background
  thread, recording each action's live status via ``run_plan``'s existing ``on_start``/
  ``progress`` callbacks (the same mechanism PR #306 added for the ``rig init`` TUI's live Apply
  screen — reused here, not its Textual-specific wiring) so a poll endpoint can show per-action
  progress. Exactly ONE job may run at a time process-wide (mirrors the TUI's
  ``exclusive=True, group="apply"`` worker) — a second start while one is in flight is refused.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .action_tags import tag_for_kind
from .config_web_scopes import Scope

# ── resolving a scope into a plan ────────────────────────────────────────────────────────────


@dataclass
class ScopePlan:
    """A resolved scope: the plan `rig apply info` would show, plus its config/env context."""

    scope: Scope
    plan: Any  # riglib.plan.InstallPlan
    loaded: Any  # riglib.config.LoadedConfig
    env: Any  # riglib.detect.Environment


def build_scope_plan(scope: Scope) -> ScopePlan:
    """Build the plan for one scope — the SAME plan.build() call ``rig apply``/``rig status`` make.

    A repo scope loads the full cascade (global + that repo's ``rig.yaml``), exactly like
    ``rig apply -C <repo>``. The Global scope loads ONLY the global layer
    (``config.load(..., include_repo=False)``) anchored at ``$HOME`` (it is not tied to any one
    checkout), then keeps ONLY actions whose category is WRITABLE-global
    (:func:`riglib.schema.writable_layer_for_category` — gitignore/spotlight/tg_ctl/tmux/mode).

    This is DELIBERATELY the NARROW filter, matching :func:`riglib.config_web.build_model`'s
    Global-scope VIEW exactly (same predicate) — not the broader STATUS layer
    (:func:`riglib.drift.is_global_action`, which also counts skills/harness/permissions/
    agent_hooks/models/git_hooks/env/tools as GLOBAL because they're machine-wide ARTIFACTS even
    though their VALUE is written into a committed repo file). An earlier version used the
    broader filter here, which let the Global tab's plan/apply execute actions (e.g.
    ``apply_harness``) that its own view never showed a field for — "Apply selected" could
    silently do something the tab gave no way to inspect or opt out of by editing a setting there
    (caught in review, twice). Aligning plan == view means what you see on the Global tab is
    EXACTLY what "Apply selected" can do there — skills/harness/etc. drift and apply still fully
    work, just from the REPO tab that actually declares them, where the view already shows them.
    """
    from .catalog import Catalog
    from .config import load
    from .detect import detect_environment
    from .layers import GLOBAL as _GLOBAL_LAYER
    from .plan import build
    from .schema import writable_layer_for_category

    if scope.is_global:
        anchor = Path.home()
        env = detect_environment(anchor)
        env.is_git_repo = False  # the Global scope never has repo context — see docstring
        loaded = load(anchor, include_repo=False)
        catalog = Catalog.scan(loaded.agent_tools_source)
        plan = build(loaded, catalog, project_type="unknown")
        plan.actions = [
            a for a in plan.actions if writable_layer_for_category(a.category) == _GLOBAL_LAYER
        ]
    else:
        if scope.repo_root is None:
            # a real invariant violation (a non-global Scope must always carry a repo_root — see
            # Scope's own dataclass contract), but `assert` is stripped under `python -O`; a
            # ValueError still fails loudly there instead of a confusing later AttributeError
            # (found in review).
            raise ValueError(f"non-global scope {scope.id!r} has no repo_root")
        env = detect_environment(scope.repo_root)
        loaded = load(scope.repo_root, include_repo=True)
        catalog = Catalog.scan(loaded.agent_tools_source)
        plan = build(loaded, catalog, project_type=env.project_type)
        # KNOWN, narrow edge case (flagged in review, deliberately not "fixed" here): a repo
        # scope with a committed rig.yaml but NO .git directory is unusual but possible.
        # `env.is_git_repo` is real (not forced) for a repo scope, unlike the Global scope above
        # — so this PLAN (and therefore /api/apply) still includes repo-scoped actions there
        # (e.g. install_ci), matching what `rig apply` itself would do for that same cwd. But
        # `compute_scope_drift` (config_web_plan.compute_scope_drift -> riglib.drift.
        # compute_drift_report) reuses the SAME non-git partition `rig status` deliberately
        # applies for ITS OWN display purposes, which drops those same repo-scoped actions from
        # the drift computation — so the drift panel can under-report drift for a scope whose
        # plan/apply still acts on it. This mirrors a PRE-EXISTING asymmetry between `rig apply`
        # (does not drop repo actions for a non-git cwd) and `rig status` (does, deliberately, for
        # display cleanliness) — config-web faithfully reproduces each CLI command's own real
        # behavior rather than inventing a THIRD, config-web-specific policy. Reconciling that
        # asymmetry is a CLI-level product decision, out of scope for this change.

    return ScopePlan(scope=scope, plan=plan, loaded=loaded, env=env)


# ── drift, per scope ──────────────────────────────────────────────────────────────────────────


def compute_scope_drift(scope_plan: ScopePlan) -> dict[str, Any]:
    """The drift panel payload for one scope — the SAME engine ``rig status`` uses.

    Delegates entirely to :func:`riglib.drift.compute_drift_report` (which itself wires
    :func:`riglib.drift.detect`'s scan-dir arguments + the disabled-category augmentation checks
    + the missing-target scan) — never a parallel drift computation. Called on-demand per active
    tab (``/api/drift?scope=<id>``), not eagerly for every discovered scope on page load.

    For the Global scope, restricts BOTH the extras-scan AND the disabled-category augmentation
    checks (dispatcher/env — but NOT gitignore, which IS writable-global) to
    :func:`riglib.schema.global_only_categories` — the SAME set the Global PLAN itself is
    narrowed to (:func:`build_scope_plan`'s docstring). Without this, the Global tab would flag
    every skill/hook/MCP entry installed by a REPO tab as unresolvable "extra" drift, AND a
    disabled ``env``/``git_hooks`` category elsewhere in the global config as drift for a
    category the Global tab never renders or can apply — its own plan declares none of those by
    design, so an unrestricted scan would permanently disagree with what the tab can actually do
    (caught in review across two passes: first the scan-dirs, then the augmentation checks).
    """
    from .drift import compute_drift_report
    from .schema import global_only_categories

    restrict = global_only_categories() if scope_plan.scope.is_global else None
    scan = compute_drift_report(
        scope_plan.plan, scope_plan.loaded, scope_plan.env, restrict_scan_categories=restrict
    )
    report, dead_targets = scan.report, scan.dead_targets
    items = [
        {
            "direction": item.direction,
            "category": item.category,
            "item": item.item,
            "target": str(item.target),
            "detail": item.detail,
        }
        for item in report.items
    ]
    missing_targets = [
        {"what": m.what, "why": m.why, "fix": m.fix} for m in dead_targets
    ]
    # on-disk items rig can PLACE but does not manage (rig-cli#357) — informational, never drift,
    # so they do not touch ``in_sync``; exposed so the panel can list them under their own heading.
    from .provenance import KIND_LABELS, PERMISSION_KINDS

    known = [
        {
            "category": k.category, "item": k.item, "target": str(k.target), "kind": k.kind,
            "name": k.name, "by": k.by, "container": k.container,
            # the same two headings the CLI renders: permission entries beyond the baseline are
            # "your additions, kept", everything else "known, not managed by rig"
            "kept": k.kind in PERMISSION_KINDS,
            "label": KIND_LABELS[k.kind],
        }
        for k in report.known
    ]
    return {
        "scope": scope_plan.scope.id,
        "in_sync": report.in_sync and not dead_targets,
        "items": items,
        "known": known,
        "missing_targets": missing_targets,
    }


# ── stable action identity + fingerprinting ──────────────────────────────────────────────────


def action_key(action: Any) -> str:
    """A stable identity for one planned action, independent of its position in the plan.

    Combines everything :meth:`~riglib.plan.Action.describe` uses (category/item/descriptor
    target) — the same fields that distinguish two actions from the same catalog item (e.g. one
    hook item emitting several descriptor actions into one target dir).
    """
    descriptor = action.options.get("descriptor", "")
    return f"{action.kind}|{action.category}|{action.item}|{descriptor}|{action.target}"


def fingerprint_plan(plan: Any) -> str:
    """A hash of the ORDERED plan shape — changes iff anything about it would apply differently.

    The preview endpoint returns this; the apply endpoint re-builds the plan and must see the
    same fingerprint before it will run anything (see :meth:`ApplyJobStore.start`). Covers not
    just WHICH actions are present (:func:`action_key`) but also ``plan.on_conflict``, each
    action's ``options``, AND each action's ``source`` (the carrier path in the agent-tools
    checkout / catalog content that gets copied/installed) — hashing the key set ALONE missed two
    behavior-changing edits that leave the action list identical: flipping
    ``defaults.on_conflict`` from ``backup`` to ``overwrite``, or repointing
    ``agent_tools_source`` at a different checkout whose items happen to share the same
    kind/category/item/target names but carry different CONTENT (the server would have accepted
    a stale preview's fingerprint and copied/installed content the user never actually saw — both
    caught in review). ``options`` values are stringified (``default=str``) so a ``Path``/other
    non-JSON-native value never raises here.

    KNOWN LIMITATION (flagged in review, deliberately not fully closed): this hashes the source
    PATH, not its file CONTENTS — an in-place change to a source directory between preview and
    apply (e.g. a ``git pull`` inside the SAME ``agent_tools_source`` checkout, or hand-editing a
    skill file there) is invisible to the fingerprint. Hashing the recursive content of every
    carrier on every preview would make the preview itself slow for a large catalog, for a threat
    this tool's own trust model doesn't otherwise defend against: ``rig apply commit`` run twice
    in a row from the CLI has NO staleness check at all between the two invocations, so this
    fingerprint is already STRICTLY more protective than the CLI's own bare workflow, not less —
    it just isn't a full audit of catalog content. rig is a local, single-user dev tool (not a
    multi-tenant service); the realistic actor able to edit the catalog checkout mid-preview is
    the same person previewing it.
    """
    payload = {
        "on_conflict": plan.on_conflict,
        "actions": [
            {
                "key": action_key(a),
                "source": str(a.source),
                # dict key order doesn't need pre-sorting here -- json.dumps(sort_keys=True)
                # below already canonicalizes it.
                "options": dict(a.options),
            }
            for a in plan.actions
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class PlanIntegrityError(RuntimeError):
    """Raised when a plan violates an invariant the web UI depends on (e.g. a duplicate action
    key). Should never happen with today's plan builders (kind+category+item+descriptor+target
    already distinguishes every real action) — this is a loud, fail-closed guard so a FUTURE
    builder change that breaks that assumption surfaces as a clear preview-build error instead of
    silently corrupting the skip-selection UI (two actions sharing one checkbox / one progress
    row stuck at "queued" forever) — flagged in review as unenforced.
    """


def _assert_unique_action_keys(plan: Any) -> None:
    seen: set[str] = set()
    for a in plan.actions:
        key = action_key(a)
        if key in seen:
            raise PlanIntegrityError(
                f"duplicate action key {key!r} — two planned actions are indistinguishable to "
                "the web UI; this is a plan-builder bug, not a config problem"
            )
        seen.add(key)


class NoConfigLayerError(RuntimeError):
    """Raised when a scope has NO declared config at all (no repo rig.yaml, no --config, no
    global config) — the plan would be built entirely from built-in defaults.

    Mirrors ``rig apply``'s own fail-closed guard (``riglib.cli.cmd_apply``: "with NO config
    layer … the empty config resolves to built-in defaults and would mutate HOME with no
    committed source of truth. Refuse.") — that guard runs BEFORE the info/commit split, so even
    ``rig apply info`` (preview-only) refuses in this state, not just ``rig apply commit``. Both
    :func:`preview_payload` and :meth:`ApplyJobStore.start` raise this the same way, for parity.
    :func:`riglib.config_web_scopes.discover_scopes` deliberately keeps the home scope even with
    no committed ``rig.yaml`` (so a fresh checkout mid-``rig init`` still shows a tab) — that
    scope's DRIFT panel stays useful (``rig status`` shows exactly this case too, via a warning,
    not a refusal), but its plan preview/apply must refuse the same way the CLI does, or
    config-web would show/apply built-in defaults for a directory `rig apply` itself would never
    touch (caught in review).
    """


def _require_declared_config(scope_plan: ScopePlan) -> None:
    if not scope_plan.loaded.layers:
        raise NoConfigLayerError(
            f"scope {scope_plan.scope.id!r} has no declared config (no rig.yaml, no --config, "
            "no global config) — run `rig init` (or `rig export -o rig.yaml`) there first; "
            "config-web will not plan/apply built-in defaults for an unconfigured directory"
        )


def preview_payload(scope_plan: ScopePlan) -> dict[str, Any]:
    """The JSON body for ``/api/plan`` — mirrors ``rig apply info``'s plan, tagged per action.

    Refuses (:class:`NoConfigLayerError`) when the scope has no declared config at all — the
    SAME guard ``rig apply info`` itself enforces before it will even preview.
    """
    _require_declared_config(scope_plan)
    plan = scope_plan.plan
    _assert_unique_action_keys(plan)
    actions = []
    for a in plan.actions:
        tag = tag_for_kind(a.kind)
        actions.append({
            "key": action_key(a),
            "kind": a.kind,
            "category": a.category,
            "item": a.item,
            "target": str(a.target),
            "describe": a.describe(),
            "tag": {
                "category": tag.category,
                "label": tag.label,
                "audience": tag.audience,
                "detail": tag.detail,
            },
        })
    return {
        "scope": scope_plan.scope.id,
        "fingerprint": fingerprint_plan(plan),
        "on_conflict": plan.on_conflict,
        "actions": actions,
        "notes": list(plan.notes),
    }


# ── the apply job: run_plan() in a background thread, polled for progress ───────────────────


@dataclass
class ActionProgress:
    key: str
    kind: str
    category: str
    item: str
    describe: str
    status: str = "queued"  # queued | running | created | updated | skipped | backed_up | error
    detail: str = ""


@dataclass
class ApplyJob:
    id: str
    scope_id: str
    fingerprint: str
    actions: list[ActionProgress]
    done: bool = False
    error: str | None = None


class ApplyBusyError(RuntimeError):
    """Raised when a second apply is requested while one is already running (process-wide)."""


class PlanStaleError(RuntimeError):
    """Raised when the fingerprint the browser echoed back no longer matches a fresh plan build."""


# How many finished jobs a long-lived config-web process keeps around for polling. Unbounded
# retention was a slow memory leak in a daemonized instance (each job holds a describe string per
# action) — flagged in review. A poller only needs the MOST RECENT run per scope in practice;
# this is generous headroom, not a tight cache.
_MAX_RETAINED_JOBS = 20


class ApplyJobStore:
    """Owns at most ONE in-flight apply job for the whole server process.

    A single lock serializes job starts (mirrors the ``rig init`` TUI's
    ``exclusive=True, group="apply"`` worker) — the shared engine mutates real machine state
    (harness settings, git hooks, launchd units), so two concurrent applies — even for different
    scopes — could race on a shared GLOBAL file. Reads (``status``) are lock-free once the job
    dict entry exists; only starting a new job takes the lock.

    KNOWN LIMITATION (flagged in review, deliberately out of scope for this pass): this lock is
    per-PROCESS, not machine-wide. ``riglib.config_web_service``'s service identity is
    (deliberately, and PRE-EXISTING this feature) per repo-root — ``rig config-web start -C
    repo-a`` and ``... -C repo-b`` run SEPARATE server processes, each with its OWN
    ``ApplyJobStore``. Since both machine-wide consoles can discover and apply the SAME scope
    (including the Global scope, or a repo the other console's registry also lists), two
    processes CAN race a genuinely concurrent apply against the same on-disk artifact (e.g. both
    writing ``mcp.json``). Closing this fully means either a single machine-wide daemon (a real
    service-lifecycle redesign, not a job-store change) or a cross-process file lock — both
    bigger changes than this ticket's scope; not attempted here. The realistic exposure is narrow
    (a user would have to deliberately run two config-web instances against overlapping scopes at
    the same time), and it does not regress anything ``rig apply`` itself already guarantees (the
    CLI run twice concurrently from two terminals has the exact same race, with no lock at all).

    Retains at most :data:`_MAX_RETAINED_JOBS` jobs TOTAL (oldest evicted first, including the
    active one in the count) — the active job itself is never evicted while still running; the
    guard for that in :meth:`_evict_if_over_capacity` is defense-in-depth (today the active id is
    always the newest-appended, so eviction — which pops from the front — can never reach it
    before it finishes; the check makes that safe by construction rather than by an unenforced
    invariant, so a future eviction-order change can't silently reintroduce a live ``KeyError``).
    A hung ``run_plan`` call (e.g. a GitHub action stuck on network/auth) still blocks all further
    applies until it resolves — no timeout is imposed here, matching ``rig apply`` itself, which
    has none either — this is a known limitation, not a NEW one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ApplyJob] = {}
        self._job_order: list[str] = []  # insertion order, for eviction
        self._active_job_id: str | None = None

    def _evict_if_over_capacity(self) -> None:
        while len(self._job_order) > _MAX_RETAINED_JOBS:
            oldest = self._job_order[0]
            if oldest == self._active_job_id and not self._jobs[oldest].done:
                break  # never evict the in-flight job
            self._job_order.pop(0)
            self._jobs.pop(oldest, None)

    def start(
        self,
        scope: Scope,
        *,
        expected_fingerprint: str,
        skip_keys: set[str],
    ) -> ApplyJob:
        """Rebuild the scope's plan fresh, verify the fingerprint, then run the SELECTED actions.

        Raises :class:`PlanStaleError` if the freshly rebuilt plan's fingerprint no longer
        matches ``expected_fingerprint`` (config changed since the browser previewed it) —
        nothing is applied. Raises :class:`ApplyBusyError` if a job is already running.
        """
        from .actions.runner import run_plan
        from .plan import InstallPlan

        with self._lock:
            # .get() rather than a bare index: safe by construction even if a future eviction-
            # order change ever let the active id be evicted (today it can't — the active id is
            # always the newest appended and eviction pops from the front — but that invariant
            # was previously unenforced by the lookup itself; found in review).
            active = self._jobs.get(self._active_job_id) if self._active_job_id else None
            if active is not None and not active.done:
                raise ApplyBusyError("an apply job is already running")
            scope_plan = build_scope_plan(scope)
            _require_declared_config(scope_plan)
            _assert_unique_action_keys(scope_plan.plan)
            fresh_fingerprint = fingerprint_plan(scope_plan.plan)
            if fresh_fingerprint != expected_fingerprint:
                raise PlanStaleError(
                    "plan changed since preview — re-fetch /api/plan before applying"
                )

            all_keys = {action_key(a) for a in scope_plan.plan.actions}
            unknown_skips = skip_keys - all_keys
            if unknown_skips:
                # Fail CLOSED, not open: a skip_key matching nothing in the fresh plan (a client
                # bug, or a key from a stale/different preview) must NOT be silently dropped —
                # that would run an action the caller explicitly tried to skip. Every other
                # mismatch in this flow (fingerprint, scope, body shape) already fails closed;
                # this was the one place that didn't (found in review).
                raise PlanStaleError(
                    f"{len(unknown_skips)} skip_key(s) do not match any action in the current "
                    "plan — re-fetch /api/plan before applying"
                )
            selected = [a for a in scope_plan.plan.actions if action_key(a) not in skip_keys]
            skipped = [a for a in scope_plan.plan.actions if action_key(a) in skip_keys]
            run_this = InstallPlan(actions=selected, on_conflict=scope_plan.plan.on_conflict)

            job_id = uuid.uuid4().hex
            progress_rows = [
                ActionProgress(
                    key=action_key(a), kind=a.kind, category=a.category, item=a.item,
                    describe=a.describe(), status="queued",
                )
                for a in scope_plan.plan.actions
            ]
            by_key = {row.key: row for row in progress_rows}
            for a in skipped:
                by_key[action_key(a)].status = "skipped"
                by_key[action_key(a)].detail = "skipped by user"

            job = ApplyJob(
                id=job_id, scope_id=scope.id, fingerprint=fresh_fingerprint, actions=progress_rows
            )
            self._jobs[job_id] = job
            self._job_order.append(job_id)
            self._active_job_id = job_id
            self._evict_if_over_capacity()

        def _on_start(action) -> None:  # noqa: ANN001
            row = by_key.get(action_key(action))
            if row is not None:
                row.status = "running"

        def _progress(result) -> None:  # noqa: ANN001
            row = by_key.get(action_key(result.action))
            if row is not None:
                row.status = result.status
                row.detail = result.detail

        def _run() -> None:
            try:
                report = run_plan(run_this, on_start=_on_start, progress=_progress)
                # run_plan() NEVER raises for a per-action failure — actions/runner.py catches
                # each one into an ActionResult(status="error") and keeps going (see run_plan's
                # own docstring: "collect, never abort the whole run"). Without this check
                # `job.error` would only ever be set for a catastrophic run_plan crash, so a run
                # where every action failed would still report done+no-error — a "applied"
                # success toast over a fully-failed apply (caught in review).
                if report.errors:
                    failed = ", ".join(f"{r.action.category}/{r.action.item}" for r in report.errors)
                    job.error = f"{len(report.errors)} action(s) failed: {failed}"
            except Exception as exc:  # noqa: BLE001 — surface, never crash the daemon thread
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.done = True

        # The thread starts OUTSIDE the lock, but `run_this` is already a fully-built, immutable
        # InstallPlan snapshot captured INSIDE it above — a config edit landing between the lock
        # release and the thread actually running `run_plan` cannot change what gets applied; it
        # would only affect a LATER preview/apply call (raised in review as a possible TOCTOU;
        # traced and ruled out — `run_this` is never re-read from disk after this point).
        threading.Thread(target=_run, daemon=True, name=f"config-web-apply-{job_id[:8]}").start()
        return job

    def get(self, job_id: str) -> ApplyJob | None:
        return self._jobs.get(job_id)
