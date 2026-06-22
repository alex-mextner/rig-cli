"""Personal CLI ecosystem provisioning — config, on-disk status, install planning.

What this is
------------
``rig`` exists FIRST AND FOREMOST to install the ecosystem of personal CLI tools — ``tg``,
``review``, ``task``, ``draw`` (and more, declaratively). Each of those tools ships its OWN
``install.sh`` that: locates its repo (a local clone or a fresh ``git clone``), installs any
language deps, symlinks its entry script into a managed PATH dir (``~/.local/bin/<tool>``), and
runs ``<tool> install-skill`` to advertise itself into the agent harness (writes
``~/.agents/skills/<tool>/SKILL.md`` + ``~/.agents/skills/.blurbs/<tool>.md`` + the SessionStart
blurb). rig does NOT reimplement any of that — it DRIVES each tool's own ``install.sh`` so the
tool stays the single source of truth for how it installs.

This module is **stdlib-only and effect-free**: it resolves WHAT should be installed (a
:class:`ToolSpec` per declared tool) and reads the CURRENT on-disk status (is the bin resolvable,
is the skill advertised). The effectful work — running ``install.sh`` — lives in
``actions/runner.py`` (``_do_provision_tools``); drift detection diffs desired vs. on-disk in
``drift.py`` (``_check_tools``). Three consumers (plan, apply, drift) share THIS module so the
desired state never drifts between them — the same shape as :mod:`riglib.tg_ctl` and
:mod:`riglib.tmux`.

How it is reached
-----------------
``plan._build_tools`` reads the ``tools:`` config block, calls :func:`resolve_tool_specs` to
turn it into a list of :class:`ToolSpec`, and emits ONE ``provision_tools`` action carrying the
specs. ``runner._do_provision_tools`` runs each spec's ``install.sh`` when
:func:`tool_status` reports it is not already current. ``drift._check_tools`` flags a declared
tool whose bin is unresolvable or whose skill is unadvertised.

Invariants
----------
- **Default OFF (opt-in).** Unlike ``tg_ctl``/``models`` (default-on), an ABSENT or empty
  ``tools:`` block provisions NOTHING. A machine opts in by listing tools under
  ``tools.items``. This keeps a clean ``rig init`` from cloning four repos a user may not have,
  and keeps the e2e suite from shelling out to real ``install.sh`` scripts.
- **The tool owns its install.** rig runs ``bash <repo>/install.sh``; it never reimplements the
  symlink/clone/skill logic. If a tool changes how it installs, rig inherits that for free.
- **Idempotent + safe.** A tool already installed (bin resolves to the declared repo's entry AND
  the skill blurb is present) is a no-op — rig does NOT re-run ``install.sh``. A bin that resolves
  ELSEWHERE (e.g. a Homebrew ``review``) but still WORKS is detected and left alone unless the
  skill is unadvertised. rig never deletes a user's existing symlink.

This is a per-MACHINE concern (the tool ecosystem on this dev box), so the ``tools:`` block
belongs in the GLOBAL layer (``~/.config/rig/config.yaml``), like ``harness``/``tmux``/
``tg_ctl`` — NOT a committed repo ``rig.yaml``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# The managed PATH dir each tool's install.sh symlinks the entry into. Matches every shipped
# install.sh (``BIN="$HOME/.local/bin"``). Overridable per-block via ``tools.target``.
DEFAULT_BIN_DIR = "~/.local/bin"

# Where ``<tool> install-skill`` advertises the tool. The blurb file is the cheap presence marker
# (one short file per tool) drift/idempotency key off — distinct from the per-tool SKILL.md dir.
SKILLS_DIR = "~/.agents/skills"
BLURBS_SUBDIR = ".blurbs"

# Where each tool repo's install.sh lives, relative to the repo root. Every shipped tool keeps it
# at the repo root, so this is a constant, not per-tool config.
INSTALL_SCRIPT = "install.sh"


def _expand(path: str) -> Path:
    """Expand ``~`` and env vars and resolve to an absolute path (no disk access)."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


@dataclass(frozen=True)
class ToolSpec:
    """One declared tool: its name + the on-disk repo whose ``install.sh`` rig runs.

    ``name`` is the command name (``tg``) and the skill-blurb stem. ``repo`` is the resolved
    absolute path to the tool's checkout (the dir holding ``install.sh``). ``bin_dir`` is the
    managed PATH dir the install symlinks into (so status can confirm the bin resolves there).
    """

    name: str
    repo: Path
    bin_dir: Path

    @property
    def install_script(self) -> Path:
        return self.repo / INSTALL_SCRIPT

    @property
    def managed_bin(self) -> Path:
        """The symlink the tool's install.sh creates: ``<bin_dir>/<name>``."""
        return self.bin_dir / self.name

    @property
    def blurb_file(self) -> Path:
        """The advertisement marker ``<skills>/.blurbs/<name>.md`` install-skill writes."""
        return _expand(SKILLS_DIR) / BLURBS_SUBDIR / f"{self.name}.md"


@dataclass(frozen=True)
class ToolStatus:
    """The current on-disk state of a tool, read by status / idempotency / drift.

    ``repo_present`` — the declared repo dir (with its install.sh) exists.
    ``bin_resolves`` — a ``<name>`` command is reachable (the managed symlink OR anywhere on PATH).
    ``advertised``   — the skill blurb marker exists (``install-skill`` ran).
    ``installed``    — bin resolves AND advertised: a working, discoverable install → apply no-op.
    """

    repo_present: bool
    bin_resolves: bool
    advertised: bool

    @property
    def installed(self) -> bool:
        return self.bin_resolves and self.advertised


def resolve_tool_specs(block: dict | None) -> list[ToolSpec]:
    """Turn a validated ``tools:`` config block into the list of specs to provision.

    Returns ``[]`` unless the block is present AND ``enabled`` is TRUTHY (default-OFF opt-in) — an
    absent block, an empty block, ``enabled: false``, OR a block that LISTS items but FORGOT
    ``enabled: true`` all provision nothing. (The schema's ``default: false`` is editor-facing only;
    the loader never injects it into the runtime dict, so a missing ``enabled`` must be read as OFF
    here — a truthy check, NOT ``is False`` — or a user who lists items but omits ``enabled`` would
    get a surprise clone+install on the next apply.) Each ``items.<name>`` entry becomes a
    :class:`ToolSpec`; an item with ``enabled: false`` is skipped. ``repo`` defaults to
    ``~/xp/<name>-cli`` (the common layout) so a bare ``tg: {}`` works; ``tools.target`` (or a
    per-item ``bin_dir``) overrides the managed PATH dir. Effect-free — paths are expanded, not
    stat'd.
    """
    if not block or not block.get("enabled"):
        return []
    default_bin = _expand(str(block.get("target") or DEFAULT_BIN_DIR))
    items = block.get("items") or {}
    specs: list[ToolSpec] = []
    for name, raw in items.items():
        spec = raw if isinstance(raw, dict) else {}
        if spec.get("enabled") is False:
            continue
        repo = _expand(str(spec.get("repo") or f"~/xp/{name}-cli"))
        bin_dir = _expand(str(spec["bin_dir"])) if spec.get("bin_dir") else default_bin
        specs.append(ToolSpec(name=str(name), repo=repo, bin_dir=bin_dir))
    return specs


def tool_status(spec: ToolSpec) -> ToolStatus:
    """Read the current on-disk status of ``spec`` (the idempotency + drift probe).

    ``bin_resolves`` is true if the managed symlink exists, OR ``shutil.which(name)`` finds a command
    that REAL-PATHS into the tool's own repo (so a Homebrew/.files install symlinked back to the
    declared checkout counts and is NOT re-clobbered). It does NOT count a same-named FOREIGN binary
    on PATH — ``task`` (Taskwarrior), ``draw``, etc. are common collisions, and a stranger's binary
    pointing nowhere near the declared repo must read as not-installed so drift flags it.
    ``advertised`` keys off the cheap blurb marker. Pure reads; no mutation.
    """
    repo_present = spec.install_script.is_file()
    bin_resolves = _bin_resolves(spec)
    advertised = spec.blurb_file.is_file()
    return ToolStatus(repo_present=repo_present, bin_resolves=bin_resolves, advertised=advertised)


def _bin_resolves(spec: ToolSpec) -> bool:
    """True if the tool's OWN bin is reachable: the managed symlink, or a PATH hit into its repo.

    The managed symlink (``<bin_dir>/<name>``) is authoritative. A bare ``shutil.which(name)`` is
    NOT trusted on its own — a same-named foreign binary (Taskwarrior's ``task``, another ``draw``)
    would falsely read as installed. So a PATH hit counts only when its real path lands inside the
    declared ``repo`` (the tool's entry, however many symlinks deep). Unknowable cases (the repo
    isn't on disk) fall back to the managed-symlink check alone.
    """
    if spec.managed_bin.exists() or spec.managed_bin.is_symlink():
        return True
    found = shutil.which(spec.name)
    if not found:
        return False
    try:
        real = Path(found).resolve()
        return spec.repo.resolve() in real.parents or real == spec.repo.resolve()
    except OSError:
        return False


@dataclass
class ToolPlan:
    """The resolved set of tool specs an apply/drift run shares (carried via the action)."""

    specs: list[ToolSpec] = field(default_factory=list)


def plan_from_action_options(options: dict) -> ToolPlan:
    """Rebuild a :class:`ToolPlan` from the action options the plan stored.

    The plan can't put dataclasses in ``Action.options`` (it stays JSON-ish for describe()), so it
    stores each spec as a small dict; this rehydrates them so apply + drift work from typed specs.
    """
    specs = [
        ToolSpec(name=s["name"], repo=Path(s["repo"]), bin_dir=Path(s["bin_dir"]))
        for s in options.get("specs", [])
    ]
    return ToolPlan(specs=specs)


def spec_to_option(spec: ToolSpec) -> dict[str, str]:
    """Serialize a spec into the JSON-ish dict the plan stores in ``Action.options``."""
    return {"name": spec.name, "repo": str(spec.repo), "bin_dir": str(spec.bin_dir)}
