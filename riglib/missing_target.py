"""missing-target scanner — find config that references a path/binary gone from disk.

Runtime reach: called by ``rig status`` (and surfaced by ``rig doctor``) to PROACTIVELY catch
a dead reference before it bites at runtime. The motivating case: the harness ``settings.json``
registers a hook whose ``command`` invokes a script that no longer exists — at runtime the
harness reports only a generic "PreToolUse error" with no hint of the cause. This scanner names
the missing file + how to regenerate/remove it, as a structured :class:`errors.MissingTargetError`.

Scope: it inspects the harness ``settings.json`` hook blocks. It only flags an ABSOLUTE path
argument that looks like a script file (so a bare PATH-resolved binary like ``gitleaks`` is not
mistaken for a missing file). Stdlib-only.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from . import errors


def _looks_like_script_path(token: str) -> bool:
    """True for an absolute path that names a file we should check exists.

    Only absolute paths qualify — a bare ``gitleaks`` (PATH-resolved) is not a file reference
    we own. We don't require a particular suffix (hooks can be ``.py``/``.sh``/extensionless),
    just that it's an absolute path token, not a flag/option.
    """
    if not token or token.startswith("-"):
        return False
    return token.startswith("/") or token.startswith("~/")


# Interpreter basenames whose FIRST path-looking argument is the script to check (so the
# interpreter token itself — even an absolute one like /usr/bin/env or /opt/homebrew/bin/python3
# — is skipped, not mistaken for the target). env is special-cased below.
_INTERPRETERS = frozenset(
    {"python", "python3", "python2", "sh", "bash", "zsh", "node", "deno", "bun", "ruby", "perl", "env"}
)


def _missing_paths_in_command(command: str) -> list[str]:
    """Return the missing SCRIPT path of a hook command (at most one), or [].

    A hook command runs ONE script. It may be invoked directly (``/abs/hook.py``) or through an
    interpreter — and that interpreter may itself be an ABSOLUTE path on macOS
    (``/usr/bin/env python3 /abs/hook.py``, ``/opt/homebrew/bin/python3 /abs/hook.py``). We must
    check the SCRIPT, not the interpreter: skip a leading interpreter token (bare or absolute)
    plus its ``env VAR=…`` / module / ``-c`` style args, then take the first remaining
    script-looking token as the target. We check only THAT token — a later absolute path is
    almost always a runtime OUTPUT arg (``--out /var/run/x.json``) that legitimately doesn't
    exist yet; flagging it would make ``rig status``/``doctor`` cry wolf on a healthy hook.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []  # unparseable command (unbalanced quotes) — skip rather than guess
    if not tokens:
        return []

    rest = tokens
    # If the command runs through a known interpreter, drop the interpreter token. The script is
    # then the first path-looking arg AFTER it; we skip the interpreter's own flags / `env`
    # NAME=VALUE assignments by scanning for the first script-PATH token among the remaining args.
    head = Path(tokens[0]).name if (tokens[0].startswith("/") or tokens[0].startswith("~/")) else tokens[0]
    via_interpreter = head in _INTERPRETERS
    if via_interpreter:
        rest = tokens[1:]

    for tok in rest:
        # `-c '<inline code>'` as an INTERPRETER flag (i.e. before any script token) means there
        # is no script FILE to verify. Only honor it before the first path — a `-c` AFTER the
        # script is the script's OWN argument, not Python's, and must not suppress the check.
        if via_interpreter and tok == "-c":
            return []
        if _looks_like_script_path(tok):
            p = Path(tok).expanduser()
            return [tok] if not p.exists() else []
    return []


def _iter_hook_commands(settings: dict):
    """Yield every hook ``command`` string under the settings.json ``hooks`` blocks.

    Tolerant of shape drift: a missing/oddly-typed key is skipped, never raised — a malformed
    settings.json should make the scan find nothing, not crash ``rig status``.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for blocks in hooks.values():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for hook in block.get("hooks", []) or []:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    yield hook["command"]


def scan_settings_hooks(settings_file: Path) -> list[errors.MissingTargetError]:
    """Scan a harness ``settings.json`` for hook commands pointing at missing files.

    Returns one :class:`errors.MissingTargetError` per missing target (deduped by path), each
    naming the gone file, where it's referenced, and how to fix it. An absent or malformed
    settings.json yields an empty list (nothing to report, never an exception).
    """
    if not settings_file.is_file():
        return []
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    seen: set[str] = set()
    findings: list[errors.MissingTargetError] = []
    for command in _iter_hook_commands(data):
        for missing in _missing_paths_in_command(command):
            if missing in seen:
                continue
            seen.add(missing)
            findings.append(
                errors.missing_target_error(
                    what_kind="hook script",
                    target=missing,
                    why=f"a hook command in {settings_file} runs `{missing}`, but that file "
                    f"is gone — the harness would fail with a generic PreToolUse error",
                    regen="re-run `rig apply` to reinstall the managed hooks, or remove the "
                    f"stale hook entry from {settings_file}",
                )
            )
    return findings
