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
from dataclasses import dataclass, field
from pathlib import Path

from .actions import fsutil
from .actions.runner import (
    _ci_companion_files,
    _git_global,
    _launchctl_loaded,
    _read_crontab,
    build_hook_descriptor,
    crontab_with_managed,
    descriptor_text,
    desired_harness_value,
    find_managed_bridge_hook,
    github_ruleset_state,
    harness_settings_file,
    hook_bridge_entries,
    GITIGNORE_BEGIN_MARKER,
    parse_mcp_command,
    resolve_agents_md,
    resolve_ci_workflow,
    resolve_gitignore,
    schedule_plan_from_action,
    skill_harness_link_target,
)
from .github_ruleset import DEFAULT_RULESET_NAME
from .plan import Action, InstallPlan


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
        elif action.kind == "register_hook_bridge":
            _check_hook_bridge(action, report)
        elif action.kind == "provision_schedule":
            _check_schedule(action, report)
        elif action.kind == "provision_agents_symlink":
            _check_agents_symlink(action, report)
        elif action.kind == "provision_github_ruleset":
            _check_github_ruleset(action, report)
        elif action.kind == "provision_gitignore":
            _check_gitignore(action, report)

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


def _check_gitignore(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured rig-managed ``.gitignore`` block and the file on disk.

    Switches on the SAME :func:`resolve_gitignore` ``state`` apply uses (one classification,
    shared via the runner), so status and apply can never disagree on "in sync":

    - ``create``   → ``missing``: no ``.gitignore`` or no managed block (apply appends it).
    - ``update``   → ``modified``: a managed block exists but its entries differ (apply replaces
                     just the block).
    - ``ok``       → no drift item (in sync).
    - ``conflict`` → ``modified``: unbalanced markers rig won't rewrite — surfaced so the
                     operator reconciles by hand (apply leaves it untouched).
    - ``io_error`` → ``modified``: the file couldn't be read (unreadable / a directory at the
                     path). NOT silently in-sync — rig couldn't even inspect it, so a green status
                     would mask a genuinely un-provisioned ignore (mirrors the github gh_error
                     could-not-verify item).
    """
    entries = [str(e) for e in action.options.get("entries", [])]
    r = resolve_gitignore(action.target, entries)
    if r.state == "ok":
        return
    if r.state == "create":
        report.items.append(
            DriftItem("missing", "gitignore", "block", action.target,
                      "rig-managed .gitignore block not present (apply adds it)")
        )
    elif r.state == "update":
        report.items.append(
            DriftItem("modified", "gitignore", "block", action.target,
                      "rig-managed .gitignore block differs from config (apply replaces just the block)")
        )
    elif r.state in ("conflict", "io_error"):
        report.items.append(
            DriftItem("modified", "gitignore", "block", action.target, r.detail)
        )


def check_disabled_gitignore(repo_root: Path, report: DriftReport) -> None:
    """Flag a still-installed rig-managed block when the config disables the ``gitignore`` category.

    apply never deletes; so a repo that previously had the managed block keeps it in
    ``.gitignore`` even after the config turns the category off. With the action gone from the
    plan, ``_check_gitignore`` never runs — so without this scan the leftover block would report as
    "in sync". Detect a present begin marker in the repo's ``.gitignore`` and report it as
    disk→config drift (mirrors :func:`check_disabled_dispatcher` for the global dispatcher).
    """
    gi = repo_root / ".gitignore"
    if not gi.is_file():
        return
    try:
        content = gi.read_text(encoding="utf-8")
    except OSError:
        return
    if any(ln.strip() == GITIGNORE_BEGIN_MARKER for ln in content.splitlines()):
        report.items.append(
            DriftItem("extra", "gitignore", "block", gi,
                      "gitignore disabled in config but the rig-managed block is still in .gitignore")
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


def _check_hook_bridge(action: Action, report: DriftReport) -> None:
    """Flag drift between the configured cc_hook_bridge wiring and the settings file.

    missing  — the settings file is absent, or a managed dispatcher hook (one whose command
               carries ``cc_hook_bridge``) is not present for an (event, matcher) we ship.
    modified — the settings file is malformed JSON, OR a managed hook is present but its
               COMMAND differs from what apply would write (stale PYTHONPATH / moved
               checkout / changed ``hook_bridge.python``). ``rig apply`` rewrites it.
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
            elif str(hk.get("command", "")) != command:
                report.items.append(
                    DriftItem("modified", "harness", action.item, config_file,
                              f"cc_hook_bridge command for {label} is stale (apply will rewrite)")
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


def _mcp_config_file(action: Action) -> Path:
    target = action.target
    return target if target.suffix == ".json" else target / "mcp.json"


def _check_mcp(action: Action, report: DriftReport) -> None:
    command = str(action.options.get("command", "")).strip()
    if not command:
        return
    server_key = str(action.options.get("server") or action.item)
    config_file = _mcp_config_file(action)
    desired = parse_mcp_command(command)
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
            DriftItem("modified", "mcp", server_key, config_file, "MCP entry differs from config (command/args)")
        )
