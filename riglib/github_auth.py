"""GitHub auth gate for the repo-settings provisioners — the #4136.1 "ASK and WAIT" requirement.

WHAT THIS IS. Every ``github.*`` provisioner reconciles a setting through an authenticated admin
session — either ``gh api`` (needs a token with ``repo``/admin scope on the repo) or an
``agent-browser`` session driving the GitHub settings UI (needs a logged-in browser). The CTO
decision #4136.1 is explicit: rig must NOT silently fail when that auth is missing. It must PROMPT
the user to log in (``gh auth login`` / browser login) and BLOCK/WAIT until auth is present, then
RESUME. This module is that gate: a single ``ensure_gh_auth`` (and ``ensure_browser_auth``) the
provisioners call BEFORE any mutation, which

  1. checks whether the required auth is present (``gh auth status`` / a browser-session probe),
  2. if missing, NOTIFIES the user via ``tg`` (the user's phone) with the exact command to run,
  3. POLLS for auth to appear, blocking until it does or a bounded deadline elapses,
  4. returns an :class:`AuthOutcome` the caller switches on (``ok`` → proceed; ``timed_out`` /
     ``unavailable`` → degrade LOUDLY, never a silent green).

WHY A SEPARATE MODULE (not inline in the runner). The probe/notify/poll loop is shared by every
``github.*`` action (ruleset, merge, ghas, actions) and by the agent-browser backend, and it has
real subprocess + sleep side effects that the tests must seam out. Keeping it here, behind narrow
monkeypatchable functions (``_gh_auth_ok``, ``_notify``, ``_sleep``, ``_now``), lets every test
drive it deterministically: a test sets ``RIG_GH_AUTH_WAIT=0`` (the default in CI/tests) so the
gate does ONE probe and returns without ever sleeping or notifying a real phone, and a test of the
wait path monkeypatches ``_gh_auth_ok`` to flip from False→True across polls.

THE DEFAULT IS NON-BLOCKING IN AUTOMATION. ``RIG_GH_AUTH_WAIT`` controls the max seconds to wait:
unset/``0`` → do not block (probe once, return immediately) so an unattended ``rig apply`` in CI
never hangs forever waiting for a human; a positive value → block up to that many seconds, polling
every ``RIG_GH_AUTH_POLL`` seconds, having notified once. An interactive operator who WANTS the
"ask and wait" behavior sets ``RIG_GH_AUTH_WAIT`` to a real budget (e.g. ``1800`` for 30 min). The
notify-and-wait is opt-in by budget precisely so the autonomous path is never wedged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

# Gate kinds (``gh`` / ``browser``) that have ALREADY notified-and-timed-out in THIS process. An
# ``rig apply`` runs the auth gate once per github.* action (~5×); without this, a still-missing
# login at a positive RIG_GH_AUTH_WAIT budget would notify the user AND block the full budget on
# EACH action — up to ~5 phone pushes and budget×5 of waiting. Once one action has exhausted the
# wait for a kind, the rest short-circuit to an immediate loud ``timed_out`` (no re-notify, no
# re-wait): the user was already told once and rig already waited; re-pinging adds nothing. Reset
# via :func:`reset_auth_gate` (the apply entry point clears it so a later run starts fresh).
_TIMED_OUT_KINDS: set[str] = set()


def reset_auth_gate() -> None:
    """Forget which gate kinds already timed out — call once at the start of an apply run.

    So a fresh ``rig apply`` re-notifies + re-waits (the user may have logged in since), but WITHIN
    one apply the per-action gate doesn't spam. Idempotent; safe to call when the set is empty.
    """
    _TIMED_OUT_KINDS.clear()


@dataclass(frozen=True)
class AuthOutcome:
    """The result of an auth gate — what the caller switches on.

    ``state`` is one of:
      - ``ok``          — the required auth is present: proceed with the mutation.
      - ``timed_out``   — auth was missing and did not appear within the wait budget: the caller
                          degrades LOUDLY (a visible error, never a silent green).
      - ``unavailable`` — the auth tool itself is missing (no ``gh`` binary / no ``agent-browser``):
                          the caller degrades loudly too. Distinct from ``timed_out`` so the message
                          can name the missing tool.
    ``detail`` is a human one-liner (the exact ``gh auth login`` command, or the missing-tool note).
    ``notified`` records whether the user was pinged (so the caller doesn't double-notify).
    """

    state: str
    detail: str = ""
    notified: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _wait_budget() -> float:
    """Max seconds to block waiting for auth. 0 (default) → do not block; probe once and return.

    Kept unset/0 in CI and tests so an unattended apply never hangs; an interactive operator opts
    into the "ask and wait" behavior by exporting a real budget (e.g. ``RIG_GH_AUTH_WAIT=1800``).
    """
    raw = os.environ.get("RIG_GH_AUTH_WAIT", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        return 0.0


def _poll_interval() -> float:
    """Seconds between auth re-probes while blocking. Defaults to 5s; floored at 1s."""
    raw = os.environ.get("RIG_GH_AUTH_POLL", "").strip()
    try:
        return max(1.0, float(raw)) if raw else 5.0
    except ValueError:
        return 5.0


def _now() -> float:
    """Monotonic clock seam (tests monkeypatch this so they never depend on real wall-clock)."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Sleep seam (tests monkeypatch this so the poll loop never actually blocks)."""
    time.sleep(seconds)


def _gh_auth_ok() -> bool:
    """True iff ``gh`` is installed AND reports an authenticated, valid github.com session.

    The single auth probe seam — tests monkeypatch THIS, so no test spawns ``gh`` or hits the
    network. A missing binary → False (the caller maps it to ``unavailable`` with a clear message).
    ``gh auth status`` exits 0 only when at least one host has a VALID active token, so its exit
    code is the auth signal. (Scope adequacy — ``repo`` for admin — is enforced by the API call
    itself returning 403, which the action surfaces; this gate covers the "no token at all" case
    the #4136.1 requirement is about.)
    """
    if not shutil.which("gh"):
        return False
    try:
        res = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _browser_auth_ok() -> bool:
    """True iff ``agent-browser`` is installed (a logged-in GitHub session is probed by the action).

    The CLI presence is the gate this module enforces; whether the running browser session is
    actually logged into GitHub is a per-action concern the agent-browser backend reports. Kept a
    seam so tests never require the binary.
    """
    return shutil.which("agent-browser") is not None


def _notify(message: str) -> bool:
    """Ping the user's phone via ``tg`` so they know to log in. Returns True iff the send launched.

    Best-effort + non-fatal: a missing ``tg`` or a send failure must NOT crash the apply (the gate
    still degrades loudly via its return value). Tests monkeypatch this so no real message is sent.
    Tagged ``PROBLEM`` because a blocked apply is exactly the "rig needs you" signal the user wants
    surfaced, with an explicit title so the report isn't a context-free one-liner.
    """
    if not shutil.which("tg"):
        return False
    try:
        res = subprocess.run(
            ["tg", "--tag", "problem", "--title", "rig: GitHub login needed", message],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # A non-zero exit (tg misconfigured / send rejected) must NOT read as "notified" — otherwise an
    # auth wait would silently proceed believing the user was pinged when they never were.
    return res.returncode == 0


def ensure_gh_auth(*, owner: str, repo: str) -> AuthOutcome:
    """Block until ``gh`` is authenticated for admin on ``owner/repo``, or return a loud failure.

    The #4136.1 gate for the ``gh api`` backend. If ``gh`` is already authed → ``ok`` immediately
    (zero side effects: no notify, no sleep). If not, NOTIFY the user once with the exact
    ``gh auth login`` command, then POLL up to the ``RIG_GH_AUTH_WAIT`` budget (re-probing every
    ``RIG_GH_AUTH_POLL`` seconds) for auth to appear. Returns ``ok`` the moment it does, else
    ``timed_out`` (or ``unavailable`` if ``gh`` isn't installed at all). The caller NEVER proceeds
    to a mutation on a non-``ok`` outcome — it surfaces the detail as a visible error.
    """
    if _gh_auth_ok():
        return AuthOutcome("ok")
    if not shutil.which("gh"):
        return AuthOutcome(
            "unavailable",
            detail="gh CLI not found on PATH — install it and run `gh auth login` to provision "
            f"GitHub settings on {owner}/{repo}",
        )
    cmd = "gh auth login -h github.com -s repo"
    message = (
        f"rig needs an admin-authenticated `gh` to provision repository settings on "
        f"{owner}/{repo}, but `gh` is not logged in.\n\nRun:\n    {cmd}\n\n"
        "rig is WAITING and will resume automatically once you're logged in."
    )
    return _wait_for(
        kind="gh",
        probe=_gh_auth_ok,
        notify_message=message,
        timeout_detail=(
            f"gh is not authenticated — run `{cmd}` and re-run `rig apply commit` to provision "
            f"settings on {owner}/{repo}"
        ),
    )


def ensure_browser_auth(*, owner: str, repo: str) -> AuthOutcome:
    """Block until an ``agent-browser`` session is available, or return a loud failure.

    The #4136.1 gate for the ``agent-browser`` backend (settings the API does not expose). Mirrors
    :func:`ensure_gh_auth`: present → ``ok``; absent → notify with the browser-login instruction
    and poll the same budget; never silently proceed. The per-page "is this session logged into
    GitHub" check is the agent-browser action's job — this gate only ensures the tool is present
    and gives the user the chance to log in before the action drives the UI.
    """
    if _browser_auth_ok():
        return AuthOutcome("ok")
    message = (
        f"rig needs `agent-browser` (a logged-in GitHub browser session) to provision the "
        f"repository settings on {owner}/{repo} that the API does not expose, but it is not "
        "available.\n\nInstall agent-browser and log into github.com in it, then rig will resume."
    )
    return _wait_for(
        kind="browser",
        probe=_browser_auth_ok,
        notify_message=message,
        timeout_detail=(
            "agent-browser is not available — install it and log into github.com to provision "
            f"the API-unreachable settings on {owner}/{repo}"
        ),
    )


def _wait_for(*, kind: str, probe, notify_message: str, timeout_detail: str) -> AuthOutcome:
    """Shared notify-then-poll loop. Notify (only if we'll wait), then re-``probe`` until True or the
    budget elapses.

    PER-PROCESS DEDUP. ``rig apply`` runs this gate once per github.* action for the same ``kind``.
    If a previous action of this kind already NOTIFIED and WAITED OUT the budget, re-running the full
    notify+wait for every later action would fire ~5 pushes and block budget×5 (e.g. 30 min × 5).
    So once a kind has timed out in this process (:data:`_TIMED_OUT_KINDS`), later actions of that
    kind short-circuit to an immediate ``timed_out`` — no re-notify, no re-wait. (The apply entry
    point calls :func:`reset_auth_gate` so a fresh run re-notifies; the user may have logged in.)

    NOTIFY ONLY WHEN WE WILL ACTUALLY WAIT (budget > 0). At the 0 budget (the CI/test/unattended
    default) there is nothing to ping the user ABOUT — rig isn't going to block for a login, it
    degrades immediately. At 0 budget "loud" is the visible error in the ActionResult, NOT a phone
    push; the notify is reserved for the interactive "ask and WAIT" path (positive budget). With a
    positive budget this blocks, re-probing every poll interval, returning ``ok`` the instant auth
    appears.
    """
    # already gave up on this kind earlier in this apply → degrade immediately, no spam, no re-wait
    if kind in _TIMED_OUT_KINDS:
        return AuthOutcome("timed_out", detail=timeout_detail, notified=False)
    budget = _wait_budget()
    notified = _notify(notify_message) if budget > 0 else False
    interval = _poll_interval()
    deadline = _now() + budget
    while True:
        if probe():
            return AuthOutcome("ok", notified=notified)
        if _now() >= deadline:
            _TIMED_OUT_KINDS.add(kind)  # remember so sibling actions don't re-notify/re-wait
            return AuthOutcome("timed_out", detail=timeout_detail, notified=notified)
        _sleep(min(interval, max(0.0, deadline - _now())))
