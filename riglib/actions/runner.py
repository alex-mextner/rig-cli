"""The plan executor — runs each :class:`~riglib.plan.Action`, returns results.

Stdlib only. Each ``_do_<kind>`` handler implements one action kind and returns an
:class:`ActionResult`. Non-fatal errors are collected (the runner continues); the caller
decides how to surface them.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import (
    GITIGNORE_BEGIN_MARKER,
    GITIGNORE_BLOCK_COMMENT,
    GITIGNORE_END_MARKER,
    SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
    SHIP_DELEGATOR_EXCLUDE_COMMENT,
    SHIP_DELEGATOR_EXCLUDE_END_MARKER,
    SHIP_DELEGATOR_REL_PATH,
    linter_path_escapes_repo,
)
from ..github_actions import (
    build_permissions_body,
    build_workflow_permissions_body,
    normalize_permissions,
    normalize_workflow_permissions,
)
from ..github_auth import ensure_browser_auth, ensure_gh_auth, reset_auth_gate
from ..github_browser import build_command_plan as build_browser_plan
from ..github_browser import desired_toggles as browser_desired_toggles
from ..github_ghas import (
    SUBRESOURCE_KNOBS,
    build_security_analysis_body,
    desired_code_scanning,
    desired_subresource,
    normalize_security_analysis,
)
from ..github_merge import build_merge_body, normalize_merge
from ..github_ruleset import (
    build_ruleset_body,
    find_managed_ruleset,
    normalize_ruleset,
    parse_github_remote,
)
from ..logging import log_event
from ..plan import Action, InstallPlan
from .. import project_tools
from . import fsutil


@dataclass
class ActionResult:
    action: Action
    status: str  # created | updated | skipped | backed_up | error
    detail: str
    backup: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status != "error"


@dataclass
class ApplyReport:
    results: list[ActionResult] = field(default_factory=list)

    @property
    def errors(self) -> list[ActionResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def changed(self) -> int:
        return sum(1 for r in self.results if r.status in ("created", "updated", "backed_up"))

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def run_plan(
    plan: InstallPlan,
    *,
    dry_run: bool = False,
    progress: Callable[[ActionResult], None] | None = None,
) -> ApplyReport:
    """Execute (or dry-run) every action in the plan. Returns the collected report."""
    # Fresh auth-gate state per run: a new apply re-notifies + re-waits for a missing login (the user
    # may have logged in since), but WITHIN this run the per-action gate dedups (no ~5× push/wait).
    reset_auth_gate()
    report = ApplyReport()
    for action in plan.actions:
        if dry_run:
            res = ActionResult(action, "planned", action.describe())
        else:
            try:
                res = _dispatch(action, plan.on_conflict)
            except Exception as exc:  # noqa: BLE001 — collect, never abort the whole run
                res = ActionResult(action, "error", f"{type(exc).__name__}: {exc}")
        report.results.append(res)
        log_event(
            "rig.action",
            kind=action.kind,
            item=f"{action.category}/{action.item}",
            status=res.status,
        )
        if progress is not None:
            progress(res)
    return report


def _dispatch(action: Action, on_conflict: str) -> ActionResult:
    handler = _HANDLERS.get(action.kind)
    if handler is None:
        return ActionResult(action, "error", f"no handler for action kind '{action.kind}'")
    return handler(action, on_conflict)


# ── handlers ────────────────────────────────────────────────────────────────────
def _skill_backup_dir(skill_target: Path) -> Path:
    """Where a conflict-backup of an installed skill goes — a sibling ``.rig-backups/`` OUTSIDE
    the scanned skills dir.

    ``skill_target`` is ``<skills_target>/<name>`` (e.g. ``~/.agents/skills/naming``), so its
    parent is the natively-scanned skills dir. A same-parent ``<name>.rig-bak-*/`` backup still
    holds a ``SKILL.md`` and opencode (which auto-scans ``~/.agents/skills``) re-discovers it as
    a duplicate skill (rig-cli#57). Putting the backup one level up — next to the skills dir,
    not inside it — keeps the restore point without it ever being scanned as a skill.
    """
    return skill_target.parent.parent / ".rig-backups"


def _do_copy_skill(action: Action, on_conflict: str) -> ActionResult:
    out = fsutil.copy_tree(
        action.source, action.target, on_conflict, backup_dir=_skill_backup_dir(action.target)
    )
    return ActionResult(action, out.status, out.detail, out.backup)


def skill_harness_link_target(action: Action) -> tuple[Path, Path]:
    """The (symlink_path, desired_destination) a ``link_skill_harness`` action maintains.

    The symlink lives at ``action.target`` inside the harness skill dir; it should resolve to
    ``action.source`` (the installed skill in skills_target). Shared with drift so the install
    and the drift check agree on what "correct" means. The destination is the absolute
    installed-skill path — an absolute target keeps the link valid regardless of the relative
    distance between the two dirs.
    """
    return action.target, action.source.resolve()


def _do_link_skill_harness(action: Action, on_conflict: str) -> ActionResult:
    """Maintain an idempotent symlink making an installed skill discoverable by the harness.

    The agent harness lists/loads Skill-tool skills from its own dir (claude-code:
    ``~/.claude/skills``), NOT from ``skills_target`` (``~/.agents/skills``). So for every
    enabled skill rig symlinks ``<harness_skill_dir>/<skill> -> <skills_target>/<skill>``.

    - already a symlink to the correct destination → no-op (``skipped``).
    - a symlink to a WRONG destination → re-point it (``updated``); never honors on_conflict
      backup for a stale symlink (a symlink carries no user data to preserve).
    - a real (non-symlink) directory/file already there → LEAVE IT (``skipped`` with a
      warning). Some harness skills are real dirs (h-reason, debate-swarm,
      moshi-best-practices); clobbering them would destroy real content. on_conflict=overwrite
      does NOT override this — a real dir at the harness path is never rig's to replace.
    """
    link_path, dest = skill_harness_link_target(action)
    if not dest.exists():
        # the installed skill the link should point at isn't on disk — the copy_skill action
        # surfaces that failure; don't create a dangling link on top of it.
        return ActionResult(action, "error", f"skill-link/{action.item}: install target missing: {dest}")
    link_path.parent.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        try:
            current = link_path.readlink()
        except OSError as exc:
            return ActionResult(action, "error", f"skill-link/{action.item}: cannot read symlink {link_path}: {exc}")
        if _same_link_dest(link_path, current, dest):
            return ActionResult(action, "skipped", f"skill-link/{action.item}: already linked → {dest}")
        # stale/wrong symlink — re-point it (a symlink holds no user data; no backup needed).
        link_path.unlink()
        link_path.symlink_to(dest)
        return ActionResult(action, "updated", f"skill-link/{action.item}: re-pointed → {dest}")

    if link_path.exists():
        # a REAL dir/file already occupies the harness path (not a rig symlink). Never clobber
        # it — it may be a hand-authored skill (h-reason, debate-swarm). Leave it, warn.
        kind = "directory" if link_path.is_dir() else "file"
        return ActionResult(
            action, "skipped",
            f"skill-link/{action.item}: a real {kind} already exists at {link_path} "
            f"(not a rig symlink) — left untouched; skill may shadow the installed one",
        )

    link_path.symlink_to(dest)
    return ActionResult(action, "created", f"skill-link/{action.item}: linked {link_path} → {dest}")


def _same_link_dest(link_path: Path, current: Path, dest: Path) -> bool:
    """True when ``current`` (the symlink's stored target) resolves to the desired ``dest``.

    Handles both an absolute stored target and a relative one (resolved against the link's
    parent), so a correct link written either way is recognized as a no-op.
    """
    if current.is_absolute():
        resolved = current
    else:
        resolved = (link_path.parent / current).resolve()
    try:
        return resolved.resolve() == dest.resolve()
    except OSError:
        return False


def build_hook_descriptor(action: Action) -> tuple[dict, str]:
    """Build the descriptor dict + the script's absolute cmd for an agent-hook action.

    Shared by the install action and the drift check so both agree on what the installed
    descriptor SHOULD contain (absolute ``cmd`` per the agents-hooks/v1 contract + any
    config ``on_error`` override). Raises on a missing/unreadable source descriptor.
    """
    descriptor_name = action.options.get("descriptor") or ""
    src_descriptor = action.source / descriptor_name
    spec = json.loads(src_descriptor.read_text(encoding="utf-8"))
    cmd = str(spec.get("cmd", ""))
    if "/ABSOLUTE/PATH/TO/" in cmd:
        rel = cmd.split("/ABSOLUTE/PATH/TO/", 1)[1]
        cmd = str((Path(action.options["agent_tools_source"]) / rel).resolve())
    elif not os.path.isabs(cmd):
        cmd = str((Path(action.options["agent_tools_source"]) / cmd).resolve())
    spec["cmd"] = cmd
    on_error = action.options.get("on_error")
    if on_error:
        spec["on_error"] = on_error
    return spec, cmd


def descriptor_text(spec: dict) -> str:
    """The canonical on-disk serialization of a descriptor (single source of truth)."""
    return json.dumps(spec, indent=2) + "\n"


_CHANGED_STATUSES = frozenset({"created", "updated", "backed_up"})


def _chmod_x_if_changed(path: Path, outcome: fsutil.WriteOutcome) -> None:
    """Ensure the exec bit on a managed script, without violating ``on_conflict=skip``.

    chmod when rig wrote the file (created/updated/backed_up) OR when the content is already
    identical (the file IS the managed one — restoring a lost exec bit is correct
    convergence). Do NOT chmod a *conflict*-skip: there the existing file is a DIFFERENT
    user file left untouched by policy, so touching its mode would violate skip.
    """
    if not path.is_file():
        return
    is_change = outcome.status in _CHANGED_STATUSES
    is_identical = outcome.status == "skipped" and "identical" in outcome.detail
    if is_change or is_identical:
        path.chmod(path.stat().st_mode | 0o111)


def parse_mcp_command(command: str) -> dict:
    """Split an MCP launch command into ``{command, args}`` with shell-aware quoting.

    Shared by the install action and drift detection so both interpret quoted args and
    spaces-in-paths identically. ``shlex.split`` handles quotes; an unparsable string falls
    back to a naive split rather than raising (the entry is still recorded).
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return {"command": command, "args": []}
    return {"command": parts[0], "args": parts[1:]}


def desired_mcp_server_entry(options: dict) -> dict:
    """Build the desired ``mcpServers.<name>`` entry from an action's options.

    Back-compat: without an explicit ``args`` key, ``command`` is parsed as the legacy shell-like
    command string. With ``args`` present, ``command`` is the executable value exactly and ``args``
    is the argv list exactly.
    """
    command = str(options.get("command", ""))
    if "args" in options:
        args = options.get("args", [])
        entry = {"command": command, "args": list(args) if isinstance(args, list) else []}
    else:
        entry = parse_mcp_command(command)
    env = options.get("env")
    if isinstance(env, dict) and env:
        entry["env"] = dict(env)
    return entry


def _do_install_agent_hook(action: Action, on_conflict: str) -> ActionResult:
    """Copy the hook dir + write an absolute-path descriptor into the harness hook dir.

    Per the agents-hooks/v1 contract the descriptor ``cmd`` must be an ABSOLUTE path. We
    rewrite the ``/ABSOLUTE/PATH/TO/...`` placeholder to the real script path inside the
    agent-tools checkout, applying any ``on_error`` override from config.
    """
    descriptor_name = action.options.get("descriptor") or ""
    src_descriptor = action.source / descriptor_name
    if not src_descriptor.is_file():
        return ActionResult(action, "error", f"descriptor not found: {src_descriptor}")
    try:
        spec, cmd = build_hook_descriptor(action)
    except (OSError, ValueError) as exc:
        return ActionResult(action, "error", f"bad descriptor json: {exc}")

    # The descriptor's cmd points at a script INSIDE the agent-tools checkout. rig consumes
    # agent-tools READ-ONLY — never chmod (or otherwise mutate) the source. agent-tools
    # ships its hook scripts executable; if one is not, surface that as an explicit warning
    # rather than silently flipping the source's bits.
    note = ""
    script = Path(cmd)
    if script.is_file() and not os.access(script, os.X_OK):
        note = f" (warning: source script not executable: {script})"

    target_descriptor = action.target / descriptor_name
    out = fsutil.write_file(target_descriptor, descriptor_text(spec), on_conflict)
    return ActionResult(action, out.status, f"hook descriptor {out.detail}{note}", out.backup)


def _do_install_dispatcher(action: Action, on_conflict: str) -> ActionResult:
    """Install the global-hook dispatcher and wire it as global ``core.hooksPath``.

    The dispatcher has three on-disk pieces (agent-tools git-hooks/global-dispatcher):
      - ``run-global-hooks`` — the runner, placed beside the composer dir so the
        composers' ``../run-global-hooks`` reference resolves.
      - ``hooks/`` — the per-event COMPOSERS (pre-commit/commit-msg/pre-push + review-gate).
        **core.hooksPath must point HERE**, not at the runner's parent: git looks for an
        executable named after the event in core.hooksPath, and those live in hooks/.
      - ``global-hooks.d/<event>/`` — the drop-in fragments the runner enumerates.

    Idempotent: every copy skips-if-identical; the ``core.hooksPath`` set checks the
    current value first and records the prior value for restore.
    """
    src = action.source  # the global-dispatcher dir in agent-tools
    notes: list[str] = []
    statuses: list[str] = []  # sub-outcome statuses, rolled up into the overall status
    backup: Path | None = None
    # The runner sits at the configured runner path; the composer dir (the real
    # core.hooksPath target) is its sibling ``hooks/`` directory.
    runner_target = Path(action.options["runner"])
    composer_target = runner_target.parent / "hooks"

    # 1. the runner script
    src_runner = src / "run-global-hooks"
    if src_runner.is_file():
        out = fsutil.copy_file(src_runner, runner_target, on_conflict)
        _chmod_x_if_changed(runner_target, out)
        notes.append(f"runner {out.status}")
        statuses.append(out.status)
        backup = backup or out.backup

    # 2. the composers (core.hooksPath target). Git ignores a non-executable hook, so the
    # exec bit must be set even when the content is already identical (else a re-apply
    # leaves the dispatcher silently disabled). Only a CONFLICT-skip leaves them untouched.
    src_hooks = src / "hooks"
    if src_hooks.is_dir():
        out = fsutil.copy_tree(src_hooks, composer_target, on_conflict)
        chmod_ok = out.status in _CHANGED_STATUSES or (
            out.status == "skipped" and "identical" in out.detail
        )
        if chmod_ok:
            for f in composer_target.iterdir():
                if f.is_file():
                    f.chmod(f.stat().st_mode | 0o111)
        notes.append(f"composers {out.status}")
        statuses.append(out.status)
        backup = backup or out.backup

    # 3. fragments dir (global-hooks.d) — honor the per-fragment enable config.
    src_frag = src / "global-hooks.d"
    if src_frag.is_dir():
        out = _install_fragments(src_frag, action.target, action.options.get("fragments", {}), on_conflict)
        notes.append(f"fragments {out.status}")
        statuses.append(out.status)
        backup = backup or out.backup
        # the runner enumerates ${GLOBAL_HOOKS_DIR:-~/.config/git/global-hooks.d}. If the
        # configured fragments dir is anything else, the runner won't see it unless that
        # env is exported — warn rather than silently install fragments that never run.
        default_dir = Path(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        ) / "git" / "global-hooks.d"
        if action.target.resolve() != default_dir.resolve():
            notes.append(
                f"WARNING: fragments dir {action.target} is not the runner default "
                f"{default_dir}; export GLOBAL_HOOKS_DIR={action.target} or the runner "
                "will not enumerate them"
            )

    # 4. retrofit script onto PATH-ish location (~/.local/bin)
    if action.options.get("install_local_retrofit_script"):
        src_retro = src / "install-local-hooks.sh"
        if src_retro.is_file():
            bin_dir = Path(os.path.expanduser("~/.local/bin"))
            retro_target = bin_dir / "install-local-hooks.sh"
            out = fsutil.copy_file(src_retro, retro_target, on_conflict)
            _chmod_x_if_changed(retro_target, out)
            notes.append(f"retrofit-script {out.status}")
            statuses.append(out.status)
            backup = backup or out.backup

    # 5. set global core.hooksPath → the composer dir (record prior value)
    if action.options.get("set_global_hooks_path"):
        hooks_path = str(composer_target)
        current = _git_global("core.hooksPath")
        if current == hooks_path:
            notes.append("core.hooksPath already set")
            statuses.append("skipped")
        else:
            if current:
                notes.append(f"prior core.hooksPath={current} (restore with: git config --global core.hooksPath {current})")
            rc = _set_git_global("core.hooksPath", hooks_path)
            notes.append(f"core.hooksPath → {hooks_path}" if rc == 0 else "core.hooksPath set FAILED")
            statuses.append("created" if rc == 0 else "error")

    # roll up: error wins, else any change wins, else skipped (idempotent no-op)
    if "error" in statuses:
        overall = "error"
    elif any(s in ("created", "updated", "backed_up") for s in statuses):
        overall = "backed_up" if backup else "created"
    else:
        overall = "skipped"
    return ActionResult(action, overall, "; ".join(notes) or "dispatcher installed", backup)


def _install_fragments(
    src_frag: Path, target: Path, fragments_cfg: dict, on_conflict: str
) -> fsutil.WriteOutcome:
    """Install shipped ``global-hooks.d`` fragments PER FILE, honoring per-fragment config.

    Fragment files live at ``<event>/<NN-name>`` (e.g. ``pre-commit/10-secret-scan``). A
    config entry ``fragments.<name>.enabled: false`` skips every file whose name contains
    ``<name>``. The drop-in dir is a SHARED namespace — other tools and prior installs may
    have their own fragments there — so we merge file-by-file and never copy the whole tree
    (a whole-tree copy would back up / clobber unrelated drop-ins). Extras left in the dir
    are surfaced by ``rig status``, not reconciled here.

    Returns the most-significant per-file outcome (``backed_up`` > ``created`` > ``skipped``)
    and carries the FIRST backup path so the dispatcher report keeps a restore hint.
    """
    disabled = {
        name
        for name, spec in (fragments_cfg or {}).items()
        if isinstance(spec, dict) and spec.get("enabled") is False
    }
    target.mkdir(parents=True, exist_ok=True)
    best = "skipped"
    first_backup: Path | None = None
    rank = {"skipped": 0, "created": 1, "updated": 1, "backed_up": 2}
    for event_dir in sorted(p for p in src_frag.iterdir() if p.is_dir()):
        dst_event = target / event_dir.name
        for frag in sorted(event_dir.iterdir()):
            if not frag.is_file():
                continue
            if any(name in frag.name for name in disabled):
                continue
            out = fsutil.copy_file(frag, dst_event / frag.name, on_conflict)
            _chmod_x_if_changed(dst_event / frag.name, out)
            if rank.get(out.status, 0) > rank.get(best, 0):
                best = out.status
            if out.backup is not None and first_backup is None:
                first_backup = out.backup
    suffix = f" ({len(disabled)} disabled)" if disabled else ""
    detail = f"per-file{suffix}"
    if first_backup is not None:
        detail += f"; backed up → {first_backup}"
    return fsutil.WriteOutcome("created" if best == "updated" else best, detail, first_backup)


def _do_install_ci(action: Action, on_conflict: str) -> ActionResult:
    slot = action.options.get("slot", action.item)
    if slot == "ship":
        ship = action.source / "ship.sh"
        if not ship.is_file():
            return ActionResult(action, "error", "ship.sh not found in catalog item")
        target = action.target / "ship"
        out = fsutil.copy_file(ship, target, on_conflict)
        _chmod_x_if_changed(target, out)
        detail = f"ship {out.detail}"
        if action.options.get("gh_alias"):
            rc = _gh_alias_set("ship", str(target))
            detail += "; gh alias set" if rc == 0 else "; gh alias FAILED (gh missing?)"
        return ActionResult(action, out.status, detail, out.backup)

    # fail-closed on a requested variant the catalog doesn't ship — a variant is a config
    # decision, so silently installing a DIFFERENT workflow than asked is wrong.
    variant = action.options.get("variant")
    if variant and not (action.source / f"workflow-{variant}.yml").is_file():
        return ActionResult(action, "error", f"ci/{slot}: requested variant '{variant}' not found")
    workflow = resolve_ci_workflow(action.source, slot, variant)
    if workflow is None:
        return ActionResult(action, "error", f"no workflow file for ci/{slot}")
    target = action.target / f"{slot}.yml"
    out = fsutil.copy_file(workflow, target, on_conflict)

    # Vendor the companion helpers the workflow invokes, at their required install paths
    # (most ci/<slot>/, some fixed like pr-checklist → .github/scripts/). These are relative
    # to the CHECKOUT ROOT (passed explicitly), not derived from ci.target which may be
    # customized to e.g. .ci/workflows.
    repo_root = Path(action.options.get("repo_root") or action.target.parent.parent)
    companions = _ci_companion_files(action.source, workflow)
    # roll up the overall status so a skipped workflow + a changed companion still reports a
    # change (not a misleading 'skipped'/changed=0). rank: backed_up > created > skipped.
    rank = {"skipped": 0, "created": 1, "updated": 1, "backed_up": 2}
    best_status = out.status
    for comp, rel in companions:
        comp_target = repo_root / rel
        c_out = fsutil.copy_file(comp, comp_target, on_conflict)
        if comp.suffix in (".sh", ".mjs"):
            _chmod_x_if_changed(comp_target, c_out)
        if c_out.backup is not None and out.backup is None:
            out.backup = c_out.backup
        if rank.get(c_out.status, 0) > rank.get(best_status, 0):
            best_status = "created" if c_out.status == "updated" else c_out.status
    extra = f"; +{len(companions)} companion(s)" if companions else ""
    return ActionResult(action, best_status, f"workflow {out.detail}{extra}", out.backup)


# companion files to vendor alongside a workflow (helpers the workflow shells out to).
_CI_COMPANION_SUFFIXES = (".sh", ".mjs")
_CI_COMPANION_EXTRA = ("pull_request_template.md",)
# slots whose workflow imports a companion from a FIXED path (not ci/<slot>/). Maps the
# companion filename → its repo-relative install dir, per the agent-tools workflow's import.
_CI_COMPANION_FIXED_PATHS = {
    "pr-checklist": {
        "checklist-gate.mjs": ".github/scripts",
        "pull_request_template.md": ".github",
    },
}


def _ci_companion_files(source: Path, workflow: Path) -> list[tuple[Path, str]]:
    """Companions a CI slot's workflow invokes + their repo-relative install path.

    Returns ``(source_file, repo_relative_install_path)`` pairs. Most companions live in
    ``ci/<slot>/`` (where the workflow runs ``bash ci/<slot>/x.sh``), but some workflows
    import from a fixed path (pr-checklist → ``.github/scripts/``); those are mapped
    explicitly. Excludes the workflow yml, READMEs, test files, and gitleaks config.
    """
    slot = source.name
    fixed = _CI_COMPANION_FIXED_PATHS.get(slot, {})
    out: list[tuple[Path, str]] = []
    for p in sorted(source.iterdir()):
        if not p.is_file() or p == workflow:
            continue
        if p.name.endswith(".test.mjs") or p.name.startswith("README"):
            continue
        if p.suffix in _CI_COMPANION_SUFFIXES or p.name in _CI_COMPANION_EXTRA:
            install_dir = fixed.get(p.name, f"ci/{slot}")
            out.append((p, f"{install_dir}/{p.name}"))
    return out


def resolve_ci_workflow(source: Path, slot: str, variant: str | None) -> Path | None:
    """Resolve the source workflow file for a CI slot (shared by the runner and drift).

    Prefer the variant-specific workflow, else ``workflow.yml``, else a slot-named file
    (e.g. secret-scan ships ``secret-scan.yml``), else the single non-config ``*.yml``.
    """
    candidates: list[Path] = []
    if variant:
        candidates.append(source / f"workflow-{variant}.yml")
    candidates += [source / "workflow.yml", source / f"{slot}.yml"]
    workflow = next((c for c in candidates if c.is_file()), None)
    if workflow is None:
        ymls = [
            p
            for p in sorted(source.glob("*.yml")) + sorted(source.glob("*.yaml"))
            if "gitleaks" not in p.name  # config files, not workflows
        ]
        workflow = ymls[0] if len(ymls) == 1 else None
    return workflow


def _do_register_mcp(action: Action, on_conflict: str) -> ActionResult:
    """Merge an MCP server entry into the harness MCP config, idempotent by name.

    The harness config is a JSON file (``<target>/mcp.json`` or the target if it's a
    .json file). We merge under ``mcpServers.<name>`` and never overwrite an existing
    differing entry unless ``on_conflict=overwrite``.
    """
    command = str(action.options.get("command", "")).strip()
    # the registration KEY is the configured server name (falls back to the item name).
    server_key = str(action.options.get("server") or action.item)
    if not command:
        return ActionResult(action, "skipped", f"mcp/{action.item}: no command set, nothing to register")

    target = action.target
    config_file = target if target.suffix == ".json" else target / "mcp.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            # a malformed existing config must NOT be silently discarded — that would
            # erase the user's MCP setup. Per on_conflict: skip leaves it, others back it
            # up; never overwrite blind.
            if on_conflict == "skip":
                return ActionResult(
                    action, "skipped",
                    f"mcp/{action.item}: existing {config_file} is malformed JSON (on_conflict=skip), left untouched",
                )
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            data = {}
            backup_note = f" (backed up malformed config → {bak})"
        else:
            backup_note = ""
    else:
        backup_note = ""
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"mcp/{action.item}: {config_file} is not a JSON object")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return ActionResult(action, "error", f"mcp/{action.item}: mcpServers is not an object in {config_file}")
    entry = desired_mcp_server_entry(action.options)

    status = "created"
    if server_key in servers:
        if servers[server_key] == entry:
            return ActionResult(action, "skipped", f"mcp/{server_key}: already registered")
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"mcp/{server_key}: entry exists (on_conflict=skip), left untouched",
            )
        if on_conflict == "backup" and backup_note == "":
            # back up the whole config before converging the differing entry, so apply can
            # reconcile MCP drift under the default policy (not only under overwrite).
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            backup_note = f" (backed up prior → {bak})"
        status = "backed_up" if on_conflict == "backup" else "updated"
    servers[server_key] = entry
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ActionResult(action, status, f"mcp/{server_key} registered → {config_file}{backup_note}")


# ── harness auto-mode / permission provisioning ───────────────────────────────────
# The JSON key each supported harness uses for its permission mode. Keep this beside the
# handler so the runner and the drift check (which imports these) agree on the on-disk shape.
_HARNESS_MODE_KEY = {
    "claude-code": ("permissions", "defaultMode"),
}


def harness_settings_file(action: Action) -> Path:
    """The settings file an ``apply_harness`` action targets (shared with drift)."""
    target = action.target
    return target if target.suffix == ".json" else target / "settings.json"


def desired_harness_value(action: Action) -> tuple[tuple[str, str], str]:
    """Return ((section, key), value) the harness settings file should contain.

    Shared by the install action and drift so both agree on what auto-mode writes. Raises
    ``KeyError`` for an unsupported kind — the plan only emits supported kinds.
    """
    kind = str(action.options.get("kind", "claude-code"))
    section, key = _HARNESS_MODE_KEY[kind]
    value = str(action.options.get("mode_value", ""))
    return (section, key), value


def _do_apply_harness(action: Action, on_conflict: str) -> ActionResult:
    """Merge the harness auto/permission setting into the harness settings JSON.

    Idempotent (a re-apply with the same value is a no-op) and backup-noted: an existing
    settings file with a DIFFERENT value for the managed key is backed up before converging
    under ``on_conflict=backup`` (skip leaves it; overwrite replaces without a backup). Only
    the single managed key is touched — every other setting in the file is preserved.
    """
    (section, key), value = desired_harness_value(action)
    config_file = harness_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    backup_note = ""
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            # never silently discard an existing settings file (it holds the user's harness
            # config). skip leaves it untouched; others back it up before rewriting.
            if on_conflict == "skip":
                return ActionResult(
                    action, "skipped",
                    f"harness/{action.item}: existing {config_file} is malformed JSON "
                    "(on_conflict=skip), left untouched",
                )
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            data = {}
            backup_note = f" (backed up malformed config → {bak})"
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"harness/{action.item}: {config_file} is not a JSON object")

    sect = data.get(section, {})
    if not isinstance(sect, dict):
        return ActionResult(action, "error", f"harness/{action.item}: '{section}' is not an object in {config_file}")
    current = sect.get(key)
    if current == value:
        return ActionResult(action, "skipped", f"harness/{action.item}: {section}.{key} already '{value}'")

    status = "created" if current is None else "updated"
    if current is not None and current != value:
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                f"harness/{action.item}: {section}.{key}='{current}' exists "
                f"(on_conflict=skip), left untouched",
            )
        if on_conflict == "backup" and backup_note == "":
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            backup_note = f" (backed up prior → {bak})"
        status = "backed_up" if on_conflict == "backup" else "updated"

    sect[key] = value
    data[section] = sect
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    auto = "auto-mode ON" if action.options.get("auto_mode") else "interactive"
    return ActionResult(
        action, status,
        f"harness/{action.item}: {section}.{key} → '{value}' ({auto}) in {config_file}{backup_note}",
    )


# ── permission-allowlist provisioning (claude-code permissions.allow / opencode permission.bash) ──
def permissions_settings_file(action: Action) -> Path:
    """The settings file a ``provision_permissions`` action targets (shared with drift).

    Mirrors :func:`harness_settings_file`: a ``.json`` target is the file itself, else a dir
    holding the per-harness settings filename. The plan resolves an absolute path; this just
    normalizes the file-vs-dir target.
    """
    target = action.target
    return target if target.suffix == ".json" else target / "settings.json"


def desired_permission_entries(action: Action) -> tuple[tuple[str, ...], str, list[str], str | None]:
    """Return ``(key_path, container, entries, value)`` the allowlist should contain.

    Shared by the install handler and drift so both agree on the on-disk shape: where the
    allowlist lives (``key_path`` dotted segments), whether it is a JSON ``"array"`` or
    ``"object"``, the per-harness entry strings, and the object-form ``value`` (``"allow"``).
    Reads the resolved tool list + kind straight off the action options (the plan resolved them),
    so the harness module is consulted ONCE at plan time and the runner stays pure of config.
    """
    from ..permissions import HARNESS_ALLOWLISTS, desired_entries

    kind = str(action.options.get("kind", "claude-code"))
    try:
        spec = HARNESS_ALLOWLISTS[kind]
    except KeyError:
        # The plan only emits supported kinds (it gates on harness_supported), so this is a
        # defensive guard — a stale/hand-built action with an N/A kind gets a clean error, not a
        # raw KeyError traceback out of the runner/drift.
        raise ValueError(f"no allowlist mechanism for harness kind {kind!r}") from None
    tools = [str(t) for t in action.options.get("tools", [])]
    return spec.key_path, spec.container, desired_entries(kind, tools), spec.value


def _container_at(data: dict, key_path: tuple[str, ...]) -> tuple[dict, str]:
    """Walk ``data`` to the PARENT of the allowlist container, creating intermediate dicts.

    Returns ``(parent_dict, leaf_key)`` so the caller can read/set ``parent[leaf_key]`` — the
    container itself (array/object) is created lazily by the caller so its shape is explicit.
    Raises ``ValueError`` if an intermediate exists as a NON-dict (we never clobber the user's
    differently-typed value silently — that surfaces as an action error).
    """
    cur = data
    for seg in key_path[:-1]:
        nxt = cur.get(seg)
        if nxt is None:
            nxt = {}
            cur[seg] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(f"'{'.'.join(key_path)}' parent '{seg}' is not an object")
        cur = nxt
    return cur, key_path[-1]


def _do_provision_permissions(action: Action, on_conflict: str) -> ActionResult:
    """Merge the per-harness command allowlist into the harness settings JSON — ADDITIVE.

    The invariant this enforces (and the tests assert): every existing allowlist entry is
    PRESERVED, the desired ecosystem/external entries are MERGED IN, the result is DEDUPED, and a
    re-apply with the same config is a true no-op. The accumulated ``permissions.allow`` in the
    user's live settings (auto-mode, docker, psql, …) is never clobbered — we only ever ADD the
    entries that are missing.

    - array form (claude-code): append each missing entry to the existing list, order-stable.
    - object form (opencode): set each missing entry KEY → ``"allow"`` only when absent (never
      downgrade a user's ``"deny"``/``"ask"`` — that is the user's call, not rig's).

    Backup-noted under ``on_conflict=backup`` when the file changes; ``skip`` leaves a malformed
    file untouched; a non-dict settings root or a non-array/object container is a hard error
    (never a blind overwrite of the user's data).
    """
    try:
        key_path, container, entries, value = desired_permission_entries(action)
    except ValueError as exc:
        return ActionResult(action, "error", f"permissions/{action.item}: {exc}")
    config_file = permissions_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    backup_note = ""
    existed = config_file.is_file()
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            if on_conflict == "skip":
                return ActionResult(
                    action, "skipped",
                    f"permissions/{action.item}: existing {config_file} is malformed JSON "
                    "(on_conflict=skip), left untouched",
                )
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            data = {}
            backup_note = f" (backed up malformed config → {bak})"
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"permissions/{action.item}: {config_file} is not a JSON object")

    try:
        parent, leaf = _container_at(data, key_path)
    except ValueError as exc:
        return ActionResult(action, "error", f"permissions/{action.item}: {exc} in {config_file}")

    dotted = ".".join(key_path)
    added = 0
    if container == "array":
        existing = parent.get(leaf)
        if existing is None:
            existing = []
        if not isinstance(existing, list):
            return ActionResult(action, "error", f"permissions/{action.item}: '{dotted}' is not an array in {config_file}")
        present = set(existing)
        for entry in entries:
            if entry not in present:
                existing.append(entry)
                present.add(entry)
                added += 1
        parent[leaf] = existing
    else:  # object form — entry KEY → value, only when the key is absent
        existing = parent.get(leaf)
        if existing is None:
            existing = {}
        if not isinstance(existing, dict):
            return ActionResult(action, "error", f"permissions/{action.item}: '{dotted}' is not an object in {config_file}")
        for entry in entries:
            if entry not in existing:
                existing[entry] = value
                added += 1
        parent[leaf] = existing

    if added == 0:
        detail = (
            f"permissions/{action.item}: no desired entries to provision (empty tool set)"
            if not entries
            else f"permissions/{action.item}: all {len(entries)} entries already in {dotted} of {config_file}"
        )
        return ActionResult(action, "skipped", detail)

    if _should_backup(on_conflict) and backup_note == "" and config_file.is_file():
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # "backed_up" when we preserved a prior copy; else "updated" if the file pre-existed (we merged
    # entries into it), or "created" if we wrote it fresh. Don't report "created" for a file we
    # actually modified under a non-backup policy (the review's misleading-status finding).
    status = "backed_up" if backup_note else ("updated" if existed else "created")
    return ActionResult(
        action, status,
        f"permissions/{action.item}: added {added} of {len(entries)} entries to {dotted} in {config_file}{backup_note}",
    )


# ── agents-hooks/v1 → Claude Code bridge (settings.json hook registration) ──────────
# A managed cc_hook_bridge command is identified by this substring — so we can find,
# update, and de-dup OUR entries without ever touching the user's other hooks.
_BRIDGE_MARKER = "cc_hook_bridge"


def hook_bridge_entries(action: Action) -> dict[str, list[tuple[str, str]]]:
    """The (matcher, command) pairs the bridge maintains per CC hook event.

    Single source of truth shared by the install handler and drift, so both agree on what
    ``settings.json`` should contain. The command runs the dispatcher with the agent-tools
    ``lib/`` on PYTHONPATH so ``cc_hook_bridge`` resolves against the same checkout whose
    ``agent-hooks/`` scripts the installed descriptors point at.

    Only PreToolUse (real prevention) and Stop are wired — PostToolUse cannot block a tool
    that already ran (see lib/cc_hook_bridge/README.md). pre-write covers every CC
    file-mutating tool via a `|`-alternation matcher.
    """
    lib_dir = str(action.options["lib_dir"])
    py = str(action.options.get("python", "python3"))

    def cmd(event: str) -> str:
        # quote BOTH the lib path and the interpreter: a path with spaces would break the
        # hook, and an unquoted config-supplied `python` would let shell syntax be injected
        # into every CC hook command. `-m {_BRIDGE_MARKER}` keeps the run command and the
        # presence-marker in lockstep (rename the module → both change together).
        return f"PYTHONPATH={shlex.quote(lib_dir)} {shlex.quote(py)} -m {_BRIDGE_MARKER} {event}"

    return {
        "PreToolUse": [
            ("Bash", cmd("PreToolUse")),
            ("Edit|Write|MultiEdit|NotebookEdit", cmd("PreToolUse")),
        ],
        "Stop": [("", cmd("Stop"))],
    }


def find_managed_bridge_hook(blocks, matcher: str) -> dict | None:
    """Return OUR managed hook dict for ``matcher`` in an event's block list, else None.

    Single source of truth for "where is the cc_hook_bridge entry" — shared by apply
    (upsert) and drift (compare command), so both agree on what counts as the managed hook
    and never diverge. A managed hook is one whose ``command`` carries the bridge marker.
    """
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict) or str(block.get("matcher", "")) != matcher:
            continue
        for hk in block.get("hooks", []) or []:
            if isinstance(hk, dict) and _BRIDGE_MARKER in str(hk.get("command", "")):
                return hk
    return None


def _bridge_block(matcher: str, command: str) -> dict:
    """One settings.json hook block for an (matcher, command) pair."""
    block: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        # an empty matcher (Stop) is omitted entirely → CC treats it as "match all".
        block = {"matcher": matcher, "hooks": block["hooks"]}
    return block


def _should_backup(on_conflict: str) -> bool:
    """One predicate for "back up settings.json before mutating it", applied uniformly."""
    return on_conflict == "backup"


def _do_register_hook_bridge(action: Action, on_conflict: str) -> ActionResult:
    """Register the cc_hook_bridge dispatcher in the harness ``settings.json`` hooks.

    Idempotent and additive: each (event, matcher) gets OUR managed block appended only if
    an equivalent managed block (command contains ``cc_hook_bridge``) is not already there.
    Every other hook in the file — the user's rtk-rewrite, tg-ctl, etc. — is preserved
    untouched: ``hooks`` is a SHARED namespace, so we never rewrite a whole event array.

    A managed block whose COMMAND drifted (e.g. the agent-tools lib path moved) is rewritten
    in place — UNLESS ``on_conflict=skip``, which leaves the stale command untouched (matching
    the file-level skip semantics; ``rig status`` still surfaces it as drift). Removing an
    unmanaged matcher's hooks, or a matcher we no longer ship, is left to ``rig status``.
    """
    config_file = harness_settings_file(action)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    backup_note = ""
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            if on_conflict == "skip":
                return ActionResult(
                    action, "skipped",
                    f"hook_bridge/{action.item}: existing {config_file} is malformed JSON "
                    "(on_conflict=skip), left untouched",
                )
            bak = fsutil.backup_path(config_file)
            shutil.copy2(str(config_file), str(bak))
            data = {}
            backup_note = f" (backed up malformed config → {bak})"
    if not isinstance(data, dict):
        return ActionResult(action, "error", f"hook_bridge/{action.item}: {config_file} is not a JSON object")

    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        return ActionResult(action, "error", f"hook_bridge/{action.item}: 'hooks' is not an object in {config_file}")

    changed = 0
    skipped_stale = 0
    for event, pairs in hook_bridge_entries(action).items():
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            blocks = []
        for matcher, command in pairs:
            outcome = _upsert_bridge(blocks, matcher, command, on_conflict)
            if outcome == "changed":
                changed += 1
            elif outcome == "skipped-stale":
                skipped_stale += 1
        hooks[event] = blocks

    if changed == 0:
        note = "dispatcher already wired"
        if skipped_stale:
            note = f"{skipped_stale} managed hook(s) drifted but left untouched (on_conflict=skip)"
        return ActionResult(action, "skipped", f"hook_bridge/{action.item}: {note} in {config_file}")

    data["hooks"] = hooks
    if _should_backup(on_conflict) and backup_note == "" and config_file.is_file():
        bak = fsutil.backup_path(config_file)
        shutil.copy2(str(config_file), str(bak))
        backup_note = f" (backed up prior → {bak})"
    config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    status = "backed_up" if backup_note else "created"
    return ActionResult(
        action, status,
        f"hook_bridge/{action.item}: wired {changed} dispatcher hook(s) in {config_file}{backup_note}",
    )


def _upsert_bridge(blocks: list, matcher: str, command: str, on_conflict: str) -> str:
    """Insert/refresh OUR managed block for (matcher, command). Returns the outcome.

    - already present with the SAME command → ``"noop"`` (idempotent).
    - present with a DIFFERENT command (path drift) → rewrite in place → ``"changed"``,
      UNLESS ``on_conflict=skip`` → leave it, return ``"skipped-stale"``.
    - absent → append a fresh managed block → ``"changed"``.
    """
    hk = find_managed_bridge_hook(blocks, matcher)
    if hk is not None:
        if str(hk.get("command", "")) == command:
            return "noop"
        if on_conflict == "skip":
            return "skipped-stale"
        hk["command"] = command
        return "changed"
    blocks.append(_bridge_block(matcher, command))
    return "changed"


# ── model-freshness schedule provisioning (launchd / crontab) ──────────────────────
def schedule_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.schedule.SchedulePlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired
    artifact (plist XML / crontab line) from the action's options. Lazy import keeps the
    actions package import-light.
    """
    from ..schedule import build_schedule

    opts = action.options
    return build_schedule(
        checker_path=Path(opts["checker_path"]),
        hour=int(opts.get("hour", 12)),
        minute=int(opts.get("minute", 0)),
        label=str(opts.get("label", "")),
        platform=str(opts.get("platform", "")) or None,
    )


def _read_crontab() -> tuple[bool, str]:
    """Return (has_crontab, contents). A missing crontab is (False, "") — not an error."""
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if res.returncode != 0:
        # `crontab -l` exits non-zero when no crontab exists for the user — treat as empty.
        return False, ""
    return True, res.stdout


def _write_crontab(contents: str) -> int:
    try:
        res = subprocess.run(
            ["crontab", "-"], input=contents, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _crontab_without_managed(contents: str, label: str) -> list[str]:
    """Strip rig's managed sentinel pair (comment + following cron line) for ``label``.

    Removes the ``# rig-managed: <label>`` comment AND the cron line immediately after it,
    leaving every other (user) line untouched. The basis for idempotent re-write + removal.
    """
    from ..schedule import CRON_SENTINEL_PREFIX

    sentinel = f"{CRON_SENTINEL_PREFIX} {label}"
    lines = contents.splitlines()
    out: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == sentinel:
            skip_next = True  # drop the cron line that follows the sentinel
            continue
        out.append(line)
    return out


def crontab_with_managed(contents: str, label: str, desired_pair: list[str]) -> list[str] | None:
    """Return the crontab lines with rig's managed pair updated IN PLACE — or None if no
    change is needed (already exactly present at its current position).

    Position-preserving: if the managed pair already exists, the cron line is replaced where
    it sits (keeping any user lines that follow it); if absent, the pair is appended. This
    avoids the spurious "drift → reorder-to-end" churn that a strip-then-append would cause
    when the user has their own crontab lines after rig's block. Returns None when the desired
    pair is already present unchanged (the idempotent no-op).
    """
    from ..schedule import CRON_SENTINEL_PREFIX

    sentinel = f"{CRON_SENTINEL_PREFIX} {label}"
    lines = contents.splitlines()
    out: list[str] = []
    replaced = False
    changed = False
    i = 0
    while i < len(lines):
        if lines[i].strip() == sentinel:
            # found our block: emit the desired pair here (in place), skip the old cron line
            out.extend(desired_pair)
            old_cron = lines[i + 1] if i + 1 < len(lines) else ""
            if old_cron.strip() != desired_pair[1].strip():
                changed = True
            replaced = True
            i += 2  # consume the sentinel + the cron line that followed it
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        # append our block (drop a trailing blank so the file stays tidy)
        while out and not out[-1].strip():
            out.pop()
        out.extend(desired_pair)
        changed = True
    return out if changed else None


def _schedule_dry_run() -> bool:
    """Honor RIG_SCHEDULE_DRY_RUN — write the artifact file but DON'T touch the live daemon.

    For CI / containers / smoke where a real ``launchctl load`` (a per-user daemon mutation
    HOME can't redirect) or a real ``crontab`` write is unwanted. The plist file still lands
    (in the configured/HOME path), but the daemon load / crontab write is skipped.
    """
    return os.environ.get("RIG_SCHEDULE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _tmux_dry_run() -> bool:
    """Honor RIG_TMUX_DRY_RUN — write the tmux artifacts but DON'T run the LIVE activation.

    The live activation (clone tpm/resurrect/continuum, create ~/.tmux/resurrect, ``launchctl
    load -w`` the boot agent, take a first ``resurrect save``, clean continuum's stale macOS
    boot Login Items) is real network + daemon + ``tmux``-server access — unwanted in CI /
    containers / the unit suite. With the flag set, the on-disk artifacts still land; the live
    effects are skipped. Mirrors :func:`_schedule_dry_run`.
    """
    return os.environ.get("RIG_TMUX_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _do_provision_schedule(action: Action, on_conflict: str) -> ActionResult:
    """Install the daily model-freshness schedule IF MISSING (idempotent).

    The "check whether the cron exists and install it if missing" rule: a re-apply that finds the schedule
    already present and current is a no-op (``skipped``); a missing/drifted schedule is
    (re)installed. Cross-platform: launchd plist + ``launchctl load`` on macOS, a managed
    sentinel-fenced crontab line on Linux. ``on_conflict`` is honored for the macOS plist
    (a user-modified plist at the path is backed up before rewrite under ``backup``).
    ``RIG_SCHEDULE_DRY_RUN`` writes the artifact file but skips the live daemon mutation.
    """
    sched = schedule_plan_from_action(action)
    if sched.platform == "launchd":
        return _provision_launchd(action, sched, on_conflict)
    return _provision_crontab(action, sched)


def _provision_launchd(action: Action, sched, on_conflict: str) -> ActionResult:
    plist_path = sched.plist_path
    if plist_path is None:
        return ActionResult(action, "error", "launchd: no plist path resolved")
    desired = sched.plist_xml()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = (sched.log_path or Path.home() / "Library" / "Logs").parent
    log_dir.mkdir(parents=True, exist_ok=True)

    already = plist_path.is_file()
    current = plist_path.read_text(encoding="utf-8") if already else ""
    if already and current == desired and _launchctl_loaded(sched.label):
        # present AND current AND loaded → nothing to do (the install-if-missing no-op).
        return ActionResult(action, "skipped", f"models/{action.item}: launchd job '{sched.label}' already installed at {sched.human_time}")

    out = fsutil.write_file(plist_path, desired, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"models/{action.item}: {out.detail}", out.backup)
    # Conflict-skip: the existing plist DIFFERS from desired but on_conflict=skip told us not to
    # write it. The desired schedule never hit disk, so we must NOT unload/load the stale plist
    # (that would mutate launchd with the wrong schedule) and must NOT report 'updated' (that
    # would mask the unresolved drift). Surface 'skipped' so the drift is visible. We reach this
    # only when `desired != current` (the byte-identical case took the early-return no-op above
    # or write_file's identical-bytes path, which still proceeds to a needed (re)load below).
    if out.status == "skipped" and already and current != desired:
        return ActionResult(
            action, "skipped",
            f"models/{action.item}: launchd plist {plist_path} differs but on_conflict=skip — "
            f"left unchanged (drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
        )
    if _schedule_dry_run():
        return ActionResult(
            action, out.status if out.status != "skipped" else "created",
            f"models/{action.item}: wrote plist {plist_path} (RIG_SCHEDULE_DRY_RUN — skipped launchctl load)",
            out.backup,
        )
    # (re)load the agent so launchd picks up the (possibly new) calendar interval. unload
    # first is safe — it is a no-op when the label isn't loaded.
    _launchctl("unload", str(plist_path))
    rc = _launchctl("load", str(plist_path))
    if rc != 0:
        return ActionResult(
            action, "error",
            f"models/{action.item}: wrote {plist_path} but `launchctl load` failed (rc={rc})",
            out.backup,
        )
    # We reach here only when the early-return no-op did NOT fire — i.e. the plist was
    # written/changed OR the job was not loaded. So the run made a real change: report the
    # file-write status, but never a misleading 'skipped' (the file may be byte-identical yet
    # we still (re)loaded the job into launchd, which IS the change).
    status = out.status if out.status in ("created", "backed_up") else "updated"
    verb = "installed" if not already else "updated"
    return ActionResult(
        action, status,
        f"models/{action.item}: launchd job '{sched.label}' {verb} → daily {sched.human_time} ({plist_path})",
        out.backup,
    )


def _provision_crontab(action: Action, sched) -> ActionResult:
    desired_pair = sched.crontab_lines()
    _has, current = _read_crontab()
    # Position-preserving update: replace our block where it sits (or append if absent), so a
    # user's own crontab lines that come AFTER rig's block don't trigger a spurious reorder.
    new_lines = crontab_with_managed(current, sched.label, desired_pair)
    if new_lines is None:
        # already present and current at its position → idempotent no-op.
        return ActionResult(action, "skipped", f"models/{action.item}: crontab line for '{sched.label}' already installed at {sched.human_time}")
    if _schedule_dry_run():
        return ActionResult(
            action, "created",
            f"models/{action.item}: RIG_SCHEDULE_DRY_RUN — would install crontab line '{sched.label}' at {sched.human_time} (not written)",
        )
    new_contents = "\n".join(new_lines).rstrip("\n") + "\n"
    rc = _write_crontab(new_contents)
    if rc != 0:
        return ActionResult(action, "error", f"models/{action.item}: `crontab -` write failed (rc={rc})")
    verb = "installed" if not any(sched.label in ln for ln in current.splitlines()) else "updated"
    return ActionResult(
        action, "created",
        f"models/{action.item}: crontab line '{sched.label}' {verb} → daily {sched.human_time}",
    )


def _launchctl(verb: str, arg: str) -> int:
    try:
        res = subprocess.run(["launchctl", verb, arg], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _launchctl_loaded(label: str) -> bool:
    try:
        res = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _launchctl_load_enable(plist: Path) -> int:
    """``launchctl load -w <plist>`` — load the agent AND enable it across reboots (``-w``).

    Separate from :func:`_launchctl` because ``-w`` is a FLAG that must be its own argv token
    (``["launchctl", "load", "-w", <plist>]``), not folded into the verb. Used to actually FIRE
    the tmux boot agent at login — rig previously wrote the plist but never loaded it (DEFECT 1).
    """
    try:
        res = subprocess.run(
            ["launchctl", "load", "-w", str(plist)], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


# ── gui-domain launchctl (modern bootstrap/bootout) ─────────────────────────────────
# The model-freshness schedule uses the legacy ``launchctl load/unload`` (``_launchctl_loaded``
# above); the tg-ctl inbound daemon uses the MODERN per-user ``gui/<uid>`` domain verbs
# (``bootstrap``/``bootout``), which is what macOS recommends and what loads a fresh agent without
# a reboot. Kept separate so the two services don't share a verb set.
def _gui_domain() -> str:
    """The per-user GUI launchd domain target ``gui/<uid>`` for the current user."""
    return f"gui/{os.getuid()}"


def _launchctl_gui(verb: str, plist_path: str) -> int:
    """``launchctl <verb> gui/<uid> <plist>`` — bootstrap (load) / bootout (unload) an agent in
    the per-user GUI domain. Returns the rc; a non-zero rc from ``bootout`` of an unloaded agent
    is harmless (the caller bootstraps after). One shell for both verbs (DRY)."""
    try:
        res = subprocess.run(
            ["launchctl", verb, _gui_domain(), plist_path],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


# Thin, self-documenting wrappers so call sites read as bootout/bootstrap (and tests can spy on
# each verb independently).
def _launchctl_bootout(plist_path: str) -> int:
    return _launchctl_gui("bootout", plist_path)


def _launchctl_bootstrap(plist_path: str) -> int:
    return _launchctl_gui("bootstrap", plist_path)


def _launchctl_gui_loaded(label: str) -> bool:
    """True when ``label`` is loaded in the per-user GUI domain (``launchctl print gui/<uid>/
    <label>`` returns 0). Used by the install no-op check + drift so a written-but-not-loaded
    agent is still flagged."""
    try:
        res = subprocess.run(
            ["launchctl", "print", f"{_gui_domain()}/{label}"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


# ── git/gh shells (isolated for testability) ──────────────────────────────────────
def _git_global(key: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "config", "--global", key], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def _set_git_global(key: str, value: str) -> int:
    try:
        res = subprocess.run(
            ["git", "config", "--global", key, value], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _gh_alias_set(name: str, path: str) -> int:
    if not shutil.which("gh"):
        return 1
    try:
        res = subprocess.run(
            ["gh", "alias", "set", name, f"!{path}"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


# ── tmux configuration provisioning (generate + migrate) ───────────────────────────
def tmux_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tmux.TmuxPlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired
    artifacts (the generated rig.tmux.conf, the cc scripts, the import line / managed block)
    from the action's options. ``Path.home()`` is the resolved HOME at apply time (a test
    monkeypatches it to a tmp HOME). Lazy import keeps the actions package import-light.
    """
    from ..tmux import build_tmux

    opts = action.options
    return build_tmux(
        repo_home=Path.home(),
        apply_mode=str(opts.get("apply_mode", "import")),
        conf_path=str(opts.get("conf_path", "~/.tmux.conf")),
        generated_dir=str(opts.get("generated_dir", "~/.config/rig/tmux")),
        resurrect=dict(opts.get("resurrect", {}) or {}),
        continuum=dict(opts.get("continuum", {}) or {}),
        moshi=dict(opts.get("moshi", {}) or {}),
        cc_restore=dict(opts.get("cc_restore", {}) or {}),
        anti_sprawl=dict(opts.get("anti_sprawl", {}) or {}),
        boot=dict(opts.get("boot", {}) or {}),
        login_shell=dict(opts.get("login_shell", {}) or {}),
    )


def _timestamped_backup_path(base: Path) -> Path:
    """Turn a fixed backup path (``<conf>.rig-bak``) into a UNIQUE timestamped one
    (``<conf>.rig-bak-<UTC>``). A fixed name was written only on the FIRST migration
    (guarded by ``not exists()``), so a later apply — after the user hand-edited the conf —
    would neutralize those edits WITHOUT a backup. A timestamped name never collides, so every
    migrating apply keeps its own restore point. Microsecond precision avoids a same-second
    collision. (CTO 2026-06-16)
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    path = base.with_name(f"{base.name}-{stamp}")
    n = 1
    while path.exists():  # paranoia: two applies inside the same microsecond
        path = base.with_name(f"{base.name}-{stamp}-{n}")
        n += 1
    return path


def _do_provision_tmux(action: Action, on_conflict: str) -> ActionResult:
    """Generate the rig-managed tmux artifacts and migrate ``~/.tmux.conf`` (idempotent).

    What it writes:
      - ``<generated_dir>/rig.tmux.conf`` — the rig-owned config (wholesale rewrite; the
        ordering guarantee that fixes the continuum-hook-wipe bug lives in the generator).
      - ``<generated_dir>/cc-save.sh`` + ``cc-restore.sh`` — the managed scripts, chmod +x.
      - ``~/.tmux.conf`` — in IMPORT mode, a single ``source-file`` line (the rest of the
        user's file untouched); in BLOCK mode, the managed body spliced between sentinels.
      - the boot launchd plist (macOS) when ``boot.enabled`` — written but NEVER loaded into
        launchd here (the user reloads on their own reboot; we don't disrupt a live server).

    Migration + backup: when the existing ``~/.tmux.conf`` carries rig-owned settings inline
    (resurrect/continuum/tpm/Moshi), the ORIGINAL is backed up to ``~/.tmux.conf.rig-bak``
    BEFORE the rewrite — and an existing backup is never overwritten (the true original is
    preserved). User-specific lines are left intact.

    Idempotent: a re-apply that finds every artifact already current is a ``skipped`` no-op.
    rig NEVER reloads the user's LIVE tmux server (no ``tmux source-file``) — the on-disk
    result is prepared; the user reloads when ready.
    """
    from ..tmux import has_inline_rig_settings, neutralize_inline_rig_lines, splice_managed_block

    plan = tmux_plan_from_action(action)
    plan.generated_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    backup: Path | None = None
    # collect EVERY backup fsutil makes (a differing generated file moved to .rig-bak-* under
    # on_conflict=backup) so a replacement is never silently lost — the repo's "backup-noted"
    # contract. The migration backup (below) is the headline one returned in ActionResult.backup.
    extra_backups: list[Path] = []
    details: list[str] = []
    # conflict-skipped managed scripts (left as the user's under on_conflict=skip) — surfaced in
    # the result detail but NOT counted as a change (nothing was written): unresolved drift.
    skipped_conflicts: list[str] = []

    # 1) the generated rig.tmux.conf (wholesale, idempotent on identical bytes).
    conf_out = fsutil.write_file(plan.rig_conf_path, plan.render_rig_conf(), on_conflict)
    if conf_out.status == "error":
        return ActionResult(action, "error", f"tmux: {conf_out.detail}")
    if conf_out.backup:
        extra_backups.append(conf_out.backup)
    if conf_out.status != "skipped":
        changed = True
        details.append(f"generated {plan.rig_conf_path.name}")
    elif not conf_out.detail.startswith("identical"):
        # rig OWNS rig.tmux.conf — a DIFFERING one left untouched under on_conflict=skip is stale
        # (e.g. an upgrade still carrying the old `@continuum-boot 'on'`) → unresolved drift the
        # sourced tmux still uses. Surface it (NOT silently 'already current'), like the scripts.
        skipped_conflicts.append(
            f"{plan.rig_conf_path.name} differs and on_conflict=skip — NOT regenerated; tmux "
            f"still sources the STALE rig config (re-run with backup/overwrite to update it)"
        )

    # 2) the managed scripts (chmod +x) — cc-save/cc-restore always, the anti-sprawl
    # attach-or-create entry when enabled, the boot script when boot is enabled. `managed_scripts()`
    # is the ONE source apply and drift share, so they can't diverge on which scripts exist.
    # `boot_script_conflicted` records a DIFFERING boot script left untouched under skip: the plist
    # the launchd agent runs points at THIS script, so loading the agent would run a stale boot
    # script — suppress the load in that case too (review P1), like the plist/conf conflicts below.
    boot_script_conflicted = False
    for path, body in plan.managed_scripts():
        out = fsutil.write_file(path, body, on_conflict)
        if out.status == "error":
            return ActionResult(action, "error", f"tmux: {out.detail}")
        if out.backup:
            extra_backups.append(out.backup)
        wrote = out.status != "skipped"
        identical_skip = out.status == "skipped" and out.detail.startswith("identical")
        conflict_skip = out.status == "skipped" and not identical_skip
        if wrote:
            path.chmod(0o755)
            changed = True
            details.append(f"installed {path.name}")
        elif identical_skip and not os.access(path, os.X_OK):
            # drift-heal: contents identical (OUR file) but +x stripped → chmod IS a real change;
            # report it (don't hide a hook-executable-bit repair behind 'already current'). We
            # NEVER chmod a conflict-skip (a differing pre-existing file is the user's under skip).
            path.chmod(0o755)
            changed = True
            details.append(f"restored +x on {path.name}")
        if conflict_skip:
            if path == plan.boot_script_path:
                boot_script_conflicted = True
            # a pre-existing DIFFERING file at rig's script path was left untouched under
            # on_conflict=skip — but the generated config wires a resurrect hook at this path, so
            # resurrect would run the user's/stale file. SURFACE it in the detail, but do NOT set
            # `changed` — nothing was written, so a re-apply must NOT report `created` or inflate
            # ApplyReport.changed every run; this is unresolved drift, reported, not a change.
            skipped_conflicts.append(
                f"{path.name} differs and on_conflict=skip — NOT applied; the resurrect hook "
                f"points at an unmanaged file (re-run with backup/overwrite to wire rig's script)"
            )

    # 3) the boot launchd plist (macOS) — written, never loaded (the user reboots).
    # `boot_plist_conflicted` records a DIFFERING plist left untouched under on_conflict=skip: the
    # activation must then NOT `launchctl load -w` it (that would ENABLE a stale/unmanaged boot
    # path despite skip semantics — codex finding). Same for a conflict-skipped ~/.tmux.conf below.
    boot_plist_conflicted = False
    boot_plist_changed = False  # plist (re)written this apply → an already-loaded agent is reloaded.
    if plan.boot_enabled and sys.platform == "darwin":
        plan.boot_plist_path.parent.mkdir(parents=True, exist_ok=True)
        boot_out = fsutil.write_file(plan.boot_plist_path, plan.render_boot_plist(), on_conflict)
        if boot_out.status == "error":
            return ActionResult(action, "error", f"tmux: {boot_out.detail}")
        if boot_out.backup:
            extra_backups.append(boot_out.backup)
        if boot_out.status != "skipped":
            changed = True
            boot_plist_changed = True
            details.append(f"wrote boot plist {plan.boot_plist_path.name} (load on next login/reboot)")
        elif not boot_out.detail.startswith("identical"):
            # rig OWNS the boot plist — a DIFFERING one left untouched under skip is stale.
            boot_plist_conflicted = True
            skipped_conflicts.append(
                f"{plan.boot_plist_path.name} differs and on_conflict=skip — NOT updated "
                f"(re-run with backup/overwrite to refresh the boot plist)"
            )

    # 4) ~/.tmux.conf — migrate (back up an inline-settings original) then wire the managed region.
    conf = plan.conf_path
    existing = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    # Migration backup: whenever the conf still carries rig-owned settings inline (so migration
    # will NEUTRALIZE live user lines), snapshot it FIRST under a UNIQUE timestamped name. A
    # fixed `.rig-bak` was written only on the first migration (`not exists()`), so a second
    # apply after the user hand-edited ~/.tmux.conf would neutralize those edits WITHOUT backing
    # them up — losing the in-between state. A timestamped name keeps every restore point.
    if existing and has_inline_rig_settings(existing):
        backup_target = _timestamped_backup_path(plan.backup_path)
        backup_target.write_text(existing, encoding="utf-8")
        backup = backup_target
        details.append(f"backed up original → {backup_target.name}")
        changed = True

    conf_conflicted = False
    desired_conf = _tmux_conf_with_managed(
        plan, existing, splice_managed_block, neutralize_inline_rig_lines
    )
    if desired_conf != existing:
        # Honor on_conflict=skip for the user's OWN file: if ~/.tmux.conf already exists and
        # differs, `skip` means leave it untouched (consistent with the generated artifacts,
        # which go through fsutil.write_file's skip path). A non-existent conf is always created
        # (there's nothing to conflict with). backup/overwrite both proceed to write.
        if on_conflict == "skip" and existing:
            conf_conflicted = True
            details.append(f"~/.tmux.conf differs but on_conflict=skip — left unwired ({conf.name})")
        else:
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(desired_conf, encoding="utf-8")
            changed = True
            details.append(f"wired {plan.apply_mode} into {conf.name}")

    # 5) LIVE activation (DEFECTS 1/4/5/6) — make a CLEAN machine fully working with no manual
    # steps: create ~/.tmux/resurrect, clone missing plugins, launchctl-load the boot agent,
    # take a FIRST resurrect save (only if none exists), clean continuum's stale macOS boot. Each
    # step is idempotent (skip-if-present) and runs even when the file-write path was a no-op (a
    # deleted plugin must be re-cloned on re-apply). RIG_TMUX_DRY_RUN skips ALL of it (CI/unit).
    # Returns (real changes, warnings): ONLY real changes mark `changed` (so a steady-state
    # re-apply stays a no-op); warnings (failed clone/launchctl — offline) are surfaced but never
    # inflate ApplyReport.changed (codex/opus idempotency finding).
    #
    # boot_load_safe gates the launchctl-load: if the boot plist, the BOOT SCRIPT it runs, OR the
    # ~/.tmux.conf wiring was CONFLICT-skipped (left stale/unwired), loading the agent would enable
    # a stale/unmanaged boot path despite skip semantics — so we suppress the load then (review
    # findings). Plugins / the resurrect dir / the first save are still safe to run.
    act_changes, act_warnings = _tmux_activate(
        plan,
        boot_load_safe=not (boot_plist_conflicted or boot_script_conflicted or conf_conflicted),
        boot_plist_changed=boot_plist_changed,
    )
    if act_changes:
        changed = True
        details.extend(act_changes)
    skipped_conflicts.extend(act_warnings)

    if not changed:
        # nothing was written. If a managed script was conflict-skipped, that is UNRESOLVED drift
        # (the hook points at an unmanaged file) — report it as `skipped` with the warning, NOT a
        # `created` change (a re-apply must stay idempotent and not inflate ApplyReport.changed).
        if skipped_conflicts:
            return ActionResult(
                action, "skipped",
                f"tmux/config: already current EXCEPT — {'; '.join(skipped_conflicts)}",
            )
        return ActionResult(action, "skipped", f"tmux/config: already current ({plan.apply_mode} mode)")
    # surface EVERY backup (migration + any generated-file .rig-bak-* moves) so a replacement's
    # restore path is never hidden — the repo's "backup-noted" contract. The headline backup is
    # the migration one if present, else the first generated-file backup.
    if extra_backups:
        details.append("backups: " + ", ".join(b.name for b in extra_backups))
    # a conflict-skipped script alongside other real changes is still surfaced (so the operator
    # sees the unwired hook), appended to the detail of the (genuinely-changed) result.
    details.extend(skipped_conflicts)
    headline = backup or (extra_backups[0] if extra_backups else None)
    status = "backed_up" if headline else "created"
    return ActionResult(action, status, f"tmux/config: {'; '.join(details)}", headline)


def _tmux_activate(
    plan, *, boot_load_safe: bool = True, boot_plist_changed: bool = False
) -> tuple[list[str], list[str]]:
    """Bring the rig-managed tmux LIVE on this machine (DEFECTS 1/4/5/6).

    Returns ``(changes, warnings)``: ``changes`` are real mutations the run performed (the caller
    marks the apply ``changed`` ONLY for these, so a steady-state re-apply with nothing to do is a
    genuine no-op); ``warnings`` are non-fatal degradations (a failed clone / launchctl on an
    offline machine) — surfaced to the operator but NEVER counted as a change (else every re-apply
    would falsely report ``created`` — codex/opus idempotency finding).

    ``boot_load_safe`` (caller-supplied): when False — the boot plist / boot script / ``~/.tmux.conf``
    wiring was CONFLICT-skipped (left stale/unwired) — the launchctl-load is SUPPRESSED so we never
    enable a stale/unmanaged boot path despite on_conflict=skip (review finding). The non-boot steps
    (plugins / resurrect dir / first save) still run; they don't risk activating a stale boot.

    ``boot_plist_changed`` (caller-supplied): the boot plist was (re)written this apply. We load the
    agent when it is NOT loaded; we only UNLOAD-then-reload an ALREADY-loaded agent when the plist
    CHANGED. A steady-state re-apply (loaded + unchanged) does nothing — so we never restart the
    agent every run (re-spawning a ``main`` session on the live server) and a transient load failure
    can't disable a working unchanged agent (review findings).

    Steps, each idempotent and non-fatal (a clean machine must end up FULLY working with zero
    manual steps; a partial/offline machine degrades, never aborts the whole apply):

      4) create ``~/.tmux/resurrect`` so resurrect can write its ``tmux_resurrect_*.txt``
         snapshot (absent dir = no snapshot ever written = nothing to restore on reboot).
      6) clone the canonical tmux plugins (tpm + resurrect + continuum) into ``~/.tmux/plugins``
         if MISSING (default branch, one-shot, never auto-upgraded — see tmux.PLUGINS' trust
         contract), so the ``@plugin`` declarations actually resolve on a clean machine.
      1) on macOS, ``launchctl load -w`` the boot agent so it FIRES at login (rig used to write
         the plist but never load it). The boot script itself is idempotent (``has-session`` →
         exit 0), so loading it never disrupts an active session.
      5) on macOS, clean continuum's OWN stale boot (``osx_iterm/terminal_start_tmux.sh`` Login
         Items + an old ``Tmux.Start`` launchd agent) that competes with rig's boot agent — gated
         on ``boot.enabled`` (if the user opted OUT of rig boot, never nuke their own autostart).
      6b) take a FIRST ``resurrect save`` ONLY when no snapshot exists yet — so a re-apply never
         re-saves (idempotency) and never clobbers a good snapshot with an empty/partial one.

    ``RIG_TMUX_DRY_RUN`` skips every live step (the file artifacts already landed in the caller).
    """
    if _tmux_dry_run():
        return [], []

    from .. import tmux as tmod

    changes: list[str] = []
    warnings: list[str] = []

    # 4) the resurrect snapshot dir.
    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        resurrect_dir.mkdir(parents=True, exist_ok=True)
        changes.append("created ~/.tmux/resurrect")

    # 6) clone missing plugins (idempotent: skip a COMPLETE checkout that already exists). A
    # partial dir from a prior failed clone is NOT treated as installed — it is removed and
    # re-cloned, so the "offline retries next apply" contract holds (codex finding).
    plugins_dir = plan.home / ".tmux" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for name, (repo, entrypoint) in tmod.PLUGINS.items():
        dest = plugins_dir / name
        if (dest / entrypoint).exists():
            continue
        if dest.exists():
            # a partial/broken checkout (entrypoint missing) — clear it so the clone retries clean.
            shutil.rmtree(dest, ignore_errors=True)
        rc = _git_clone(repo, dest)
        if rc == 0:
            changes.append(f"installed plugin {name}")
        else:
            # non-fatal (offline / no git): a WARNING, not a change — must not inflate `changed`.
            shutil.rmtree(dest, ignore_errors=True)  # drop any partial dir the failed clone left.
            warnings.append(f"plugin {name} NOT installed (clone failed rc={rc} — offline?)")

    # 1) launchctl-load the boot agent (macOS) so it fires at login. `-w` enables it across reboots.
    # Load ONLY when there is real work:
    #   - NOT loaded  → load it (no unload first — nothing to unload; a steady-state re-apply where
    #     it's already loaded does NOTHING, so apply stays a no-op AND we never restart it every run
    #     / re-spawn a `main` session on the live server: review Low).
    #   - loaded BUT the plist was rewritten this apply (boot_plist_changed) → unload then load -w so
    #     launchd picks up the new definition (codex: a stale loaded job must be refreshed).
    # We unload ONLY in the changed-plist branch, so a transient load failure can leave the agent
    # off only when we deliberately refreshed a CHANGED plist (surfaced as a warning) — never for an
    # unchanged steady-state agent (review Medium: unconditional unload could disable a working one).
    # SUPPRESSED entirely when boot_load_safe is False — the plist / boot script / ~/.tmux.conf was
    # conflict-skipped (stale/unwired), so loading would enable a stale boot path (review finding).
    #
    # rig_boot_active tracks whether rig's REPLACEMENT boot agent is actually in place after this
    # block — freshly loaded, or already-loaded-and-still-safe. The stale-boot cleanup (step 5) is
    # gated on it: removing continuum's own autostart (Login Items / Tmux.Start) while rig has NOT
    # got a working replacement loaded would leave the machine with NO tmux autostart at all on the
    # next login (a conflict-skip / offline / launchctl-failure path). So we only clean once rig's
    # boot is confirmed active (review finding).
    rig_boot_active = False
    if plan.boot_enabled and sys.platform == "darwin" and plan.boot_plist_path.is_file():
        if not boot_load_safe:
            warnings.append(
                "boot agent NOT loaded — the boot plist, boot script, or ~/.tmux.conf was "
                "conflict-skipped (stale/unwired); re-run with on_conflict=backup/overwrite to load it"
            )
        elif not _launchctl_loaded(plan.boot_label):
            rc = _launchctl_load_enable(plan.boot_plist_path)
            if rc != 0:
                warnings.append(f"boot agent NOT loaded (launchctl rc={rc})")
            else:
                changes.append(f"launchctl load -w {plan.boot_plist_path.name}")
                rig_boot_active = True
        elif boot_plist_changed:
            # refresh a CHANGED plist into the already-running agent: unload then load -w.
            _launchctl("unload", str(plan.boot_plist_path))
            rc = _launchctl_load_enable(plan.boot_plist_path)
            if rc != 0:
                warnings.append(
                    f"boot agent reload FAILED (launchctl rc={rc}) — it may be left unloaded; "
                    f"re-run `rig apply` or `launchctl load -w {plan.boot_plist_path}`"
                )
            else:
                changes.append(f"reloaded boot agent {plan.boot_plist_path.name} (plist changed)")
                rig_boot_active = True
        else:
            # already loaded + safe + unchanged → rig's boot is in place (steady-state re-apply).
            rig_boot_active = True

    # 5) clean continuum's stale macOS boot (Login Items + old Tmux.Start agent) — macOS only, only
    # when rig owns boot (don't remove the user's own autostart if they opted out of rig boot), AND
    # only when rig's REPLACEMENT boot is actually active (never strip the last autostart while our
    # own replacement failed to load / was conflict-skipped — review finding).
    if plan.boot_enabled and sys.platform == "darwin" and rig_boot_active:
        if _clean_stale_continuum_boot(plan):
            changes.append("cleaned stale continuum boot (Login Items / old Tmux.Start)")

    # 6b) take a FIRST resurrect save ONLY if no snapshot exists yet — so a re-apply is a no-op and
    # an existing good snapshot is never clobbered by an empty/partial one (opus finding).
    if not _resurrect_snapshot_exists(plan) and _tmux_resurrect_save(plan) == 0:
        changes.append("took first resurrect save")

    return changes, warnings


def _resurrect_snapshot_exists(plan) -> bool:
    """True if a resurrect snapshot already exists (so we DON'T re-take a first save). resurrect
    writes ``tmux_resurrect_*.txt`` files plus a ``last`` symlink to the newest; either signals a
    prior save."""
    resurrect_dir = plan.home / ".tmux" / "resurrect"
    if not resurrect_dir.is_dir():
        return False
    if (resurrect_dir / "last").exists():
        return True
    return any(resurrect_dir.glob("tmux_resurrect_*.txt"))


def _git_clone(repo: str, dest: Path) -> int:
    """Shallow-clone ``repo`` to ``dest``. Returns 0 on success, non-zero on any failure (so the
    caller treats an offline/no-git machine as 'plugin not installed', never an apply abort)."""
    if not shutil.which("git"):
        return 127
    try:
        res = subprocess.run(
            ["git", "clone", "--depth", "1", repo, str(dest)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _tmux_resurrect_save(plan) -> int:
    """Take a resurrect snapshot of the CURRENT live tmux server. Returns 0 on success, non-zero
    otherwise. Non-fatal: a machine with no running server (or no resurrect plugin) just doesn't
    get a save (a later apply, after the user starts tmux, retries).

    Saves the server ONLY IF one is ALREADY running — it does NOT start a server to save (opus
    finding): booting a bare ``main`` session and immediately saving could snapshot an empty,
    pre-restore state and clobber a good snapshot. resurrect ships a standalone ``scripts/save.sh``
    that writes the snapshot without a key-binding; we invoke it directly. Does NOT depend on the
    boot script existing (so it still works when ``boot.enabled`` is false but a server is up —
    the boot script is only written when boot is enabled).
    """
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        return 127
    save_script = plan.home / ".tmux" / "plugins" / "tmux-resurrect" / "scripts" / "save.sh"
    if not save_script.is_file():
        return 1
    try:
        # only save when a server is already running — never start one just to snapshot it.
        # Probe with `list-sessions`, NOT `has-session`: a bare `has-session` (no `-t`) resolves a
        # target session from $TMUX / the most-recent session, so OUTSIDE tmux (a launchd/cron/plain
        # apply — exactly how rig runs) it can return non-zero even when a server IS up, silently
        # skipping the first save. `list-sessions` exits 0 iff any server with a session is alive,
        # regardless of attach context (1 + "no server running" otherwise). (review finding)
        probe = subprocess.run(
            [tmux_bin, "list-sessions"], capture_output=True, text=True, timeout=10
        )
        if probe.returncode != 0:
            return 1  # no live server → nothing to save (a later apply retries when one is up).
        res = subprocess.run(
            ["bash", str(save_script)], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return res.returncode


def _clean_stale_continuum_boot(plan) -> bool:
    """Disable/remove continuum's OWN macOS boot artifacts that compete with rig's boot agent
    (DEFECT 5). Returns True if there WAS stale state and it was cleaned; False on a clean
    machine (so a re-apply is a no-op — true idempotency, not "ran osx_disable.sh again").

    continuum, when ``@continuum-boot on`` was ever set, installs an iTerm/Terminal-coupled boot:
      - ``~/.tmux/plugins/tmux-continuum/scripts/…`` registers ``osx_iterm_start_tmux.sh`` /
        ``osx_terminal_start_tmux.sh`` as macOS Login Items, AND
      - an old ``Tmux.Start`` launchd agent (``~/Library/LaunchAgents/Tmux.Start.plist``).
    Both fight rig's single launchd boot agent. The stale-boot SIGNAL is the old
    ``Tmux.Start.plist`` (continuum writes it when its boot is enabled; rig never does). Only when
    that signal is present do we (a) run continuum's documented ``osx_disable.sh`` to un-register
    its Login Items, and (b) ``launchctl bootout`` + remove the old plist. A machine with no
    Tmux.Start plist has no stale boot → nothing to do → return False.
    """
    old_plist = plan.home / "Library" / "LaunchAgents" / "Tmux.Start.plist"
    if not old_plist.is_file():
        return False  # no stale continuum boot present — idempotent no-op.

    # un-register continuum's Login Items via its own documented disable script (if installed).
    osx_disable = (
        plan.home / ".tmux" / "plugins" / "tmux-continuum" / "scripts" / "osx_disable.sh"
    )
    if osx_disable.is_file():
        try:
            subprocess.run(["bash", str(osx_disable)], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
    # bootout + unload + remove the old Tmux.Start launchd agent (continuum's iTerm boot).
    uid = os.getuid() if hasattr(os, "getuid") else 0
    _launchctl("bootout", f"gui/{uid}/Tmux.Start")
    _launchctl("unload", str(old_plist))
    try:
        old_plist.unlink()
    except OSError:
        pass
    return True


def _tmux_conf_with_managed(plan, existing: str, splice, neutralize) -> str:
    """The desired ``~/.tmux.conf`` text for the plan's apply mode (pure).

    - import mode: neutralize the inline rig-owned lines (so the sourced rig config is
      authoritative), then ensure the single ``source-file`` import is present exactly once at
      the end (drop a prior copy so a moved generated path doesn't leave a stale import).
    - block mode: neutralize the SAME inline rig-owned lines in the user's region (a hand-written
      conf carries the old plugin/continuum/resurrect init OUTSIDE where rig splices its block —
      without neutralizing, the user's `run-shell …/continuum.tmux` still fires the double-init
      the live machine hit), then splice the generated body between the sentinels (conda-init
      style). `neutralize` itself skips lines INSIDE the managed block (they are rig's generated
      config), so it never corrupts rig's own region — the correctness does NOT rely on splice
      overwriting the interior.
    """
    if plan.apply_mode == "block":
        neutralized = neutralize(existing) if existing else existing
        return splice(neutralized, plan.render_rig_conf())
    # import mode — neutralize the redundant/harmful inline rig-owned lines FIRST (so the
    # sourced rig.tmux.conf is authoritative; otherwise a leftover inline Moshi `status-right
    # ''` after the user's own continuum init still wipes continuum's hook), then ensure the
    # single source-file import is present exactly once at the END (after the user's lines).
    neutralized = neutralize(existing) if existing else existing
    import_line = plan.import_line()
    rig_conf_name = plan.rig_conf_path.name  # "rig.tmux.conf" — rig owns this filename
    kept = [
        ln for ln in neutralized.splitlines()
        if not _is_rig_import_line(ln, import_line, rig_conf_name)
    ]
    body = "\n".join(kept).rstrip("\n")
    if body:
        return body + "\n" + import_line + "\n"
    return import_line + "\n"


def _is_rig_import_line(line: str, import_line: str, rig_conf_name: str) -> bool:
    """True if ``line`` is rig's OWN ``source-file …/rig.tmux.conf`` import (current or a stale
    one pointing at an old generated_dir) — so it is dropped before re-appending the current
    import exactly once. A comment or a keybinding that merely mentions the path is NOT matched
    (we require the line to BE a `source-file` directive whose argument ends with the rig file).
    """
    s = line.strip()
    if s == import_line:
        return True
    if s.startswith("#"):
        return False
    parts = s.split()
    # `source-file [-q] <path>` — rig's own import names the rig.tmux.conf file as its argument.
    if parts and parts[0] == "source-file":
        arg = parts[-1].strip("'\"")
        return Path(arg).name == rig_conf_name
    return False


# ── tg-ctl inbound-daemon LaunchAgent provisioning (macOS) ──────────────────────────
def _tg_ctl_dry_run() -> bool:
    """Honor RIG_TG_CTL_DRY_RUN — write the managed plist file but make NO live/destructive change.

    For CI / containers / smoke where a real ``launchctl bootstrap`` (a per-user daemon mutation
    HOME can't redirect) is unwanted. Under dry-run: the managed plist still lands under the
    (HOME-isolated) path (so callers can assert the artifact), but the gui-domain (re)load AND the
    stale-predecessor teardown (its ``bootout`` AND the on-disk backup+remove of its plist) are
    BOTH skipped — dry-run never mutates the live launchd domain nor deletes the predecessor file,
    it only reports what it would do. Mirrors ``RIG_SCHEDULE_DRY_RUN`` (the schedule's seam).
    """
    return os.environ.get("RIG_TG_CTL_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _tg_ctl_config_dir_default() -> str:
    """The tg-cli config dir, honoring ``$TG_CTL_CONFIG_DIR`` (the daemon's own env), else the
    documented default ``~/.config/tg-cli``. The launchd logs land here, next to the daemon's
    .env/config.yaml, so everything tg-ctl is in one place."""
    return os.environ.get("TG_CTL_CONFIG_DIR", "").strip() or "~/.config/tg-cli"


def tg_ctl_plan_from_action(action: Action):
    """Rebuild the pure :class:`~riglib.tg_ctl.TgCtlPlan` an action describes.

    Shared by the install handler and the drift check so both agree on the exact desired plist
    from the action's options. ``Path.home()`` is the resolved HOME at apply time (a test
    monkeypatches it to a tmp HOME). The bun path is discovered at apply time unless pinned.
    Lazy import keeps the actions package import-light.
    """
    from ..tg_ctl import DEFAULT_BOOT_LABEL, DEFAULT_TG_CTL_PATH, build_tg_ctl

    opts = action.options
    config_dir = opts.get("config_dir") or _tg_ctl_config_dir_default()
    # `boot` is default-ON: a YAML `boot:` with no value (None) means "use the default" (True),
    # NOT False — `bool(None)` would silently disable it (codex P1). Only an explicit `False` is off.
    boot = opts.get("boot", True)
    return build_tg_ctl(
        home=Path.home(),
        boot=True if boot is None else bool(boot),
        boot_label=str(opts.get("label") or DEFAULT_BOOT_LABEL),
        bun_path=opts.get("bun_path") or None,
        tg_ctl_path=str(opts.get("tg_ctl_path") or DEFAULT_TG_CTL_PATH),
        config_dir=str(config_dir),
    )


def _tg_ctl_teardown_stale(plan, dry: bool) -> tuple[Path | None, str | None]:
    """Tear down the dead predecessor service (``com.ultra.codex-tg-bot``) if its plist exists:
    bootout + timestamped backup + remove. Returns (backup_path, detail) or (None, None) when
    there is nothing to tear down. Under ``dry`` it touches NOTHING — neither launchd nor disk —
    and returns a "would remove" detail (the dry-run contract: no live mutation, no disk write).
    """
    stale = plan.stale_plist_path
    if not stale.is_file():
        return None, None
    if dry:
        return None, (
            f"RIG_TG_CTL_DRY_RUN — would boot out + remove stale predecessor {stale.name}"
        )
    _launchctl_bootout(str(stale))  # harmless rc!=0 if it wasn't loaded
    bak = _timestamped_backup_path(stale.with_name(stale.name + ".rig-bak"))
    bak.write_text(stale.read_text(encoding="utf-8"), encoding="utf-8")
    stale.unlink()
    return bak, f"removed stale predecessor {stale.name} (booted out, backed up → {bak.name})"


# How long a single tool's install.sh may run before we give up (clone + dep install can be slow,
# but a hung script must never wedge `rig apply`). Default 300s; overridable via the env var.
_TOOL_INSTALL_TIMEOUT_DEFAULT_S = 300


def _tool_install_timeout_s() -> int:
    """The per-tool install.sh timeout, read at CALL time with a safe fallback.

    Read here, not at module top: a non-numeric ``RIG_TOOL_INSTALL_TIMEOUT_S`` must NOT crash the
    import of ``runner`` (which would wedge the WHOLE CLI, not just tools). A junk/absent value
    falls back to the 300s default; a positive int wins.
    """
    raw = os.environ.get("RIG_TOOL_INSTALL_TIMEOUT_S")
    if not raw:
        return _TOOL_INSTALL_TIMEOUT_DEFAULT_S
    try:
        val = int(raw)
    except ValueError:
        return _TOOL_INSTALL_TIMEOUT_DEFAULT_S
    return val if val > 0 else _TOOL_INSTALL_TIMEOUT_DEFAULT_S


def _do_provision_tools(action: Action, on_conflict: str) -> ActionResult:
    """Provision the personal CLI ecosystem by running each declared tool's OWN install.sh.

    For each :class:`ToolSpec` carried by the action: if the tool is already installed (bin resolves
    AND the skill blurb is advertised — :func:`tools.tool_status`) it is a no-op (``skipped``); else
    rig runs ``bash <repo>/install.sh``, which does the tool's own locate→symlink→install-skill
    dance. SAFE: rig never deletes a user's existing symlink; an already-working bin (even a Homebrew
    one) counts as resolved and is left alone — only an unadvertised tool is (re)installed to wire up
    the skill. A missing repo (no install.sh on disk) is an ``error`` for that tool, not a crash.

    Returns ``skipped`` when every tool was already current, ``created`` when at least one was
    installed, ``error`` if any tool's install failed or its repo was absent. ``RIG_TOOLS_DRY_RUN``
    reports what WOULD install without running any install.sh (used by the e2e suite).
    """
    from .. import tools as toolsmod

    plan = toolsmod.plan_from_action_options(action.options)
    if not plan.specs:
        return ActionResult(action, "skipped", "tools: no tools declared")

    dry = bool(os.environ.get("RIG_TOOLS_DRY_RUN"))
    installed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for spec in plan.specs:
        status, detail = _provision_one_tool(spec, dry)
        if status == "installed":
            installed.append(detail)
        elif status == "skipped":
            skipped.append(detail)
        else:
            errors.append(detail)

    return _tools_result(action, installed, skipped, errors)


def _provision_one_tool(spec: object, dry: bool) -> tuple[str, str]:
    """Install one tool if needed. Returns ``(status, detail)`` — status ∈ installed/skipped/error.

    ``spec`` is a :class:`tools.ToolSpec`. Already-installed (bin resolves + advertised) → skipped.
    Missing repo (no install.sh) → error. Otherwise run its install.sh (or, under dry-run, report).
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    st = toolsmod.tool_status(spec)
    if st.installed:
        return "skipped", f"{spec.name} already installed ({spec.managed_bin})"
    if not st.repo_present:
        return "error", f"{spec.name}: no install.sh at {spec.install_script} (repo missing?)"
    if dry:
        return "installed", f"{spec.name} (dry-run — would run {spec.install_script})"
    rc, out = _run_tool_install(spec)
    if rc != 0:
        tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
        return "error", f"{spec.name}: install.sh exited {rc} — {tail}"
    return "installed", f"{spec.name} installed via {spec.install_script.name}"


def _run_tool_install(spec: object) -> tuple[int, str]:
    """Run ``bash <repo>/install.sh`` for one tool. Returns ``(returncode, combined_output)``.

    The tool's install.sh is self-locating (it sees its own BASH_SOURCE → uses the local clone) and
    self-idempotent (re-symlink, re-run install-skill). We just invoke it with a timeout so a hung
    script can't wedge apply; the bin dir + skill dir it writes are HOME-anchored by the script.
    """
    from .. import tools as toolsmod

    assert isinstance(spec, toolsmod.ToolSpec)
    timeout_s = _tool_install_timeout_s()
    try:
        proc = subprocess.run(
            ["bash", str(spec.install_script)],
            cwd=str(spec.repo),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"install.sh timed out after {timeout_s}s"
    except OSError as exc:
        return 1, f"could not run install.sh: {exc}"


def _tools_result(
    action: Action, installed: list[str], skipped: list[str], errors: list[str]
) -> ActionResult:
    """Fold per-tool outcomes into one ActionResult (error if any failed, else created/skipped)."""
    parts: list[str] = []
    if installed:
        parts.append(f"installed: {', '.join(installed)}")
    if skipped:
        parts.append(f"already current: {', '.join(skipped)}")
    if errors:
        parts.append(f"FAILED: {'; '.join(errors)}")
    detail = "tools: " + ("; ".join(parts) if parts else "nothing to do")
    if errors:
        return ActionResult(action, "error", detail)
    if installed:
        return ActionResult(action, "created", detail)
    return ActionResult(action, "skipped", detail)


def _do_provision_tg_ctl(action: Action, on_conflict: str) -> ActionResult:
    """Provision the tg-ctl inbound daemon as a macOS LaunchAgent (idempotent).

    Mirrors the tmux-boot provisioning shape (render -> backup-differing -> write), but unlike
    tmux this agent IS (re)loaded into launchd so a clean ``rig init`` starts it without a reboot:

      - render ``~/Library/LaunchAgents/ai.hyperide.tg-ctl.plist`` (byte-exact to the working
        hand-created file — see riglib.tg_ctl.render_plist's sort_keys note);
      - back up any existing DIFFERING plist (on_conflict=backup) before rewrite;
      - ensure the log dir (the tg-cli config dir) exists so launchd can open the logs;
      - tear down the stale predecessor (``com.ultra.codex-tg-bot``): ``bootout`` + timestamped
        backup + remove its plist, if present;
      - (re)load via ``launchctl bootout``/``bootstrap`` in the per-user gui domain.

    macOS-only (launchd). Off darwin it is a ``skipped`` no-op (no Linux equivalent yet).
    A re-apply that finds the plist byte-identical AND the agent loaded is a ``skipped`` no-op —
    the idempotency contract that keeps a re-apply against the live plist from rewriting it.
    ``RIG_TG_CTL_DRY_RUN`` writes the plist but skips every live launchd mutation.
    """
    plan = tg_ctl_plan_from_action(action)

    if sys.platform != "darwin":
        return ActionResult(
            action, "skipped",
            "tg_ctl/boot: skipped (launchd is macOS-only; no Linux equivalent yet)",
        )

    details: list[str] = []
    changed = False
    headline_backup: Path | None = None
    dry = _tg_ctl_dry_run()

    # 0) tear down the dead predecessor service first (bootout + backup + remove its plist).
    # Under dry-run this is a pure no-op that only REPORTS what it would do (no disk mutation).
    stale_backup, stale_detail = _tg_ctl_teardown_stale(plan, dry)
    if stale_detail:
        details.append(stale_detail)
    if stale_backup is not None:  # a real removal happened (never under dry-run)
        headline_backup = stale_backup
        changed = True

    if not plan.boot_enabled:
        # boot disabled: don't write/load the agent. A leftover plist is surfaced by drift as an
        # EXTRA (it still auto-starts the daemon), but apply never deletes the user's own file.
        if not changed:
            return ActionResult(action, "skipped", "tg_ctl/boot: disabled (tg_ctl.boot=false) — nothing to provision")
        return ActionResult(action, "backed_up" if headline_backup else "created",
                            f"tg_ctl/boot: {'; '.join(details)}", headline_backup)

    # 1) ensure the log dir (tg-cli config dir) exists so launchd can open StandardOut/ErrorPath.
    plan.config_dir.mkdir(parents=True, exist_ok=True)

    # 2) write the plist (idempotent on identical bytes; honors on_conflict for a differing one).
    plan.plist_path.parent.mkdir(parents=True, exist_ok=True)
    desired = plan.render_plist()
    already = plan.plist_path.is_file()
    current = plan.plist_path.read_text(encoding="utf-8") if already else ""

    # The install-if-missing no-op: present AND byte-identical AND already loaded → nothing to do.
    # (Skip the loaded check under dry-run — it would query the real launchd domain.)
    if already and current == desired and not changed and (dry or _launchctl_gui_loaded(plan.boot_label)):
        return ActionResult(
            action, "skipped",
            f"tg_ctl/boot: launchd agent '{plan.boot_label}' already installed and loaded",
        )

    out = fsutil.write_file(plan.plist_path, desired, on_conflict)
    if out.status == "error":
        return ActionResult(action, "error", f"tg_ctl/boot: {out.detail}", out.backup)
    if out.backup:
        headline_backup = headline_backup or out.backup
        details.append(f"backed up prior plist → {out.backup.name}")
    # Conflict-skip: the existing plist DIFFERS but on_conflict=skip said don't write it. The
    # desired agent never hit disk, so we must NOT (re)load the stale plist and must NOT report a
    # change — surface the unresolved drift instead (mirrors the schedule conflict-skip path).
    if out.status == "skipped" and already and current != desired:
        if changed:  # the stale-predecessor removal still happened — report it, plus the skip.
            details.append(
                f"{plan.plist_path.name} differs but on_conflict=skip — left unchanged "
                f"(drift NOT reconciled; re-run with on_conflict=backup/overwrite)"
            )
            return ActionResult(action, "backed_up" if headline_backup else "created",
                                f"tg_ctl/boot: {'; '.join(details)}", headline_backup)
        return ActionResult(
            action, "skipped",
            f"tg_ctl/boot: launchd plist {plan.plist_path} differs but on_conflict=skip — "
            f"left unchanged (drift NOT reconciled; re-run with on_conflict=backup/overwrite)",
        )
    if out.status != "skipped":
        changed = True
        details.append(f"wrote {plan.plist_path.name}")

    if dry:
        return ActionResult(
            action,
            "backed_up" if headline_backup else ("created" if changed else "skipped"),
            f"tg_ctl/boot: {'; '.join(details) or 'plist already current'} "
            f"(RIG_TG_CTL_DRY_RUN — skipped launchctl bootstrap)",
            headline_backup,
        )

    # 3) (re)load via the per-user gui domain so a changed plist is picked up without a reboot.
    # bootout first is safe — it is a harmless no-op when the label isn't loaded. We (re)load
    # whenever the plist was (re)written OR the agent isn't currently loaded.
    needs_load = changed or not _launchctl_gui_loaded(plan.boot_label)
    if needs_load:
        _launchctl_bootout(str(plan.plist_path))
        rc = _launchctl_bootstrap(str(plan.plist_path))
        if rc != 0:
            return ActionResult(
                action, "error",
                f"tg_ctl/boot: wrote {plan.plist_path} but `launchctl bootstrap` failed (rc={rc})",
                headline_backup,
            )
        if not changed:  # plist byte-identical but the agent wasn't loaded → loading IS the change.
            changed = True
            details.append(f"loaded launchd agent '{plan.boot_label}'")
        else:
            details.append(f"(re)loaded launchd agent '{plan.boot_label}'")

    if not changed:
        return ActionResult(action, "skipped",
                            f"tg_ctl/boot: launchd agent '{plan.boot_label}' already installed and loaded")
    status = "backed_up" if headline_backup else "created"
    return ActionResult(action, status, f"tg_ctl/boot: {'; '.join(details)}", headline_backup)


# ── AGENTS.md / CLAUDE.md canonical + symlink ─────────────────────────────────────
# One file is the real source of truth; the other is a relative symlink to it, so every
# agent harness (Claude Code reads CLAUDE.md; Codex/others read AGENTS.md) sees identical
# guidance. apply and drift BOTH go through `resolve_agents_md` so they can never disagree
# on canonical direction or on what counts as in-sync.
_AGENTS_MD_PLACEHOLDER = (
    "# Agent guide\n\n"
    "Repository instructions for coding agents (Claude Code, Codex, etc.). This is the\n"
    "canonical file; the other agent-guide filename is a symlink to it, so every agent\n"
    "reads the same guide.\n\n"
    "Replace this placeholder with the conventions, commands, and guardrails for this repo.\n"
)


def agents_md_paths(repo_root: Path) -> tuple[Path, Path]:
    """The (AGENTS.md, CLAUDE.md) pair at a repo root."""
    return repo_root / "AGENTS.md", repo_root / "CLAUDE.md"


def _is_real_file(p: Path) -> bool:
    """True for a regular file that is NOT a symlink (a real source of truth, not a link)."""
    return p.is_file() and not p.is_symlink()


def _is_broken_symlink(p: Path) -> bool:
    """True for a dangling symlink — a link whose target no longer resolves to anything.

    The spec's "broken link": a pair rig provisioned (real canonical + symlink) where the
    canonical was later deleted, leaving the link dangling. rig surfaces this by NAME (a broken
    symlink, with how to recover) rather than as a generic "not a real file", but it never
    auto-recreates the missing canonical — an empty placeholder would silently mask the loss of
    the (possibly curated) content that was deleted, turning a visible, git-recoverable failure
    into an invisible one. ``status`` flags it; the human restores the canonical or removes the
    link.
    """
    return p.is_symlink() and not p.exists()


def symlink_points_to(link: Path, canonical_name: str) -> bool:
    """True when ``link`` is a symlink already resolving to ``canonical_name`` (same dir).

    Accepts either a relative stored target (just the filename) or an absolute one that
    resolves to the same path, so a correct link written either way is a no-op. Shared by the
    install action and the drift check so both agree on "already correct".
    """
    if not link.is_symlink():
        return False
    current = link.readlink()
    if str(current) == canonical_name:
        return True
    try:
        return (link.parent / current).resolve() == (link.parent / canonical_name).resolve()
    except (OSError, RuntimeError):
        # OSError: a broken/unreadable path. RuntimeError: pathlib raises it for a symlink loop
        # (e.g. AGENTS.md → AGENTS.md). Either way the link does not resolve to the canonical.
        return False


@dataclass(frozen=True)
class AgentsMdResolution:
    """The desired AGENTS.md/CLAUDE.md outcome for a repo — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on, so they can never disagree
    about what is in sync:

    - ``ok``          — already a real canonical + a correct symlink: no-op.
    - ``create_both`` — both slots empty: write ``canonical`` placeholder + symlink the other.
    - ``create_link`` — one real canonical, the other empty: symlink the other → canonical.
    - ``converge``    — both real & identical: collapse ``link`` to a symlink (honors on_conflict).
    - ``conflict``    — anything ambiguous/unsafe (both real & different, a real file at one slot
                        with a foreign symlink/dir at the other, peer-link loops, neither-real
                        with a stray symlink/dir): rig NEVER mutates these; ``detail`` says why.

    ``canonical`` is the real source-of-truth filename; ``link`` is the slot that should be a
    symlink → canonical (for ``converge`` that is ``CLAUDE.md``).
    """

    agents: Path
    claude: Path
    state: str
    canonical: str
    canonical_path: Path
    link: Path
    detail: str = ""


def resolve_agents_md(repo_root: Path) -> AgentsMdResolution:
    """Classify the on-disk AGENTS.md/CLAUDE.md state into one desired ``state`` (pure, no writes).

    Safety-first: rig only ever (a) creates into an EMPTY slot, (b) collapses two identical
    REAL files to a symlink, or (c) no-ops a correct pair. Every other shape — a foreign
    symlink, a directory, divergent real files, a peer-link loop — is a ``conflict`` rig leaves
    untouched and surfaces, so it can never clobber a real file or a user-placed symlink.
    """
    agents, claude = agents_md_paths(repo_root)
    agents_real = _is_real_file(agents)
    claude_real = _is_real_file(claude)

    def _r(state, canonical, canonical_path, link, detail=""):
        return AgentsMdResolution(agents, claude, state, canonical, canonical_path, link, detail)

    if agents_real and claude_real:
        try:
            identical = agents.read_bytes() == claude.read_bytes()
        except OSError as exc:
            return _r("conflict", "AGENTS.md", agents, claude,
                      detail=f"cannot read AGENTS.md/CLAUDE.md to compare: {exc}")
        if identical:
            return _r("converge", "AGENTS.md", agents, claude)  # link = CLAUDE.md
        return _r("conflict", "AGENTS.md", agents, claude,
                  detail="AGENTS.md and CLAUDE.md are both real files with different content "
                         "— reconcile into one (keep the canonical), then re-run")

    if agents_real or claude_real:
        if agents_real:
            canonical, canonical_path, link = "AGENTS.md", agents, claude
        else:
            canonical, canonical_path, link = "CLAUDE.md", claude, agents
        if link.is_symlink():
            if symlink_points_to(link, canonical):
                return _r("ok", canonical, canonical_path, link)
            return _r("conflict", canonical, canonical_path, link,
                      detail=f"{link.name} is a symlink to something other than {canonical} "
                             f"— left untouched; remove it to let rig manage the pair")
        if link.exists():  # not real (canonical is the only real file) and not a symlink → a dir
            return _r("conflict", canonical, canonical_path, link,
                      detail=f"{link.name} exists but is not a regular file — left untouched")
        return _r("create_link", canonical, canonical_path, link)

    # neither slot is a real file
    agents_present = agents.is_symlink() or agents.exists()
    claude_present = claude.is_symlink() or claude.exists()
    if not agents_present and not claude_present:
        return _r("create_both", "AGENTS.md", agents, claude)

    # The spec's headline "broken link": EXACTLY one slot is a rig-shaped dangling link — a symlink
    # whose target is the OTHER managed name, and that name is now missing — while the other slot
    # is empty. This is precisely "rig provisioned the pair, then the real canonical was deleted".
    # Name it with recovery guidance, but NEVER recreate the canonical: an empty placeholder would
    # silently mask the loss of possibly-curated content (a visible, git-recoverable failure turned
    # invisible). The narrow shape deliberately EXCLUDES a peer-link loop (the other slot is also a
    # symlink, not empty → both "broken") and a foreign/competing occupant in the other slot, which
    # would make the "canonical was deleted" narrative wrong; those fall through below.
    for link, other, canonical in ((claude, agents, "AGENTS.md"), (agents, claude, "CLAUDE.md")):
        if (
            _is_broken_symlink(link)
            and symlink_points_to(link, canonical)
            and not (other.is_symlink() or other.exists())
        ):
            return _r("conflict", "AGENTS.md", agents, claude,
                      detail=f"{link.name} → {canonical} (missing) is a broken symlink — its "
                             f"canonical target {canonical} does not exist (deleted out from "
                             "under a provisioned pair, or never created). Restore it from "
                             "version control (e.g. git checkout) or remove the dangling link, "
                             "then re-run; rig will not recreate it, to avoid masking lost content")

    # Any other "neither real" shape that still contains a dangling link (a foreign/unresolved
    # target, a peer-link loop, or a dangling link beside a competing occupant): name it as a
    # broken link so status says what's wrong, WITHOUT the (here-inaccurate) "canonical deleted"
    # narrative or git-restore advice.
    dangling = [p.name for p in (agents, claude) if _is_broken_symlink(p)]
    if dangling:
        joined = " and ".join(dangling)
        # plural/singular; the advice deliberately does NOT presume the target is absent — for a
        # peer loop (A→B, B→A) each "target" exists as the other link, so "restore the target"
        # would be incoherent. The universal fix is one real file plus a link to it.
        subject = (
            "are broken/dangling symlinks (their targets do not resolve)"
            if len(dangling) > 1 else "is a broken/dangling symlink (its target does not resolve)"
        )
        return _r("conflict", "AGENTS.md", agents, claude,
                  detail=f"{joined} {subject} — reconcile the pair to one real file plus a symlink "
                         "to it (remove the dangling link(s); create or restore the canonical), "
                         "then re-run")
    return _r("conflict", "AGENTS.md", agents, claude,
              detail="AGENTS.md/CLAUDE.md present but neither is a real file (a symlink or "
                     "directory occupies a slot) — reconcile to one real file, then re-run")


def _do_provision_agents_symlink(action: Action, on_conflict: str) -> ActionResult:
    """Provision the AGENTS.md (canonical, real) + CLAUDE.md (symlink) invariant in a repo.

    Switches on the shared :func:`resolve_agents_md` ``state`` — apply and drift read the same
    classification, so ``status`` never misreports the on-disk state. Only ``create_*`` and
    ``converge`` mutate disk (``converge`` only when ``on_conflict`` is not ``skip``); ``ok``
    and ``conflict`` never touch a file. As elsewhere in rig, ``on_conflict`` decides whether
    apply *reconciles* a reported drift, not whether status reports it.
    """
    r = resolve_agents_md(action.target)

    if r.state == "ok":
        return ActionResult(action, "skipped", f"agents-md: {r.link.name} already links → {r.canonical}")

    if r.state == "conflict":
        return ActionResult(action, "skipped", f"agents-md: {r.detail}")

    if r.state == "create_link":
        r.link.symlink_to(r.canonical)
        return ActionResult(action, "created", f"agents-md: {r.link.name} → {r.canonical}")

    if r.state == "create_both":
        r.canonical_path.write_text(_AGENTS_MD_PLACEHOLDER, encoding="utf-8")
        r.link.symlink_to(r.canonical)
        return ActionResult(
            action, "created",
            f"agents-md: created {r.canonical_path.name} (canonical) + {r.link.name} → {r.canonical}",
        )

    if r.state == "converge":
        if on_conflict == "skip":
            return ActionResult(
                action, "skipped",
                "agents-md: AGENTS.md and CLAUDE.md are identical real files; on_conflict=skip "
                "— left as two real files (set on_conflict=backup/overwrite to converge to a symlink)",
            )
        backup = None
        if on_conflict == "backup":
            backup = fsutil.backup_path(r.link)
            shutil.copy2(r.link, backup)
        r.link.unlink()
        r.link.symlink_to(r.canonical)
        suffix = f"; backed up → {backup.name}" if backup else ""
        # match the handler status contract: a replacement that kept a backup is "backed_up"
        # (CLI summaries/icons key off it), a clean overwrite is "updated".
        return ActionResult(
            action, "backed_up" if backup else "updated",
            f"agents-md: converged {r.link.name} → {r.canonical} (identical content{suffix})",
            backup,
        )

    return ActionResult(action, "error", f"agents-md: unhandled state {r.state!r}")


# ── per-repo `gh ship` delegator (.claude/scripts/pr-ship.sh) ──────────────────────
# `gh ship` is a gh alias → `<repo>/.claude/scripts/pr-ship.sh`. Historically that delegator
# existed ONLY in agent-tools, so `gh ship` FAILED in every other managed repo (papered over by a
# runtime alias fallback). rig provisions the delegator into EVERY managed repo so `gh ship` works
# on a clean machine, and ignores it via the repo's `.git/info/exclude` (a per-repo, never-committed
# git exclude) so the provisioned file does not dirty the worktree — ship refuses a dirty tree, so
# an un-ignored provisioned file would break the very command it enables. apply + drift both go
# through `resolve_ship_delegator`, so they can never disagree on whether the repo is in sync.


def ship_delegator_content(canonical_ship: Path) -> str:
    """The exact bytes of the provisioned ``.claude/scripts/pr-ship.sh`` for a canonical ship.sh.

    A thin delegator: the global ``gh ship`` alias runs ``<repo>/.claude/scripts/pr-ship.sh``; this
    script execs the canonical generalized ship implementation. It prefers a REPO-LOCAL
    ``ci/ship/ship.sh`` (so agent-tools — which carries the real ship.sh — self-hosts and always
    runs its own checked-out version) and otherwise execs the rig-baked ``canonical_ship`` (the
    ``ci/ship/ship.sh`` in the agent-tools checkout rig applied from), so ``gh ship`` works in every
    other repo on a clean machine. Deterministic for a given ``canonical_ship`` so a re-apply / drift
    compare is a byte-for-byte no-op; ``canonical_ship`` is the un-resolved configured/expanded path
    rig stored at plan time.

    SECURITY: ``canonical_ship`` derives from the user-controlled ``agent_tools_source`` config, so
    it is shell-quoted with :func:`shlex.quote` before being baked in. Without that, a path
    containing ``"``, ``$()``, backticks, or a space would either break the generated script or
    EXECUTE arbitrary commands every time ``gh ship`` runs (e.g. ``agent_tools_source:
    /tmp/$(curl evil)/agent-tools``). Single-quoting renders every metacharacter inert.
    """
    canon = shlex.quote(str(canonical_ship))
    return (
        "#!/usr/bin/env bash\n"
        "# Provisioned by rig (ship_delegator). The global `gh ship` alias runs\n"
        "# <repo>/.claude/scripts/pr-ship.sh. agent-tools' canonical, generalized ship\n"
        "# implementation lives at ci/ship/ship.sh — delegate to it so `gh ship` works in this\n"
        "# repo with the same green-CI-gated merge + cleanup as everywhere else. Repo-local\n"
        "# ci/ship/ship.sh wins (agent-tools self-hosts); otherwise the rig-baked canonical path.\n"
        "set -euo pipefail\n"
        # `git rev-parse` EXITS NON-ZERO outside a git repo; under `set -e` a failing command
        # substitution in this assignment would abort the whole script (exit 128) before we ever
        # reach the canonical fallback. Guard it: run git separately with `|| true` so a non-git cwd
        # (or no git at all) just leaves toplevel empty and falls through to the baked path.
        'toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"\n'
        'repo_local="${toplevel:+$toplevel/ci/ship/ship.sh}"\n'
        'if [[ -n "$repo_local" && -f "$repo_local" ]]; then\n'
        '  exec "$repo_local" "$@"\n'
        "fi\n"
        # canonical is shlex.quote'd above — the value is user-derived (agent_tools_source).
        f"canonical={canon}\n"
        'if [[ -f "$canonical" ]]; then\n'
        '  exec "$canonical" "$@"\n'
        "fi\n"
        'echo "pr-ship.sh: canonical ship.sh not found (repo-local $repo_local nor $canonical)." >&2\n'
        'echo "Re-run \'rig apply\' against a current agent-tools checkout to refresh it." >&2\n'
        "exit 127\n"
    )


def ship_delegator_exclude_block_text() -> str:
    """The exact marker-delimited block rig owns in a repo's ``.git/info/exclude``.

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte on
    what the managed entry SHOULD be: the begin marker, a fixed explanatory comment, the one ignored
    path (the provisioned delegator), then the end marker. Rendered byte-for-byte so a re-apply is a
    true zero-churn no-op.
    """
    return "\n".join(
        [
            SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER,
            SHIP_DELEGATOR_EXCLUDE_COMMENT,
            f"/{SHIP_DELEGATOR_REL_PATH}",
            SHIP_DELEGATOR_EXCLUDE_END_MARKER,
        ]
    )


def repo_info_exclude_path(repo_root: Path) -> Path | None:
    """Resolve the repo's git exclude file (``info/exclude``), worktree-aware.

    In a plain repo this is ``<repo>/.git/info/exclude``. In a git WORKTREE, ``<repo>/.git`` is a
    FILE pointing at that worktree's private gitdir (``<common>/.git/worktrees/<name>``), and
    ``info/exclude`` is PER-WORKTREE — it lives in that private gitdir, NOT in the common dir (only
    things like ``HEAD``/objects are shared). A naive ``<repo>/.git/info/exclude`` would therefore
    fail in a worktree (``.git`` is a file, not a dir). We ask git for the canonical path
    (``git -C <repo> rev-parse --git-path info/exclude``), which returns the correct per-layout file
    — so ignoring the delegator in a worktree affects ONLY that worktree, exactly right. Returns
    ``None`` when the path is not a git repo (git errors) so the caller treats "no git" as
    nothing-to-ignore, never a crash.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    raw = res.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    # `--git-path` yields a path relative to the repo root (e.g. `.git/info/exclude`); anchor it.
    return p if p.is_absolute() else (repo_root / p)


@dataclass(frozen=True)
class ShipDelegatorResolution:
    """The desired ship-delegator outcome for a repo — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the delegator file is present and exactly correct AND the ``.git/info/exclude``
                       entry is present: no-op.
      - ``create``   — the delegator file is absent: write it (and add the exclude entry).
      - ``update``   — the delegator file exists but its bytes differ, OR the file is correct but the
                       exclude entry is missing: rewrite the file and/or add the entry.
      - ``io_error`` — the delegator path could not be inspected (a directory sits there, unreadable):
                       apply reports an ERROR, never a silent skip. ``detail`` carries why.

    ``content`` is the canonical delegator bytes; ``exclude_path`` is the resolved per-repo exclude
    file (``None`` when the repo is not a git repo — then there is nothing to ignore and the file
    write alone reconciles); ``exclude_ok`` records whether the exclude entry is already present;
    ``file_correct`` records whether the delegator FILE already matches ``content`` (so apply can
    skip a needless rewrite-with-backup when only the exclude is missing, and drift can tell the
    file-edited case from the missing-ignore case without re-reading the file).
    """

    delegator_path: Path
    content: str
    state: str
    exclude_path: Path | None
    exclude_ok: bool
    file_correct: bool = False
    detail: str = ""


def _exclude_has_entry(exclude_path: Path | None) -> bool:
    """True when the repo's ``.git/info/exclude`` carries EXACTLY ONE correct rig ship-delegator block.

    A repo with no git (no exclude path) → True (nothing to ignore, so "not missing"). An absent or
    unreadable exclude file → False. Otherwise this returns True ONLY when the file holds exactly one
    well-formed managed block whose body equals :func:`ship_delegator_exclude_block_text` — so all of
    these report as NOT-ok (drift, which ``_reconcile_ship_exclude`` then repairs): no block, an
    UNBALANCED pair (a begin with no end — reconcile refuses it, so it must not read "ok"), and
    DUPLICATED blocks (a prior non-idempotent edit — reconcile collapses them). Mirrors the global-
    excludes "ok" semantics so the two managed-block reconcilers agree on what "in sync" means.
    """
    if exclude_path is None:
        return True
    if not exclude_path.is_file():
        return False
    try:
        # newline="" — read RAW (no CRLF→LF translation), so this byte-compare matches
        # _reconcile_ship_exclude's raw read EXACTLY. Reading with translation here (read_text's
        # default) would normalize a CRLF file's \r away and report "ok" while the reconcile, seeing
        # the raw \r, rewrites the block — the two would disagree on "in sync" on a CRLF exclude file.
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError:
        return False
    begins = _find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER)
    ends = _find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_END_MARKER)
    if len(begins) != 1 or len(ends) != 1:
        return False  # absent, unbalanced, or duplicated → not in sync (reconcile fixes it)
    # exactly one balanced pair: it is "ok" only when its body matches the canonical block byte-for-
    # byte. CONTRACT: `_find_marker_lines` returns (line_start_offset, end_of_line_offset) where the
    # end offset is the newline terminating the marker line (or len(content)) — so content[b_start:
    # e_end] is exactly the block text WITHOUT a trailing newline, matching ship_delegator_exclude_
    # block_text(). `_reconcile_ship_exclude` slices identically, so the two stay in lockstep; if that
    # offset semantics ever changes, BOTH must update together (a SYNC pair).
    b_start, _b_end = begins[0]
    e_start, e_end = ends[0]
    if e_start < b_start:  # end before begin → misordered, reconcile by hand
        return False
    return content[b_start:e_end] == ship_delegator_exclude_block_text()


def resolve_ship_delegator(repo_root: Path, canonical_ship: Path) -> ShipDelegatorResolution:
    """Classify the on-disk ship-delegator state vs desired (pure, no writes).

    Reads the delegator file + the repo's ``.git/info/exclude`` and returns the one ``state`` apply
    and drift both switch on, so ``status`` never misreports the on-disk state. Idempotent: a correct
    file + a present exclude entry is ``ok``; an absent file is ``create``; a drifted file OR a
    correct file with a missing exclude entry is ``update``; a directory/unreadable path is
    ``io_error``.
    """
    content = ship_delegator_content(canonical_ship)
    deleg = repo_root / SHIP_DELEGATOR_REL_PATH
    exclude_path = repo_info_exclude_path(repo_root)
    exclude_ok = _exclude_has_entry(exclude_path)

    if not deleg.exists():
        return ShipDelegatorResolution(deleg, content, "create", exclude_path, exclude_ok)
    if not deleg.is_file():
        return ShipDelegatorResolution(
            deleg, content, "io_error", exclude_path, exclude_ok,
            detail=f"{deleg} is not a regular file",
        )
    try:
        on_disk = deleg.read_text(encoding="utf-8")
    except OSError as exc:
        return ShipDelegatorResolution(
            deleg, content, "io_error", exclude_path, exclude_ok, detail=f"cannot read {deleg}: {exc}"
        )
    file_correct = on_disk == content
    if file_correct and exclude_ok:
        return ShipDelegatorResolution(deleg, content, "ok", exclude_path, exclude_ok, file_correct=True)
    return ShipDelegatorResolution(deleg, content, "update", exclude_path, exclude_ok, file_correct=file_correct)


def _reconcile_ship_exclude(exclude_path: Path | None) -> tuple[bool, str]:
    """Reconcile rig's managed ship-delegator block in ``.git/info/exclude``. Returns ``(ok, note)``.

    Reuses the same marker-block reconcile as the global excludes (``resolve_global_excludes``) so
    the per-repo entry collapses duplicates and preserves every other line verbatim. A repo with no
    git (no exclude path) is a no-op (nothing to ignore → ``ok``).

    ``ok`` is FALSE — and the caller surfaces it as an ERROR, not a misleading "created" — for any
    state where the ignore could NOT be established: an unreadable/unwritable exclude file (OSError),
    or an unbalanced/misordered marker pair rig refuses to rewrite. This matters because the whole
    point of the ignore is to keep the worktree clean (ship refuses a dirty tree); a delegator
    written-but-not-ignored is the exact failure mode this feature prevents, so it must never be
    reported as success.
    """
    if exclude_path is None:
        return True, "no git repo — delegator not ignored (nothing to reconcile)"
    # resolve_global_excludes fences entries with the GLOBAL markers; the ship delegator wants its
    # OWN marker text, so we render the desired block ourselves and splice it via the SAME offset
    # machinery (_find_marker_lines) to keep one collapse/preserve code path.
    desired = ship_delegator_exclude_block_text()
    if not exclude_path.is_file():
        # is_file (not exists): a directory at info/exclude must NOT take the fresh-write branch (it
        # would IsADirectoryError on the read below with a misleading message) — let the open() below
        # raise and surface as a clear "could not read".
        if not exclude_path.exists():
            try:
                exclude_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_exclude(exclude_path, desired + "\n")
            except OSError as exc:
                return False, f"could not write {exclude_path}: {exc}"
            return True, f"ignored in {exclude_path}"
    try:
        with exclude_path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"could not read {exclude_path}: {exc}"
    begins = _find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_BEGIN_MARKER)
    ends = _find_marker_lines(content, SHIP_DELEGATOR_EXCLUDE_END_MARKER)
    if len(begins) != len(ends):
        return False, f"{exclude_path} has unbalanced rig ship-delegator markers — reconcile by hand"
    if not begins:
        # Preserve the existing file VERBATIM (including a whitespace-only file's blank lines) and
        # append the block. Ensure exactly one trailing newline before the block so we neither glue
        # the block onto a non-newline-terminated last line nor accrete extra blanks; an empty file
        # gets just the block. Mirrors the global reconcile but never discards pre-existing content.
        if not content:
            new_content = f"{desired}\n"
        else:
            lead = content if content.endswith("\n") else content + "\n"
            new_content = f"{lead}{desired}\n"
    else:
        # collapse every managed region to ONE correct block in place (mirrors the global reconcile).
        markers = sorted(
            [(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends]
        )
        pairs: list[tuple[int, int]] = []
        expect = "begin"
        pending = -1
        for start, line_end, kind in markers:
            if kind != expect:
                return False, f"{exclude_path} has misordered rig ship-delegator markers — reconcile by hand"
            if kind == "begin":
                pending = start
                expect = "end"
            else:
                pairs.append((pending, line_end))
                expect = "begin"
        if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
            return True, f"already ignored in {exclude_path}"
        out: list[str] = []
        cursor = 0
        for idx, (r_start, r_end) in enumerate(pairs):
            out.append(content[cursor:r_start])
            if idx == 0:
                out.append(desired)
            elif content[r_end : r_end + 1] == "\n":
                r_end += 1
            cursor = r_end
        out.append(content[cursor:])
        new_content = "".join(out)
    try:
        _atomic_write_exclude(exclude_path, new_content)
    except OSError as exc:
        return False, f"could not write {exclude_path}: {exc}"
    return True, f"ignored in {exclude_path}"


def _atomic_write_exclude(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` ATOMICALLY (tmp file in the same dir + ``os.replace``).

    ``.git/info/exclude`` may hold the USER's own hand-added ignore lines. A plain truncate-rewrite
    that is interrupted (SIGINT, disk-full, OSError mid-write) would leave that content destroyed
    with no backup. Writing a sibling temp file and renaming it over the target makes the swap atomic
    on POSIX (and best-effort on Windows): either the old file or the fully-written new one is present
    — never a truncated half. ``newline=""`` writes LF verbatim (no CRLF translation), matching the
    raw read in ``_exclude_has_entry`` so a re-apply is a byte-identical no-op.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".rig-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _do_provision_ship_delegator(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile the per-repo ``.claude/scripts/pr-ship.sh`` delegator + its git-exclude.

    Two coupled steps, both idempotent:
      1. Write ``<repo>/.claude/scripts/pr-ship.sh`` ONLY when its bytes differ from the desired
         delegator (so a correct file is a true no-op and we never create a spurious ``.rig-bak``
         backup just because the EXCLUDE entry was missing). Honors ``on_conflict`` via
         :func:`fsutil.write_file` (a hand-edited delegator is backed up before overwrite) and sets
         the exec bit.
      2. ALWAYS reconcile rig's managed block in the repo's ``.git/info/exclude`` so the provisioned
         file does not dirty the worktree (ship refuses a dirty tree). The exclude is rig-OWNED (its
         own marker block), not user content, so it is reconciled regardless of ``on_conflict`` — a
         ``skip`` that leaves a hand-edited delegator file still gets the file ignored, so status
         won't re-flag a non-ignored delegator with no path forward. A repo with no git skips step 2
         (nothing to ignore).

    apply and drift switch on the SAME :func:`resolve_ship_delegator` ``state`` so status never
    misreports. ``io_error`` (a directory/unreadable at the delegator path) is an error, never a
    silent skip.
    """
    # Guard a missing/empty canonical_ship: a plan-builder regression that dropped the option would
    # otherwise render `canonical=` and write a silently-broken delegator (every `gh ship` → exit
    # 127 with no rig-side signal). Fail loudly instead. (The plan builder also fail-closes on a
    # checkout without ci/ship/ship.sh, so this is defense-in-depth.)
    raw_canonical = str(action.options.get("canonical_ship", "")).strip()
    if not raw_canonical:
        return ActionResult(action, "error", "ship-delegator: no canonical_ship in action options")
    canonical = Path(raw_canonical)
    r = resolve_ship_delegator(action.target, canonical)

    if r.state == "io_error":
        return ActionResult(action, "error", f"ship-delegator: {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"ship-delegator: {r.delegator_path.name} already correct + ignored")

    # Step 1 — write the file only when its bytes are wrong/absent (no needless backup when only the
    # exclude was missing). `out` is initialized so the status read below never hits an unbound name
    # if this branch logic is later edited (the ternary already guards it, this is belt-and-braces).
    out: fsutil.WriteOutcome | None = None
    if r.file_correct:
        file_note = f"{r.delegator_path.name} already correct"
        backup = None
        skipped_user_file = False
    else:
        out = fsutil.write_file(r.delegator_path, r.content, on_conflict)
        _chmod_x_if_changed(r.delegator_path, out)
        file_note = out.detail
        backup = out.backup
        # on_conflict=skip on a DIFFERENT existing file: write_file skips without writing. We still
        # reconcile the exclude (below) so a left-as-is delegator is at least ignored.
        skipped_user_file = out.status == "skipped" and "on_conflict=skip" in out.detail

    # Step 2 — always reconcile the git-exclude (rig-owned block). A FAILURE here (unreadable/
    # unwritable exclude, or a marker pair rig won't rewrite) means the just-written delegator is NOT
    # ignored → the worktree is now dirty, the exact thing this feature prevents. Surface it as an
    # error, not a misleading "created" (the delegator is on disk, but the job is not done).
    exclude_ok, exclude_note = _reconcile_ship_exclude(r.exclude_path)
    if not exclude_ok:
        return ActionResult(
            action, "error",
            f"ship-delegator: {file_note}; {exclude_note} — delegator written but NOT git-ignored "
            "(worktree will be dirty; ship refuses a dirty tree)",
            backup,
        )

    if skipped_user_file:
        return ActionResult(action, "skipped", f"ship-delegator: {file_note}; {exclude_note}", backup)
    # status: a changed file OR a freshly-added exclude entry is a change. file_correct→exclude-only
    # change reports "updated" (the exclude was added); a written file reports its own write status.
    status = out.status if out is not None else "updated"
    return ActionResult(action, status, f"ship-delegator: {file_note}; {exclude_note}", backup)


# ── linter / formatter config files (the `linters` block) ──────────────────────────
# rig provisions per-repo linter+formatter config files (CTO decision #4136.2). Each `linters.items`
# entry names a tool + a repo-relative path + the exact file content; apply writes/reconciles it,
# drift byte-compares it. UNLIKE the ship delegator, a linter config is a COMMITTED file (not
# git-ignored), so there is no .git/info/exclude leg — just an idempotent file write that honors
# on_conflict (a hand-edited config is backed up before overwrite, never clobbered silently). apply
# and drift share `resolve_linter_config` so status never misreports what apply would do.
def _linter_label(role: str, tool: str, item: str) -> str:
    """The human label for a linter item in apply/drift output: ``<role> <tool>:<item>``.

    Renders the ``role`` (``linter``/``formatter``) so the per-item config knob is REFLECTED in
    output (not a recorded-but-unused field): a formatter and a linter that happen to share a tool
    name read distinctly. apply and drift both call this so they name the same item identically.
    Degrades gracefully: a missing tool drops the ``:tool`` half; role defaults to ``linter``.
    """
    base = f"{tool}:{item}" if tool else item
    return f"{role or 'linter'} {base}"


@dataclass(frozen=True)
class LinterConfigResolution:
    """The desired linter-config-file outcome for one item — the source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the file is present and its bytes already equal ``content``: a no-op.
      - ``create``   — the file is absent: write it.
      - ``update``   — the file exists but its bytes differ: rewrite it (honoring ``on_conflict``).
      - ``io_error`` — a directory, a symlink, or an unreadable/non-UTF-8 file sits at the path:
                       apply reports an ERROR, never a silent skip. ``detail`` carries why.

    ``target_path`` is the resolved absolute file path; ``content`` the desired bytes.
    """

    target_path: Path
    content: str
    state: str
    detail: str = ""


def _unsafe_path_component(repo_root: Path, rel_path: str) -> tuple[Path, str] | None:
    """The first LEXICAL component of ``repo_root/rel_path`` that rig must not write through.

    Returns ``(component, reason)`` for the first offending ancestor/leaf, or ``None`` when the whole
    chain is real directories (plus a leaf that may or may not exist) and a write stays safely
    contained. Two classes of offender, both checked WITHOUT resolving the path first (so an in-repo
    symlink is caught too, not just one escaping the repo):

    - **a symlink** at ANY component (a parent dir OR the final leaf). ``configs -> /outside`` +
      ``path: configs/ruff.toml`` has a clean rel_path and a non-existent leaf, yet ``write_file``
      would ``mkdir``/write THROUGH ``configs`` and escape the repo; ``.oxfmtrc.jsonc -> shared.json``
      (a link INSIDE the repo) would be rewritten THROUGH, clobbering the link target. rig refuses
      every symlink — the docstringed "symlinks are refused" rule — so the user resolves it first.
    - **a non-directory PARENT** (a regular file / something else where a directory must be). With a
      regular file at ``config`` and ``path: config/ruff.toml`` the leaf "doesn't exist", so a naive
      classify says ``create``; apply then hits ``write_file``'s ``mkdir(parents=True)`` and raises a
      bare ``FileExistsError``. Classify it as ``io_error`` up front so status and apply agree.

    Walks ``repo_root / part1 / part2 / …`` lexically (``repo_root`` itself is NOT inspected — a repo
    that legitimately sits under a symlink, e.g. macOS ``/tmp`` → ``/private/tmp``, must not trip).
    """
    parts = Path(rel_path).parts
    cur = repo_root
    for i, part in enumerate(parts):
        cur = cur / part
        if cur.is_symlink():
            return cur, "symlink"
        is_last = i == len(parts) - 1
        # a NON-final component that exists but is not a directory can't be descended into.
        if not is_last and cur.exists() and not cur.is_dir():
            return cur, "not-a-directory"
    return None


def resolve_linter_config(repo_root: Path, rel_path: str, content: str) -> LinterConfigResolution:
    """Classify the on-disk linter-config state vs desired (pure, no writes).

    Reads the file at ``repo_root/rel_path`` and returns the one ``state`` apply and drift both
    switch on, so ``status`` never misreports. Idempotent: an absent file is ``create``; a correct
    file is ``ok``; a differing file is ``update``. Both sides are compared LF-normalized — the
    desired ``content`` has its line endings collapsed to LF here, and the on-disk read uses
    universal-newline translation — so CRLF-vs-LF is never spurious drift in EITHER direction (a CRLF
    file on disk OR a CRLF ``content:`` literal); the returned ``content`` is the LF form apply writes.
    ``io_error`` covers every case where rig must NOT blindly write:

    - an ABSOLUTE or ``..``-escaping ``rel_path`` (this is public API; it self-guards rather than
      trust callers, so a direct caller can never read/write OUTSIDE the repo),
    - a SYMLINK at the path OR anywhere in its parent chain (in-repo or escaping) — rig refuses to
      follow it (writing through a link could clobber the target or escape the repo); resolve it first,
    - a non-directory PARENT component (a file where a dir must be — apply could not mkdir there),
    - a directory at the path,
    - an unreadable file (``OSError``),
    - a non-UTF-8 / binary file (``UnicodeDecodeError`` — a subclass of ``ValueError``, NOT
      ``OSError``; catching only ``OSError`` would let it crash ``apply``/``rig status`` instead of
      classifying as drift-needing-attention).
    """
    target = repo_root / rel_path
    # Normalize the DESIRED content's line endings to LF up front, and carry the normalized form in
    # the resolution so apply writes exactly what we compare. The on-disk read below uses
    # universal-newline translation (read_text → LF), so a CRLF `content:` literal (common when
    # pasted on Windows) would otherwise NEVER equal the normalized read: write CRLF → read LF →
    # compare to CRLF `content` → perpetual `update` (apply rewrites every run, status always drifts).
    # rig-managed config files are LF-only; this makes the feature converge regardless of how the
    # config string was authored.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Self-protecting containment: the runner/drift already pre-check `linter_path_escapes_repo`, but
    # this is PUBLIC API (no `_` prefix, imported across modules), so guard here too rather than trust
    # every caller. An absolute or `..`-escaping `rel_path` makes `repo_root / rel_path` point OUTSIDE
    # the repo (`Path("/repo") / "/etc/x" == Path("/etc/x")`); refuse it before any stat/read so a
    # direct caller can never read or write through an escaping path.
    if linter_path_escapes_repo(rel_path):
        return LinterConfigResolution(target, content, "io_error", detail=f"{rel_path!r} escapes the repo (rig won't read/write through it)")
    # Component containment (defends against a symlinked OR file ancestor, and an in-repo symlink leaf)
    # — checked BEFORE exists()/read so status and apply agree and no write follows an unsafe path.
    unsafe = _unsafe_path_component(repo_root, rel_path)
    if unsafe is not None:
        comp, reason = unsafe
        detail = (
            f"{comp} is a symlink (rig won't write through it)" if reason == "symlink"
            else f"{comp} is not a directory (rig can't create {target.name} under it)"
        )
        return LinterConfigResolution(target, content, "io_error", detail=detail)
    if not target.exists():
        return LinterConfigResolution(target, content, "create")
    if not target.is_file():
        return LinterConfigResolution(target, content, "io_error", detail=f"{target} is not a regular file")
    try:
        on_disk = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LinterConfigResolution(target, content, "io_error", detail=f"cannot read {target}: {exc}")
    if on_disk == content:
        return LinterConfigResolution(target, content, "ok")
    return LinterConfigResolution(target, content, "update")


def _do_provision_linter_config(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile ONE per-repo linter/formatter config file (the ``linters`` block).

    Writes ``<repo>/<rel_path>`` with the exact ``content`` from config ONLY when its bytes differ
    (a correct file is a true no-op — no spurious ``.rig-bak``). Honors ``on_conflict`` via
    :func:`fsutil.write_file`: a hand-edited config is BACKED UP before overwrite (default) / left
    untouched (``skip``) / overwritten (``overwrite``) — rig never clobbers a hand-written file
    without a backup. apply and drift switch on the SAME :func:`resolve_linter_config` ``state`` so
    status never misreports. ``io_error`` (a directory/unreadable at the path) is an error, never a
    silent skip.
    """
    # Do NOT strip rel_path — operate on the LITERAL value the validator saw (which rejects
    # whitespace-padded paths), so apply and validation never disagree on what file is meant.
    rel_path = str(action.options.get("rel_path", ""))
    content = action.options.get("content")
    tool = str(action.options.get("tool") or "")
    role = str(action.options.get("role") or "linter")
    label = _linter_label(role, tool, str(action.item))
    # Guard a malformed action (a plan-builder regression that dropped a required option). The
    # validator + builder already fail-closed, so this is defense-in-depth; fail loudly rather than
    # write a 0-byte / wrong-path file. `not content` rejects an empty string too (mirroring the
    # plan builder): the validator requires non-empty content, so an empty one means a synthetic /
    # replayed Action — write_file would otherwise emit the 0-byte file this guard exists to prevent.
    if not rel_path or not isinstance(content, str) or not content:
        return ActionResult(action, "error", f"linter-config ({label}): malformed action (missing rel_path/content)")
    # Re-enforce repo containment here too (not only at config load): a hand-built / replayed Action
    # could carry an escaping `rel_path` (`../x`, `/etc/x`) that the validator never saw. Refuse to
    # write outside the repo — same predicate the validator uses, so the two never disagree.
    if linter_path_escapes_repo(rel_path):
        return ActionResult(action, "error", f"linter-config ({label}): path {rel_path!r} escapes the repo (refusing to write)")
    r = resolve_linter_config(action.target, rel_path, content)
    if r.state == "io_error":
        return ActionResult(action, "error", f"linter-config ({label}): {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"linter-config ({label}): {r.target_path.name} already correct")
    out = fsutil.write_file(r.target_path, r.content, on_conflict)
    return ActionResult(action, out.status, f"linter-config ({label}): {out.detail}", out.backup)


def _do_provision_project_tool(action: Action, on_conflict: str) -> ActionResult:
    """Provision one repo-local project-tool carrier or live operation.

    File entries are ordinary repo files with the same conflict policy as every other rig-managed
    config. The Haft Codex MCP entry is a managed TOML section, so an existing ``.codex/config.toml``
    keeps unrelated user config. Sverklo live operations are idempotent and dry-run gated.
    """
    tool = str(action.options.get("tool") or "project-tool")
    operation = str(action.options.get("operation") or "file")
    if operation in {"register", "reindex"} and tool == "sverklo":
        status, detail = project_tools.run_sverklo(action.target, operation)
        return ActionResult(action, status, detail)

    rel_path = str(action.options.get("rel_path") or "")
    content = action.options.get("content")
    label = f"{tool}/{action.item}"
    if not rel_path or not isinstance(content, str):
        return ActionResult(action, "error", f"project-tool ({label}): malformed action (missing rel_path/content)")
    r = project_tools.resolve_entry(action.target, rel_path, content, operation)
    if r.state == "io_error":
        return ActionResult(action, "error", f"project-tool ({label}): {r.detail}")
    if r.state == "ok":
        return ActionResult(action, "skipped", f"project-tool ({label}): {rel_path} already correct")
    out = fsutil.write_file(r.target_path, r.content, on_conflict)
    return ActionResult(action, out.status, f"project-tool ({label}): {out.detail}", out.backup)


# ── GitHub repository ruleset (gh api) ─────────────────────────────────────────────
# rig reconciles a branch ruleset on the repo's DEFAULT branch — the modern replacement for
# branch protection — declaratively, the same way every other category is reconciled. The
# DESIRED body, the rule assembly, the normalized desired-vs-actual comparison, and the
# merge-lockout guard (rig never emits the `update` rule) all live in `riglib/github_ruleset.py`
# (pure, no Action dependency, so plan/state import them without a cycle); this module holds
# only the `gh` subprocess seams, the live-API classification, and the Action handler. apply and
# drift share `github_ruleset_state` + the pure builders, so they can never disagree on
# "in sync". All `gh` invocations go through `_gh_api` so tests can monkeypatch one seam; the
# RIG_GH_DRY_RUN env seam (mirrors RIG_SCHEDULE_DRY_RUN) computes what WOULD change without any
# POST/PUT, so CI and tests never mutate a real repo or hit the network.
def github_owner_repo(repo_root: Path) -> tuple[str, str] | None:
    """Resolve ``(owner, repo)`` from the repo's ``origin`` remote, or None if not on github.

    Shells out to ``git -C <repo_root> remote get-url origin`` and parses an SSH or HTTPS
    github.com URL via :func:`github_ruleset.parse_github_remote`. A repo with no remote, a
    non-github remote, or no git at all → None (the caller treats that as "nothing to
    provision", never an error). Isolated so tests can monkeypatch it without a real git remote.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return parse_github_remote(res.stdout.strip())


def _gh_api(args: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
    """Run ``gh api <args>`` and return ``(returncode, stdout, stderr)``.

    The single seam every ruleset call funnels through — tests monkeypatch THIS, so no test
    ever spawns ``gh`` or touches the network. A missing ``gh`` binary returns a non-zero rc
    with a clear message (the caller surfaces it as a skipped/error result, never a crash).
    """
    if not shutil.which("gh"):
        return 127, "", "gh CLI not found on PATH"
    try:
        res = subprocess.run(
            ["gh", "api", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"gh api failed: {exc}"
    return res.returncode, res.stdout, res.stderr


def _gh_dry_run() -> bool:
    """Honor RIG_GH_DRY_RUN — compute what WOULD change but make no POST/PUT to GitHub.

    Mirrors RIG_SCHEDULE_DRY_RUN: GET requests (read-only) still run so drift/apply can see
    the current rulesets, but the mutating create/update is skipped. CI and the test suite set
    this (or monkeypatch ``_gh_api`` wholesale) so a real repo is never mutated.
    """
    return os.environ.get("RIG_GH_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _gh_list_rulesets(owner: str, repo: str) -> tuple[list | None, str]:
    """GET ALL the repo's OWN rulesets (paginated). Returns ``(list_or_None, error_detail)``.

    ``includes_parents=false`` excludes inherited org/enterprise rulesets, so the list holds
    only this repo's rulesets — `find_managed_ruleset` then never picks up a same-named parent.
    ``--paginate`` + ``per_page=100`` is LOAD-BEARING for idempotency: the endpoint returns only
    30 rulesets per page by default, so a repo with >30 rulesets where the managed one sits on a
    later page would otherwise look absent → apply would POST a DUPLICATE ``rig-managed`` ruleset
    on every run. With ``--paginate`` gh concatenates every page's JSON array into one.
    """
    rc, out, err = _gh_api(
        [f"repos/{owner}/{repo}/rulesets?includes_parents=false&per_page=100", "--paginate"]
    )
    if rc != 0:
        return None, (err.strip() or out.strip() or f"gh api exited {rc}")
    try:
        data = json.loads(out)
    except ValueError:
        return None, "gh api returned non-JSON ruleset list"
    return (data if isinstance(data, list) else []), ""


def github_ruleset_state(action: Action) -> tuple[str, dict]:
    """Classify the on-disk-vs-desired ruleset state — the one source apply + drift share.

    Returns ``(state, info)`` where ``state`` is one of:
      - ``no_remote``  — the repo has no github origin remote: nothing to provision.
      - ``gh_error``   — listing rulesets failed (gh missing / not authed / API error).
      - ``create``     — no rig-managed ruleset exists: apply POSTs one.
      - ``update``     — a rig-managed ruleset exists but its rules/bypass/enforcement differ.
      - ``ok``         — a rig-managed ruleset exists and matches the desired body.
    ``info`` carries ``owner``/``repo``/``desired`` and, when present, the existing ruleset's
    ``id`` and ``detail`` (the error string for ``gh_error``).
    """
    owner_repo = github_owner_repo(action.target)
    desired = build_ruleset_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired}
    rulesets, err = _gh_list_rulesets(owner, repo)
    if rulesets is None:
        info["detail"] = err
        return "gh_error", info
    existing = find_managed_ruleset(rulesets, desired["name"])
    if existing is None:
        return "create", info
    rs_id = existing.get("id")
    if rs_id is None:
        # a listed ruleset with no id can't be fetched/updated — don't GET .../rulesets/None.
        info["detail"] = f"managed ruleset '{desired['name']}' has no id in the API response"
        return "gh_error", info
    info["id"] = rs_id
    # the list endpoint omits rules/bypass_actors; fetch the full ruleset to compare.
    rc, out, gerr = _gh_api([f"repos/{owner}/{repo}/rulesets/{rs_id}"])
    if rc != 0:
        info["detail"] = gerr.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        current = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON ruleset"
        return "gh_error", info
    if normalize_ruleset(current) == normalize_ruleset(desired):
        return "ok", info
    return "update", info


def _do_provision_github_ruleset(action: Action, on_conflict: str) -> ActionResult:
    """Provision (create/update) the rig-managed GitHub branch ruleset via ``gh api``.

    Shares :func:`github_ruleset_state` with drift, so status and apply read one
    classification. No github remote → ``skipped`` (never an error). The #4136.1 auth gate runs
    FIRST (before the live read), so an unauthenticated apply notifies + waits + resumes rather than
    degrading to a plain read error. ``RIG_GH_DRY_RUN`` computes the create/update but skips both the
    gate and the POST/PUT, returning what WOULD change. A read/write failure (no admin / API error)
    surfaces as an ``error`` result with the detail.
    """
    _owner, _repo, early = _github_action_preamble(action, "github-ruleset")
    if early is not None:
        return early
    state, info = github_ruleset_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-ruleset: {info.get('detail', 'gh api failed')}")

    owner, repo, desired = info["owner"], info["repo"], info["desired"]
    name = desired["name"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-ruleset: '{name}' already matches on {owner}/{repo}")

    body = json.dumps(desired)
    if state == "create":
        if _gh_dry_run():
            return ActionResult(action, "created", f"github-ruleset: RIG_GH_DRY_RUN — would CREATE '{name}' on {owner}/{repo} (not posted)")
        rc, out, err = _gh_api(
            ["--method", "POST", f"repos/{owner}/{repo}/rulesets", "--input", "-"],
            input_text=body,
        )
        if rc != 0:
            return _ruleset_write_error(action, "create", owner, repo, err, out)
        return ActionResult(action, "created", f"github-ruleset: created '{name}' on {owner}/{repo}")

    # state == "update"
    rs_id = info.get("id")
    if _gh_dry_run():
        return ActionResult(action, "updated", f"github-ruleset: RIG_GH_DRY_RUN — would UPDATE '{name}' (id={rs_id}) on {owner}/{repo} (not put)")
    rc, out, err = _gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/rulesets/{rs_id}", "--input", "-"],
        input_text=body,
    )
    if rc != 0:
        return _ruleset_write_error(action, "update", owner, repo, err, out)
    return ActionResult(action, "updated", f"github-ruleset: updated '{name}' (id={rs_id}) on {owner}/{repo}")


def _looks_like_ruleset_plan_limited(detail: str) -> bool:
    """Heuristic: does this gh error mean the repo's PLAN can't enforce a ruleset (vs. a real bug)?

    A FREE private repo can't use branch protection / rulesets — the ``POST/PUT .../rulesets``
    call returns 403 with a body that names the plan limit ("Upgrade to GitHub Team", "upgrade
    your plan", "not available for private repositories", "make this repository public"). On THAT
    we want a specific, loud "ZERO server-side enforcement" message (it is a capability limit, not
    a rig bug). But a BARE 403/422 is NOT enough — that could equally be a no-admin token or a real
    payload bug we must surface as-is. So, like :func:`_looks_like_ghas_unlicensed`, match the
    PLAN/UPGRADE WORDING only; a transient/service or auth error stays a generic error.

    The phrase list is a white-list of GitHub's KNOWN plan-limit wording and must be kept current as
    GitHub rephrases (the same maintenance burden as :func:`_looks_like_ghas_unlicensed`). A MISS is
    SAFE: an unmatched plan-limit body falls through to a generic loud ``error`` (never a silent
    no-op), so a stale list degrades gracefully rather than masking the failure.
    """
    low = detail.lower()
    plan_phrases = (
        "upgrade to github",
        "upgrade your plan",
        "upgrade this repository",
        "not available for private",
        "make this repository public",
        "make the repository public",
        "rulesets are not available",
        "branch protection is not available",
        "not available on your plan",
    )
    return any(p in low for p in plan_phrases)


def _ruleset_write_error(
    action: Action, verb: str, owner: str, repo: str, err: str, out: str
) -> ActionResult:
    """Build the LOUD error result for a failed ruleset create/update — never a silent no-op.

    When the failure wording says the repo's plan can't enforce a ruleset (free private repo, 403),
    the message is SPECIFIC and actionable. The wording differs by verb:
    - ``create``: ZERO server-side enforcement is active (no ruleset was created at all).
    - ``update``: the existing ruleset was NOT reconciled (old config still active on GitHub).
    Any other failure surfaces the raw gh detail. Either way the result is an ``error`` rig status
    reports — never a swallowed success.
    """
    detail = err.strip() or out.strip() or "gh api reported a non-zero exit with no output"
    if _looks_like_ruleset_plan_limited(detail):
        if verb == "create":
            enforcement_note = (
                "ZERO server-side enforcement is active: a merge is gated ONLY by the "
                "client-side `gh ship` preflight, which can be bypassed (raw `gh pr merge`, "
                "the web UI, --skip-ci)."
            )
        else:
            enforcement_note = (
                "the existing ruleset on GitHub was NOT reconciled to the desired state "
                "(old config remains active — check `rig status` for the current drift)."
            )
        return ActionResult(
            action,
            "error",
            f"github-ruleset: branch protection unavailable on {owner}/{repo}'s plan — GitHub "
            f"rejected the ruleset {verb} (free private repos cannot enforce rulesets). "
            f"{enforcement_note} Fix: upgrade to GitHub Team/Pro, or make the repo public. "
            f"(gh said: {detail})",
        )
    return ActionResult(action, "error", f"github-ruleset: {verb} failed: {detail}")


# ── the #4136.1 auth gate — shared by every github.* gh-api mutation ─────────────────────
# CTO #4136.1: rig must NOT silently fail when `gh` isn't authenticated for the admin scope it
# needs. Every github.* provisioner calls `_require_gh_auth` BEFORE its first mutation: if `gh` is
# already authed it returns None (proceed); otherwise it has ALREADY notified the user (via tg) and
# waited up to the RIG_GH_AUTH_WAIT budget, and returns a LOUD error ActionResult the handler
# returns as-is. Under RIG_GH_DRY_RUN the gate is skipped entirely (dry-run mutates nothing, so it
# needs no auth) — that keeps the test suite and CI from ever blocking on a login prompt.
def _require_gh_auth(action: Action, owner: str, repo: str, label: str) -> ActionResult | None:
    """Block on the #4136.1 auth gate; return None to proceed, or a loud error ActionResult.

    Skipped under RIG_GH_DRY_RUN (no mutation → no auth needed), so dry-run + tests never touch the
    gate's notify/poll path. On a non-ok outcome the user has already been pinged and rig has waited
    the configured budget; the returned ActionResult surfaces the exact `gh auth login` command so
    the failure is actionable, never a silent green.

    CALLED BEFORE THE FIRST READ. The #4136.1 contract is "not-authed → notify + WAIT → resume", and
    the very FIRST thing every github.* provisioner does is a live `gh api` read inside its
    `*_state` classifier — a read that itself FAILS without a token. So the gate must run BEFORE that
    read (not just before the mutation), otherwise an unauthenticated apply degrades to a plain
    `gh_error` and the notify-and-wait path is dead code on the exact case it exists for. Running it
    first means an unauthenticated `rig apply` pings the user and blocks for login, then the read +
    mutation proceed once authed.
    """
    if _gh_dry_run():
        return None
    outcome = ensure_gh_auth(owner=owner, repo=repo)
    if outcome.ok:
        return None
    return ActionResult(action, "error", f"{label}: {outcome.detail}")


def _github_action_preamble(
    action: Action, label: str
) -> tuple[str, str, ActionResult | None]:
    """Resolve ``(owner, repo)`` and clear the #4136.1 auth gate BEFORE any live read.

    Returns ``(owner, repo, early)``. When ``early`` is not None the caller returns it immediately:
      - ``skipped`` — the repo has no github origin remote (nothing to provision).
      - an auth ``error`` — gh is not authenticated and did not become so within the wait budget
        (the user was already pinged via tg). Under RIG_GH_DRY_RUN the gate is skipped, so dry-run
        never blocks and ``early`` is None whenever a remote exists.
    On success ``early`` is None and the caller proceeds to read live state (now that gh is authed)
    and mutate. Shared by every gh-api provisioner so the gate fires on the no-token case for all of
    them, not just the ones whose first call happened to be the mutation.
    """
    owner_repo = github_owner_repo(action.target)
    if owner_repo is None:
        return "", "", ActionResult(action, "skipped", f"{label}: no github origin remote — nothing to provision")
    owner, repo = owner_repo
    gate = _require_gh_auth(action, owner, repo, label)
    if gate is not None:
        return owner, repo, gate
    return owner, repo, None


# ── rig-managed GitHub repo MERGE-button policy (§5 github.merge) ────────────────────
# rig reconciles the repo's merge-button policy — squash-only merge model, auto-delete head branch
# on merge, allow-auto-merge — via `PATCH /repos/{owner}/{repo}` on the same `gh api` backend as the
# ruleset. The DESIRED body, managed-field set, and normalized comparison live in
# `riglib/github_merge.py` (pure); this module holds only the live-API classification and the Action
# handler. apply and drift share `github_merge_state`. CAPABILITY DEGRADE: these are repo SETTINGS,
# not a ruleset, so a PATCH never locks anyone out — a no-admin token gets HTTP 403, surfaced as a
# visible error (apply) / "could not verify" (drift), never a silent green or crash.
def github_merge_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired merge policy — the one source apply + drift share.

    States: ``no_remote`` (no github origin), ``gh_error`` (read failed: gh missing / not authed /
    no admin / API error), ``update`` (live differs → apply PATCHes), ``ok`` (already converged).
    There is no ``create`` — a github repo ALWAYS has merge settings, so the only outcomes are
    converge or already-converged. ``info`` carries owner/repo/desired and, on gh_error, a detail.
    """
    owner_repo = github_owner_repo(action.target)
    desired = build_merge_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired}
    rc, out, err = _gh_api([f"repos/{owner}/{repo}"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        repo_obj = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON repo object"
        return "gh_error", info
    if normalize_merge(repo_obj) == normalize_merge(desired):
        return "ok", info
    return "update", info


def _do_provision_github_merge(action: Action, on_conflict: str) -> ActionResult:
    """Provision (PATCH) the repo merge-button policy via ``gh api``.

    Shares :func:`github_merge_state` with drift. No github remote → ``skipped``. The #4136.1 auth
    gate runs FIRST (before the live read), so an unauthenticated apply notifies + waits + resumes
    rather than degrading to a plain read error. ``RIG_GH_DRY_RUN`` computes the PATCH but skips both
    the gate and the mutation. A read/PATCH failure (no admin / API error) surfaces as a loud
    ``error``.
    """
    _owner, _repo, early = _github_action_preamble(action, "github-merge")
    if early is not None:
        return early
    state, info = github_merge_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-merge: {info.get('detail', 'gh api failed')}")
    owner, repo, desired = info["owner"], info["repo"], info["desired"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-merge: policy already matches on {owner}/{repo}")
    if _gh_dry_run():
        return ActionResult(action, "updated", f"github-merge: RIG_GH_DRY_RUN — would UPDATE merge policy on {owner}/{repo} (not patched)")
    rc, out, err = _gh_api(
        ["--method", "PATCH", f"repos/{owner}/{repo}", "--input", "-"],
        input_text=json.dumps(desired),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-merge: update failed: {err.strip() or out.strip()}")
    return ActionResult(action, "updated", f"github-merge: updated merge policy on {owner}/{repo}")


# ── rig-managed GitHub Advanced Security (§5 github.ghas) ────────────────────────────────
# rig reconciles the repo's GHAS toggles — dependency graph + secret-scanning (+ push protection)
# via `security_and_analysis` on `PATCH /repos/{o}/{r}`, vuln-alerts + Dependabot security updates
# via their own `PUT/DELETE` sub-resources, and CodeQL default-setup via its own endpoint. The
# DESIRED knobs/bodies live in `riglib/github_ghas.py` (pure). CAPABILITY DEGRADE: code/secret
# scanning on a PRIVATE repo needs a GHAS-licensed plan; the API returns 403/422 there — that is a
# loud "could not enable (plan does not include GHAS)", NOT a crash and NOT a silent green. Free
# features (dep-graph / vuln-alerts / Dependabot) degrade independently so one unlicensed scanner
# never masks a successfully-toggled free feature.
def _gh_subresource_enabled(owner: str, repo: str, endpoint: str) -> bool | None:
    """Read whether a GHAS sub-resource (vuln-alerts / Dependabot fixes) is ON. None = couldn't tell.

    ``vulnerability-alerts`` is a presence endpoint: GET returns 204 when enabled, 404 when not (gh
    reports the 404 as a non-zero rc). ``automated-security-fixes`` GET returns a JSON object with an
    ``enabled`` field. We treat a clean 204/enabled as True and a 404 as False; any OTHER error
    (auth/permission/network) → None so the caller can tell "off" apart from "couldn't check" and
    never reports a confident in-sync behind a failed read.

    The "off" signal is keyed on the HTTP STATUS (``HTTP 404``), NOT a loose substring like
    "disabled" or "not found": gh prints the status as ``(HTTP 404)`` / ``HTTP 404:`` for the
    not-enabled case, whereas a 403/422 validation body could legitimately contain the word
    "disabled" and must NOT be misread as "the resource is off" (that would mask a real
    auth/permission failure as a confident False). So we match the 404 status token only; anything
    else with a non-zero rc is an honest "unknown" (None).
    """
    rc, out, err = _gh_api([f"repos/{owner}/{repo}/{endpoint}"])
    if rc == 0:
        if not out.strip():
            return True  # 204 No Content (vulnerability-alerts enabled)
        try:
            obj = json.loads(out)
        except ValueError:
            return True  # a 2xx with a non-JSON body still means the resource is present/enabled
        return bool(obj.get("enabled", True)) if isinstance(obj, dict) else True
    blob = (err + out).lower()
    # Match the 404 STATUS token (gh: "(HTTP 404)" / "HTTP 404:"), not a bare "404" anywhere or the
    # word "disabled" — so a 422 body that happens to contain "disabled" isn't misread as "off".
    if "http 404" in blob or "status: 404" in blob:
        return False
    return None  # a real error (auth/permission/network) — unknown, not "off"


def _gh_code_scanning_state(owner: str, repo: str) -> str | None:
    """Read CodeQL default-setup's state (``configured``/``not-configured``), or None if unreadable.

    The endpoint is GHAS-plan-gated; on a repo whose plan doesn't include it the GET errors. We
    return the state on a clean read and None on any error — the caller treats None as "could not
    verify" (a loud unknown), never as a confident in-sync.
    """
    rc, out, err = _gh_api([f"repos/{owner}/{repo}/code-scanning/default-setup"])
    if rc != 0:
        return None
    try:
        return str(json.loads(out).get("state", ""))
    except (ValueError, AttributeError):
        return None


def github_ghas_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired GHAS settings — the one source apply + drift share.

    States: ``no_remote``, ``gh_error`` (the REPO read itself failed — no auth / repo gone, so
    nothing can be classified), ``update`` (a readable setting differs OR an individual scanner is
    unverifiable), ``ok`` (every managed setting was readable AND matches). The signal covers the
    repo-object ``security_and_analysis`` block, the vuln-alerts / Dependabot sub-resources, AND
    CodeQL default-setup — each via its own GET.

    A SINGLE unreadable SCANNER endpoint (a GHAS-plan-gated 403/422 on a private repo without the
    GHAS license) does NOT collapse the whole classification to ``gh_error`` — that would make rig
    refuse to apply the FREE features (dep-graph / vuln-alerts / Dependabot) just because one
    licensed scanner is unavailable, the exact opposite of the documented "free features applied
    independently" design. Instead each unverifiable scanner is recorded in ``info["unverifiable"]``
    and forces ``update`` (so a green status never masks it, and apply runs to degrade it loudly
    while still applying everything else). Only a failed REPO read — where we can't classify
    ANYTHING — is ``gh_error``. ``info`` carries owner/repo/desired, ``unverifiable`` (a list of
    "endpoint (detail)" notes), and on gh_error a ``detail``.
    """
    owner_repo = github_owner_repo(action.target)
    desired_sa = build_security_analysis_body(action.options)
    if owner_repo is None:
        return "no_remote", {"desired": desired_sa}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "desired": desired_sa, "unverifiable": []}
    rc, out, err = _gh_api([f"repos/{owner}/{repo}"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        repo_obj = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON repo object"
        return "gh_error", info
    desired_norm = {f: n["status"] for f, n in desired_sa.items()}
    # Record the security_and_analysis-block drift SEPARATELY so apply can gate the repo PATCH on it
    # (a PATCH when only a sub-resource / CodeQL drifted is a no-op that also prints a misleading
    # "security_and_analysis converged"). The aggregate `drifted` still drives the overall state.
    sa_drifted = normalize_security_analysis(repo_obj) != desired_norm
    info["sa_drifted"] = sa_drifted
    drifted = sa_drifted
    # the sub-resources: an UNREADABLE one is recorded as unverifiable (forces `update` so status
    # surfaces it and apply degrades it loudly) — NOT a whole-block gh_error that would also block
    # the free features. Distinct from "read OK but off", which is plain drift.
    for knob in SUBRESOURCE_KNOBS:
        endpoint = "vulnerability-alerts" if knob == "vulnerability_alerts" else "automated-security-fixes"
        live = _gh_subresource_enabled(owner, repo, endpoint)
        if live is None:
            info["unverifiable"].append(f"{endpoint} (could not read — plan-gated / no admin / API error)")
            drifted = True
        elif live != desired_subresource(action.options, knob):
            drifted = True
    # CodeQL default-setup: unreadable on a plan that gates it is unverifiable (degraded), not a
    # whole-block gh_error — same reasoning as the sub-resources above.
    want_codeql = "configured" if desired_code_scanning(action.options) else "not-configured"
    live_codeql = _gh_code_scanning_state(owner, repo)
    if live_codeql is None:
        info["unverifiable"].append("code-scanning default-setup (could not read — plan-gated / no admin / API error)")
        drifted = True
    elif live_codeql != want_codeql:
        drifted = True
    return ("update", info) if drifted else ("ok", info)


def _do_provision_github_ghas(action: Action, on_conflict: str) -> ActionResult:
    """Provision the repo GHAS settings via ``gh api`` (PATCH + sub-resource PUT/DELETEs).

    No github remote → ``skipped``. The #4136.1 auth gate runs FIRST (before the live read), so an
    unauthenticated apply notifies + waits + resumes. ``RIG_GH_DRY_RUN`` computes what WOULD change
    but mutates nothing. A GHAS-licensed scanner unavailable on the repo's plan degrades to a loud
    note in the result detail and does NOT block the free features (dep-graph / vuln-alerts /
    Dependabot), never a crash.
    """
    _owner, _repo, early = _github_action_preamble(action, "github-ghas")
    if early is not None:
        return early
    state, info = github_ghas_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-ghas: {info.get('detail', 'gh api failed')}")
    owner, repo, desired_sa = info["owner"], info["repo"], info["desired"]

    if _gh_dry_run():
        verb = "already matches" if state == "ok" else "would UPDATE"
        return ActionResult(action, "skipped" if state == "ok" else "updated",
                            f"github-ghas: RIG_GH_DRY_RUN — {verb} security settings on {owner}/{repo} (not mutated)")

    notes: list[str] = []
    # Scanners that couldn't even be read (plan-gated on a private repo) are seeded into the loud
    # degrade list up front — apply still runs the free features below; these surface as DEGRADED.
    degraded: list[str] = list(info.get("unverifiable", []))
    # GENUINE (non-plan-limit) write failures on a sub-resource / CodeQL go here — they make the
    # whole action an ERROR at the end, so a real auth/permission failure is never a silent green.
    hard_errors: list[str] = []
    # 1) security_and_analysis block (dep-graph + secret-scanning + push protection) via repo PATCH.
    # Gate on the SA-block's OWN drift (not the aggregate `state == update`): when only a sub-resource
    # or CodeQL drifted, the SA block already matches, so PATCHing it is a no-op that would also print
    # a misleading "security_and_analysis converged". `sa_drifted` is computed by the classifier.
    if info.get("sa_drifted"):
        rc, out, err = _gh_api(
            ["--method", "PATCH", f"repos/{owner}/{repo}", "--input", "-"],
            input_text=json.dumps({"security_and_analysis": desired_sa}),
        )
        if rc != 0:
            detail = err.strip() or out.strip()
            # A private repo without a GHAS-licensed plan → 403/422 on the scanners. Degrade loudly
            # rather than failing the whole apply. A GENUINE failure goes into hard_errors (final
            # status = error) but does NOT early-return — so the FREE features (vuln-alerts /
            # Dependabot / CodeQL) are still attempted, matching the "applied independently" design
            # and the symmetric handling of the sub-resource / CodeQL write failures below.
            if _looks_like_ghas_unlicensed(detail):
                degraded.append(f"secret-scanning/dep-graph (plan does not include GHAS: {detail[:80]})")
            else:
                hard_errors.append(f"security_and_analysis ({detail[:80]})")
        else:
            notes.append("security_and_analysis converged")
    # 2) vuln-alerts + Dependabot security updates — their own PUT/DELETE sub-resources. Read the
    # live state first and skip the mutation when it already matches, so a re-apply is a true no-op
    # (reports `skipped`, not a phantom `updated`) instead of blindly PUTting every run.
    #
    # When the classifier ALREADY found this endpoint unreadable (a read-only / non-admin token gets
    # 403 on the sub-resource while the repo GET itself passed), it is in `info["unverifiable"]` and
    # thus already seeded into `degraded`. Skip the mutation then — it would 403 the same way and
    # append a SECOND, redundant degrade line for the same endpoint (or, on a transient read failure
    # followed by a lucky write, a contradictory "enabled" note vs. "could not read" degrade). Same
    # guard as the CodeQL block below.
    for knob in SUBRESOURCE_KNOBS:
        want = desired_subresource(action.options, knob)
        endpoint = "vulnerability-alerts" if knob == "vulnerability_alerts" else "automated-security-fixes"
        if any(u.startswith(endpoint) for u in info.get("unverifiable", [])):
            continue  # unreadable per the classifier — already degraded; a write would just 403 again
        live = _gh_subresource_enabled(owner, repo, endpoint)
        if live == want:
            continue  # already in the desired state — no mutation, no churn
        method = "PUT" if want else "DELETE"
        rc, out, err = _gh_api(["--method", method, f"repos/{owner}/{repo}/{endpoint}"])
        if rc != 0:
            full = err.strip() or out.strip()  # classify on the FULL detail
            # A plan/feature limit (dep-graph not on this plan) is an acceptable DEGRADE; a genuine
            # auth/permission/network failure is a HARD error that must NOT be masked as
            # "updated (degraded)" — else automation checking `status != error` reads a false green.
            # Classify on the FULL string; truncate only the displayed copy (a verbose gh "Validation
            # Failed" prefix could push the plan-limit phrase past a truncation and misclassify it).
            (degraded if _looks_like_ghas_unlicensed(full) else hard_errors).append(f"{endpoint} ({full[:80]})")
        else:
            notes.append(f"{endpoint} {'enabled' if want else 'disabled'}")
    # 3) CodeQL default-setup — its own endpoint with a `{state: configured|not-configured}` body.
    # Both directions are reconciled: `false` sends `not-configured` so a user can turn CodeQL OFF
    # again, not just on (a one-directional enable would strand the setting once enabled). Read the
    # current state first and PATCH only on a real difference, so a converged repo stays a no-op
    # rather than reporting `updated` every run.
    #
    # When the classifier ALREADY found this endpoint unreadable (plan-gated on a private repo), it
    # is in `info["unverifiable"]` and thus already in `degraded` — so we skip the best-effort PATCH
    # (it would 403 the same way and append a SECOND, redundant code-scanning degrade line). The
    # endpoint being unreadable is already surfaced loudly; a doomed write adds noise, not signal.
    codeql_unreadable = any("code-scanning" in u for u in info.get("unverifiable", []))
    if not codeql_unreadable:
        want_codeql = desired_code_scanning(action.options)
        codeql_state = "configured" if want_codeql else "not-configured"
        rc, out, err = _gh_api([f"repos/{owner}/{repo}/code-scanning/default-setup"])
        live_codeql: str | None = None
        if rc == 0:
            try:
                live_codeql = str(json.loads(out).get("state", ""))
            except (ValueError, AttributeError):
                live_codeql = None
        if live_codeql != codeql_state:
            rc, out, err = _gh_api(
                ["--method", "PATCH", f"repos/{owner}/{repo}/code-scanning/default-setup", "--input", "-"],
                input_text=json.dumps({"state": codeql_state}),
            )
            if rc != 0:
                full = err.strip() or out.strip()  # classify on the FULL detail, truncate for display
                # Same split as the sub-resources: a plan/feature limit degrades; a real failure is
                # a hard error (CodeQL default-setup needs Actions enabled + GHAS — a 403 from a
                # non-admin token must surface, not be swallowed as a silent green).
                (degraded if _looks_like_ghas_unlicensed(full) else hard_errors).append(
                    f"code-scanning default-setup ({full[:120]})"
                )
            else:
                notes.append(f"code-scanning default-setup {codeql_state}")

    summary = "; ".join(notes) or "GHAS settings reconciled"
    if degraded:
        summary += " — DEGRADED: " + "; ".join(degraded)
    # A genuine (non-plan-limit) failure on ANY sub-resource / CodeQL write is a HARD error — never
    # masked as "updated (degraded)", which automation checking `status != error` would read green.
    if hard_errors:
        summary += " — FAILED: " + "; ".join(hard_errors)
        return ActionResult(action, "error", f"github-ghas: {summary} on {owner}/{repo}")
    status = "skipped" if (state == "ok" and not notes and not degraded) else "updated"
    return ActionResult(action, status, f"github-ghas: {summary} on {owner}/{repo}")


def _looks_like_ghas_unlicensed(detail: str) -> bool:
    """Heuristic: does this gh error mean the repo's plan does not include GHAS (vs. a real failure)?

    A private repo without GitHub Advanced Security gets a 403/422 whose body mentions the missing
    feature/plan. We degrade loudly on THAT (it's a capability limit, not a rig bug), but a genuine
    auth/permission error — or a generic 422 validation/payload bug — must still surface as an
    error. So we match the PLAN/FEATURE WORDING only; a bare status code (403 / 422) is NOT enough,
    because that could equally be a no-admin token or a real request bug we want to see, not swallow.

    The phrases are deliberately SPECIFIC to a plan/feature limit. A bare ``"not available"`` is NOT
    matched on its own (a transient ``503 Service not available`` or a network ``… not available``
    would be mis-swallowed as a capability limit); we require it paired with a feature/plan word
    (``not available for``, ``feature is not available``) so only the real GHAS-not-on-this-plan body
    degrades, and a transient/service error stays a hard error.
    """
    low = detail.lower()
    # Phrases that unambiguously mean "this repo's PLAN doesn't include the feature". Deliberately
    # NOT "must be enabled for" — CodeQL on a repo with Actions OFF fails "Actions must be enabled
    # for default setup", which is a FIXABLE config collision (turn Actions on), not a plan limit, and
    # must surface as a real error, not a degrade.
    plain = ("advanced security", "not included in", "upgrade your plan", "ghas")
    if any(s in low for s in plain):
        return True
    # "not available" only counts when it's about a feature/repo plan limit, not a service outage
    return ("not available for" in low) or ("feature is not available" in low) or ("not available on" in low)


# ── rig-managed GitHub Actions permissions (§5 github.actions) ───────────────────────────
# rig reconciles the repo's Actions permissions on two endpoints: `PUT .../actions/permissions`
# (Actions enabled + allowed_actions) and `PUT .../actions/permissions/workflow` (the default
# GITHUB_TOKEN scope + whether workflows may approve PRs). Secure defaults: Actions enabled, but the
# token is READ-only and workflows may NOT approve PRs (least privilege). The DESIRED bodies live in
# `riglib/github_actions.py` (pure). Settings, not a ruleset — a mis-set value at worst restricts a
# workflow token, never locks a human out.
def github_actions_state(action: Action) -> tuple[str, dict]:
    """Classify the live-vs-desired Actions permissions — the one source apply + drift share.

    States: ``no_remote``, ``gh_error``, ``update`` (either endpoint differs), ``ok``. ``info``
    carries owner/repo and the two desired bodies (and on gh_error a detail). Both endpoints are
    read so a difference on EITHER reads as drift.
    """
    owner_repo = github_owner_repo(action.target)
    perms = build_permissions_body(action.options)
    wf = build_workflow_permissions_body(action.options)
    if owner_repo is None:
        return "no_remote", {"perms": perms, "wf": wf}
    owner, repo = owner_repo
    info: dict = {"owner": owner, "repo": repo, "perms": perms, "wf": wf}
    rc, out, err = _gh_api([f"repos/{owner}/{repo}/actions/permissions"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        live_perms = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON actions permissions"
        return "gh_error", info
    # When the config DISABLES Actions, the apply handler deliberately skips the workflow-permissions
    # PUT (GitHub rejects it with Actions off). The classifier MUST mirror that: don't read or compare
    # the workflow-token endpoint either — else its (now-irrelevant) live value would read as
    # perpetual drift, making apply report `updated` every run and `rig status` show `modified`
    # forever, never converging. With Actions disabled, only the permissions endpoint is in scope.
    if not perms.get("enabled", True):
        in_sync = normalize_permissions(live_perms) == normalize_permissions(perms)
        return ("ok", info) if in_sync else ("update", info)
    rc, out, err = _gh_api([f"repos/{owner}/{repo}/actions/permissions/workflow"])
    if rc != 0:
        info["detail"] = err.strip() or out.strip() or f"gh api exited {rc}"
        return "gh_error", info
    try:
        live_wf = json.loads(out)
    except ValueError:
        info["detail"] = "gh api returned non-JSON workflow permissions"
        return "gh_error", info
    in_sync = (
        normalize_permissions(live_perms) == normalize_permissions(perms)
        and normalize_workflow_permissions(live_wf) == normalize_workflow_permissions(wf)
    )
    return ("ok", info) if in_sync else ("update", info)


def _do_provision_github_actions(action: Action, on_conflict: str) -> ActionResult:
    """Provision the repo Actions permissions via ``gh api`` (two PUTs).

    No github remote → ``skipped``. The #4136.1 auth gate runs FIRST (before the live read), so an
    unauthenticated apply notifies + waits + resumes. ``RIG_GH_DRY_RUN`` computes the PUTs but skips
    both the gate and the mutation. A failure (no admin / API error) surfaces as a loud error.
    """
    _owner, _repo, early = _github_action_preamble(action, "github-actions")
    if early is not None:
        return early
    state, info = github_actions_state(action)
    if state == "gh_error":
        return ActionResult(action, "error", f"github-actions: {info.get('detail', 'gh api failed')}")
    owner, repo, perms, wf = info["owner"], info["repo"], info["perms"], info["wf"]
    if state == "ok":
        return ActionResult(action, "skipped", f"github-actions: permissions already match on {owner}/{repo}")
    if _gh_dry_run():
        return ActionResult(action, "updated", f"github-actions: RIG_GH_DRY_RUN — would UPDATE permissions on {owner}/{repo} (not put)")
    rc, out, err = _gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/actions/permissions", "--input", "-"],
        input_text=json.dumps(perms),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-actions: permissions update failed: {err.strip() or out.strip()}")
    # The default-workflow-token PUT only applies when Actions is ENABLED. When the config disables
    # Actions (`actions_enabled: false`), GitHub rejects a workflow-permissions PUT (there are no
    # workflows to scope), so issuing it would turn a legitimate "disable Actions" config into an
    # error. Skip it — disabling Actions is the whole change, and the token scope is moot.
    if not perms.get("enabled", True):
        return ActionResult(action, "updated", f"github-actions: disabled Actions on {owner}/{repo}")
    rc, out, err = _gh_api(
        ["--method", "PUT", f"repos/{owner}/{repo}/actions/permissions/workflow", "--input", "-"],
        input_text=json.dumps(wf),
    )
    if rc != 0:
        return ActionResult(action, "error", f"github-actions: workflow permissions update failed: {err.strip() or out.strip()}")
    return ActionResult(action, "updated", f"github-actions: updated permissions on {owner}/{repo}")


# ── rig-managed GitHub settings via agent-browser (§5 — API-unreachable toggles) ─────────
# A first-class second backend invoked INSIDE apply for settings the REST API does NOT expose. The
# settings-page URL, the per-toggle accessibility selectors, and the agent-browser command PLAN are
# pure (`riglib/github_browser.py`); this handler ensures a logged-in browser via the #4136.1 gate
# (ensure_browser_auth) and replays the plan through a single `_agent_browser` seam. CAPABILITY
# DETECTION: agent-browser absent / not logged in / the toggle not on the page → a loud "could not
# drive the UI", never a silent green or a blind click.
def _agent_browser(args: list[str]) -> tuple[int, str, str]:
    """Run ``agent-browser <args>`` — the single seam every browser-backend call funnels through.

    Tests monkeypatch THIS so no test launches a browser. A missing binary → rc 127 with a clear
    message (the handler surfaces it as a degrade, never a crash).
    """
    if not shutil.which("agent-browser"):
        return 127, "", "agent-browser not found on PATH"
    try:
        res = subprocess.run(
            ["agent-browser", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"agent-browser failed: {exc}"
    return res.returncode, res.stdout, res.stderr


def _do_provision_github_browser(action: Action, on_conflict: str) -> ActionResult:
    """Provision API-unreachable GitHub settings by driving the settings UI with agent-browser.

    No github remote → ``skipped``. ``RIG_GH_DRY_RUN`` builds + returns the command plan but runs
    nothing. Ensures a logged-in browser via the #4136.1 gate first; on any per-step failure the
    setting degrades loudly (the UI moved / the toggle is gone / not logged in) rather than a silent
    green. ``RIG_GH_BROWSER`` gates the whole backend OFF by default — driving a real browser is a
    heavier, slower path than gh api, so it only runs when explicitly enabled (so an ordinary
    `rig apply` never spawns a browser unexpectedly); the plan is still computed for status/tests.
    """
    owner_repo = github_owner_repo(action.target)
    desired = browser_desired_toggles(action.options)
    if owner_repo is None:
        return ActionResult(action, "skipped", "github-browser: no github origin remote — nothing to provision")
    owner, repo = owner_repo
    plan = build_browser_plan(owner, repo, desired)

    # The backend-enabled gate is checked BEFORE dry-run so a preview reflects what apply would
    # ACTUALLY do: with RIG_GH_BROWSER unset, a real apply is `skipped`, so the dry-run preview must
    # say "would be skipped" too — not "would drive N steps" (which an apply in the same env never
    # would). Only when the backend IS enabled does dry-run report the would-drive preview.
    browser_enabled = os.environ.get("RIG_GH_BROWSER", "").strip().lower() in ("1", "true", "yes")
    if not browser_enabled:
        verb = "RIG_GH_DRY_RUN — would be SKIPPED" if _gh_dry_run() else "disabled"
        return ActionResult(action, "skipped",
                            f"github-browser: {verb} (set RIG_GH_BROWSER=1 to drive {len(plan)} UI step(s) on {owner}/{repo})")
    if _gh_dry_run():
        return ActionResult(action, "updated",
                            f"github-browser: RIG_GH_DRY_RUN — would drive {len(plan)} UI step(s) on {owner}/{repo} (not run)")

    outcome = ensure_browser_auth(owner=owner, repo=repo)
    if not outcome.ok:
        return ActionResult(action, "error", f"github-browser: {outcome.detail}")

    # The #4136.1 notify-and-wait already happened above: `ensure_browser_auth` pinged the user and
    # blocked for the browser to become available. This is the SECOND, per-page check it cannot do —
    # whether the (now-present) browser session is actually LOGGED IN to github.com. The FIRST plan
    # step is `open <settings-url>`; after navigating, probe the resulting URL. If GitHub bounced us
    # to `/login` the session is logged out, so we degrade LOUDLY (a visible error telling the user
    # to log in and re-run) rather than blind-clicking the login page. (We don't re-enter the
    # notify/poll loop here — the gate already gave the user their chance; a still-logged-out session
    # at this point is an explicit, actionable failure, not another wait.)
    open_step, toggle_steps = plan[0], plan[1:]
    rc, out, err = _agent_browser(open_step)
    if rc != 0:
        return ActionResult(action, "error",
                            f"github-browser: could not open settings on {owner}/{repo}: {(err.strip() or out.strip())[:120]}")
    if _browser_on_login_page(owner, repo):
        return ActionResult(action, "error",
                            f"github-browser: the browser session is not logged into github.com — log in and re-run "
                            f"with RIG_GH_BROWSER=1 to provision the UI-only settings on {owner}/{repo}")

    for step in toggle_steps:
        rc, out, err = _agent_browser(step)
        if rc != 0:
            return ActionResult(action, "error",
                                f"github-browser: UI step {' '.join(step)!r} failed on {owner}/{repo}: {(err.strip() or out.strip())[:120]}")
    return ActionResult(action, "updated", f"github-browser: drove {len(toggle_steps)} UI toggle(s) on {owner}/{repo}")


def _browser_on_login_page(owner: str, repo: str) -> bool:
    """True iff the current browser URL is a github.com login/SSO page (i.e. NOT logged in).

    Reads `agent-browser get url`. GitHub redirects an unauthenticated request for a repo settings
    page to ``/login`` (or ``/sessions/...`` for SSO). A read that fails is treated as "assume logged
    in" (the subsequent toggle step will surface a real failure loudly) so a transient probe error
    doesn't block an otherwise-authenticated run. Seamed via `_agent_browser` so tests drive it.

    The login signal is parsed via urllib, NOT a bare ``"/login" in url`` substring, which would
    false-positive on a repo or OWNER literally named ``login`` (``github.com/login/<repo>/settings``
    or ``github.com/<owner>/login/settings``) and abort a perfectly authenticated run. Two guards:
      1. If the URL path is exactly our ``<owner>/<repo>`` page (the settings URL we navigated to),
         it is the logged-IN destination — never a login page, even if owner or repo IS "login".
      2. Otherwise, an auth bounce lands on a known sign-in path: ``/login`` or ``/sessions/...``
         (password sign-in) OR ``/orgs/<org>/sso`` (a SAML/SSO redirect for an org-protected repo).
         We match those path shapes; an unrecognized bounce still degrades later (the toggle step
         fails to find its control), just with a generic message instead of this specific one.
    """
    rc, out, _ = _agent_browser(["get", "url"])
    if rc != 0:
        return False
    parsed = urllib.parse.urlparse(out.strip())
    segments = [s.lower() for s in parsed.path.split("/") if s]
    # guard 1: our own settings page (…/<owner>/<repo>/…) is the logged-in destination
    if len(segments) >= 2 and segments[0] == owner.lower() and segments[1] == repo.lower():
        return False
    if not segments:
        return False
    # guard 2a: password sign-in (/login, /sessions/…)
    if segments[0] in ("login", "sessions"):
        return True
    # guard 2b: SAML/SSO redirect (/orgs/<org>/sso[...])
    return len(segments) >= 3 and segments[0] == "orgs" and segments[2].startswith("sso")


# ── rig-managed GLOBAL git-excludes block ──────────────────────────────────────────
# rig maintains a marker-delimited block in git's GLOBAL ``core.excludesfile`` so harness
# artifacts (chiefly Claude Code's throwaway ``**/.claude/worktrees/``) are ignored in EVERY repo
# on the machine, with zero per-repo commits — not by a per-repo committed ``.gitignore`` and not
# by a hand-edited global ignore. This is the global counterpart of the git-hooks dispatcher: a
# ``git config --global`` setting plus a managed file. The markers fence ONLY rig's lines; every
# other line the user (or another tool) has in the excludes file is preserved verbatim. apply and
# drift BOTH go through ``resolve_global_excludes`` so they can never disagree on the desired block
# or whether the file is in sync. The marker constants live in ``config.py`` (the schema layer) so
# validation can reject a marker-colliding entry without an import cycle; this module imports them
# at top for its own block construction/detection.


def global_excludes_block_text(entries: list[str]) -> str:
    """The exact marker-delimited block rig owns for ``entries`` (no trailing newline).

    Single source of truth shared by the install handler and drift, so both agree byte-for-byte
    on what the managed block SHOULD contain. The block is the begin marker, a fixed explanatory
    comment line (so a human reading the global excludes file knows what it is), one line per entry
    (in the order given), then the end marker. The comment is rendered byte-for-byte and matches the
    block already present on a provisioned machine, so a re-apply is a true zero-churn no-op.
    """
    return "\n".join([GITIGNORE_BEGIN_MARKER, GITIGNORE_BLOCK_COMMENT, *entries, GITIGNORE_END_MARKER])


@dataclass(frozen=True)
class GlobalExcludesResolution:
    """The desired global-excludes outcome — the one source apply + drift share.

    ``state`` is the single discriminator both consumers switch on:
      - ``ok``       — the managed block is already present and exactly correct: no-op.
      - ``create``   — no excludes file or no managed block: append the block (creating the file
                       if absent), preserving any existing content.
      - ``update``   — a managed block exists but its body differs, OR the file has MULTIPLE
                       rig-managed blocks (a prior non-idempotent appender): collapse the managed
                       region to ONE correct block in place, preserving every line outside the
                       markers (verbatim — CRLF and trailing blanks included).
      - ``conflict`` — the file has unbalanced markers (a begin with no end, an end before a
                       begin): rig won't guess the block's extent, so it leaves the file untouched
                       and surfaces it. ``detail`` says why.
      - ``io_error`` — the path could not be read (unreadable, or a directory sits there). Unlike a
                       marker ``conflict`` (the file is fine, the operator must reconcile) this is a
                       failure to even inspect the file: apply reports it as an ERROR, never a silent
                       skip. ``detail`` carries the OS error.

    ``desired_block`` is the canonical block text; ``new_content`` is the full desired file content
    for ``create``/``update`` (``None`` for ``ok``/``conflict``/``io_error``).
    """

    path: Path
    state: str
    desired_block: str
    new_content: str | None = None
    detail: str = ""


def resolve_global_excludes(path: Path, entries: list[str]) -> GlobalExcludesResolution:
    """Classify the on-disk global excludes file vs the desired managed block (pure, no writes).

    Idempotent + non-destructive: rig only ever (a) appends the block to a file that lacks it
    (creating the file if absent), (b) collapses the existing managed region to ONE correct block
    in place, or (c) no-ops a correct single block. Crucially this is STRICTLY idempotent even when
    a prior tool appended the block MORE THAN ONCE: a file with several rig-managed blocks resolves
    to ``update`` and collapses to exactly one. An unbalanced marker pair is a ``conflict`` rig
    never rewrites. Every line OUTSIDE the managed region is preserved byte-for-byte: the region is
    located and spliced by raw character offset (not splitlines/rejoin), so a CRLF file, a file with
    no trailing newline, and trailing blank lines all survive untouched.
    """
    desired = global_excludes_block_text(entries)
    if not path.exists():
        return GlobalExcludesResolution(path, "create", desired, new_content=desired + "\n")
    try:
        # newline="" disables universal-newline translation so a CRLF file is read (and later
        # re-written) byte-for-byte outside the managed region — the documented verbatim guarantee.
        with path.open(encoding="utf-8", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        # unreadable, or a directory at the path — a failure to inspect, not a marker conflict.
        return GlobalExcludesResolution(path, "io_error", desired, detail=f"cannot read {path}: {exc}")

    # Find each marker line by its raw [start, end_of_line] offsets so the splice preserves every
    # other byte verbatim (line ending included). A marker is a line whose stripped text equals the
    # marker constant — tolerant of trailing whitespace on the marker line itself.
    begins = _find_marker_lines(content, GITIGNORE_BEGIN_MARKER)
    ends = _find_marker_lines(content, GITIGNORE_END_MARKER)
    # An unbalanced pair (different counts, or an end with no begin) is ambiguous — rig won't guess
    # the region's extent. NOTE a balanced N-pairs (N>1) is NOT a conflict: it is a non-idempotent
    # duplicate we collapse below.
    if len(begins) != len(ends) or (ends and not begins):
        return GlobalExcludesResolution(
            path, "conflict", desired,
            detail=f"{path} has unbalanced rig-managed markers — reconcile by hand, then re-run",
        )
    if not begins:
        # no managed block: append it, keeping a single blank-line separator from prior content.
        body = content.rstrip("\n")
        sep = "\n\n" if body else ""
        new_content = f"{body}{sep}{desired}\n"
        return GlobalExcludesResolution(path, "create", desired, new_content=new_content)

    # Pair the markers by interleaving them in document order: they must strictly alternate
    # begin, end, begin, end, … Anything else (an end before a begin, a begin immediately followed
    # by another begin → a nested/overlapping block) is ambiguous and rig won't guess — conflict.
    # Each valid pair fences ONE managed block region ``[begin_start, end_line_end]``; the spans
    # between consecutive pairs are USER content (e.g. a hand-added ignore that landed between two
    # duplicated rig blocks) and MUST be preserved.
    markers = sorted(
        [(b[0], b[1], "begin") for b in begins] + [(e[0], e[1], "end") for e in ends]
    )
    pairs: list[tuple[int, int]] = []  # (region_start, region_end) per managed block
    expect = "begin"
    pending_start = -1
    for start, line_end, kind in markers:
        if kind != expect:
            return GlobalExcludesResolution(
                path, "conflict", desired,
                detail=f"{path} has misordered/nested rig-managed markers — reconcile by hand, then re-run",
            )
        if kind == "begin":
            pending_start = start
            expect = "end"
        else:
            pairs.append((pending_start, line_end))
            expect = "begin"

    # Already exactly one correct block? (the common steady state — a true no-op).
    if len(pairs) == 1 and content[pairs[0][0] : pairs[0][1]] == desired:
        return GlobalExcludesResolution(path, "ok", desired)

    # Splice OUT every managed-block region (preserving all USER content outside the markers,
    # including any text BETWEEN duplicated blocks), then re-insert ONE correct block where the
    # FIRST block sat. Build the result left-to-right so each non-managed span is copied verbatim.
    out_parts: list[str] = []
    cursor = 0
    for idx, (r_start, r_end) in enumerate(pairs):
        out_parts.append(content[cursor:r_start])  # user content before this block, verbatim
        if idx == 0:
            out_parts.append(desired)  # the single canonical block replaces the first one
        else:
            # A removed duplicate block leaves a seam: if both the text before and after it end/
            # start with a newline we'd otherwise create a doubled blank line. Drop one leading
            # newline of the following span so collapsing N blocks doesn't accrete blank lines.
            if content[r_end : r_end + 1] == "\n":
                r_end += 1
        cursor = r_end
    out_parts.append(content[cursor:])  # trailing user content, verbatim
    new_content = "".join(out_parts)
    return GlobalExcludesResolution(path, "update", desired, new_content=new_content)


def _find_marker_lines(content: str, marker: str) -> list[tuple[int, int]]:
    """Return ``(line_start_offset, line_end_offset)`` for each line equal to ``marker``.

    ``line_end_offset`` is the offset of the newline that terminates the line (or ``len(content)``
    for an un-terminated final line) — so a splice on ``[start, end)`` drops the marker line's text
    but not its line ending, letting the caller re-emit a clean block. A line MATCHES when its text
    with surrounding whitespace stripped equals ``marker`` (tolerant of a marker line that picked up
    trailing spaces), so such a line is still recognized and normalized on the next apply.
    """
    out: list[tuple[int, int]] = []
    pos = 0
    n = len(content)
    while pos <= n:
        nl = content.find("\n", pos)
        line_end = n if nl == -1 else nl
        if content[pos:line_end].strip() == marker:
            out.append((pos, line_end))
        if nl == -1:
            break
        pos = nl + 1
    return out


def _resolve_excludes_target(action: Action) -> tuple[Path, bool, str | None]:
    """Resolve WHICH file holds the managed block, and whether ``core.excludesfile`` must be set.

    Honors the user's existing choice: if ``core.excludesfile`` is ALREADY set (the common case —
    e.g. ``~/.gitignore``), the block goes in THAT file and git config is left alone. If it is NOT
    set, rig points ``core.excludesfile`` at the XDG default (``~/.config/git/ignore``) and writes
    the block there — so on a clean machine ``rig init`` does everything itself. An explicit
    ``gitignore.excludesfile`` override in config forces a specific file (and rig sets
    ``core.excludesfile`` to it when git's value doesn't already match).

    Returns ``(target_path, needs_set, set_value)``:
      - ``target_path`` — the resolved, ``~``/``$XDG``-expanded file to reconcile the block in.
      - ``needs_set``   — True when ``core.excludesfile`` must be written (unset, or override
                          differs from the current value).
      - ``set_value``   — the value to write into ``core.excludesfile`` (the un-expanded, portable
                          form so git stores ``~/.config/git/ignore``, not a machine path); None
                          when ``needs_set`` is False.

    The git-config READ goes through ``_git_global`` (the same seam the dispatcher uses), so tests
    monkeypatch one function and never run real ``git config --global``.
    """
    current = _git_global("core.excludesfile")
    override = action.options.get("excludesfile")
    xdg_default = action.options.get("xdg_default") or "~/.config/git/ignore"
    if isinstance(override, str) and override:
        # explicit override: reconcile in this file; set core.excludesfile when git doesn't match.
        target = _expand_user_path(override)
        needs_set = current != override
        return target, needs_set, (override if needs_set else None)
    if current:
        # respect the user's existing choice — manage the block in their file, touch no git config.
        return _expand_user_path(current), False, None
    # unset: point git at the XDG default AND write the block there (clean-machine path).
    return _expand_user_path(xdg_default), True, xdg_default


def _expand_user_path(path_str: str) -> Path:
    """Expand ``~`` and ``$XDG_CONFIG_HOME`` (for the ``~/.config`` prefix) to a concrete path.

    SYNC: this is the ``~/.config`` → ``$XDG_CONFIG_HOME`` mapping from ``plan._expand`` (keep the
    two in step). It is duplicated rather than imported because ``plan._expand`` ALSO anchors a
    relative remainder at the repo root — meaningless for a GLOBAL excludes path (which is always
    absolute after ``~`` expansion) and would couple this global action to a repo root it does not
    have. Matching git's XDG-aware read location matters when a test/machine points
    ``$XDG_CONFIG_HOME`` somewhere non-default.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and (path_str == "~/.config" or path_str.startswith("~/.config/")):
        path_str = xdg + path_str[len("~/.config"):]
    return Path(os.path.expanduser(os.path.expandvars(path_str)))


def _do_provision_global_excludes(action: Action, on_conflict: str) -> ActionResult:
    """Provision/reconcile rig's managed block in the GLOBAL git ``core.excludesfile``.

    Two coupled steps, in order:
      1. Resolve the target file from ``core.excludesfile`` (honor an existing value; set it to the
         XDG default when unset — so a clean machine is fully provisioned by ``rig init`` alone).
      2. Reconcile the marker block via the shared :func:`resolve_global_excludes` ``state`` — apply
         and drift read the same classification, so ``status`` never misreports the on-disk state.

    Idempotent: a correct single block with ``core.excludesfile`` already set is a true no-op; a
    missing block is appended (creating the file if absent); a drifted OR DUPLICATED managed region
    is collapsed IN PLACE to one correct block, preserving every other line verbatim. An unbalanced
    marker pair is a ``conflict`` rig leaves untouched (``skipped``), and an unreadable path is an
    ``error`` (never a silent skip). There is no backup: rig only ever edits its OWN fenced lines
    plus a git-config setting, so ``on_conflict`` is irrelevant here (consistent with the dispatcher
    and the surgical hook-bridge upsert).
    """
    entries = [str(e) for e in action.options.get("entries", [])]
    target, needs_set, set_value = _resolve_excludes_target(action)

    notes: list[str] = []
    cfg_status = "skipped"
    # Step 1: wire core.excludesfile when it is unset / doesn't match the override.
    if needs_set and set_value is not None:
        rc = _set_git_global("core.excludesfile", set_value)
        if rc == 0:
            notes.append(f"core.excludesfile → {set_value}")
            cfg_status = "created"
        else:
            return ActionResult(action, "error", "gitignore: failed to set global core.excludesfile")

    # Step 2: reconcile the managed block in the resolved file.
    r = resolve_global_excludes(target, entries)
    if r.state == "ok":
        if cfg_status == "created":
            # rare: git config was unset but the file already had the exact block — config write is
            # itself a change, so report it (not a silent no-op).
            return ActionResult(action, "created", f"gitignore: {'; '.join(notes)} (block already correct in {target})")
        return ActionResult(action, "skipped", f"gitignore: managed block already correct in {target}")
    if r.state == "conflict":
        return ActionResult(action, "skipped", f"gitignore: {r.detail}")
    if r.state == "io_error":
        return ActionResult(action, "error", f"gitignore: {r.detail}")
    if r.new_content is None:  # defensive — create/update always carry content
        return ActionResult(action, "error", f"gitignore: unhandled state {r.state!r}")
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the bytes we computed (which may carry the user's CRLF outside the block) are
    # written verbatim, with no platform newline translation.
    with target.open("w", encoding="utf-8", newline="") as fh:
        fh.write(r.new_content)
    n = len(entries)
    plural = "entry" if n == 1 else "entries"
    if r.state == "create":
        verb = "added block to" if existed else "created"
        block_note = f"{verb} {target} ({n} {plural})"
    else:
        block_note = f"updated managed block in {target}"
    notes.append(block_note)
    return ActionResult(action, "created" if r.state == "create" else "updated", f"gitignore: {'; '.join(notes)}")


_HANDLERS: dict[str, Callable[[Action, str], ActionResult]] = {
    "copy_skill": _do_copy_skill,
    "link_skill_harness": _do_link_skill_harness,
    "install_agent_hook": _do_install_agent_hook,
    "install_dispatcher": _do_install_dispatcher,
    "install_ci": _do_install_ci,
    "register_mcp": _do_register_mcp,
    "apply_harness": _do_apply_harness,
    "provision_permissions": _do_provision_permissions,
    "register_hook_bridge": _do_register_hook_bridge,
    "provision_schedule": _do_provision_schedule,
    "provision_agents_symlink": _do_provision_agents_symlink,
    "provision_ship_delegator": _do_provision_ship_delegator,
    "provision_linter_config": _do_provision_linter_config,
    "provision_project_tool": _do_provision_project_tool,
    "provision_github_ruleset": _do_provision_github_ruleset,
    "provision_github_merge": _do_provision_github_merge,
    "provision_github_ghas": _do_provision_github_ghas,
    "provision_github_actions": _do_provision_github_actions,
    "provision_github_browser": _do_provision_github_browser,
    "provision_tmux": _do_provision_tmux,
    "provision_global_excludes": _do_provision_global_excludes,
    "provision_tools": _do_provision_tools,
    "provision_tg_ctl": _do_provision_tg_ctl,
}
