"""Per-harness skill/instruction discovery — the single source of truth for how each
supported agent harness DISCOVERS the skills (and instruction files) rig installs.

What this is
------------
rig copies every enabled skill into ``skills_target`` (default ``~/.agents/skills``), but a
harness never lists/loads a skill from there — each harness discovers agent guidance from its
OWN location, and the shape of that location differs per harness. This module is the registry
that maps a ``harness.kind`` to its discovery convention, so the plan/runner/drift code keys
off ``harness.kind`` exactly like the per-harness permission-allowlist provisioning
(:mod:`riglib.permissions`) and a new harness is one table entry, never scattered literals.

Two discovery families
-----------------------
Harnesses split into two families by HOW they surface skills:

- **skills-directory harnesses** — they enumerate a directory of Agent-Skill folders and load
  each ``SKILL.md`` as a callable skill. rig makes an installed skill discoverable by
  idempotently symlinking ``<skill_dir>/<skill> -> <skills_target>/<skill>`` (one
  ``link_skill_harness`` action per enabled skill). These are in :data:`HARNESS_SKILL_DIRS`.

    - **claude-code** → ``~/.claude/skills`` (its userSettings skill dir; symlinks there resolve
      to the real skill). This is the original, proven path.
    - **opencode** → ``~/.config/opencode/skill`` (opencode's Agent-Skills dir; XDG-aware, same
      ``skill/<name>/SKILL.md`` layout). ``~/.config`` maps to ``$XDG_CONFIG_HOME`` when set, via
      the plan's ``_expand``.

- **instruction-file harnesses** — they have NO per-skill discovery directory; agent guidance
  reaches them through a single global INSTRUCTION FILE (``AGENTS.md`` / ``GEMINI.md``) that the
  ``agents_md`` provisioning area maintains, not through a symlinked skill folder. rig records
  these N/A for skill-LINKING (it never invents a fake skills dir), and the discovery file path
  is documented here so ``rig status`` can report "N/A — uses <file>" instead of an empty,
  silent gap. These are in :data:`HARNESS_INSTRUCTION_FILES`.

    - **codex** → ``~/.codex/AGENTS.md`` (Codex CLI reads ``AGENTS.md``-style global instructions).
    - **gemini** → ``~/.gemini/GEMINI.md`` (Gemini CLI's global instruction file).
    - **commandcode** → ``~/.commandcode/AGENTS.md`` (its global instruction dir, AGENTS.md-style).
    - **pi** → ``~/.config/pi/AGENTS.md`` (AGENTS.md-style global instructions).

Stdlib-only (the repo import rule): this is a pure registry; callers expand/serialize.
"""

from __future__ import annotations

# ── skills-directory harnesses: rig symlinks each installed skill into this dir ──────────────
# A skill copied into ``skills_target`` is invisible to the harness unless it ALSO appears here,
# so the plan emits a ``link_skill_harness`` action per enabled skill keyed off ``harness.kind``.
# ``~/.config/...`` prefixes are XDG-aware (the plan's ``_expand`` maps them to $XDG_CONFIG_HOME).
HARNESS_SKILL_DIRS: dict[str, str] = {
    "claude-code": "~/.claude/skills",
    "opencode": "~/.config/opencode/skill",
}

# ── instruction-file harnesses: no per-skill discovery dir; guidance via a global file ───────
# These harnesses do not enumerate a skills directory; their agent guidance comes from one global
# instruction file (AGENTS.md / GEMINI.md), provisioned by the ``agents_md`` area — not by a
# per-skill symlink. Recorded here (with the file path) so ``rig`` reports "N/A — uses <file>"
# rather than silently linking nothing OR guessing a directory that does not exist.
HARNESS_INSTRUCTION_FILES: dict[str, str] = {
    "codex": "~/.codex/AGENTS.md",
    "gemini": "~/.gemini/GEMINI.md",
    "pi": "~/.config/pi/AGENTS.md",
    "commandcode": "~/.commandcode/AGENTS.md",
}

# Every harness kind rig knows a skill/instruction discovery convention for — the union of the two
# families. ``harness.kind`` is accepted when it is in this set (rig can provision SOMETHING for
# it), even if a specific area (auto-mode write, allowlist) does not yet support it.
KNOWN_HARNESS_KINDS: frozenset[str] = frozenset(HARNESS_SKILL_DIRS) | frozenset(
    HARNESS_INSTRUCTION_FILES
)


def harness_links_skills(kind: str) -> bool:
    """True when ``kind`` discovers skills from a DIRECTORY rig symlinks into (vs an instruction file)."""
    return kind in HARNESS_SKILL_DIRS


def skill_dir_for(kind: str) -> str | None:
    """The harness skill-discovery dir to symlink installed skills into, or ``None`` for a
    harness that uses an instruction file (or an unknown kind). Unexpanded — callers expand."""
    return HARNESS_SKILL_DIRS.get(kind)


def instruction_file_for(kind: str) -> str | None:
    """The global instruction file an instruction-file harness reads, or ``None`` (skills-dir /
    unknown kind). Used to render the "N/A — uses <file>" status note. Unexpanded."""
    return HARNESS_INSTRUCTION_FILES.get(kind)


def is_known_kind(kind: str) -> bool:
    """True when rig knows ANY skill/instruction discovery convention for ``kind``."""
    return kind in KNOWN_HARNESS_KINDS
