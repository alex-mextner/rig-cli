"""Drift detection — compares the config-declared state against on-disk reality.

Drift is surfaced in **both directions** and never silently reconciled:

- **config→disk (missing)**: rig.yaml declares item X but it is absent / differs on disk.
- **disk→config (extra)**: an installed item Z exists on disk but is not declared in
  rig.yaml (orphan / hand-added).

The plan tells us what *should* be on disk; this module walks the resolved targets and
diffs them. ``rig status`` renders the result; ``rig apply`` converges the
config→disk side (it does not delete extras — extras are reported for the human to
decide, per the "surface, don't auto-reconcile" rule).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .actions import fsutil
from .actions.runner import (
    _ci_companion_files,
    _find_marker_lines,
    _git_global,
    _launchctl_gui_loaded,
    _launchctl_loaded,
    _read_crontab,
    _tmux_dry_run,
    _resolve_excludes_target,
    build_hook_descriptor,
    crontab_with_managed,
    descriptor_text,
    desired_harness_value,
    desired_permission_specs,
    find_managed_bridge_hook,
    github_actions_state,
    github_ghas_state,
    github_merge_state,
    github_ruleset_state,
    harness_settings_file,
    hook_bridge_entries,
    managed_bridge_hook_in_sync,
    desired_mcp_server_entry,
    permissions_settings_file,
    _is_rig_import_line,
    resolve_agents_md,
    _linter_label,
    resolve_ci_workflow,
    resolve_global_excludes,
    resolve_linter_config,
    repo_self_hosts_ship,
    resolve_ship_delegator,
    ship_env_file_content,
    ship_env_file_path,
    schedule_plan_from_action,
    skill_harness_link_target,
    tg_ctl_plan_from_action,
    tmux_plan_from_action,
)
from .config import GITIGNORE_BEGIN_MARKER, linter_path_escapes_repo
from .github_ruleset import DEFAULT_RULESET_NAME
from .plan import Action, InstallPlan
from . import project_tools
from .tg_ctl import STALE_PREDECESSOR_LABEL


@dataclass
class DriftItem:
    direction: str  # "missing" (config→disk) | "extra" (disk→config) | "modified"
    category: str
    item: str
    target: Path
    detail: str


@dataclass
class DriftReport:
    items: list[DriftItem] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.items

    def by_direction(self, direction: str) -> list[DriftItem]:
        return [i for i in self.items if i.direction == direction]


def detect(
    plan: InstallPlan,
    *,
    scan_skill_dirs: list[Path] | None = None,
    scan_ci_dirs: list[Path] | None = None,
    scan_mcp_files: list[Path] | None = None,
    scan_hook_dirs: list[Path] | None = None,
) -> DriftReport:
    """Compute drift for a resolved plan against current disk state.

    The ``scan_*`` arguments are configured target locations that should be scanned for
    disk→config extras EVEN IF no action targets them (e.g. ``ci: {all: false}`` yields zero
    CI actions, but undeclared workflows on disk are still drift; an MCP config with no
    declared servers should still surface undeclared entries). The caller passes the
    resolved category targets so the extras scan is complete.
    """
    report = DriftReport()
    declared_skill_dirs: dict[Path, set[str]] = {d: set() for d in (scan_skill_dirs or [])}
    declared_ci_dirs: dict[Path, set[str]] = {d: set() for d in (scan_ci_dirs or [])}
    declared_mcp: dict[Path, set[str]] = {f: set() for f in (scan_mcp_files or [])}
    declared_hook_dirs: dict[Path, set[str]] = {d: set() for d in (scan_hook_dirs or [])}

    for action in plan.actions:
        if action.kind == "copy_skill":
            declared_skill_dirs.setdefault(action.target.parent, set()).add(action.target.name)
            _check_copy_skill(action, report)
        elif action.kind == "link_skill_harness":
            _check_skill_harness_link(action, report)
        elif action.kind == "install_agent_hook":
            _check_agent_hook(action, report)
            descriptor = action.options.get("descriptor")
            if descriptor:
                declared_hook_dirs.setdefault(action.target, set()).add(descriptor)
        elif action.kind == "install_ci":
            _check_ci(action, report)
            if action.options.get("slot") != "ship":
                declared_ci_dirs.setdefault(action.target, set()).add(f"{action.item}.yml")
        elif action.kind == "install_dispatcher":
            _check_dispatcher(action, report)
        elif action.kind == "register_mcp":
            _check_mcp(action, report)
            cf = _mcp_config_file(action)
            server_key = str(action.options.get("server") or action.item)
            declared_mcp.setdefault(cf, set()).add(server_key)
        elif action.kind == "apply_harness":
            _check_harness(action, report)
        elif action.kind == "provision_permissions":
            _check_permissions(action, report)
        elif action.kind == "register_hook_bridge":
            _check_hook_bridge(action, report)
        elif action.kind == "provision_schedule":
            _check_schedule(action, report)
        elif action.kind == "provision_agents_symlink":
            _check_agents_symlink(action, report)
        elif action.kind == "provision_ship_delegator":
            _check_ship_delegator(action, report)
        elif action.kind == "provision_linter_config":
            _check_linter_config(action, report)
        elif action.kind == "provision_project_tool":
            _check_project_tool(action, report)
        elif action.kind == "provision_github_ruleset":
            _check_github_ruleset(action, report)
        elif action.kind == "provision_github_merge":
            _check_github_merge(action, report)
        elif action.kind == "provision_github_ghas":
            _check_github_ghas(action, report)
        elif action.kind == "provision_github_actions":
            _check_github_actions(action, report)
        elif action.kind == "provision_github_browser":
            pass  # the agent-browser backend has no cheap read-back; status doesn't probe the UI
        elif action.kind == "provision_tmux":
            _check_tmux(action, report)
        elif action.kind == "provision_global_excludes":
            _check_global_excludes(action, report)
        elif action.kind == "provision_tools":
            _check_tools(action, report)
        elif action.kind == "provision_tg_ctl":
            _check_tg_ctl(action, report)

    _extras_skills(declared_skill_dirs, report)
    _extras_ci(declared_ci_dirs, report)
    _extras_mcp(declared_mcp, report)
    _extras_hooks(declared_hook_dirs, report)
    return report


def _extras_hooks(declared_hook_dirs: dict[Path, set[str]], report: DriftReport) -> None:
    """Flag agent-hook descriptors on disk (``*.json``) not declared in config.

    Dispatcher *fragments* are intentionally NOT flagged: ``global-hooks.d`` is a shared
    drop-in namespace where other tools' fragments legitimately coexist, so undeclared
    fragments there are expected, not drift.
    """
    for hook_dir, declared in declared_hook_dirs.items():
        if not hook_dir.is_dir():
            continue
        for entry in sorted(hook_dir.glob("*.json")):
            if entry.name not in declared:
                report.items.append(
                    DriftItem("extra", "agent_hooks", entry.stem, entry, "hook descriptor on disk but not declared in config")
                )


def _extras_skills(declared_skill_dirs: dict[Path, set[str]], report: DriftReport) -> None:
    for skills_dir, declared in declared_skill_dirs.items():
        if not skills_dir.is_dir():
            continue
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in declared:
                continue
            if (entry / "SKILL.md").is_file():  # only flag things that look like skills
                report.items.append(
                    DriftItem("extra", "skills", entry.name, entry, "installed on disk but not declared in config")
                )


def _extras_ci(declared_ci_dirs: dict[Path, set[str]], report: DriftReport) -> None:
    for wf_dir, declared in declared_ci_dirs.items():
        if not wf_dir.is_dir():
            continue
        for entry in sorted(wf_dir.iterdir()):
            if entry.suffix not in (".yml", ".yaml") or not entry.is_file():
                continue
            if entry.name in declared:
                continue
            report.items.append(
                DriftItem("extra", "ci", entry.stem, entry, "workflow on disk but not declared in config")
            )


def _extras_mcp(declared_mcp: dict[Path, set[str]], report: DriftReport) -> None:
    for cf, declared in declared_mcp.items():
        if not cf.is_file():
            continue
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
        except ValueError:
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        if not isinstance(servers, dict):
            continue
        for name in sorted(servers):
            if name not in declared:
                report.items.append(
                    DriftItem("extra", "mcp", name, cf, "MCP entry registered but not declared in config")
                )


def _check_copy_skill(action: Action, report: DriftReport) -> None:
    if not action.target.exists():
        report.items.append(
            DriftItem("missing", "skills", action.item, action.target, "declared, not on disk")
        )
    elif not fsutil.dirs_identical(action.source, action.target):
        report.items.append(
            DriftItem("modified", "skills", action.item, action.target, "on disk differs from source")
        )


def _check_skill_harness_link(action: Action, report: DriftReport) -> None:
    """Flag drift on a skill's harness-discovery symlink.

    missing  — no symlink (and no real dir) at the harness path: the harness won't list the
               skill. apply creates it.
    modified — a symlink pointing at the WRONG destination: apply re-points it.
    A REAL (non-symlink) dir/file at the path is NOT flagged — it's a legitimately
    hand-authored skill rig must not touch, so reporting it as drift (which apply ignores
    anyway) would be misleading noise.
    """
    link_path, dest = skill_harness_link_target(action)
    if link_path.is_symlink():
        from .actions.runner import _same_link_dest

        try:
            current = link_path.readlink()
        except OSError:
            return
        if not _same_link_dest(link_path, current, dest):
            report.items.append(
                DriftItem("modified", "skills", f"{action.item} (harness link)", link_path,
                          f"harness symlink points elsewhere, expected → {dest}")
            )
        return
    if link_path.exists():
        # a real dir/file occupies the harness path — not rig's to manage; not drift.
        return
    report.items.append(
        DriftItem("missing", "skills", f"{action.item} (harness link)", link_path,
                  "skill not symlinked into harness dir (harness won't list it)")
    )


def _check_agents_symlink(action: Action, report: DriftReport) -> None:
    """Flag drift on the AGENTS.md (canonical) + CLAUDE.md (symlink) invariant.

    Switches on the SAME :func:`resolve_agents_md` ``state`` apply uses, so status and apply read
    one classification. Any non-``ok`` state is drift: the desired end-state is one real
    canonical + a correct symlink, and a repo that isn't there is out of sync. As everywhere
    else in rig, ``on_conflict`` governs only whether ``apply`` *reconciles* the drift, not
    whether ``status`` *reports* it — so two identical real files (``converge``) read as drift
    even though an ``on_conflict: skip`` apply would decline to collapse them (same as a
    skip'd modified skill staying visible as drift).
    """
    r = resolve_agents_md(action.target)
    if r.state == "ok":
        return
    if r.state == "create_both":
        report.items.append(
            DriftItem("missing", "agents_md", "symlink", r.canonical_path,
                      "AGENTS.md/CLAUDE.md not present (apply creates canonical + symlink)")
        )
    elif r.state == "create_link":
        report.items.append(
            DriftItem("missing", "agents_md", "symlink", r.link,
                      f"{r.link.name} missing (apply symlinks it → {r.canonical})")
        )
    elif r.state == "converge":
        report.items.append(
            DriftItem("modified", "agents_md", "symlink", r.link,
                      "AGENTS.md and CLAUDE.md are identical real files "
                      "(apply converges to a symlink under on_conflict=backup/overwrite)")
        )
    elif r.state == "conflict":
        report.items.append(
            DriftItem("modified", "agents_md", "symlink", r.link, r.detail)
        )


def _nearest_existing_ancestor(path: Path) -> Path | None:
    """The deepest ancestor of ``path`` (inclusive) that exists on disk (lexists), or ``None``.

    Used to diagnose a blocked ``mkdir(parents=True)``: if the returned ancestor is not a
    directory, creating ``path`` errors there. ``lexists`` so a dangling symlink counts as the
    blocker it really is (mkdir errors on it too).
    """
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            return candidate
    return None


def check_ship_env_for_dropped_repo_action(action: Action, report: DriftReport) -> None:
    """The ``ship_env`` half of the ship-delegator drift check, for a NON-GIT status run.

    ``rig status`` in a non-git dir drops repo-scoped actions before drift detection (they have
    no repo layer to report under) — but ``rig apply`` does NOT drop them, and it still
    reconciles the MACHINE-level env file there (the delegator write skips only the git
    ``info/exclude`` part). Dropping the whole action would therefore hide GLOBAL ship_env drift
    apply would repair — status could look clean in ``~`` while every portable delegator on the
    machine exits 127. This runs just the env-file check for such a dropped action, keeping
    status/apply parity for the machine-wide artifact.

    A non-git target is never self-hosting (``repo_self_hosts_ship`` needs a git toplevel), so
    no self-hosting skip applies here. A malformed action (no ``canonical_ship``) fails CLOSED,
    mirroring the in-git guard: apply errors on it (it cannot render the env file), so status
    must flag it too — but emitted under ``ship_env`` (GLOBAL, renderable outside git), since
    the repo-categorised ``ship_delegator`` item has no repo layer to render under here.
    """
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", ship_env_file_path(),
                      "ship_delegator action has no canonical_ship (malformed plan; apply "
                      "errors on it and cannot render the machine env file)")
        )
        return
    _check_ship_env_file(Path(raw_canonical), report)


def _check_ship_env_file(canonical: Path, report: DriftReport) -> None:
    """Flag drift on the MACHINE-level ``agent-tools/env`` file (category ``ship_env``).

    Parity with the runner's three outcomes: a NON-FILE at the path or an unreadable file makes
    apply ERROR (not rewrite), so report those as ``modified`` with the real failure — never a
    misleading "missing (apply rewrites it)". Only a genuinely absent file is ``missing``.
    ``is_symlink() or lexists-and-not-file`` matches ``_reconcile_ship_env_file`` exactly: a
    dangling symlink, a dir, or ANY symlink is a non-file apply refuses to replace. A NON-DIRECTORY
    on the PARENT chain (e.g. ``~/.config/agent-tools`` exists as a file) is the same class: the
    env path itself does not exist, but apply's ``mkdir(parents=True)`` errors on it — so it must
    be ``modified`` with the real failure, never "missing (apply rewrites it)".
    """
    env_path = ship_env_file_path()
    if not os.path.lexists(env_path):
        blocker = _nearest_existing_ancestor(env_path.parent)
        # is_dir() follows symlinks, matching mkdir(parents=True, exist_ok=True): a symlink
        # RESOLVING to a dir is traversable (no error); a plain file or dangling link blocks.
        if blocker is not None and not blocker.is_dir():
            report.items.append(
                DriftItem("modified", "ship_env", "env-file", env_path,
                          f"a non-directory blocks the env file's parent path at {blocker} "
                          "(apply errors on it)")
            )
            return
    if env_path.is_symlink() or (os.path.lexists(env_path) and not env_path.is_file()):
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", env_path,
                      "a non-file sits at the machine env file path (apply errors on it)")
        )
        return
    try:
        # UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 byte in the file must
        # yield a drift item (apply errors on it the same way), never crash detect() itself.
        env_current = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
    except (OSError, UnicodeDecodeError) as exc:
        report.items.append(
            DriftItem("modified", "ship_env", "env-file", env_path,
                      f"machine env file unreadable (apply errors on it): {exc}")
        )
        return
    if env_current != ship_env_file_content(canonical):
        report.items.append(
            DriftItem("missing" if env_current is None else "modified",
                      "ship_env", "env-file", env_path,
                      "machine env file with AGENT_TOOLS_ROOT absent/stale (apply rewrites it; "
                      "a delegator with no $AGENT_TOOLS_ROOT and no repo-local ship.sh exits 127)")
        )


def _check_ship_delegator(action: Action, report: DriftReport) -> None:
    """Flag drift on the per-repo ``.claude/scripts/pr-ship.sh`` (``gh ship`` delegator).

    Switches on the SAME :func:`resolve_ship_delegator` ``state`` apply uses, so status and apply
    read one classification:

    - ``ok``       → no drift (file correct + git-ignored).
    - ``create``   → ``missing``: the delegator file is absent (apply writes it + ignores it).
    - ``update``   → ``modified`` when the FILE differs, else ``missing`` when the file is correct
                     but the ``.git/info/exclude`` entry is gone (an un-ignored delegator would
                     dirty the worktree → ship refuses). ``apply`` reconciles either.
    - ``io_error`` → ``modified``: a directory/unreadable sits at the delegator path; apply errors.

    Also byte-compares the MACHINE-level env file (``$XDG_CONFIG_HOME/agent-tools/env`` — where the
    agent-tools root lives now that the delegator itself is a portable constant): a missing/stale
    env file is drift apply repairs, and without it a provisioned delegator (absent an explicit
    ``$AGENT_TOOLS_ROOT`` and a repo-local ship.sh) exits 127. Its items are emitted under the
    ``ship_env`` category — classified GLOBAL in :mod:`riglib.layers`, because the file is a
    machine-wide artifact, not this repo's — so ``rig status`` renders it under the machine layer
    instead of blaming the repo. (The check runs from the repo-scoped ship_delegator action — for
    ANY non-self-hosting target, git or plain dir, mirroring apply, which reconciles the env file
    for those targets too; only a self-hosting repo skips it, see below.)
    """
    # Mirror the runner's fail-closed guard: a malformed action with no canonical_ship must NOT
    # pass silently — apply errors on it (it cannot render the machine env file), so status must
    # flag it too or the two would disagree. Surface it as a modified item.
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        report.items.append(
            DriftItem("modified", "ship_delegator", "delegator", action.target,
                      "ship_delegator action has no canonical_ship (malformed plan)")
        )
        return
    canonical = Path(raw_canonical)
    # A SELF-HOSTING repo (carries its own ci/ship/ship.sh — agent-tools) never reads the env
    # file: the delegator's repo-local branch wins first, and apply skips the env reconcile for
    # it. Skip the env check for such a repo so status can't flag drift apply would never repair
    # (status/apply parity) — every other target, including a non-git dir carrying a stray
    # ci/ship/ship.sh (whose runtime git probe fails, so the env file IS needed), still gets the
    # full check. `repo_self_hosts_ship` is the ONE shared predicate apply uses too.
    if not repo_self_hosts_ship(action.target):
        _check_ship_env_file(canonical, report)
    r = resolve_ship_delegator(action.target)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "ship_delegator", "delegator", r.delegator_path,
                      "pr-ship.sh not provisioned (apply writes it + ignores it in .git/info/exclude)")
        )
    elif r.state == "io_error":
        report.items.append(
            DriftItem("modified", "ship_delegator", "delegator", r.delegator_path, r.detail)
        )
    elif r.state == "update":
        # The two degradations are INDEPENDENT and both must surface (apply fixes both, but status
        # must show the full picture): (a) the FILE differs from the rig-generated delegator, and
        # (b) the .git/info/exclude entry is missing (an un-ignored delegator dirties the worktree →
        # ship refuses). The resolver already classified the file (``file_correct``), so trust it
        # rather than re-reading (avoids a redundant, racy TOCTOU read).
        if not r.file_correct:
            report.items.append(
                DriftItem("modified", "ship_delegator", "delegator", r.delegator_path,
                          "provisioned pr-ship.sh differs from the rig-generated delegator")
            )
        if not r.exclude_ok:
            report.items.append(
                DriftItem("missing", "ship_delegator", "ignore", r.exclude_path or r.delegator_path,
                          "pr-ship.sh present but NOT git-ignored (apply adds it to .git/info/exclude "
                          "so it can't dirty the worktree)")
            )


def _check_linter_config(action: Action, report: DriftReport) -> None:
    """Flag drift on ONE per-repo linter/formatter config file (the ``linters`` block).

    Switches on the SAME :func:`resolve_linter_config` ``state`` apply uses, so status and apply read
    one classification:

    - ``ok``       → no drift (file present + bytes correct).
    - ``create``   → ``missing``: the config file is absent (apply writes it).
    - ``update``   → ``modified``: the file exists but its bytes differ from config (apply rewrites
                     it, backing up a hand-edited file per ``on_conflict``).
    - ``io_error`` → ``modified``: a directory, a symlink, or an unreadable/non-UTF-8 file sits at
                     the path (or the path escapes the repo); apply errors.
    """
    # Do NOT strip rel_path — match the runner + validator (which reject whitespace-padded paths) so
    # status and apply operate on the identical literal value.
    rel_path = str(action.options.get("rel_path", ""))
    content = action.options.get("content")
    role = str(action.options.get("role") or "linter")
    tool = str(action.options.get("tool") or "")
    label = _linter_label(role, tool, str(action.item))
    # Mirror the runner's fail-closed guards (same label scheme, so status and apply name the broken
    # item identically): a malformed action, then a path that escapes the repo. Surface either as a
    # `modified` item so status flags it rather than silently passing.
    # `not content` rejects an empty string too (mirroring the runner + plan builder): the validator
    # requires non-empty content, so an empty one means a synthetic / replayed Action — flag it as
    # drift so status and apply agree it is broken rather than treating a 0-byte write as "in sync".
    if not rel_path or not isinstance(content, str) or not content:
        report.items.append(
            DriftItem("modified", "linters", label, action.target,
                      "linters action is missing rel_path/content (malformed plan)")
        )
        return
    if linter_path_escapes_repo(rel_path):
        report.items.append(
            DriftItem("modified", "linters", label, action.target,
                      f"linters path {rel_path!r} escapes the repo (apply refuses to write it)")
        )
        return
    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "linters", label, r.target_path,
                      f"{rel_path} not provisioned (apply writes it from config)")
        )
    elif r.state == "io_error":
        report.items.append(
            DriftItem("modified", "linters", label, r.target_path, r.detail)
        )
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "linters", label, r.target_path,
                      f"provisioned {rel_path} differs from the rig-managed config")
        )


def _check_project_tool(action: Action, report: DriftReport) -> None:
    """Flag drift for one repo-local project-tool carrier or registration."""
    tool = str(action.options.get("tool") or "project-tool")
    operation = str(action.options.get("operation") or "file")
    label = f"{tool}/{action.item}"
    if operation == "register" and tool == "sverklo":
        registered, detail = project_tools.sverklo_registered(action.target)
        if not registered:
            report.items.append(
                DriftItem(
                    "missing", "project_tools", label, action.target,
                    f"sverklo registry missing this repo ({detail}) — apply runs sverklo register",
                )
            )
        return
    if operation == "reindex" and tool == "sverklo":
        return  # apply-only maintenance; no cheap read-back beyond registration.

    rel_path = str(action.options.get("rel_path") or "")
    content = action.options.get("content")
    if not rel_path or not isinstance(content, str):
        report.items.append(
            DriftItem("modified", "project_tools", label, action.target,
                      "project_tools action is missing rel_path/content (malformed plan)")
        )
        return
    r = project_tools.resolve_entry(action.target, rel_path, content, operation)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "project_tools", label, r.target_path,
                      f"{rel_path} not provisioned (apply writes it from project_tools)")
        )
    elif r.state == "io_error":
        report.items.append(DriftItem("modified", "project_tools", label, r.target_path, r.detail))
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "project_tools", label, r.target_path,
                      f"provisioned {rel_path} differs from project_tools config")
        )


def _check_github_ruleset(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub branch ruleset and the live repo.

    Switches on the SAME :func:`github_ruleset_state` apply uses (one classification, shared via
    the runner), so status and apply can never disagree on what "in sync" means:

    - ``create``   → ``missing``: no rig-managed ruleset on the repo (apply POSTs one).
    - ``update``   → ``modified``: a rig-managed ruleset exists but its rules/bypass/enforcement
                     differ from config (apply PUTs the desired body).
    - ``ok``       → no drift item (in sync).
    - ``no_remote``→ no drift item (a repo with no github origin has nothing to reconcile).
    - ``gh_error`` → a VISIBLE "could not verify" item (not silent in-sync): rig genuinely
                     couldn't reach the ruleset (gh missing / not authed / API error), so it must
                     NOT report the repo as in sync — that would mask a real missing/drifted
                     ruleset behind a green status. It is not a ``missing``/``modified`` (we
                     don't know the on-repo state), but it surfaces so the operator sees rig
                     couldn't check (and `rig apply` would error on the same state).
    """
    state, info = github_ruleset_state(action)
    desired = info.get("desired", {})
    name = desired.get("name", DEFAULT_RULESET_NAME)
    # owner/repo are guaranteed present for the create/update/gh_error states (no_remote never
    # reaches the branches below), so index directly — a future contract break surfaces as a
    # real KeyError instead of a misleading "None/None" in the message.
    if state == "create":
        report.items.append(
            DriftItem("missing", "github", "ruleset", action.target,
                      f"no rig-managed ruleset '{name}' on {info['owner']}/{info['repo']} (apply creates it)")
        )
    elif state == "update":
        report.items.append(
            DriftItem("modified", "github", "ruleset", action.target,
                      f"ruleset '{name}' on {info['owner']}/{info['repo']} differs from config (apply converges it)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "ruleset", action.target,
                      f"could not verify ruleset '{name}' on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_merge(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub merge-button policy and the live repo.

    Switches on the SAME :func:`github_merge_state` apply uses (one classification): ``update`` →
    ``modified`` (live differs, apply PATCHes); ``ok``/``no_remote`` → no item; ``gh_error`` → a
    VISIBLE "could not verify" item (never a silent in-sync — that would mask real drift behind a
    green status; gh missing / not authed / no admin / API error).
    """
    state, info = github_merge_state(action)
    if state == "update":
        report.items.append(
            DriftItem("modified", "github", "merge", action.target,
                      f"merge policy on {info['owner']}/{info['repo']} differs from config (apply converges it)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "merge", action.target,
                      f"could not verify merge policy on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_ghas(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GHAS settings and the live repo.

    Switches on the SAME :func:`github_ghas_state` apply uses: ``update`` → ``modified``;
    ``ok``/``no_remote`` → no item; ``gh_error`` (the repo read itself failed) → a VISIBLE "could
    not verify" item. The signal covers the ``security_and_analysis`` block, the vuln-alerts /
    Dependabot sub-resources, and code-scanning. A scanner that couldn't be READ (plan-gated on a
    private repo) is reported in the same ``modified`` item (it forces ``update``) with an explicit
    "could not verify" suffix, so status is honest that one scanner's state is unknown — rather than
    masking it behind a green status (the old behavior turned this into a whole-block gh_error that
    also blocked applying the free features).
    """
    state, info = github_ghas_state(action)
    if state == "update":
        detail = f"security settings on {info['owner']}/{info['repo']} differ from config (apply converges them)"
        unverifiable = info.get("unverifiable") or []
        if unverifiable:
            detail += " — could not verify: " + "; ".join(unverifiable)
        report.items.append(DriftItem("modified", "github", "ghas", action.target, detail))
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "ghas", action.target,
                      f"could not verify security settings on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_github_actions(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured GitHub Actions permissions and the live repo.

    Switches on the SAME :func:`github_actions_state` apply uses: ``update`` → ``modified``;
    ``ok``/``no_remote`` → no item; ``gh_error`` → a VISIBLE "could not verify" item.
    """
    state, info = github_actions_state(action)
    if state == "update":
        report.items.append(
            DriftItem("modified", "github", "actions", action.target,
                      f"Actions permissions on {info['owner']}/{info['repo']} differ from config (apply converges them)")
        )
    elif state == "gh_error":
        report.items.append(
            DriftItem("modified", "github", "actions", action.target,
                      f"could not verify Actions permissions on {info.get('owner')}/{info.get('repo')} "
                      f"({info.get('detail', 'gh api failed')}) — status unknown, not confirmed in sync")
        )


def _check_agent_hook(action: Action, report: DriftReport) -> None:
    descriptor = action.target / (action.options.get("descriptor") or "")
    if not descriptor.exists():
        report.items.append(
            DriftItem("missing", "agent_hooks", action.item, descriptor, "descriptor not installed")
        )
        return
    # content comparison: an edited cmd/on_error (or a config on_error change) is drift —
    # apply would replace it. Build the expected descriptor the same way the install does.
    try:
        spec, _ = build_hook_descriptor(action)
        expected = descriptor_text(spec)
    except (OSError, ValueError):
        return  # unreadable source — the install action surfaces this, not drift
    if descriptor.read_text(encoding="utf-8") != expected:
        report.items.append(
            DriftItem("modified", "agent_hooks", action.item, descriptor, "descriptor on disk differs from config")
        )


def _check_ci(action: Action, report: DriftReport) -> None:
    if action.options.get("slot") == "ship":
        ship = action.target / "ship"
        if not ship.exists():
            report.items.append(
                DriftItem("missing", "ci", "ship", ship, "ship not installed")
            )
            return
        src_ship = action.source / "ship.sh"
        if src_ship.is_file() and ship.read_text(encoding="utf-8") != src_ship.read_text(encoding="utf-8"):
            report.items.append(
                DriftItem("modified", "ci", "ship", ship, "installed ship differs from source")
            )
        return
    wf = action.target / f"{action.item}.yml"
    if not wf.is_file():
        # absent, or a stale directory/non-file where the workflow should be — both are
        # drift apply would resolve (file-vs-dir is a recoverable conflict on apply).
        report.items.append(
            DriftItem("missing", "ci", action.item, wf,
                      "workflow not written" if not wf.exists() else "target is not a regular file")
        )
        return
    # content comparison: a workflow edited in place (e.g. a job disabled) is drift even
    # though the file still exists — apply would replace it.
    slot = action.options.get("slot", action.item)
    src_wf = resolve_ci_workflow(action.source, slot, action.options.get("variant"))
    if src_wf is None or not src_wf.is_file():
        return
    if wf.read_text(encoding="utf-8") != src_wf.read_text(encoding="utf-8"):
        report.items.append(
            DriftItem("modified", "ci", action.item, wf, "on disk differs from source workflow")
        )
    # vendored companion scripts — a deleted/edited one breaks the gate that apply would
    # recreate, so it is config→disk drift too. Companions install at their required paths
    # relative to the checkout root (passed explicitly, independent of ci.target).
    repo_root = Path(action.options.get("repo_root") or action.target.parent.parent)
    for comp, rel in _ci_companion_files(action.source, src_wf):
        dst = repo_root / rel
        if not dst.exists():
            report.items.append(
                DriftItem("missing", "ci", f"{slot}:{comp.name}", dst, "CI companion script not installed")
            )
        elif dst.read_text(encoding="utf-8") != comp.read_text(encoding="utf-8"):
            report.items.append(
                DriftItem("modified", "ci", f"{slot}:{comp.name}", dst, "CI companion differs from source")
            )


def check_disabled_dispatcher(repo_root: Path, report: DriftReport) -> None:
    """Flag a still-installed global dispatcher when the config disables it.

    apply never deletes; so a repo that previously installed the dispatcher keeps git's
    global ``core.hooksPath`` pointing at the composer dir even after the config turns it
    off. Detect that (the global core.hooksPath dir contains a rig composer that references
    ``run-global-hooks``) and report it as disk→config drift.
    """
    current = _git_global("core.hooksPath")
    if not current:
        return
    pre_commit = Path(current) / "pre-commit"
    if pre_commit.is_file():
        try:
            body = pre_commit.read_text(encoding="utf-8")
        except OSError:
            return
        if "run-global-hooks" in body:
            report.items.append(
                DriftItem(
                    "extra", "git_hooks", "dispatcher", Path(current),
                    "dispatcher disabled in config but still wired as global core.hooksPath",
                )
            )


def _check_dispatcher(action: Action, report: DriftReport) -> None:
    runner = Path(action.options["runner"])
    src_runner = action.source / "run-global-hooks"
    if not runner.exists():
        report.items.append(
            DriftItem("missing", "git_hooks", "dispatcher", runner, "runner not installed")
        )
    elif src_runner.is_file() and runner.read_text(encoding="utf-8") != src_runner.read_text(encoding="utf-8"):
        report.items.append(
            DriftItem("modified", "git_hooks", "run-global-hooks", runner, "runner differs from source")
        )
    # the composer dir is the real core.hooksPath target — compare EVERY shipped composer
    # (pre-commit/commit-msg/pre-push/review-gate): a missing or edited one means that hook
    # event stops invoking the dispatcher even though apply would recreate it.
    composer = runner.parent / "hooks"
    src_hooks = action.source / "hooks"
    if src_hooks.is_dir():
        for src_composer in sorted(p for p in src_hooks.iterdir() if p.is_file()):
            dst = composer / src_composer.name
            if not dst.exists():
                report.items.append(
                    DriftItem("missing", "git_hooks", src_composer.name, dst, "composer hook not installed")
                )
            elif dst.read_text(encoding="utf-8") != src_composer.read_text(encoding="utf-8"):
                report.items.append(
                    DriftItem("modified", "git_hooks", src_composer.name, dst, "composer hook differs from source")
                )
    # if the config wired core.hooksPath, verify git still points at the composer dir —
    # files can exist while git no longer invokes them (someone re-set core.hooksPath).
    if action.options.get("set_global_hooks_path"):
        current = _git_global("core.hooksPath")
        if current != str(composer):
            report.items.append(
                DriftItem(
                    "modified", "git_hooks", "dispatcher", composer,
                    f"global core.hooksPath is {current or 'unset'}, expected {composer}",
                )
            )

    # each enabled, shipped fragment must be present + match the source on disk — a deleted
    # or edited fragment is config→disk drift that apply would recreate.
    src_frag = action.source / "global-hooks.d"
    disabled = {
        name
        for name, spec in (action.options.get("fragments", {}) or {}).items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    }
    if src_frag.is_dir():
        for event_dir in sorted(p for p in src_frag.iterdir() if p.is_dir()):
            for frag in sorted(event_dir.iterdir()):
                if not frag.is_file():
                    continue
                dst = action.target / event_dir.name / frag.name
                if any(name in frag.name for name in disabled):
                    # explicitly disabled — but install doesn't delete; a leftover copy
                    # still runs in every repo, so surface it as disk→config drift.
                    if dst.exists():
                        report.items.append(
                            DriftItem("extra", "git_hooks", frag.name, dst,
                                      "fragment disabled in config but still installed (will still run)")
                        )
                    continue
                if not dst.exists():
                    report.items.append(
                        DriftItem("missing", "git_hooks", frag.name, dst, "dispatcher fragment not installed")
                    )
                elif dst.read_text(encoding="utf-8") != frag.read_text(encoding="utf-8"):
                    report.items.append(
                        DriftItem("modified", "git_hooks", frag.name, dst, "dispatcher fragment differs from source")
                    )


def _check_global_excludes(action: Action, report: DriftReport) -> None:
    """Flag drift in the GLOBAL git-excludes provisioning (a GLOBAL-section drift item).

    Two coupled checks, mirroring apply's two steps:
      1. ``core.excludesfile`` — if it is unset (and no override pins a file), rig WOULD set it;
         surface that as ``missing`` so ``status`` says "apply will wire core.excludesfile". The
         resolution goes through the SAME :func:`_resolve_excludes_target` apply uses, so status and
         apply agree on the target file and whether git config needs writing.
      2. The managed block — switch on the SAME :func:`resolve_global_excludes` ``state`` apply uses:

         - ``create``   → ``missing``: no excludes file or no managed block (apply adds it).
         - ``update``   → ``modified``: a managed block exists but differs, OR the file has
                          duplicated rig-managed blocks (apply collapses to one correct block).
         - ``ok``       → no block drift item (in sync).
         - ``conflict`` → ``modified``: unbalanced markers rig won't rewrite — surfaced so the
                          operator reconciles by hand (apply leaves it untouched).
         - ``io_error`` → ``modified``: the file couldn't be read. NOT silently in-sync — rig
                          couldn't even inspect it, so a green status would mask an un-provisioned
                          ignore.
    """
    entries = [str(e) for e in action.options.get("entries", [])]
    target, needs_set, set_value = _resolve_excludes_target(action)
    if needs_set:
        report.items.append(
            DriftItem(
                "missing", "gitignore", "core.excludesfile", target,
                f"global core.excludesfile is unset (apply sets it → {set_value})",
            )
        )
    r = resolve_global_excludes(target, entries)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "gitignore", "block", target,
                      "rig-managed global-excludes block not present (apply adds it)")
        )
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "gitignore", "block", target,
                      "rig-managed global-excludes block differs from config (apply reconciles it)")
        )
    elif r.state in ("conflict", "io_error"):
        report.items.append(
            DriftItem("modified", "gitignore", "block", target, r.detail)
        )


def check_disabled_global_excludes(action: Action, report: DriftReport) -> None:
    """Flag a still-installed managed block when the config disables the ``gitignore`` category.

    apply never deletes; so a machine that previously provisioned the global-excludes block keeps
    it in ``core.excludesfile`` even after the config turns the category off. With the action gone
    from the plan, ``_check_global_excludes`` never runs — so without this scan the leftover block
    would report as "in sync". Resolve the target the SAME way apply does and report a present begin
    marker as disk→config drift (mirrors :func:`check_disabled_dispatcher`). Marker detection reuses
    :func:`_find_marker_lines` (the same offset-based scanner :func:`resolve_global_excludes` uses)
    read with newline translation off, so detection never diverges from apply.
    """
    target, _needs_set, _set_value = _resolve_excludes_target(action)
    if not target.is_file():
        return
    try:
        with target.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError:
        return
    if _find_marker_lines(content, GITIGNORE_BEGIN_MARKER):
        report.items.append(
            DriftItem("extra", "gitignore", "block", target,
                      "gitignore disabled in config but the rig-managed block is still in the global excludes file")
        )


def _check_harness(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured harness auto/permission mode and the file on disk.

    missing  — the settings file or the managed key is absent.
    modified — the managed key on disk has a different value than the config declares
               (someone flipped auto-mode off, or the harness rewrote it). apply converges.
    Only the single managed key is compared; other settings in the file are irrelevant here.
    """
    (section, key), value = desired_harness_value(action)
    config_file = harness_settings_file(action)
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, "harness settings file not written")
        )
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    sect = data.get(section, {}) if isinstance(data, dict) else {}
    current = sect.get(key) if isinstance(sect, dict) else None
    if current is None:
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, f"{section}.{key} not set")
        )
    elif current != value:
        report.items.append(
            DriftItem(
                "modified", "harness", action.item, config_file,
                f"{section}.{key} is '{current}', config declares '{value}'",
            )
        )


def _check_permissions(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured permissions layer and the harness settings file.

    One action spans EVERY container rig manages (rig-cli#100): the allow list plus, for
    claude-code, the deny/ask rule baselines. Per container:

    missing  — the settings file is absent, the container is absent, OR a desired entry is not
               present in it. ``rig apply`` ADDS the missing entries (additive merge — never
               removing the user's own).
    modified — the settings file is malformed JSON, OR (object form) a desired entry's KEY is
               present but its value is not ``"allow"`` (the user set it to ``ask``/``deny`` — that
               is the user's call, surfaced so status isn't a silent green, but apply does NOT
               downgrade it).
    extra    — an entry in the container BEYOND the rig-managed baseline. Hand-edits to the
               permissions lists must be visible drift, not silent config rot — but rig NEVER
               deletes them (see :func:`_report_permission_extras` for the allow-vs-deny/ask
               reporting shape).

    Shares :func:`desired_permission_specs` with the install handler so apply and status never
    diverge on what rig manages.
    """
    specs = desired_permission_specs(action)
    config_file = permissions_settings_file(action)
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "permissions", action.item, config_file, "harness settings file not written")
        )
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "permissions", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    # A non-object root is a SHAPE problem apply ERRORS on — report `modified` (not a stream of
    # `missing` entries), so status and apply agree on what the operator must fix.
    if not isinstance(data, dict):
        report.items.append(
            DriftItem("modified", "permissions", action.item, config_file, "harness settings file is not a JSON object")
        )
        return
    # Two passes on purpose: apply errors out on the FIRST shape problem and fixes NOTHING in the
    # file, so when ANY container is mis-shaped the per-entry "apply adds it" claims would be false
    # — report only the shape `modified` items then (status and apply agree on what to fix first).
    resolved: list[tuple[object, object]] = []
    shape_errors: list[str] = []
    for ps in specs:
        node, err = _resolve_permission_container(data, ps)
        if err is not None:
            # dedup by detail: a non-dict `permissions` node yields the SAME error for every
            # container spec — apply raises it once, so status reports it once too
            if err not in shape_errors:
                shape_errors.append(err)
        else:
            resolved.append((ps, node))
    if shape_errors:
        for err in shape_errors:
            report.items.append(DriftItem("modified", "permissions", action.item, config_file, err))
        return
    for ps, node in resolved:
        _check_permission_entries(action, ps, node, config_file, report)


def _resolve_permission_container(data: dict, ps) -> tuple[object, str | None]:
    """Walk (read-only) to ``ps``'s container → ``(node, shape_error)``.

    ``shape_error`` is the `modified` detail when an INTERMEDIATE segment exists as a non-object
    or the container itself has the wrong type — the same problems apply hard-errors on (it can't
    create a container under a scalar / merge into a wrong-typed one). An ABSENT intermediate or
    container is fine (``node=None`` — apply creates it), never a shape error.
    """
    dotted = ".".join(ps.key_path)
    node: object = data
    for seg in ps.key_path[:-1]:
        nxt = node.get(seg) if isinstance(node, dict) else None
        if nxt is not None and not isinstance(nxt, dict):
            # container-AGNOSTIC wording on purpose: a scalar `permissions` node blocks every
            # container, and naming one container per spec would emit 3 items for 1 problem —
            # the caller dedups identical details, so this reports once (matching apply's one error)
            return None, f"'{seg}' in the harness settings is not an object"
        node = nxt
        if node is None:
            return None, None
    node = node.get(ps.key_path[-1]) if isinstance(node, dict) else None
    if node is not None:
        if ps.container == "array" and not isinstance(node, list):
            return None, f"{dotted} is not an array"
        if ps.container == "object" and not isinstance(node, dict):
            return None, f"{dotted} is not an object"
    return node, None


def _check_permission_entries(action: Action, ps, node, config_file: Path, report: DriftReport) -> None:
    """Per-entry drift for ONE well-shaped container — missing/modified for the desired entries,
    then the user's extras (see :func:`_report_permission_extras` for the reporting shape)."""
    dotted = ".".join(ps.key_path)
    desired = set(ps.entries)
    if ps.container == "array":
        raw_list = node if isinstance(node, list) else []
        junk = [e for e in raw_list if not isinstance(e, str)]
        if junk:
            # apply works AROUND non-string junk (string-only membership) but never removes it —
            # surface it, or status would look clean over a hand-mangled list (apply/status parity)
            report.items.append(
                DriftItem("modified", "permissions", action.item, config_file,
                          f"{dotted} contains {len(junk)} non-string entr{'y' if len(junk) == 1 else 'ies'} "
                          "(hand-edit — apply ignores them, prune by hand)")
            )
        present_list = [e for e in raw_list if isinstance(e, str)]
        present = set(present_list)
        for entry in ps.entries:
            if entry not in present:
                report.items.append(
                    DriftItem("missing", "permissions", action.item, config_file,
                              f"'{entry}' not in {dotted} (apply adds it)")
                )
        _report_permission_extras(action, ps, [e for e in present_list if e not in desired], config_file, report)
        return
    existing = node if isinstance(node, dict) else {}
    for entry in ps.entries:
        if entry not in existing:
            report.items.append(
                DriftItem("missing", "permissions", action.item, config_file,
                          f"'{entry}' not in {dotted} (apply adds it)")
            )
        elif existing.get(entry) != ps.value:
            report.items.append(
                DriftItem("modified", "permissions", action.item, config_file,
                          f"'{entry}' in {dotted} is {existing.get(entry)!r}, not {ps.value!r} "
                          "(user override — apply leaves it)")
            )
    _report_permission_extras(action, ps, [k for k in existing if k not in desired], config_file, report)


def _report_permission_extras(action: Action, ps, extras: list[str], config_file: Path, report: DriftReport) -> None:
    """User entries beyond the rig baseline — REPORTED as drift-extras, NEVER deleted (rig-cli#100).

    The reporting shape differs by role on purpose:
    - ``allow`` extras are SUMMARIZED into one item with a count — the live allowlist accumulates
      hundreds of hand-approved "don't ask again" entries, and a per-entry dump would drown
      ``rig status``. The remedy is named in the item: adopt via ``permissions.allow`` or prune
      by hand.
    - ``deny``/``ask`` extras are PER-ENTRY — those lists are small, and a rule someone slipped
      into deny/ask is a semantically loud event worth naming individually.
    """
    if not extras:
        return
    dotted = ".".join(ps.key_path)
    if ps.role == "allow":
        n = len(extras)
        report.items.append(
            DriftItem("extra", "permissions", action.item, config_file,
                      f"{n} entr{'y' if n == 1 else 'ies'} in {dotted} beyond the rig-managed baseline "
                      "(rig never removes — adopt via permissions.allow in the config, or prune by hand)")
        )
        return
    for entry in extras:
        report.items.append(
            DriftItem("extra", "permissions", action.item, config_file,
                      f"'{entry}' in {dotted} but not in the rig-managed baseline (rig never removes it)")
        )


def _check_hook_bridge(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured cc_hook_bridge wiring and the settings file.

    missing  — the settings file is absent, or a managed dispatcher hook (one whose command
               carries ``cc_hook_bridge``) is not present for an (event, matcher) we ship.
    modified — the settings file is malformed JSON, OR a managed hook is present but its
               TYPE/COMMAND differs from what apply would write (stale PYTHONPATH / moved
               checkout / changed ``hook_bridge.python`` / malformed hook type). ``rig apply``
               rewrites it.
    Drift and apply share ``find_managed_bridge_hook`` + ``hook_bridge_entries`` so they
    never diverge. Only OUR managed blocks are checked; the user's other hooks are ignored.
    """
    config_file = harness_settings_file(action)
    if not config_file.is_file():
        report.items.append(
            DriftItem("missing", "harness", action.item, config_file, "harness settings file not written")
        )
        return
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except ValueError:
        report.items.append(
            DriftItem("modified", "harness", action.item, config_file, "harness settings file is malformed JSON")
        )
        return
    hooks = data.get("hooks") if isinstance(data, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    for event, pairs in hook_bridge_entries(action).items():
        blocks = hooks.get(event)
        for matcher, command in pairs:
            label = f"{event}[{matcher or '*'}]"
            hk = find_managed_bridge_hook(blocks, matcher)
            if hk is None:
                report.items.append(
                    DriftItem("missing", "harness", action.item, config_file,
                              f"cc_hook_bridge not wired for {label}")
                )
            elif not managed_bridge_hook_in_sync(hk, command):
                report.items.append(
                    DriftItem("modified", "harness", action.item, config_file,
                              f"cc_hook_bridge hook for {label} is stale (apply will rewrite)")
                )


def _check_schedule(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured daily model-freshness schedule and disk.

    missing  — the launchd plist / crontab managed line is absent (or, on macOS, the plist
               exists but the job is not loaded into launchd).
    modified — the artifact on disk differs from the desired one (e.g. someone changed the
               run time, or the checker path). ``rig apply`` converges.
    Cross-platform: launchd plist content + loaded state on macOS; the sentinel-fenced
    crontab pair on Linux. Shares the desired-artifact computation with the install handler.
    """
    sched = schedule_plan_from_action(action)
    if sched.platform == "launchd":
        plist = sched.plist_path
        if plist is None or not plist.is_file():
            report.items.append(
                DriftItem("missing", "models", action.item, plist or action.target, "launchd plist not installed")
            )
            return
        if plist.read_text(encoding="utf-8") != sched.plist_xml():
            report.items.append(
                DriftItem("modified", "models", action.item, plist, "launchd plist differs from configured schedule")
            )
            return
        if not _launchctl_loaded(sched.label):
            report.items.append(
                DriftItem("missing", "models", action.item, plist, f"launchd job '{sched.label}' not loaded")
            )
        return
    # crontab branch — position-preserving (a user's lines after rig's block are NOT drift).
    # `crontab_with_managed` returns None iff our managed pair is already present unchanged at
    # its position; a non-None result means an apply WOULD change something → drift.
    _has, current = _read_crontab()
    desired_pair = sched.crontab_lines()
    if not any(sched.label in ln for ln in current.splitlines()):
        report.items.append(
            DriftItem("missing", "models", action.item, action.target, f"crontab line for '{sched.label}' not installed")
        )
    elif crontab_with_managed(current, sched.label, desired_pair) is not None:
        report.items.append(
            DriftItem("modified", "models", action.item, action.target, "crontab schedule differs from configured time/checker")
        )


def _check_tmux(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-MANAGED tmux region only (never the user's hand-written lines).

    missing  — the generated ``rig.tmux.conf`` is absent, OR (import mode) ``~/.tmux.conf``
               lost its ``source-file`` import line, OR (block mode) the managed block is gone.
    modified — the generated ``rig.tmux.conf`` on disk differs from the desired one (a hand
               edit of rig's own file), OR a cc script / the boot plist / the managed block differs.
    Shares the desired-artifact computation with the install handler, so apply and status can
    never disagree. User-specific lines in ``~/.tmux.conf`` are ignored entirely. Every region
    ACCUMULATES into ``report.items`` (no early return), so simultaneously-drifted regions are
    ALL surfaced, not just the first.
    """
    from .tmux import BLOCK_BEGIN, BLOCK_END

    plan = tmux_plan_from_action(action)
    desired_conf = plan.render_rig_conf()  # render once; reused by the block-mode check below.

    # 1) the generated rig.tmux.conf — present + byte-identical to the desired render.
    _file_drift(report, action, plan.rig_conf_path, desired_conf, "generated rig.tmux.conf")

    # 2) the managed scripts (cc-save/cc-restore; attach-or-create when anti-sprawl is on) —
    # the SAME list apply writes (managed_scripts), so drift can't disagree on which exist.
    # Content AND the executable bit: a stripped +x means the resurrect hook can't run them.
    for path, body in plan.managed_scripts():
        _file_drift(report, action, path, body, path.name)
        if path.is_file() and not os.access(path, os.X_OK):
            report.items.append(
                DriftItem("modified", "tmux", action.item, path,
                          f"{path.name} is not executable (resurrect hook can't run it)")
            )
    # When anti-sprawl is DISABLED, apply stops writing tmux-attach.sh but never deletes a
    # previously-installed one — and the user may still source it from their shell rc (so
    # anti-sprawl stays active). Surface the leftover as a disk→config extra, like the boot plist.
    if not plan.anti_sprawl_enabled and plan.attach_path.is_file():
        report.items.append(
            DriftItem("extra", "tmux", action.item, plan.attach_path,
                      "tmux-attach.sh present but tmux.anti_sprawl is disabled "
                      "(it may still be wired from your shell rc — remove it or re-enable anti_sprawl)")
        )

    # 3) the boot launchd plist (macOS). When boot is ENABLED, apply writes it → check content.
    # When boot is DISABLED, apply does NOT delete a previously-written plist, so a leftover
    # plist (it still starts tmux at login) is a disk→config EXTRA — surface it, don't go silent.
    if _on_darwin():
        if plan.boot_enabled:
            _file_drift(report, action, plan.boot_plist_path, plan.render_boot_plist(),
                        "boot launchd plist")
        elif plan.boot_plist_path.is_file():
            report.items.append(
                DriftItem("extra", "tmux", action.item, plan.boot_plist_path,
                          "boot launchd plist present but tmux.boot is disabled "
                          "(it still starts tmux at login — remove it or re-enable boot)")
            )

    # 4) the wiring in ~/.tmux.conf (the managed REGION only — user lines are ignored).
    conf_text = plan.conf_path.read_text(encoding="utf-8") if plan.conf_path.is_file() else ""
    if plan.apply_mode == "block":
        if BLOCK_BEGIN not in conf_text or BLOCK_END not in conf_text:
            report.items.append(
                DriftItem("missing", "tmux", action.item, plan.conf_path,
                          "managed tmux block missing from ~/.tmux.conf")
            )
        elif _managed_block_body(conf_text, BLOCK_BEGIN, BLOCK_END) != desired_conf.strip("\n"):
            report.items.append(
                DriftItem("modified", "tmux", action.item, plan.conf_path,
                          "managed tmux block in ~/.tmux.conf differs from the configured block")
            )
    else:  # import mode — the EXACT import line must be present (not a substring/comment).
        import_line = plan.import_line()
        rig_conf_name = plan.rig_conf_path.name
        if not any(ln.strip() == import_line for ln in conf_text.splitlines()):
            report.items.append(
                DriftItem("missing", "tmux", action.item, plan.conf_path,
                          "source-file import line missing from ~/.tmux.conf")
            )
        # A STALE rig import (a `source-file …/rig.tmux.conf` from an OLD generated_dir) is still
        # active — tmux sources that old managed file — and `rig apply` WOULD remove it. Flag it,
        # so status doesn't read in-sync while a stale managed config is live (codex P2).
        elif any(
            _is_rig_import_line(ln, import_line, rig_conf_name) and ln.strip() != import_line
            for ln in conf_text.splitlines()
        ):
            report.items.append(
                DriftItem("modified", "tmux", action.item, plan.conf_path,
                          "a STALE rig source-file import (old generated_dir) is still in "
                          "~/.tmux.conf — apply removes it")
            )
        else:
            # ORDERING drift: rig appends its import LAST so its generated config (which fixes the
            # Moshi-vs-continuum ordering) wins. If a user added an executable line AFTER the rig
            # import, tmux runs it after rig.tmux.conf and can undo the fix (e.g. a trailing
            # `status-right ''`). apply re-appends the import at the end → flag it (codex P2).
            code = [ln for ln in conf_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if code and code[-1].strip() != import_line:
                report.items.append(
                    DriftItem("modified", "tmux", action.item, plan.conf_path,
                              "rig's source-file import is not the LAST line of ~/.tmux.conf "
                              "(a later line can undo the generated ordering) — apply re-appends it")
                )

    # 5) live activation state (DEFECTS 4/6): the resurrect snapshot dir + the plugin checkouts.
    # apply now MANAGES these; status must surface them so a clean machine's "no snapshot / no
    # plugins" doesn't read as in-sync (codex finding). A missing/partial plugin or absent
    # resurrect dir is `missing` drift apply reconciles. (launchd loaded-state is intentionally
    # NOT checked here — `rig status` must stay read-only + offline, and a `launchctl list` probe
    # is a live-daemon query; the plist content drift above already covers the agent definition.)
    #
    # RIG_TMUX_DRY_RUN suppresses the LIVE half of apply (no plugin clone, no resurrect dir), so
    # status MUST suppress the matching live-state drift too — else status would report drift apply
    # is deliberately not converging (apply/status would disagree, and `rig status` could never read
    # in-sync under the flag, e.g. CI/smoke). The file artifacts (sections 1-4) are written even
    # under dry-run, so their drift stays checked above; only this live section is gated. Reuse the
    # runner's flag helper so the dry-run truthiness has ONE definition across apply + status.
    if _tmux_dry_run():
        return

    from .tmux import PLUGINS

    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        report.items.append(
            DriftItem("missing", "tmux", action.item, resurrect_dir,
                      "~/.tmux/resurrect missing (resurrect writes no snapshot → nothing to "
                      "restore on reboot) — apply creates it")
        )
    plugins_dir = plan.home / ".tmux" / "plugins"
    for name, (_repo, entry) in PLUGINS.items():
        dest = plugins_dir / name
        if not (dest / entry).exists():
            report.items.append(
                DriftItem("missing", "tmux", action.item, dest,
                          f"tmux plugin {name} not installed (the @plugin decl won't resolve) "
                          "— apply clones it")
            )


def _check_tools(action: Action, report: DriftReport) -> None:
    """Flag drift on the personal CLI ecosystem (a declared tool not installed/advertised).

    missing — a declared tool whose bin does NOT resolve (no managed symlink, not on PATH): the
              ecosystem install never ran or was removed. ``rig apply`` runs its install.sh.
    modified — a tool whose bin resolves but whose skill is NOT advertised (no blurb marker):
               it's reachable but agents can't auto-discover it; apply re-runs install.sh to wire
               up ``install-skill``.
    Shares the desired-spec computation (:func:`tools.tool_status`) with the install handler, so
    apply and status can never disagree. Cross-platform (no launchd); pure on-disk reads.
    """
    from . import tools as toolsmod

    plan = toolsmod.plan_from_action_options(action.options)
    for spec in plan.specs:
        st = toolsmod.tool_status(spec)
        if not st.bin_resolves:
            report.items.append(
                DriftItem(
                    "missing", "tools", spec.name, spec.managed_bin,
                    f"tool '{spec.name}' declared but not installed "
                    f"(no bin at {spec.managed_bin}, not on PATH) — apply runs its install.sh",
                )
            )
        elif not st.advertised:
            report.items.append(
                DriftItem(
                    "modified", "tools", spec.name, spec.blurb_file,
                    f"tool '{spec.name}' installed but not advertised "
                    f"(no skill blurb at {spec.blurb_file}) — apply re-runs install-skill",
                )
            )


def _check_tg_ctl(action: Action, report: DriftReport) -> None:
    """Flag drift on the rig-managed tg-ctl LaunchAgent (macOS).

    missing  — boot enabled but the plist is absent, OR present + byte-identical but the agent is
               NOT loaded in the gui domain (launchd never picked it up → daemon not running).
    modified — the plist on disk differs from the desired byte-exact render (a hand edit, or an
               upgrade that changed the args/PATH). ``rig apply`` reconciles + (re)loads.
    extra    — a leftover plist when boot is DISABLED (it still auto-starts the daemon), OR the
               stale predecessor ``com.ultra.codex-tg-bot`` plist (apply boots it out + removes it).
    Off darwin (no launchd) tg-ctl provisioning is a no-op, so there is nothing to check.
    Shares the desired-plist computation with the install handler, so apply and status can never
    disagree.
    """
    if not _on_darwin():
        return
    plan = tg_ctl_plan_from_action(action)

    # the stale predecessor service is always drift while it exists (apply removes it).
    if plan.stale_plist_path.is_file():
        report.items.append(
            DriftItem("extra", "tg_ctl", action.item, plan.stale_plist_path,
                      f"stale predecessor '{STALE_PREDECESSOR_LABEL}' LaunchAgent present — "
                      f"apply boots it out and removes it")
        )

    if not plan.boot_enabled:
        # boot disabled: a leftover managed plist still auto-starts the daemon → surface it as a
        # disk->config extra (apply never deletes the user's own file).
        if plan.plist_path.is_file():
            report.items.append(
                DriftItem("extra", "tg_ctl", action.item, plan.plist_path,
                          "tg-ctl boot plist present but tg_ctl.boot is disabled "
                          "(it still starts the daemon at login — remove it or re-enable boot)")
            )
        return

    desired = plan.render_plist()
    if not plan.plist_path.is_file():
        report.items.append(
            DriftItem("missing", "tg_ctl", action.item, plan.plist_path,
                      "tg-ctl boot LaunchAgent not installed")
        )
        return
    if plan.plist_path.read_text(encoding="utf-8") != desired:
        report.items.append(
            DriftItem("modified", "tg_ctl", action.item, plan.plist_path,
                      "tg-ctl boot LaunchAgent differs from the configured plist")
        )
        return
    if not _launchctl_gui_loaded(plan.boot_label):
        report.items.append(
            DriftItem("missing", "tg_ctl", action.item, plan.plist_path,
                      f"tg-ctl LaunchAgent '{plan.boot_label}' installed but not loaded")
        )


def _file_drift(report: DriftReport, action: Action, path: Path, desired: str, label: str) -> None:
    """Append a ``missing`` (absent) or ``modified`` (content differs) DriftItem for a rig file."""
    if not path.is_file():
        report.items.append(DriftItem("missing", "tmux", action.item, path, f"{label} not installed"))
    elif path.read_text(encoding="utf-8") != desired:
        report.items.append(DriftItem("modified", "tmux", action.item, path, f"{label} differs from generated"))


def _managed_block_body(conf_text: str, begin: str, end: str) -> str | None:
    """The text BETWEEN the managed sentinels (exclusive), or None when absent/malformed."""
    b = conf_text.find(begin)
    e = conf_text.find(end)
    if b == -1 or e == -1 or e <= b:
        return None
    return conf_text[b + len(begin):e].strip("\n")


def _on_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


def _mcp_config_file(action: Action) -> Path:
    target = action.target
    return target if target.suffix == ".json" else target / "mcp.json"


def _check_mcp(action: Action, report: DriftReport) -> None:
    command = str(action.options.get("command", "")).strip()
    if not command:
        return
    server_key = str(action.options.get("server") or action.item)
    config_file = _mcp_config_file(action)
    desired = desired_mcp_server_entry(action.options)
    existing = None
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            existing = servers.get(server_key) if isinstance(servers, dict) else None
        except ValueError:
            existing = None
    if existing is None:
        report.items.append(
            DriftItem("missing", "mcp", server_key, config_file, "MCP entry not registered")
        )
    elif existing != desired:
        report.items.append(
            DriftItem("modified", "mcp", server_key, config_file, "MCP entry differs from config (command/args/env)")
        )
