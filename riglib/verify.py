"""Post-apply verification framework — did each provisioned thing actually take effect?

What this is
------------
``rig init`` / ``rig apply`` write artifacts; this module CHECKS, right after, that they landed
and are live. It is the general framework the CTO asked for (tg#8010): every provisioner declares
its own check, and ``rig apply`` runs them all at the end, reporting pass/fail per provisioner and
exiting non-zero on failure. "should work" is not acceptance — this is how rig proves it did.

How it is reached
-----------------
``cli.cmd_apply`` (and the ``init --apply`` path) call :func:`verify_plan` on the just-applied
:class:`~riglib.plan.InstallPlan` and render the report. A verifier runs ONLY for a provisioner
that is actually in the plan (a check for a thing this config never provisioned is not run).

Adding a check to a new provisioner
-----------------------------------
Register one function keyed by the action ``kind`` with :func:`register_verifier` (or add to
:data:`_VERIFIERS`). It receives the :class:`~riglib.plan.Action` and returns a list of
:class:`VerifyResult`. That is the whole contract — the framework discovers, runs, and reports it.

Effectful by nature
-------------------
Unlike the pure planning modules, verifiers INSPECT the live system (stat files, ``launchctl
list``). A launchd check under ``RIG_*_DRY_RUN`` or on a non-macOS host returns a SKIPPED result
(``passed is None``), never a failure — so the hermetic suite / CI never fail on an environment
that legitimately can't run the live activation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .plan import Action, InstallPlan


@dataclass(frozen=True)
class VerifyResult:
    """One check outcome. ``passed`` is True/False, or ``None`` when the check was SKIPPED
    (not applicable on this host, or a live activation was suppressed by a dry-run flag)."""

    category: str
    item: str
    passed: bool | None
    evidence: str

    @property
    def state(self) -> str:
        if self.passed is None:
            return "skipped"
        return "pass" if self.passed else "FAIL"


@dataclass
class VerifyReport:
    results: list[VerifyResult] = field(default_factory=list)

    @property
    def failures(self) -> list[VerifyResult]:
        return [r for r in self.results if r.passed is False]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.state] = out.get(r.state, 0) + 1
        return out


Verifier = Callable[[Action], list[VerifyResult]]
_VERIFIERS: dict[str, Verifier] = {}

# How many matched dirs the spotlight verifier samples for the sentinel — keeps the verify read
# cheap on a huge tree (a full stat of every node_modules would be slow and pointless).
_VERIFY_SAMPLE_LIMIT = 20


def register_verifier(kind: str) -> Callable[[Verifier], Verifier]:
    """Register a verifier for an action ``kind``. One decorator = full framework participation."""

    def _register(fn: Verifier) -> Verifier:
        _VERIFIERS[kind] = fn
        return fn

    return _register


def verify_plan(plan: InstallPlan, applied: object = None) -> VerifyReport:
    """Run every registered verifier for the provisioners present in the plan.

    ``applied`` is the just-run apply results (``ApplyReport.results``): when given, a verifier
    runs ONLY for an action that was actually applied (status not ``error``/``skipped``) — so a
    stubbed or no-op action is not asserted to have taken effect (this is what keeps the hermetic
    suite from shelling out to the real launchctl for a test-stubbed daemon). When ``None`` (a
    direct/unit call), every provisioner in the plan is verified.

    Deliberate tradeoff: an ``already correct`` action ALSO reports ``skipped``, so a steady-state
    re-apply does not re-verify an unchanged artifact. That is acceptable — verification matters
    most on the apply that actually does the work (which reports ``created``/``updated`` and IS
    verified); "skipped" is indistinguishable from "stubbed/not-applied" by status alone, and
    erring toward not-asserting is what keeps the check honest and the suite hermetic.
    """
    skip = _skipped_actions(applied)
    report = VerifyReport()
    for action in plan.actions:
        verifier = _VERIFIERS.get(action.kind)
        if verifier is None or id(action) in skip:
            continue
        try:
            report.results.extend(verifier(action))
        except Exception as exc:  # noqa: BLE001 — a broken check must not abort the whole verify
            report.results.append(
                VerifyResult(action.category, action.item, False, f"{type(exc).__name__}: {exc}")
            )
    return report


def _skipped_actions(applied: object) -> set[int]:
    """The ``id()``s of actions whose apply result was ``error``/``skipped`` (don't verify those)."""
    if not applied:
        return set()
    skip: set[int] = set()
    for res in applied:  # type: ignore[union-attr]
        status = getattr(res, "status", "")
        action = getattr(res, "action", None)
        if action is not None and status in ("error", "skipped"):
            skip.add(id(action))
    return skip


# ── shared launchd helpers ────────────────────────────────────────────────────────────
def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _dry_run(env_var: str) -> bool:
    return os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes")


def _launchctl_loaded(label: str) -> bool:
    """True when ``launchctl list <label>`` exits 0 (the agent is loaded)."""
    try:
        res = subprocess.run(
            ["launchctl", "list", label], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _verify_launchd_agent(
    category: str, item: str, label: str, plist_path: Path, dry_run_var: str
) -> list[VerifyResult]:
    """Shared launchd check: the plist exists on disk AND the agent is loaded.

    On a non-macOS host there is NO launchd (the provisioner writes no plist by design), so the
    WHOLE check is a single SKIPPED result — asserting a missing plist there would be a false
    failure. On macOS the plist presence IS a real assertion; the loaded check is skipped only
    when the live activation was suppressed by ``dry_run_var``.
    """
    if not _is_darwin():
        return [VerifyResult(category, item, None, "launchd N/A on non-macOS host")]
    results: list[VerifyResult] = []
    exists = plist_path.is_file()
    results.append(
        VerifyResult(category, item, exists, f"plist {'present' if exists else 'MISSING'}: {plist_path}")
    )
    if _dry_run(dry_run_var):
        results.append(VerifyResult(category, item, None, f"launchd load check skipped ({dry_run_var} set)"))
        return results
    loaded = _launchctl_loaded(label)
    results.append(
        VerifyResult(category, item, loaded, f"launchd agent '{label}' {'loaded' if loaded else 'NOT loaded'}")
    )
    return results


# ── spotlight ─────────────────────────────────────────────────────────────────────────
@register_verifier("provision_spotlight")
def _verify_spotlight(action: Action) -> list[VerifyResult]:
    """The sentinel exists in a SAMPLE of matched dirs AND the launchd sweep agent is loaded."""
    from . import spotlight

    opts = action.options
    roots, deny, max_depth = spotlight.sweep_args_from_options(opts)
    results: list[VerifyResult] = []

    matched = spotlight.iter_target_dirs(roots, deny, max_depth)
    sample = matched[:_VERIFY_SAMPLE_LIMIT]
    if not sample:
        results.append(
            VerifyResult("spotlight", "sweep", None, "no dependency/build dirs found under roots (nothing to sample)")
        )
    else:
        missing = [d for d in sample if not spotlight.has_sentinel(d)]
        passed = not missing
        detail = f"{len(sample) - len(missing)}/{len(sample)} sampled matched dirs carry {spotlight.SENTINEL_NAME}"
        if missing:
            detail += f" — missing in e.g. {missing[0]}"
        results.append(VerifyResult("spotlight", "sweep", passed, detail))

    label = str(opts.get("label") or spotlight.DEFAULT_BOOT_LABEL)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    results.extend(
        _verify_launchd_agent("spotlight", "agent", label, plist_path, "RIG_SPOTLIGHT_DRY_RUN")
    )
    return results


# ── tmux (retrofit) ─────────────────────────────────────────────────────────────────────
@register_verifier("provision_tmux")
def _verify_tmux(action: Action) -> list[VerifyResult]:
    """The tmux boot agent's plist is present + loaded, and (evidence-only) has log paths set."""
    from . import tmux

    boot = action.options.get("boot", {})
    if isinstance(boot, dict) and boot.get("enabled") is False:
        return [VerifyResult("tmux", "boot", None, "tmux boot agent disabled — nothing to verify")]
    label = str((boot or {}).get("label") or tmux.DEFAULT_BOOT_LABEL)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    results = _verify_launchd_agent("tmux", "boot", label, plist_path, "RIG_TMUX_DRY_RUN")
    # Evidence-only (never a failure): does the plist wire StandardOut/ErrorPath logging? A boot
    # agent with no log path is a silent debugging hole. Reported so `rig apply` surfaces it,
    # without coupling this check to the plist's exact rendering.
    if plist_path.is_file():
        text = _read_text(plist_path)
        has_log = "StandardOutPath" in text and "StandardErrorPath" in text
        results.append(
            VerifyResult("tmux", "boot-logging", None,
                         f"boot plist log paths {'set' if has_log else 'NOT set (add StandardOut/ErrorPath)'}")
        )
    return results


# ── tg_ctl (retrofit) ────────────────────────────────────────────────────────────────
@register_verifier("provision_tg_ctl")
def _verify_tg_ctl(action: Action) -> list[VerifyResult]:
    """The tg-ctl daemon LaunchAgent plist is present + loaded."""
    from .tg_ctl import DEFAULT_BOOT_LABEL

    label = str(action.options.get("label") or DEFAULT_BOOT_LABEL)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return _verify_launchd_agent("tg_ctl", "boot", label, plist_path, "RIG_TG_CTL_DRY_RUN")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


__all__ = [
    "VerifyResult",
    "VerifyReport",
    "verify_plan",
    "register_verifier",
]
