"""Claude Code log-source parser — the primary, richest source.

On-disk layout (verified on this machine, 2026-06):
  ``~/.claude/projects/<encoded-path>/<session-uuid>.jsonl``
Each line is one JSON event. The ones we care about have ``type == "assistant"`` and a
``message.content`` array; tool calls are the array entries with ``type == "tool_use"``,
carrying ``name`` (the tool) and ``input`` (its args — ``input.command`` for Bash,
``input.skill`` for the Skill tool). Every event also carries a top-level ISO ``timestamp``
and a ``cwd``.

Repo mapping: the directory name is the project path with every ``/`` (and any literal
``.``/``_`` that was a path separator) flattened to ``-`` — it is LOSSY (you can't tell a
``-`` that was a dash from one that was a slash). So we DON'T trust the decoded name as the
repo; we read the real absolute path from the event's ``cwd`` field and fall back to a
best-effort decode of the dir name only when no event carries a cwd.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..model import ToolInvocation
from ..taxonomy import categorize
from ._shellutil import detail_of
from .base import LogSource, parse_iso, register


@register
class ClaudeCodeSource(LogSource):
    name = "claude-code"

    def root(self) -> Path:
        return self.home / ".claude" / "projects"

    def _decode_dir(self, encoded: str) -> str:
        """Best-effort: ``-Users-ultra-xp-rig-cli`` → ``/Users/ultra/xp/rig-cli``. Lossy;
        only used when no event in the session carries a real ``cwd``."""
        if encoded.startswith("-"):
            return "/" + encoded.lstrip("-").replace("-", "/")
        return encoded

    def iter_invocations(self, *, repos: frozenset[str] | None = None) -> Iterator[ToolInvocation]:
        root = self.root()
        if not root.exists():
            return
        for proj_dir in sorted(root.iterdir()):
            if not proj_dir.is_dir():
                continue
            decoded = self._decode_dir(proj_dir.name)
            for session_file in sorted(proj_dir.glob("*.jsonl")):
                yield from self._iter_session(session_file, decoded, repos)

    def _iter_session(
        self, session_file: Path, decoded_dir: str, repos: frozenset[str] | None
    ) -> Iterator[ToolInvocation]:
        session = session_file.stem
        # cwd is per-event; cache the last-seen one so tool events that omit it still map.
        cwd = decoded_dir
        try:
            # errors="replace": a malformed multibyte must not raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) and abort the whole `rig stats` command — one bad
            # byte degrades to U+FFFD, the rest of the log still parses. (review finding)
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
                    ev_cwd = event.get("cwd")
                    if isinstance(ev_cwd, str) and ev_cwd:
                        cwd = ev_cwd
                    if repos is not None and cwd not in repos:
                        # cheap pre-filter; the caller still filters authoritatively.
                        continue
                    if event.get("type") != "assistant":
                        continue
                    msg = event.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    ts = parse_iso(event.get("timestamp"))
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        yield self._invocation(block, ts, cwd, session)
        except OSError:
            return

    def _invocation(self, block: dict, ts, cwd: str, session: str) -> ToolInvocation:
        raw = str(block.get("name", "") or "")
        inp = block.get("input")
        inp = inp if isinstance(inp, dict) else {}
        command = inp.get("command") if isinstance(inp.get("command"), str) else None
        skill = inp.get("skill") if isinstance(inp.get("skill"), str) else None
        category, label = categorize(raw, command=command, skill=skill)
        detail = detail_of(command or (f"skill:{skill}" if skill else ""))
        return ToolInvocation(
            timestamp=ts,
            harness=self.name,
            repo=cwd,
            session=session,
            tool_name=label,
            category=category,
            raw_tool=raw,
            detail=detail,
        )
