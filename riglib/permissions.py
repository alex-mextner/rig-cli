"""Permission-allowlist provisioning — the per-harness command allowlist single source of truth.

What this is
------------
rig provisions each agent harness's permission ALLOWLIST so our ecosystem CLIs (``tg``,
``review``, ``draw``, ``3d``, ``rig``, ``task``, ``dev``, ``pm``, ``research``) and the
recommended external tools we
rely on (``gh``, ``git``, ``rg``, ``uv``, ``bun``, ``jq``, ``gitleaks``) are pre-allowed — the agent
never stops to ask permission for a known-safe command. The tool list is CONFIG-DRIVEN
(declared in ``rig.yaml`` / the global config under the ``permissions`` block) with a sensible
default set ON; this module is the registry that backs it and the renderer that turns one tool
name into the exact allowlist ENTRY each harness honors.

Why a module and not inline strings
------------------------------------
Each harness expresses "auto-allow command ``foo`` and its subcommands" in a DIFFERENT shape:

- **claude-code** — ``~/.claude/settings.json`` JSON, ``permissions.allow`` is a JSON ARRAY of
  strings; the entry is ``"Bash(foo:*)"`` (the proven prefix-glob form CC honors).
- **opencode** — ``~/.config/opencode/opencode.json`` JSON, ``permission.bash`` (singular
  ``permission``) is an OBJECT whose KEYS are command globs and whose VALUES are
  ``"allow"``/``"ask"``/``"deny"``; the entry is ``"foo *": "allow"``.
- **codex** — N/A. ``~/.codex/config.toml`` has no per-command allowlist; command execution is
  gated by ``approval_policy``/``sandbox_mode`` (coarse) and Starlark ``execpolicy`` ``.rules``
  files (``prefix_rule(pattern=[...], decision="allow")``) — a separate mechanism, not a config
  array rig can additively merge. Recorded N/A here.
- **pi** — N/A. No documented per-command auto-approve allowlist that leaves the toolset intact;
  recorded N/A rather than write a setting that could break the harness.

Keeping the per-harness shape behind :data:`HARNESS_ALLOWLISTS` means the plan/runner/drift code
keys off ``harness.kind`` exactly like the existing skill/hook provisioning, and a new harness is
one table entry plus its renderer — never scattered string literals.

Stdlib-only (the repo import rule): no yaml/json here; callers serialize.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# ── the default tool list — our ecosystem CLIs + the safe-to-allow external dev tools ────────
# CONFIG-DRIVEN: this is the DEFAULT set rig pre-allows; a config ``permissions.tools`` replaces
# it wholesale, and ``permissions.extra`` / ``permissions.disable`` apply deltas on top. The grant
# is at the command-PREFIX level (``Bash(<tool>:*)`` covers a tool's subcommands/flags — so ``git``
# does include ``git push --force``); the restraint is in WHICH tools are listed — dev/VCS tooling
# we already lean on, NEVER inherently-destructive standalone commands (``rm``/``sudo``/``dd``),
# which are deliberately absent so they stay behind a prompt.
#
# Our ecosystem CLIs (tg/review/draw/3d/rig/task/dev/pm/research) and the external tools we lean
# on. ``task`` is alex-mextner/task-cli (the binary is ``task``). ``dev`` is the agent-tools
# project-local development command surface: rig provisions the permission entry, while the dev
# helper's own implementation/provenance stays in agent-tools. ``pm`` (pm-cli) and ``research``
# (research-cli) are read-only ecosystem coordinators — a project-manager observer/reconciler and
# a multi-provider research/panel CLI; both observe and never edit code, matching the safe
# read-only profile of ``review``/``task``. ``rg`` is ripgrep's binary name.
DEFAULT_ECOSYSTEM_TOOLS: tuple[str, ...] = (
    "tg", "review", "draw", "3d", "rig", "task", "dev", "pm", "research",
)
DEFAULT_EXTERNAL_TOOLS: tuple[str, ...] = ("gh", "git", "rg", "uv", "bun", "jq", "gitleaks")
DEFAULT_TOOLS: tuple[str, ...] = DEFAULT_ECOSYSTEM_TOOLS + DEFAULT_EXTERNAL_TOOLS


def _render_claude_code(tool: str) -> str:
    """The claude-code ``permissions.allow`` entry that pre-allows command ``tool`` + its args.

    ``Bash(foo:*)`` is the prefix-glob form Claude Code honors for "any invocation of ``foo``"
    (the colon-``*`` is the documented trailing wildcard; it matches ``foo``, ``foo sub``,
    ``foo --flag x``). This MUST match the existing accumulated entries' shape (``Bash(gh:*)``,
    ``Bash(git:*)`` are already in the live settings) so a re-apply is a true dedup no-op.
    """
    return f"Bash({tool}:*)"


def _render_opencode(tool: str) -> str:
    """The opencode ``permission.bash`` KEY that pre-allows command ``tool`` + its args.

    opencode keys ``permission.bash`` by a command GLOB; ``"foo *"`` matches ``foo`` with any
    args. The VALUE is the literal ``"allow"`` (supplied by the merge code). The space form is
    opencode's documented pattern syntax (no colon form).
    """
    return f"{tool} *"


@dataclass(frozen=True)
class HarnessAllowlist:
    """How ONE harness expresses its command allowlist — the shape the runner/drift merge into.

    ``settings_path`` is the per-machine (user-scope) config file; ``key_path`` is the dotted
    path to the allowlist container within it; ``container`` is ``"array"`` (a JSON list of entry
    strings, claude-code) or ``"object"`` (a JSON object keyed by entry string → ``value``,
    opencode). ``render`` turns a tool name into the per-harness entry string; ``value`` is the
    object-form value (``"allow"``) and is ignored for the array form.
    """

    kind: str
    settings_path: str
    key_path: tuple[str, ...]
    container: str  # "array" | "object"
    render: Callable[[str], str]
    value: str | None = None


# The harness kinds rig can provision an allowlist for. claude-code is the primary, proven one
# (its ``permissions.allow`` array is exactly what the live ~/.claude/settings.json already uses);
# opencode's ``permission.bash`` object is the second. codex + pi have NO additively-
# mergeable per-command allowlist (see the module docstring) and are absent here → recorded N/A by
# :func:`harness_supported` / the harness matrix, never written.
HARNESS_ALLOWLISTS: dict[str, HarnessAllowlist] = {
    "claude-code": HarnessAllowlist(
        kind="claude-code",
        settings_path="~/.claude/settings.json",
        key_path=("permissions", "allow"),
        container="array",
        render=_render_claude_code,
    ),
    "opencode": HarnessAllowlist(
        kind="opencode",
        settings_path="~/.config/opencode/opencode.json",
        key_path=("permission", "bash"),
        container="object",
        render=_render_opencode,
        value="allow",
    ),
}

# Harness kinds that have NO additively-mergeable per-command allowlist concept → N/A in the
# matrix. Recorded explicitly (with the reason) so ``rig`` can report "N/A" rather than silently
# doing nothing or, worse, writing a setting that breaks the harness.
HARNESS_ALLOWLIST_NA: dict[str, str] = {
    "codex": (
        "no per-command allowlist in config.toml — command execution is gated by "
        "approval_policy/sandbox_mode (coarse) and Starlark execpolicy .rules files, a separate "
        "mechanism rig does not additively merge"
    ),
    "pi": "no documented command-allowlist mechanism",
}


def harness_supported(kind: str) -> bool:
    """True when rig can provision an allowlist for ``kind`` (else N/A — see HARNESS_ALLOWLIST_NA)."""
    return kind in HARNESS_ALLOWLISTS


# ── deny / ask baselines — the OUTER enforcement belt (rig-cli#100) ──────────────────────────
# CTO decision 2026-07-01: the harness permissions layer — deny, ask, AND allow — must be
# provisioned/reconciled by rig, not hand-edited. Claude Code evaluates permission rules
# deny → ask → allow (first match wins) BEFORE PreToolUse hooks and independently of the model,
# and a user-scope deny cannot be overridden by a project-level allow — that makes these lists
# the OUTER belt; the argv-parsing agent-hooks (block-no-verify, block-raw-pr-merge, …) stay the
# deep layer underneath (they parse flags anywhere in argv, which prefix patterns cannot).
#
# The baseline is deliberately CONSERVATIVE and word-boundary precise: a deny rule that
# false-positives on legitimate commands teaches agents to route around the belt — worse than no
# rule. Verified matcher semantics (code.claude.com/docs/en/permissions, fetched 2026-07-01):
#   - ``Bash(x:*)`` — ``:*`` is the trailing word-boundary wildcard, equal to ``Bash(x *)``:
#     matches ``x`` and ``x <args>`` but never ``x2`` (boundary = space or end-of-string).
#   - a mid-pattern ``*`` matches ANY char sequence including spaces; literal `` --flag `` around
#     it keeps the boundary (``git push * --force *`` matches ``git push origin main --force``
#     but NOT ``git push --force-with-lease …`` — ``-with-lease`` breaks the boundary).
#   - compound commands are matched per subcommand (``a && b`` evaluates both independently).
#
# WHAT STAYS HOOK-ONLY (and why): ``git commit --no-verify`` with the flag in a LATER position
# (``git commit -m "…" --no-verify``, the common shape) cannot be pattern-matched safely — the
# only pattern that would catch it (``Bash(git commit *--no-verify*)``) also matches a commit
# MESSAGE that merely mentions the flag (this ecosystem writes such messages), a guaranteed
# false positive. The flag-first prefix rule below is the safe subset; the ``block-no-verify``
# agent-hook (argv-level) remains the authoritative guard. The same applies to wrapper bypasses
# in general (``sh -c '…'``, env-runner wrappers): prefix rules anchor at the command start, so
# the hooks stay the deep layer — permissions and hooks are complementary, not redundant.
CLAUDE_CODE_DENY_RULES: tuple[str, ...] = (
    # raw PR merges are banned machine-wide — merges go through `gh ship` (the gated delegator)
    "Bash(gh pr merge:*)",
    # force pushes: flag-first, mid-position AND end-anchored forms; `--force-with-lease` (the
    # safe force) is deliberately NOT matched — the word boundary after `--force` / `-f` excludes
    # it. The end-anchored forms (`… * --force`) are listed EXPLICITLY even though the docs say a
    # trailing ` *` also matches end-of-string — the common `git push origin main --force` must
    # not hinge on that one reading of the matcher (review finding, rig-cli#100).
    "Bash(git push --force:*)",
    "Bash(git push * --force *)",
    "Bash(git push * --force)",
    "Bash(git push -f:*)",
    "Bash(git push * -f *)",
    "Bash(git push * -f)",
    # hook-bypass commits — flag-first prefix only (see the module note above for the gap)
    "Bash(git commit --no-verify:*)",
    # no legitimate agent flow removes files as root
    "Bash(sudo rm:*)",
    # screenshots go through Playwright/CDP; `screencapture` black-frames windows on other
    # Spaces and trips macOS Screen Recording grants (the documented hard rule)
    "Bash(screencapture:*)",
)

# ask = sometimes-legit: force a prompt (tg-ctl relays it to the operator's phone), don't block.
CLAUDE_CODE_ASK_RULES: tuple[str, ...] = (
    # broad pattern-kills have nuked OTHER sessions' work before (never-broad-pkill doctrine);
    # reaping one's OWN strays is legit — hence ask, not deny
    "Bash(pkill:*)",
    "Bash(killall:*)",
    # `git reset --hard` has destroyed uncommitted work before; flag-first, mid + end-anchored
    "Bash(git reset --hard:*)",
    "Bash(git reset * --hard *)",
    "Bash(git reset * --hard)",
)

# The baked rule baseline per harness kind. Only claude-code: its rule syntax above is the one
# whose matcher semantics we verified against the vendor docs; other kinds are absent (empty).
DEFAULT_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "claude-code": {"deny": CLAUDE_CODE_DENY_RULES, "ask": CLAUDE_CODE_ASK_RULES},
}

# Where each rule list lives in the harness settings file. claude-code only: opencode's
# ``permission.bash`` object DOES accept "deny"/"ask" values, but its glob dialect for
# multi-word / mid-wildcard rules is UNVERIFIED — a deny you believe in but that never matches
# is worse than a reported gap, so it is recorded N/A (mirroring HARNESS_ALLOWLIST_NA) until
# someone verifies the dialect and adds a renderer.
HARNESS_RULE_CONTAINERS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude-code": {"deny": ("permissions", "deny"), "ask": ("permissions", "ask")},
}
HARNESS_RULES_NA: dict[str, str] = {
    "opencode": (
        "permission.bash accepts deny/ask values, but its glob dialect for multi-word rules "
        "is unverified — not provisioned until proven"
    ),
}


def resolve_rules(kind: str, role: str, override: list[str] | None) -> list[str]:
    """The effective ``role`` (``deny``/``ask``) rule list for harness ``kind``.

    ``override`` (the config's ``permissions.deny``/``permissions.ask``) REPLACES the baked
    default wholesale — lists are atomic decisions, mirroring ``permissions.tools`` — so an
    explicit ``[]`` disables the baseline. ``None`` (absent key) selects the default. Deduped,
    first-seen order, so the merged container stays stable across re-applies. A kind without
    rule containers has no defaults (and the plan drops a configured override with a note).
    """
    base = list(override) if override is not None else list(DEFAULT_RULES.get(kind, {}).get(role, ()))
    out: list[str] = []
    seen: set[str] = set()
    for rule in base:
        if rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


def resolve_tools(
    tools: list[str] | None,
    extra: list[str] | None,
    disable: list[str] | None,
) -> list[str]:
    """Resolve the effective tool list: (``tools`` or the default set) + ``extra`` − ``disable``.

    Deterministic + de-duplicated, preserving first-seen order so the rendered allowlist is stable
    across re-applies (no churn from set ordering). An explicit ``tools`` REPLACES the default set
    (lists are atomic decisions, mirroring the config cascade); ``extra`` adds; ``disable`` drops a
    tool from rig's DESIRED set so rig won't add it — it does NOT remove an entry already in the
    user's on-disk allowlist (the merge is additive-only; rig never deletes the user's entries).
    """
    base = list(tools) if tools is not None else list(DEFAULT_TOOLS)
    base += list(extra or [])
    removed = set(disable or [])
    out: list[str] = []
    seen: set[str] = set()
    for t in base:
        if t in removed or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def desired_entries(kind: str, tools: list[str]) -> list[str]:
    """The per-harness allowlist entry strings for ``tools``, in tool order (deduped).

    Raises ``KeyError`` for an unsupported kind — callers gate on :func:`harness_supported` first
    (the plan only emits supported kinds), so this is a defensive guard.
    """
    spec = HARNESS_ALLOWLISTS[kind]
    out: list[str] = []
    seen: set[str] = set()
    for t in tools:
        entry = spec.render(t)
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out
