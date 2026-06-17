"""Permission-allowlist provisioning — the per-harness command allowlist single source of truth.

What this is
------------
rig provisions each agent harness's permission ALLOWLIST so our ecosystem CLIs (``tg``,
``review``, ``draw``, ``3d``, ``rig``, ``task``) and the recommended external tools we rely on
(``gh``, ``git``, ``rg``, ``uv``, ``bun``, ``jq``, ``gitleaks``) are pre-allowed — the agent
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
- **gemini / pi** — N/A. Gemini's ``tools.core``/``coreTools`` is a TOOLSET RESTRICTION list, not
  a per-command auto-approve: setting it disables every unlisted built-in tool, so writing it to
  pre-allow ``git`` would BREAK the harness by removing read_file/write_file/etc. There is no
  separate per-command allowlist that leaves the toolset intact. Recorded N/A rather than risk it.

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
# Our ecosystem CLIs (tg/review/draw/3d/rig/task) and the external tools we lean on. ``task`` is
# alex-mextner/task-cli (the binary is ``task``). ``rg`` is ripgrep's binary name.
DEFAULT_ECOSYSTEM_TOOLS: tuple[str, ...] = ("tg", "review", "draw", "3d", "rig", "task")
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
# opencode's ``permission.bash`` object is the second. codex + gemini/pi have NO additively-
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
    "gemini": (
        "tools.core/coreTools is a TOOLSET RESTRICTION list, not a per-command auto-approve — "
        "writing it would disable every unlisted built-in tool; no safe per-command allowlist exists"
    ),
    "pi": "no documented command-allowlist mechanism",
}


def harness_supported(kind: str) -> bool:
    """True when rig can provision an allowlist for ``kind`` (else N/A — see HARNESS_ALLOWLIST_NA)."""
    return kind in HARNESS_ALLOWLISTS


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
