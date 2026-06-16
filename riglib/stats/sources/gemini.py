"""Gemini CLI log-source parser.

On-disk layout (verified on this machine, 2026-06):
  ``~/.gemini/tmp/<project-hash>/chats/session-<ts>-<id>.json``
The file is ONE JSON object: ``{sessionId, projectHash, messages:[...]}``. Tool calls live
on ``messages[]`` of ``type == "gemini"``, in a ``toolCalls`` array of
``{id, name, args, result}``. The shell tool is ``run_shell_command`` with ``args.command``.
Each message carries its own ISO ``timestamp``.

Repo mapping: ``projectHash`` is an opaque hash, but ``~/.gemini/projects.json`` holds the
``{absolute-path: hash}`` map, which we invert to recover the real repo path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..model import ToolInvocation
from ..taxonomy import categorize
from ._shellutil import detail_of, extract_command, is_shell_tool
from .base import LogSource, parse_iso, register


@register
class GeminiSource(LogSource):
    name = "gemini"

    def root(self) -> Path:
        return self.home / ".gemini" / "tmp"

    def _hash_to_path(self) -> dict[str, str]:
        """Invert ``~/.gemini/projects.json`` → ``{hash: absolute_path}``."""
        projects = self.home / ".gemini" / "projects.json"
        out: dict[str, str] = {}
        try:
            data = json.loads(projects.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return out
        mapping = data.get("projects") if isinstance(data, dict) else None
        if isinstance(mapping, dict):
            for path, h in mapping.items():
                if isinstance(path, str) and isinstance(h, str):
                    out[h] = path
        return out

    def iter_invocations(self, *, repos: frozenset[str] | None = None) -> Iterator[ToolInvocation]:
        root = self.root()
        if not root.exists():
            return
        hash_to_path = self._hash_to_path()
        for session_file in sorted(root.glob("*/chats/session-*.json")):
            yield from self._iter_session(session_file, hash_to_path, repos)

    def _iter_session(
        self, session_file: Path, hash_to_path: dict[str, str], repos: frozenset[str] | None
    ) -> Iterator[ToolInvocation]:
        try:
            # errors="replace": a malformed multibyte must not raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) and abort the whole command. (review finding)
            data = json.loads(session_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        session = str(data.get("sessionId") or session_file.stem)
        proj_hash = str(data.get("projectHash") or "")
        repo = hash_to_path.get(proj_hash) or _decode_dir_name(session_file)
        if repos is not None and repo not in repos:
            return
        messages = data.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            # tool calls live on `type == "gemini"` messages (the module contract). Guard on
            # it so a future user/result/echo message shape carrying a `toolCalls` key can't
            # double-count or misattribute. (review finding)
            if msg.get("type") != "gemini":
                continue
            tool_calls = msg.get("toolCalls")
            if not isinstance(tool_calls, list):
                continue
            ts = parse_iso(msg.get("timestamp"))
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                inv = self._invocation(tc, ts, repo, session)
                if inv is not None:
                    yield inv

    def _invocation(self, tc: dict, ts, repo: str, session: str) -> ToolInvocation | None:
        raw = str(tc.get("name", "") or "")
        if not raw:
            return None
        command = None
        if is_shell_tool(raw):
            command = extract_command(tc.get("args"))
            raw_for_tax = "bash"
        else:
            raw_for_tax = raw
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


def _decode_dir_name(session_file: Path) -> str:
    """Fallback when projects.json lacks the hash: use the project-dir name (often a real
    name like ``3d-cli`` for known projects, a hash otherwise)."""
    # path is .../tmp/<dir>/chats/<file>; the <dir> is two parents up.
    return session_file.parent.parent.name or "(unknown)"
