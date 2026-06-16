"""Codex CLI log-source parser.

On-disk layout (verified on this machine, 2026-06):
  ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl``
Each line is a JSON event with a top-level ISO ``timestamp`` and ``type``. The first line is
``type == "session_meta"`` whose ``payload.cwd`` is the session's working directory. Tool
calls are ``type == "response_item"`` with ``payload.type == "function_call"``, carrying
``payload.name`` (e.g. ``exec_command``, ``shell``) and ``payload.arguments`` — a JSON
STRING whose ``cmd``/``command`` is the shell command and ``workdir`` the cwd.

We map codex's shell tools onto our shell taxonomy so a codex ``exec_command`` running
``review`` is counted exactly like a CC ``Bash`` running ``review`` — the whole point is a
cross-harness adoption number.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..model import ToolInvocation
from ..taxonomy import categorize
from ._shellutil import detail_of, extract_command, is_shell_tool, parse_args_dict
from .base import LogSource, parse_iso, register


@register
class CodexSource(LogSource):
    name = "codex"

    def root(self) -> Path:
        return self.home / ".codex" / "sessions"

    def iter_invocations(self, *, repos: frozenset[str] | None = None) -> Iterator[ToolInvocation]:
        root = self.root()
        if not root.exists():
            return
        for session_file in sorted(root.glob("**/rollout-*.jsonl")):
            yield from self._iter_session(session_file, repos)

    def _iter_session(
        self, session_file: Path, repos: frozenset[str] | None
    ) -> Iterator[ToolInvocation]:
        session = session_file.stem
        cwd = "(unknown)"
        try:
            # errors="replace": a malformed multibyte must not raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) and abort the whole command. (review finding)
            with session_file.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    etype = event.get("type")
                    if etype == "session_meta":
                        meta_cwd = payload.get("cwd")
                        if isinstance(meta_cwd, str) and meta_cwd:
                            cwd = meta_cwd
                        continue
                    if payload.get("type") != "function_call":
                        continue
                    ts = parse_iso(event.get("timestamp"))
                    inv = self._invocation(payload, ts, cwd, session, repos)
                    if inv is not None:
                        yield inv
        except OSError:
            return

    def _invocation(
        self, payload: dict, ts, session_cwd: str, session: str, repos: frozenset[str] | None
    ) -> ToolInvocation | None:
        raw = str(payload.get("name", "") or "")
        if not raw:
            return None
        command = None
        repo = session_cwd
        if is_shell_tool(raw):
            args = parse_args_dict(payload.get("arguments"))
            command = extract_command(args)
            # prefer the call's OWN workdir over the session cwd: a codex session can run
            # commands across several worktrees, so per-call workdir is the correct repo.
            workdir = args.get("workdir") if isinstance(args, dict) else None
            if isinstance(workdir, str) and workdir:
                repo = workdir
            raw_for_tax = "bash"  # normalize onto the shared shell label for categorize()
        else:
            raw_for_tax = raw
        # repo pre-filter applied AFTER resolving the per-call workdir (the caller filters
        # authoritatively too, but this skips obviously out-of-scope calls cheaply).
        if repos is not None and repo not in repos:
            return None
        category, label = categorize(raw_for_tax, command=command)
        return ToolInvocation(
            timestamp=ts,
            harness=self.name,
            repo=repo,
            session=session,
            tool_name=label,
            category=category,
            raw_tool=raw,
            detail=detail_of(command),
        )
