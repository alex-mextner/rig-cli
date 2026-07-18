"""Catalog — scans an ``agent-tools`` checkout into an item registry.

This is the **integration seam** with the agent-tools umbrella repo. rig does not vendor
agent-tools content; it reads it live from a checkout (``agent_tools_source`` in config,
auto-detected otherwise) and turns the on-disk layout into a flat registry of installable
items. The catalog is the single source of truth for:

- which items exist (drives validation of ``rig.yaml`` item names),
- each item's one-line description (drives the wizard's right-hand pane),
- the on-disk carrier path the install action copies/wires.

agent-tools on-disk layout this scanner understands::

    skills/universal/<name>/SKILL.md            → category "skills", group "universal"
    skills/by-type/<kind>/<name>/SKILL.md       → category "skills", group "by-type/<kind>"
    skills/by-stack/<l1>/<lang>[/<fw>]/<n>/SKILL.md → "skills", group "by-stack/<l1>/<lang>[/<fw>]"
    agent-hooks/<name>/<name>.<point>.json      → category "agent_hooks"
    ci/<name>/{workflow.yml,*.sh}               → category "ci"
    git-hooks/{global-dispatcher,pre-commit,…}  → category "git_hooks"
    mcp/<name>/README.md                        → category "mcp"

Stdlib-only: no yaml import here — SKILL.md frontmatter is parsed with a tiny hand-rolled
reader (the ``description:`` line only), so the catalog has no dependency cost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Default locations to look for an agent-tools checkout when config does not pin one.
_DEFAULT_SOURCE_CANDIDATES = (
    "~/xp/agent-tools",
    "~/work/agent-tools",
    "~/agent-tools",
)


class CatalogError(RuntimeError):
    """Raised when the agent-tools source is missing or not a valid checkout."""


@dataclass(frozen=True)
class Item:
    """One installable unit discovered in the agent-tools checkout."""

    name: str  # fully-qualified, unique within its category (e.g. "by-type/cli/no-npx")
    category: str  # skills | agent_hooks | git_hooks | ci | mcp
    group: str  # sub-grouping for display (universal | by-type/<kind> | "")
    description: str  # one-line "what it gives you"
    path: Path  # the on-disk carrier (dir or file) the action copies/wires
    default_enabled: bool = True
    meta: dict[str, str] = field(default_factory=dict)
    # For an agent-hook item: EVERY ``*.json`` descriptor the hook dir ships. A single dir may
    # carry more than one (e.g. a pre-bash AND a pre-write guard); all of them must install.
    descriptors: tuple[str, ...] = ()


# Items the catalog marks situational — off by default unless explicitly enabled or the
# detected project type pulls in their by-type bundle.
_SITUATIONAL = frozenset(
    {
        "push-regularly",
    }
)


def resolve_source(configured: str | None) -> Path:
    """Resolve the agent-tools checkout path.

    Order: explicit config value → ``RIG_AGENT_TOOLS_SOURCE`` env → default candidates.
    Raises :class:`CatalogError` if nothing valid is found.
    """
    if configured:
        p = Path(os.path.expanduser(configured)).resolve()
        if not _looks_like_agent_tools(p):
            raise CatalogError(
                f"agent_tools_source '{configured}' is not an agent-tools checkout "
                f"(expected skills/ and agent-hooks/ subdirs under {p})"
            )
        return p

    env = os.environ.get("RIG_AGENT_TOOLS_SOURCE")
    if env:
        return resolve_source(env)

    for cand in _DEFAULT_SOURCE_CANDIDATES:
        p = Path(os.path.expanduser(cand)).resolve()
        if _looks_like_agent_tools(p):
            return p

    raise CatalogError(
        "could not locate an agent-tools checkout. Set agent_tools_source in rig.yaml, "
        "or RIG_AGENT_TOOLS_SOURCE, or clone it to one of: "
        + ", ".join(_DEFAULT_SOURCE_CANDIDATES)
    )


def _looks_like_agent_tools(p: Path) -> bool:
    return p.is_dir() and (p / "skills").is_dir() and (p / "agent-hooks").is_dir()


def _read_skill_description(skill_md: Path) -> str:
    """Pull the ``description:`` field from a SKILL.md YAML frontmatter, stdlib-only.

    Frontmatter is a leading ``---`` fenced block. We only need the single-line
    ``description:`` value, so a full YAML parse is unnecessary (and would add import
    cost). Falls back to the first non-blank heading/body line.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip()
    # fallback: first heading
    for line in lines:
        s = line.strip().lstrip("#").strip()
        if s and s != "---":
            return s
    return ""


def _first_line(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                return s
    except OSError:
        pass
    return ""


@dataclass
class Catalog:
    """The scanned registry of all installable items, keyed by ``category/name``."""

    source: Path
    items: list[Item] = field(default_factory=list)

    @classmethod
    def scan(cls, configured_source: str | None = None) -> "Catalog":
        source = resolve_source(configured_source)
        cat = cls(source=source)
        cat._scan_skills()
        cat._scan_agent_hooks()
        cat._scan_ci()
        cat._scan_git_hooks()
        cat._scan_mcp()
        return cat

    # ── scanners ────────────────────────────────────────────────────────────────
    def _scan_skills(self) -> None:
        sk = self.source / "skills"
        uni = sk / "universal"
        if uni.is_dir():
            for d in sorted(p for p in uni.iterdir() if p.is_dir()):
                md = d / "SKILL.md"
                if not md.is_file():
                    continue
                self.items.append(
                    Item(
                        name=d.name,
                        category="skills",
                        group="universal",
                        description=_read_skill_description(md),
                        path=d,
                        default_enabled=d.name not in _SITUATIONAL,
                    )
                )
        by_type = sk / "by-type"
        if by_type.is_dir():
            for kind_dir in sorted(p for p in by_type.iterdir() if p.is_dir()):
                kind = kind_dir.name
                for d in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
                    md = d / "SKILL.md"
                    if not md.is_file():
                        continue
                    # by-type skills are off by default; the detected project type or an
                    # explicit by_type.enable pulls in the whole bundle (resolved in plan).
                    self.items.append(
                        Item(
                            name=f"by-type/{kind}/{d.name}",
                            category="skills",
                            group=f"by-type/{kind}",
                            description=_read_skill_description(md),
                            path=d,
                            default_enabled=False,
                            meta={"kind": kind, "skill": d.name},
                        )
                    )

        self._scan_by_stack(sk)

    def _scan_by_stack(self, sk: Path) -> None:
        """Scan ``skills/by-stack/<l1>/<lang>[/<framework>]/<name>/SKILL.md`` into items.

        The directory PATH is the stack path (``l1/lang`` or ``l1/lang/framework``), mirroring
        the ``by-type`` convention. A SKILL.md must live at least three components below
        ``by-stack`` — ``<l1>/<lang>/<name>`` (stack ``l1/lang``) or
        ``<l1>/<lang>/<framework>/<name>`` (stack ``l1/lang/framework``). A SKILL.md only two
        components down (``by-stack/<l1>/<name>``, i.e. l1-only, no lang) is IGNORED, enforcing the
        taxonomy's "lang is required" rule at scan time. ``rel`` below is the path parts including
        the skill-dir name, so a valid item has ``len(rel) >= 3`` and the stack path is
        ``rel[:-1]``. Items are off by default; the plan pulls them in when the declared ``stack``
        is a prefix-match of the item's stack.
        """
        by_stack = sk / "by-stack"
        if not by_stack.is_dir():
            return
        for skill_md in sorted(by_stack.rglob("SKILL.md")):
            skill_dir = skill_md.parent
            rel = skill_dir.relative_to(by_stack).parts
            # rel = (<l1>, <lang>[, <framework>], <name>); stack path is everything but the name.
            if len(rel) < 3:
                # depth < 3 → the SKILL.md is at l1/<name> (l1-only) or shallower: no lang → skip.
                continue
            stack_path = "/".join(rel[:-1])
            name = rel[-1]
            self.items.append(
                Item(
                    name=f"by-stack/{stack_path}/{name}",
                    category="skills",
                    group=f"by-stack/{stack_path}",
                    description=_read_skill_description(skill_md),
                    path=skill_dir,
                    default_enabled=False,
                    meta={"stack": stack_path, "skill": name},
                )
            )

    def _scan_agent_hooks(self) -> None:
        ah = self.source / "agent-hooks"
        if not ah.is_dir():
            return
        for d in sorted(p for p in ah.iterdir() if p.is_dir()):
            descriptors = tuple(p.name for p in sorted(d.glob("*.json")))
            if not descriptors:
                continue
            desc = _first_line(d / "README.md")
            self.items.append(
                Item(
                    name=d.name,
                    category="agent_hooks",
                    group="",
                    description=desc,
                    path=d,
                    default_enabled=True,
                    # ``descriptor`` (first) kept for back-compat display; ``descriptors``
                    # carries EVERY *.json so the plan installs all of them, not just [0].
                    meta={"descriptor": descriptors[0]},
                    descriptors=descriptors,
                )
            )

    def _scan_ci(self) -> None:
        ci = self.source / "ci"
        if not ci.is_dir():
            return
        for d in sorted(p for p in ci.iterdir() if p.is_dir()):
            desc = _first_line(d / "README.md")
            self.items.append(
                Item(
                    name=d.name,
                    category="ci",
                    group="",
                    description=desc,
                    path=d,
                    default_enabled=False,
                )
            )

    def _scan_git_hooks(self) -> None:
        gh = self.source / "git-hooks"
        if not gh.is_dir():
            return
        disp = gh / "global-dispatcher"
        if disp.is_dir():
            self.items.append(
                Item(
                    name="dispatcher",
                    category="git_hooks",
                    group="",
                    description=_first_line(disp / "README.md")
                    or "Global hook dispatcher — runs your hooks in every repo.",
                    path=disp,
                    default_enabled=True,
                )
            )

    def _scan_mcp(self) -> None:
        mcp = self.source / "mcp"
        if not mcp.is_dir():
            return
        for d in sorted(p for p in mcp.iterdir() if p.is_dir()):
            self.items.append(
                Item(
                    name=d.name,
                    category="mcp",
                    group="",
                    description=_first_line(d / "README.md"),
                    path=d,
                    default_enabled=False,
                )
            )

    # ── lookup ──────────────────────────────────────────────────────────────────
    def by_category(self, category: str) -> list[Item]:
        return [i for i in self.items if i.category == category]

    def get(self, category: str, name: str) -> Item | None:
        for i in self.items:
            if i.category == category and i.name == name:
                return i
        return None

    def names(self, category: str) -> set[str]:
        return {i.name for i in self.by_category(category)}
