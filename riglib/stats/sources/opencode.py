"""opencode log-source parser.

On-disk layout (verified on this machine, 2026-06): opencode keeps BOTH a SQLite db and a
file-based JSON storage tree. We read the file tree (stdlib-only, no db lock contention):
  parts:    ``~/.local/share/opencode/storage/part/<msgID>/<partID>.json``
  sessions: ``~/.local/share/opencode/storage/session/<projectID>/<sessionID>.json``

A part with ``type == "tool"`` is a tool call: ``tool`` is the name (``bash``, ``read``,
``edit``, …), ``state.input`` the args (``state.input.command`` for ``bash``), and
``state.time.start`` an epoch-ms timestamp. The session file's ``directory`` field is the
repo path; we index sessionID → directory once up front.

The macOS default is ``~/.local/share/opencode``; honor ``XDG_DATA_HOME`` if set so this
also works on a configured Linux box.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..model import ToolInvocation
from ..taxonomy import categorize
from ._shellutil import detail_of, is_shell_tool
from .base import LogSource, parse_epoch, register


@register
class OpencodeSource(LogSource):
    name = "opencode"

    def _data_home(self) -> Path:
        # Honor XDG_DATA_HOME for REAL runs (it legitimately points outside HOME on a
        # configured Linux box). We consult os.environ only when ``self.home`` IS the real
        # process home — i.e. nobody passed a sandbox ``home=`` — so a HOME-isolated test
        # (which sets a tmp home) is never contaminated by the developer's own XDG var.
        if self.home == Path(os.path.expanduser("~")):
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg:
                return Path(xdg)
        return self.home / ".local" / "share"

    def root(self) -> Path:
        return self._data_home() / "opencode" / "storage"

    def iter_invocations(self, *, repos: frozenset[str] | None = None) -> Iterator[ToolInvocation]:
        storage = self.root()
        if not storage.exists():
            return
        session_dir = self._index_sessions(storage)
        part_root = storage / "part"
        if not part_root.exists():
            return
        for msg_dir in sorted(part_root.iterdir()):
            if not msg_dir.is_dir():
                continue
            for part_file in sorted(msg_dir.glob("*.json")):
                inv = self._invocation(part_file, session_dir, repos)
                if inv is not None:
                    yield inv

    def _index_sessions(self, storage: Path) -> dict[str, str]:
        """sessionID → directory, read once from storage/session/*/*.json."""
        out: dict[str, str] = {}
        session_root = storage / "session"
        if not session_root.exists():
            return out
        for sf in session_root.glob("*/*.json"):
            try:
                data = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                sid = data.get("id")
                directory = data.get("directory")
                if isinstance(sid, str) and isinstance(directory, str):
                    out[sid] = directory
        return out

    def _invocation(
        self, part_file: Path, session_dir: dict[str, str], repos: frozenset[str] | None
    ) -> ToolInvocation | None:
        try:
            # errors="replace": a malformed multibyte must not raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) and abort the whole command. (review finding)
            part = json.loads(part_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(part, dict) or part.get("type") != "tool":
            return None
        raw = str(part.get("tool", "") or "")
        if not raw:
            return None
        session = str(part.get("sessionID") or part_file.parent.name)
        repo = session_dir.get(session, "(unknown)")
        if repos is not None and repo not in repos:
            return None
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        command = inp.get("command") if isinstance(inp.get("command"), str) else None
        time_obj = state.get("time") if isinstance(state.get("time"), dict) else {}
        ts = parse_epoch(time_obj.get("start"))
        raw_for_tax = "bash" if is_shell_tool(raw) else raw
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
