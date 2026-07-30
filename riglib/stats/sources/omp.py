"""omp (Oh My Pi harness) log-source parser.

On-disk layout (verified on this machine, 2026-07):
  ``~/.omp/agent/sessions/<encoded-cwd>/<timestamp>_<session-uuid>.jsonl`` — the main agent
  ``~/.omp/agent/sessions/<encoded-cwd>/<session-stem>/<agent-name>.jsonl`` — per-SUBAGENT
      transcripts, disjoint from the main one (the main file holds only the main agent's
      calls), so they must be walked recursively or most of the activity is dropped.
The agent dir root is ``~/.omp/agent`` by default; omp profiles re-point it through
``$PI_CODING_AGENT_DIR`` (a full override), and ``$PI_CONFIG_DIR`` renames the ``.omp``
config-root dirname (both honored like opencode's ``XDG_DATA_HOME`` — only for real runs,
never when a sandbox ``home=`` was passed). NOTE: the vars are PI-FAMILY (omp is Oh My
*Pi*) — if a pi stats source is ever added, it must disambiguate which harness a tree
belongs to rather than both reading it and double-counting.
Each line is one JSON event. The ones we care about have ``type == "message"`` with
``message.role == "assistant"`` and a ``message.content`` array; tool calls are the array
entries with ``type == "toolCall"``, carrying ``name`` (lowercase: ``bash``, ``read``,
``grep``, ``edit``, …) and ``arguments`` (its args — ``arguments.command`` for ``bash``).
Assistant events carry a top-level ISO ``timestamp``. A ``type == "session"`` event carries
the real absolute ``cwd`` and the session ``id``.

Repo mapping: the directory name is the HOME-RELATIVE cwd flattened (``/`` → ``-``;
verified live: ``-work-hyperide`` ↔ ``$HOME/work/hyperide``) — LOSSY (a real ``-`` in a
path segment decodes as ``/`` too). So we trust the ``session`` event's ``cwd`` and fall
back to a best-effort decode under the agent home only when no event carries one. A tool
call MAY carry its own ``arguments.cwd`` (observed on ``bash``): an omp session can run
commands across several repos/worktrees, so an absolute per-call cwd wins over the session
cwd (same rule as codex's per-call ``workdir``).
omp has no Skill tool (skills load through its ``read`` tool on ``skill://<name>`` URLs), so
the parser recovers the skill signal from a ``read`` whose path is a ``skill://`` URL. A plain
filesystem read of a ``SKILL.md`` is NOT counted: it's indistinguishable from an agent
inspecting or editing a skill file, and counting it would inflate the adoption metric.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..model import ToolInvocation
from ..taxonomy import categorize
from ._shellutil import detail_of, extract_command
from .base import LogSource, parse_iso, register
from ...paths import expand_user_path


@register
class OmpSource(LogSource):
    name = "omp"

    def root(self) -> Path:
        # Honor PI_CODING_AGENT_DIR / PI_CONFIG_DIR for REAL runs (omp profiles re-point the
        # agent dir through them) via the single resolver in harness_skills; an explicit
        # sandbox ``home=`` always wins (a HOME-isolated test is never contaminated by the
        # developer's own vars).
        from ...harness_skills import omp_config_root

        home = self.home if self._home_explicit else None
        return omp_config_root(home) / "sessions"

    def _decode_dir(self, encoded: str) -> str:
        """Best-effort fallback when no ``session`` event carries the real ``cwd``. omp
        flattens the HOME-RELATIVE cwd (verified live: ``-work-hyperide`` held a session
        whose cwd was ``$HOME/work/hyperide``), so decode under ``self.home``. Lossy — a
        real dash in a path segment also decodes as ``/``."""
        if encoded.startswith("-"):
            return str(self.home / encoded.lstrip("-").replace("-", "/"))
        return encoded

    def iter_invocations(self, *, repos: frozenset[str] | None = None) -> Iterator[ToolInvocation]:
        root = self.root()
        if not root.exists():
            return
        # normalize the pre-filter ONCE per walk, not once per tool call (the pre-filter's
        # whole point is to be cheap).
        repos_norm = frozenset(r.rstrip("/") for r in repos) if repos is not None else None
        try:
            proj_dirs = sorted(root.iterdir())
        except OSError:
            return  # root exists but isn't a readable dir — degrade, don't abort the source
        for proj_dir in proj_dirs:
            if not proj_dir.is_dir():
                continue
            decoded = self._decode_dir(proj_dir.name)
            try:
                session_files = sorted(proj_dir.rglob("*.jsonl"))
            except OSError:
                continue  # one unreadable project dir must not abort the whole source
            for session_file in session_files:
                yield from self._iter_session(session_file, decoded, repos_norm, root)

    def _iter_session(
        self, session_file: Path, decoded_dir: str, repos_norm: frozenset[str] | None, root: Path
    ) -> Iterator[ToolInvocation]:
        # Fallback session id, unique at ANY nesting depth: nested per-agent transcripts
        # share bare stems across sessions (two "<session>/RigCliOmp.jsonl"), so use the
        # root-relative path. The `session` event's own `id` replaces this once seen.
        session = session_file.relative_to(root).with_suffix("").as_posix()
        # cwd comes from the `session` event; cache it so every later event maps. The
        # decoded dir name is the lossy fallback until (unless) one is seen.
        cwd = decoded_dir
        try:
            # errors="replace": a malformed multibyte must not raise UnicodeDecodeError (a
            # ValueError, NOT an OSError) and abort the whole `rig stats` command — one bad
            # byte degrades to U+FFFD, the rest of the log still parses. (Same guard as the
            # claude parser.)
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
                    if event.get("type") == "session":
                        ev_cwd = event.get("cwd")
                        if isinstance(ev_cwd, str) and ev_cwd:
                            cwd = ev_cwd
                        ev_id = event.get("id")
                        if isinstance(ev_id, str) and ev_id:
                            session = ev_id
                        continue
                    if event.get("type") != "message":
                        continue
                    msg = event.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    ts = parse_iso(event.get("timestamp"))
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "toolCall":
                            continue
                        inv = self._invocation(block, ts, cwd, session, repos_norm)
                        if inv is not None:
                            yield inv
        except OSError:
            return

    def _invocation(
        self, block: dict, ts, session_cwd: str, session: str, repos_norm: frozenset[str] | None
    ) -> ToolInvocation | None:
        raw = str(block.get("name", "") or "")
        if not raw:
            return None
        args = block.get("arguments")
        args = args if isinstance(args, dict) else {}
        command = extract_command(args)
        # prefer the call's OWN cwd over the session cwd: an omp session can run commands
        # across several repos/worktrees, so per-call cwd is the correct repo attribution.
        # Only an ABSOLUTE override is trusted — a relative one would become a junk repo key
        # that also slips past the repo pre-filter; fall back to the (absolute) session cwd.
        repo = session_cwd
        call_cwd = args.get("cwd")
        if isinstance(call_cwd, str) and call_cwd and os.path.isabs(call_cwd):
            repo = call_cwd
        # repo pre-filter applied AFTER resolving the per-call cwd (the caller filters
        # authoritatively too, but this skips obviously out-of-scope calls cheaply). Compared
        # on trailing-slash-normalized paths so a cosmetic difference can't drop a call the
        # authoritative (normalizing) filter would have kept — the pre-filter must stay at
        # least as permissive as the real one.
        if repos_norm is not None and repo.rstrip("/") not in repos_norm:
            return None
        # omp loads skills through its ``read`` tool on ``skill://<name>`` URLs — recover the
        # skill signal from the path so skill adoption isn't invisible (unlike CC, omp has no
        # dedicated Skill tool whose ``skill`` arg the taxonomy reads).
        skill = None
        if raw == "read":
            path = args.get("path")
            if isinstance(path, str) and path.startswith("skill://"):
                skill = path[len("skill://"):].lstrip("/").split("/", 1)[0] or None
        if skill:
            category, label = categorize("skill", skill=skill)
            detail = detail_of(f"skill:{skill}")
        else:
            category, label = categorize(raw, command=command)
            detail = detail_of(command)
        return ToolInvocation(
            timestamp=ts,
            harness=self.name,
            repo=repo,
            session=session,
            tool_name=label,
            category=category,
            raw_tool=raw,
            detail=detail,
        )
