"""Provenance of on-disk items rig does NOT manage — the "known, not drift" classification.

Why (rig-cli#357)
-----------------
``rig status`` scans the SHARED target dirs (``~/.agents/skills``, ``~/.claude/hooks``,
``.github/workflows``, the MCP registry) for disk→config extras. Those places legitimately hold
things rig never wrote: skills the ecosystem CLIs register with their own ``<tool> install-skill``,
packs a developer installed by hand, hook descriptors an agent-tools ``ops/*/install.sh`` writes
directly, and a repository's own CI workflows. Reporting all of that as "drift" made a freshly
applied reference machine print 48 drift lines — a newcomer reads that as "the install is
broken". This module decides, per undeclared item, whether rig can NAME its origin (an
informational "known" line) or not (real drift the user must act on). Nothing here is ever
reconciled or deleted — classification only.

The contract (also in docs/config-schema.md#provenance)
------------------------------------------------------
Skills (a dir under the skills target carrying a ``SKILL.md``). A CATALOG leaf name is rig's own
namespace (:attr:`riglib.plan.InstallPlan.catalog_items`): a dir named after a catalog item is
either declared under ``skills.known`` (the user owns a same-named pack) or must be BYTE-IDENTICAL
to the catalog source — the skills dir is MACHINE-WIDE while a plan is PER-REPO, so a
by-type/by-stack skill another repo's apply selected (a monorepo's ``parallelize-independent``, an
iOS repo's ``swiftui-mvvm``) is legitimately on disk yet unselected by THIS repo's config:
rig-installed, not drift. No installer marker can vouch for a catalog-named dir: a marker-bearing
copy with foreign content (a broken installer, a stale copy, a spoof) stays drift. (rig's own
catalog apply writes no marker on purpose — the byte-identity check IS its provenance.) Any
other name is checked in this order:

1. ``<skill>/.installed-by`` — one line naming the installer (``tg``, ``review``, …). The marker
   every ecosystem installer is asked to write (agent-tools umbrella ticket); rig's own
   ``install-skill`` writes it.
2. ``<skills>/.blurbs/<name>.md`` — the SessionStart blurb ``<tool> install-skill`` ALREADY writes
   today (:mod:`riglib.tools` keys tool freshness off it) — the de-facto marker until (1) lands
   everywhere.
3. ``<skills>/../.skill-lock.json`` — the lockfile the third-party ``skills`` CLI (``npx skills
   add <owner>/<repo>``, Vercel's skill package manager) keeps NEXT TO the skills dir
   (``~/.agents/.skill-lock.json`` for the default target): a ``skills`` map keyed by installed
   dir name, each entry naming its ``source`` repo. A registry rig reads, not a list the user
   maintains — the installer already records every skill it wrote.
4. :data:`ECOSYSTEM_TOOL_SKILLS` — a shipped allowlist of the ecosystem CLIs' own skill names, so
   a machine whose tools predate both markers still reports clean.
5. ``skills.known`` (config) — packs the user installed by hand, declared once.

Otherwise the skill is drift: not in the catalog, no marker, not declared — of unknown origin; the
user declares it under ``skills.known`` or removes it. Note the asymmetry: markers are refused for
CATALOG names (rig's own namespace, where a spoof would hide a stale/foreign copy of something rig
manages), but a marker DOES vouch for any other name — a rogue skill can write its own
``.installed-by``. That is the accepted trade, and it is NOT merely cosmetic: a marker moves the
item out of drift, so ``rig status`` exits 0 instead of the drift code — anything keyed off that
exit code (CI, monitoring) is silenced for a marker-bearing rogue skill. What a marker never
changes is what rig WRITES or REMOVES (nothing, for a known item); it grants no permission.

The ``<category>.known`` lists cascade like every list in the config (a repo list REPLACES the
global one, ``riglib.config._deep_merge``), so the list for a MACHINE-WIDE dir (skills, agent
hooks) belongs in the global config; ``ci.known`` is per-repo by nature.

Agent-hook descriptors (``<hooks>/<id>.<point>.json``). A CATALOG hook id is rig's namespace the
same way: a descriptor wearing one is either under ``agent_hooks.known`` or its ``cmd`` must run
a file INSIDE that catalog hook's directory (the hooks dir is machine-wide like the skills dir, so
a catalog hook another repo's config enabled is rig-installed, not drift); a catalog id running
something else stays drift whatever it claims. The ``cmd`` check is deliberately weaker than the
skills byte-identity check — ``point``/matcher/``env`` are not compared — because rig REWRITES
the descriptor's placeholder path at install time, so no byte-identical source exists. Any other
id, in order:

1. a top-level ``"installed_by"`` key in the descriptor JSON — the agents-hooks/v1 runner reads
   descriptors with ``dict.get`` and ignores keys it does not know, so an installer can add it
   without touching the runner.
2. :data:`AGENT_TOOLS_OPS_HOOKS` — descriptors agent-tools' ``ops/*/install.sh`` installers write
   straight into the hooks dir, bypassing rig's catalog. TEMPORARY: once such a hook ships in the
   main catalog, ``all: true`` declares it and its entry here is dead weight to remove.
3. ``agent_hooks.known`` (config) — by hook id or by the full descriptor stem.

CI workflows (``<workflows>/<stem>.yml``): rig only has an opinion about a workflow named after a
catalog CI slot — that is the only kind it can have written, so an undeclared one is a genuine
orphan. Any OTHER stem is "this repository's own workflow" (known, never drift). ``ci.known``
covers a repo workflow that happens to collide with a slot name. When the caller has NO catalog
knowledge (a hand-built plan), every undeclared workflow stays drift — the conservative reading.

MCP servers: ``mcp.known`` only (no installer writes a marker into a JSON registry entry).

Permission entries beyond the rig baseline are classified in :mod:`riglib.drift` with the kinds
declared here (:data:`PERMISSION_KINDS`) so ``rig status`` renders them under one "your additions,
kept" heading instead of as drift — rig never removes a permission entry, and each one's origin
(a former rig default, a baseline rule the config turned off, a hand-added rule) is named.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .permissions import DEFAULT_ECOSYSTEM_TOOLS
from .tools import BLURBS_SUBDIR

# ── the marker contract ─────────────────────────────────────────────────────────────────────
# A one-line file inside an installed skill dir naming the tool that wrote the skill.
INSTALLED_BY_MARKER = ".installed-by"
# A top-level key an installer adds to a hook descriptor JSON naming itself.
HOOK_INSTALLED_BY_KEY = "installed_by"
# The `skills` CLI's lockfile, a sibling of the skills dir it installs into (`~/.agents/skills` →
# `~/.agents/.skill-lock.json`): `{"skills": {"<dir name>": {"source": "<owner>/<repo>", …}}}`.
SKILL_LOCK_FILE = ".skill-lock.json"
# An installer name is a plain identifier. Anything else (control characters, a whole sentence,
# an escape sequence aimed at the terminal `rig status` prints to) is NOT a marker — the item
# then stays drift, and the raw text is never rendered.
_INSTALLER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Skills the ecosystem CLIs register themselves (``<tool> install-skill``), by skill name. The
# fallback for a machine whose installed tools predate the marker/blurb contract above. ``stt``
# (stt-cli) is an ecosystem CLI with a skill but no permission entry, hence not in
# DEFAULT_ECOSYSTEM_TOOLS; ``daily`` is registered by ``rig daily install-skill`` itself.
ECOSYSTEM_TOOL_SKILLS: frozenset[str] = frozenset(DEFAULT_ECOSYSTEM_TOOLS) | frozenset({"stt", "daily"})

# Hook ids agent-tools' ``ops/*/install.sh`` installers write directly (see the module docstring —
# temporary by design). ``agent-browser-session-claim``: written by
# ops/agent-browser-session-reaper/install.sh, the owner-record hook its SessionEnd cleanup needs.
AGENT_TOOLS_OPS_HOOKS: frozenset[str] = frozenset({"agent-browser-session-claim"})

# ── kinds (grouping keys for the status renderer) ───────────────────────────────────────────
KIND_TOOL_INSTALLED = "tool-installed"
KIND_SKILLS_CLI = "skills-cli"
KIND_ECOSYSTEM = "ecosystem"
KIND_CONFIG_KNOWN = "config-known"
KIND_CATALOG_UNSELECTED = "catalog-unselected"
KIND_OPS_INSTALLER = "ops-installer"
KIND_REPO_WORKFLOW = "repo-workflow"
KIND_RETIRED_DEFAULT = "retired-default"
KIND_DISABLED_BASELINE = "disabled-baseline"
KIND_USER_EXTRA = "user-extra"

PERMISSION_KINDS: frozenset[str] = frozenset({KIND_RETIRED_DEFAULT, KIND_DISABLED_BASELINE, KIND_USER_EXTRA})

# How many names a grouped known line shows before "… and N more" — the ONE constant `rig status`
# (`riglib.cli._print_known_groups`) and the config-web known panel both render with; a
# hand-grown allowlist can run to hundreds of entries and the line must stay one line.
KNOWN_NAMES_SHOWN = 12

# One human sentence per kind — the group heading `rig status` prints, followed by the names.
KIND_LABELS: dict[str, str] = {
    KIND_TOOL_INSTALLED: "installed by their own tool's `install-skill` (marker present)",
    KIND_SKILLS_CLI: "installed by the `skills` CLI (`npx skills add`; recorded in .skill-lock.json)",
    KIND_ECOSYSTEM: "ecosystem tool skills (shipped allowlist; the tool wrote no marker yet)",
    KIND_CONFIG_KNOWN: "declared known in your config",
    KIND_CATALOG_UNSELECTED: (
        "rig catalog items not selected by this repo's config (installed by rig for another repo's "
        "type/stack, or disabled since) — remove by hand if unwanted"
    ),
    KIND_OPS_INSTALLER: "written by an agent-tools ops installer (not via the rig catalog)",
    KIND_REPO_WORKFLOW: "this repository's own workflows (not rig catalog gates)",
    KIND_RETIRED_DEFAULT: "former rig defaults (rig-cli#41, dropped in #165), kept",
    KIND_DISABLED_BASELINE: "rig baseline rules your config turned off, kept",
    KIND_USER_EXTRA: "your own entries beyond the rig baseline, kept",
}


@dataclass(frozen=True)
class Provenance:
    """Where an undeclared on-disk item came from: a grouping ``kind`` + the installer's name
    (``by``, empty when the kind carries no specific installer)."""

    kind: str
    by: str = ""


def read_installed_by(skill_dir: Path) -> str | None:
    """The installer named by ``<skill_dir>/.installed-by`` (first non-empty line), else None.

    A line that is not a plain identifier (see ``_INSTALLER_NAME``) is treated as no marker.
    """
    marker = skill_dir / INSTALLED_BY_MARKER
    try:
        text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.strip():
            return _installer_name(line)
    return None


def _installer_name(raw: str) -> str | None:
    name = raw.strip()
    return name if _INSTALLER_NAME.match(name) else None


def skill_provenance(
    entry: Path, skills_dir: Path, *, known: set[str], catalog: dict[str, tuple[Path, ...]] | None,
) -> Provenance | None:
    """Classify an undeclared skill dir — see the module docstring for the order."""
    if entry.name in (catalog or {}):
        return _catalog_member(entry.name, known, catalog, lambda source: _same_tree(source, entry))
    installer = read_installed_by(entry)
    if installer:
        return Provenance(KIND_TOOL_INSTALLED, installer)
    if (skills_dir / BLURBS_SUBDIR / f"{entry.name}.md").is_file():
        return Provenance(KIND_TOOL_INSTALLED, entry.name)
    source = skill_lock_source(skills_dir, entry.name)
    if source is not None:
        return Provenance(KIND_SKILLS_CLI, source)
    if entry.name in ECOSYSTEM_TOOL_SKILLS:
        return Provenance(KIND_ECOSYSTEM, entry.name)
    if entry.name in known:
        return Provenance(KIND_CONFIG_KNOWN)
    return None


# A `skills` CLI source is `<owner>/<repo>` (GitHub) or a local path/URL — one path-ish token.
# Same idea as `_INSTALLER_NAME`: user-writable JSON is never rendered raw; a value that is not a
# plain source token means "no usable provenance", and the label falls back to the CLI's name.
_SKILL_LOCK_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,127}$")


def skill_lock_source(skills_dir: Path, name: str) -> str | None:
    """The ``source`` the ``skills`` CLI lockfile records for ``<skills_dir>/<name>``, else None.

    Returns the empty string when the lockfile lists the skill but its ``source`` is unusable
    (missing, not a string, not a plain token) — still known, just without a nameable origin.
    A missing / unreadable / malformed lockfile (or one without that entry) is None: not known.
    """
    lock = skills_dir.parent / SKILL_LOCK_FILE
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    skills = data.get("skills") if isinstance(data, dict) else None
    entry = skills.get(name) if isinstance(skills, dict) else None
    if not isinstance(entry, dict):
        return None
    raw = entry.get("source")
    if isinstance(raw, str) and _SKILL_LOCK_SOURCE.match(raw.strip()):
        return raw.strip()
    return ""


def _same_tree(source: Path, installed: Path) -> bool:
    """Byte-identical file set — the same comparison the declared-skill drift check uses."""
    from .actions.fsutil import dirs_identical

    return dirs_identical(source, installed)


UNDECLARED_SKILL_DETAIL = (
    "installed on disk but not declared in config and of unknown origin — declare it under "
    "skills.known, or remove it"
)
STALE_CATALOG_SKILL_DETAIL = (
    "carries a catalog skill name but its content differs from the catalog source — a stale copy "
    "rig installed from an older catalog (re-apply from the repo that selects it), or foreign "
    "content under a catalog name (remove it)"
)
UNDECLARED_HOOK_DETAIL = (
    "hook descriptor on disk but not declared in config and of unknown origin — declare it under "
    "agent_hooks.known, or remove it"
)
FOREIGN_CATALOG_HOOK_DETAIL = (
    "carries a catalog hook id but its cmd runs a script outside that catalog hook's directory — "
    "not the descriptor rig writes (remove it, or re-apply from the repo that enables it)"
)


def undeclared_skill_detail(name: str, catalog: dict[str, tuple[Path, ...]] | None) -> str:
    """Drift wording for a skill rig could not place: a catalog name-alike vs an unknown origin."""
    return STALE_CATALOG_SKILL_DETAIL if name in (catalog or {}) else UNDECLARED_SKILL_DETAIL


def undeclared_hook_detail(descriptor: Path, catalog: dict[str, tuple[Path, ...]] | None) -> str:
    """Drift wording for a hook descriptor rig could not place."""
    hook_id = descriptor.stem.split(".", 1)[0]
    return FOREIGN_CATALOG_HOOK_DETAIL if hook_id in (catalog or {}) else UNDECLARED_HOOK_DETAIL


def hook_provenance(descriptor: Path, *, known: set[str], catalog: dict[str, tuple[Path, ...]] | None) -> Provenance | None:
    """Classify an undeclared hook descriptor ``<id>.<point>.json`` — see the module docstring."""
    spec = _load_descriptor(descriptor)
    hook_id = descriptor.stem.split(".", 1)[0]
    # `known` claims a hook by id or by one descriptor's full `<id>.<point>` stem
    known_ids = known | ({hook_id} if descriptor.stem in known else set())
    if hook_id in (catalog or {}):
        return _catalog_member(hook_id, known_ids, catalog, lambda source_dir: _cmd_inside(spec.get("cmd"), source_dir))
    raw = spec.get(HOOK_INSTALLED_BY_KEY)
    installer = _installer_name(raw) if isinstance(raw, str) else None
    if installer:
        return Provenance(KIND_TOOL_INSTALLED, installer)
    if hook_id in AGENT_TOOLS_OPS_HOOKS:
        return Provenance(KIND_OPS_INSTALLER, "agent-tools")
    if hook_id in known_ids:
        return Provenance(KIND_CONFIG_KNOWN)
    return None


def _catalog_member(
    name: str, known: set[str], catalog: dict[str, tuple[Path, ...]], vouched_by_source,
) -> Provenance | None:
    """The one rule for a name in rig's catalog namespace (skills and hooks alike).

    The config may claim the name (``<category>.known`` — the user's own item under that name);
    otherwise only the catalog itself vouches for it, through ``vouched_by_source(source)`` — the
    byte-identity check for a skill, the cmd-inside-the-hook-dir check for a descriptor. Never an
    installer marker: a spoof could write one. Anything else under a catalog name is drift.
    """
    if name in known:
        return Provenance(KIND_CONFIG_KNOWN)
    if any(vouched_by_source(source) for source in catalog[name]):
        return Provenance(KIND_CATALOG_UNSELECTED, "rig")
    return None


def _load_descriptor(descriptor: Path) -> dict:
    try:
        data = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cmd_inside(cmd: object, source_dir: Path) -> bool:
    """Does the descriptor's ``cmd`` run a file inside the catalog hook's own directory?

    ``cmd`` is a shell-ish string whose first token is the script (rig writes a bare absolute
    path; the agents-hooks/v1 runner tolerates arguments after it), so only the executable is
    located — arguments never make a rig-installed descriptor look foreign.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    try:
        argv = shlex.split(cmd)
        return bool(argv) and Path(argv[0]).resolve().is_relative_to(source_dir.resolve())
    except (OSError, ValueError):
        return False


def workflow_provenance(stem: str, *, known: set[str], catalog: dict[str, tuple[Path, ...]] | None) -> Provenance | None:
    """Classify an undeclared workflow file by its stem — see the module docstring.

    Sound because the runner writes every catalog gate as ``<slot>.yml`` regardless of which
    source file (``workflow.yml`` / ``workflow-<variant>.yml``) it copies
    (``riglib/actions/runner.py::_do_install_ci``) — so "stem is a catalog leaf" is exactly
    "rig could have written this file".
    """
    if stem in known:
        return Provenance(KIND_CONFIG_KNOWN)
    if catalog is not None and stem not in catalog:
        return Provenance(KIND_REPO_WORKFLOW)
    return None


def mcp_provenance(name: str, *, known: set[str]) -> Provenance | None:
    return Provenance(KIND_CONFIG_KNOWN) if name in known else None


KNOWN_CATEGORIES: tuple[str, ...] = ("skills", "agent_hooks", "ci", "mcp")


def known_names_from_config(loaded) -> dict[str, set[str]]:
    """The ``<category>.known`` lists (skills / agent_hooks / ci / mcp) from the cascaded config.

    A user's one-time declaration of on-disk items rig does not manage (hand-installed skill packs,
    a hook another installer wrote, a repo workflow that collides with a catalog slot name). Read
    through ``loaded.category`` so the global/repo cascade applies. Non-list values are ignored —
    ``config.validate`` already rejects them for a loaded file. :func:`riglib.plan.build` stores
    the result on the plan (``InstallPlan.known_names``) next to ``catalog_items`` so every drift
    caller classifies the same way.
    """
    out: dict[str, set[str]] = {}
    for category in KNOWN_CATEGORIES:
        block = loaded.category(category)
        raw = block.get("known") if isinstance(block, dict) else None
        out[category] = {n for n in raw if isinstance(n, str)} if isinstance(raw, list) else set()
    return out
