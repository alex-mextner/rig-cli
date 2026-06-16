"""Shared shell-tool helpers for the harness parsers (kept DRY across all four sources).

Every harness exposes some shell tool under a different name (``Bash`` / ``exec_command`` /
``run_shell_command`` / ``bash``). These two helpers normalize that so each parser feeds the
SAME signal into the taxonomy: ``is_shell_tool`` decides whether a raw tool name is a shell,
and ``extract_command`` pulls the command string out of whatever shape that harness uses for
the tool input (``{"command": ...}`` / ``{"cmd": ...}`` / argv list / a JSON string).
"""

from __future__ import annotations

import json

from ..taxonomy import SHELL_TOOLS as SHELL_TOOL_NAMES  # single source of truth (no drift)

# how much of the command we keep as the ToolInvocation.detail (renderers truncate further).
DETAIL_MAX = 200


def is_shell_tool(raw_tool: str) -> bool:
    return raw_tool in SHELL_TOOL_NAMES


def parse_args_dict(args: object) -> dict:
    """Coerce a tool-input blob into a dict (parsing a JSON ``str`` if needed). Returns an
    empty dict on anything non-dict-shaped, so callers can ``.get("workdir")`` safely."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args if isinstance(args, dict) else {}


def extract_command(args: object) -> str | None:
    """Pull the command string out of a tool-input blob, across every shape we've seen:

    * ``{"command": "..."}`` (CC, gemini, opencode)
    * ``{"cmd": "..."}`` (codex)
    * either key holding an argv ``list`` → joined with spaces
    * a JSON ``str`` (codex ``arguments``) → parsed, then the above
    * a non-JSON ``str`` → returned as-is (best effort)
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args[:DETAIL_MAX]
    if not isinstance(args, dict):
        return None
    for key in ("command", "cmd"):
        val = args.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, list):
            return " ".join(str(x) for x in val)
    return None


def detail_of(command: str | None) -> str:
    """The ToolInvocation.detail value for a (possibly absent) command."""
    return (command or "")[:DETAIL_MAX]
