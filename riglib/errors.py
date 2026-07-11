"""Error system v2 — every rig error is WHAT / WHY / HOW-to-fix, with a stable exit code.

Runtime reach: raised by config/catalog/plan/status code paths; rendered by the top-level
CLI handler (:func:`guard`, wired into ``cli.main``). Centralizing the shape here means a
human staring at a failure always gets the same three things — what happened, why (the root
cause, the offending config FILE PATH + key), and a concrete command/edit to fix it — and a
script always gets a meaningful, *stable* exit code per failure class.

Invariants:
- The exit-code constants below are a PUBLIC CONTRACT. Scripts/CI branch on them; changing a
  value is a breaking change. They are documented in ``rig --help`` and ``docs/config-schema.md``.
- They follow the ``structured-exit-codes`` skill: 0 success, 2 invalid-config/usage,
  127 missing-dependency (shell convention), plus rig-specific classes (drift/unknown-item/
  missing-target/not-a-repo) that a caller wants to distinguish.
- Stdlib-only at import time (AGENTS.md hard rule) — no third-party imports here.

History: born from two same-day prod failures whose errors were thin and undiagnosable —
``unknown mcp item(s): review (known: none)`` (no hint it was a REMOVED slot or how to remove
it) and a dead hook path surfacing only as a generic harness "PreToolUse error". This module
exists so neither recurs: the removed-slot registry names ``mcp.items.review`` and prints the
exact removal PR + fix, and missing-target names the gone file + how to regenerate it.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── stable, per-class exit codes (PUBLIC CONTRACT — do not renumber) ───────────────
EXIT_OK = 0
# 1 is reserved for an UNEXPECTED/internal failure (an unhandled exception): a caller can
# tell "rig itself crashed" (1) from "rig diagnosed a known problem" (>=2).
EXIT_INTERNAL = 1
EXIT_CONFIG = 2  # malformed / invalid config value (usage class, per the skill)
EXIT_DRIFT = 3  # `rig status` found drift (config and disk disagree)
EXIT_UNKNOWN_ITEM = 4  # config names a catalog item that doesn't exist (typo / removed slot)
EXIT_MISSING_TARGET = 5  # config references a path/binary that's gone on disk
EXIT_NOT_A_REPO = 6  # a repo-scoped command run outside a git repository
EXIT_REPO_CORRUPT = 7  # a managed checkout's git config is corrupted (e.g. core.bare=true)
EXIT_CODEX_UPDATE = 8  # Codex update failed or rollback needs attention
EXIT_MISSING_DEP = 127  # a required external tool/binary isn't installed (shell convention)


@dataclass
class RigError(Exception):
    """A structured, renderable error: WHAT happened / WHY / HOW to fix + an exit code.

    ``what`` — the symptom, one line ("unknown mcp item: reviewr").
    ``why``  — the root cause + context: the offending CONFIG FILE PATH + key when relevant.
    ``fix``  — a concrete command or edit the user can run/make right now.
    ``exit_code`` — the failure class (one of the EXIT_* constants); subclasses pin it.
    """

    what: str
    why: str = ""
    fix: str = ""
    exit_code: int = EXIT_INTERNAL

    def __post_init__(self) -> None:
        # Exception's own machinery wants args set; keep str(e) == the WHAT line.
        super().__init__(self.what)

    def __str__(self) -> str:
        return self.what


@dataclass
class ConfigError(RigError):
    """Malformed/invalid config — a bad value, type, or unknown key. Fail-closed (exit 2)."""

    exit_code: int = EXIT_CONFIG


@dataclass
class UnknownItemError(RigError):
    """Config names a catalog item that doesn't exist (typo or a removed slot). Exit 4."""

    exit_code: int = EXIT_UNKNOWN_ITEM


@dataclass
class MissingTargetError(RigError):
    """Config references a path/binary that's gone on disk (a dead hook path, …). Exit 5."""

    exit_code: int = EXIT_MISSING_TARGET


@dataclass
class NotARepoError(RigError):
    """A repo-scoped command was run outside a git repository. Exit 6."""

    exit_code: int = EXIT_NOT_A_REPO


@dataclass
class RepoCorruptError(RigError):
    """A managed checkout's git config is corrupted (e.g. core.bare=true on a work tree). Exit 7."""

    exit_code: int = EXIT_REPO_CORRUPT


@dataclass
class MissingDepError(RigError):
    """A required external tool/binary isn't installed. Exit 127 (shell convention)."""

    exit_code: int = EXIT_MISSING_DEP


# ── rendering ──────────────────────────────────────────────────────────────────────
def _c(code: str, s: str, color: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if color else s


def render(err: RigError, *, color: bool = True) -> str:
    """Render a :class:`RigError` as the consistent 3-part block (what / why / fix).

    Always shows the WHAT (prefixed ``error:``); shows WHY and FIX only when populated, so a
    terse error doesn't print empty labels. The label words ``why``/``fix`` always appear when
    their field is set — the contract the CLI handler and tests rely on.
    """
    lines = [_c("31", f"error: {err.what}", color)]
    if err.why:
        lines.append(_c("2", "  why: ", color) + err.why)
    if err.fix:
        lines.append(_c("32", "  fix: ", color) + err.fix)
    return "\n".join(lines)


def guard(fn) -> int:
    """Run ``fn`` and translate any :class:`RigError` into render() + its exit code.

    The single top-level CLI handler: a command body raises a structured error and this turns
    it into a consistent printed block + the stable per-class exit code. A non-RigError
    (a real bug) is NOT swallowed — it propagates so the stack trace is visible.
    """
    try:
        return fn()
    except RigError as exc:
        import sys

        print(render(exc, color=sys.stdout.isatty()))
        return exc.exit_code


# ── did-you-mean (Levenshtein) ──────────────────────────────────────────────────────
def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance, stdlib-only (small strings — item names — so O(n*m) is fine)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def did_you_mean(bad: str, candidates: set[str]) -> str | None:
    """The nearest catalog name to ``bad`` within a sensible edit-distance threshold.

    Returns ``None`` when the catalog is empty or nothing is close enough — so we never
    fabricate a bogus suggestion for a wildly different token. The threshold scales with the
    typo length (a longer name tolerates more edits) but is capped so "zzzzzzzz" → "review"
    is rejected.
    """
    if not candidates:
        return None
    best = min(candidates, key=lambda c: (_levenshtein(bad, c), c))
    dist = _levenshtein(bad, best)
    # allow up to ~40% of the longer string's length, capped at 3 — enough for a real typo,
    # tight enough to reject an unrelated word.
    threshold = min(3, max(1, round(0.4 * max(len(bad), len(best)))))
    return best if dist <= threshold else None


# ── removed / deprecated slot registry ──────────────────────────────────────────────
@dataclass(frozen=True)
class RemovedSlot:
    """A catalog slot that USED to exist and was removed — so its config key is now invalid.

    ``reason`` names WHY/WHEN it went (the PR + the rationale); the error builder turns it
    into a precise "remove ``<key>`` from ``<config path>``" fix instead of a useless
    "unknown item (known: none)".
    """

    category: str
    name: str
    reason: str


# Seeded with the motivating case. Keyed (category, name). Add an entry whenever a catalog
# slot is removed so a lingering config reference explains itself instead of looking like a typo.
_REMOVED_SLOTS: dict[tuple[str, str], RemovedSlot] = {
    ("mcp", "review"): RemovedSlot(
        category="mcp",
        name="review",
        reason="removed in agent-tools #32: review is a CLI + skill, not an MCP server "
        "(the `review --mcp` entrypoint was dropped in the subcommand refactor)",
    ),
}


def removed_slot(category: str, name: str) -> RemovedSlot | None:
    """Look up a (category, name) in the removed-slot registry; ``None`` if it was never a slot."""
    return _REMOVED_SLOTS.get((category, name))


# ── error builders (the heuristics, assembled into structured errors) ───────────────
def unknown_item_error(
    *,
    category: str,
    key: str,
    bad: str,
    known: set[str],
    config_path: str,
) -> UnknownItemError:
    """Build the error for a config that names a non-existent catalog item.

    Priority of explanation (most specific first):
      1. **removed slot** — the name was a real slot that got removed: cite the PR + tell the
         user to remove ``<key>`` from ``<config path>``.
      2. **empty category** — the catalog has NO slots in this category: tell them to remove the
         whole ``<category>`` block, NOT "known: none".
      3. **did-you-mean** — the catalog has slots and one is close: suggest it.
      4. **fallthrough** — list the known names so they can pick a valid one.

    ``key`` is the dotted config key (``mcp.items.review``), ``bad`` the bare item name
    (``review``), ``config_path`` the offending file — all three appear in the output.
    """
    removed = removed_slot(category, bad)
    if removed is not None:
        return UnknownItemError(
            what=f"removed {category} slot: {bad}",
            why=f"`{key}` ({removed.reason}); declared in {config_path}",
            fix=f"remove `{key}` from {config_path}",
        )

    if not known:
        return UnknownItemError(
            what=f"unknown {category} item: {bad}",
            why=f"the {category} catalog has no slots (declared in {config_path})",
            fix=f"remove the `{category}` block from {config_path} — there are no {category} "
            f"slots to enable",
        )

    suggestion = did_you_mean(bad, known)
    if suggestion is not None:
        return UnknownItemError(
            what=f"unknown {category} item: {bad}",
            why=f"`{key}` names an item not in the {category} catalog (declared in {config_path})",
            fix=f"did you mean `{suggestion}`? fix `{key}` in {config_path}",
        )

    known_list = ", ".join(sorted(known))
    return UnknownItemError(
        what=f"unknown {category} item: {bad}",
        why=f"`{key}` names an item not in the {category} catalog (declared in {config_path})",
        fix=f"use one of: {known_list} — or remove `{key}` from {config_path}",
    )


def missing_target_error(
    *,
    what_kind: str,
    target: str,
    why: str,
    regen: str,
) -> MissingTargetError:
    """Build the error for a config that points at a path/binary that's gone on disk.

    ``what_kind`` is a short noun ("hook", "binary", "skill"); ``target`` the missing path;
    ``why`` the root cause; ``regen`` how to recreate it (a concrete command).
    """
    return MissingTargetError(
        what=f"missing {what_kind}: {target}",
        why=why,
        fix=regen,
    )


def core_bare_error(*, repo: str, fix_cmd: str) -> RepoCorruptError:
    """Build the error for a working checkout whose ``core.bare`` is wrongly ``true``.

    ``repo`` is the corrupted checkout's path; ``fix_cmd`` the one-line repair. A non-bare working
    checkout with ``core.bare=true`` breaks every git op there (status/diff/commit/worktree) and
    ship's main-refresh — so this is loud and names the exact one-line fix.
    """
    return RepoCorruptError(
        what=f"corrupted git config (core.bare=true) on a working checkout: {repo}",
        why="this checkout has a working tree but its core.bare is true — every git operation "
        "here (status/diff/commit/worktree) and ship's main-refresh will fail with "
        "`fatal: this operation must be run in a work tree`",
        fix=fix_cmd,
    )
