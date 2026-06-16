"""The plan executor — runs each :class:`~riglib.plan.Action`, returns results.

Stdlib only. Each ``_do_<kind>`` handler implements one action kind and returns an
:class:`ActionResult`. Non-fatal errors are collected (the runner continues); the caller
decides how to surface them.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..github_ruleset import (
    build_ruleset_body,
    find_managed_ruleset,
    normalize_ruleset,
    parse_github_remote,
)
from ..logging import log_event
from ..plan import Action, InstallPlan
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
def _do_copy_skill(action: Action, on_conflict: str) -> ActionResult:
    out = fsutil.copy_tree(action.source, action.target, on_conflict)
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
    entry = parse_mcp_command(command)

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
    )


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
    # attach-or-create entry when enabled. `managed_scripts()` is the ONE source apply and
    # drift share, so they can't diverge on which scripts exist.
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
    if plan.boot_enabled and sys.platform == "darwin":
        plan.boot_plist_path.parent.mkdir(parents=True, exist_ok=True)
        boot_out = fsutil.write_file(plan.boot_plist_path, plan.render_boot_plist(), on_conflict)
        if boot_out.status == "error":
            return ActionResult(action, "error", f"tmux: {boot_out.detail}")
        if boot_out.backup:
            extra_backups.append(boot_out.backup)
        if boot_out.status != "skipped":
            changed = True
            details.append(f"wrote boot plist {plan.boot_plist_path.name} (load on next login/reboot)")
        elif not boot_out.detail.startswith("identical"):
            # rig OWNS the boot plist — a DIFFERING one left untouched under skip is stale.
            skipped_conflicts.append(
                f"{plan.boot_plist_path.name} differs and on_conflict=skip — NOT updated "
                f"(re-run with backup/overwrite to refresh the boot plist)"
            )

    # 4) ~/.tmux.conf — migrate (back up an inline-settings original) then wire the managed region.
    conf = plan.conf_path
    existing = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    # one-time migration backup: only when the original carried rig-owned settings inline AND we
    # haven't already kept a backup (never clobber the true original).
    if existing and has_inline_rig_settings(existing) and not plan.backup_path.exists():
        plan.backup_path.write_text(existing, encoding="utf-8")
        backup = plan.backup_path
        details.append(f"backed up original → {plan.backup_path.name}")
        changed = True

    desired_conf = _tmux_conf_with_managed(
        plan, existing, splice_managed_block, neutralize_inline_rig_lines
    )
    if desired_conf != existing:
        # Honor on_conflict=skip for the user's OWN file: if ~/.tmux.conf already exists and
        # differs, `skip` means leave it untouched (consistent with the generated artifacts,
        # which go through fsutil.write_file's skip path). A non-existent conf is always created
        # (there's nothing to conflict with). backup/overwrite both proceed to write.
        if on_conflict == "skip" and existing:
            details.append(f"~/.tmux.conf differs but on_conflict=skip — left unwired ({conf.name})")
        else:
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(desired_conf, encoding="utf-8")
            changed = True
            details.append(f"wired {plan.apply_mode} into {conf.name}")

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


def _tmux_conf_with_managed(plan, existing: str, splice, neutralize) -> str:
    """The desired ``~/.tmux.conf`` text for the plan's apply mode (pure).

    - import mode: neutralize the inline rig-owned lines (so the sourced rig config is
      authoritative), then ensure the single ``source-file`` import is present exactly once at
      the end (drop a prior copy so a moved generated path doesn't leave a stale import).
    - block mode: splice the generated body between the managed sentinels (conda-init style).
    """
    if plan.apply_mode == "block":
        return splice(existing, plan.render_rig_conf())
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
    except OSError:
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
    classification. No github remote → ``skipped`` (never an error). ``RIG_GH_DRY_RUN``
    computes the create/update but skips the POST/PUT, returning what WOULD change. ``gh``
    failures (missing binary, not authed) surface as an ``error`` result with the detail.
    """
    state, info = github_ruleset_state(action)
    if state == "no_remote":
        return ActionResult(action, "skipped", "github-ruleset: no github origin remote — nothing to provision")
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
            return ActionResult(action, "error", f"github-ruleset: create failed: {err.strip() or out.strip()}")
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
        return ActionResult(action, "error", f"github-ruleset: update failed: {err.strip() or out.strip()}")
    return ActionResult(action, "updated", f"github-ruleset: updated '{name}' (id={rs_id}) on {owner}/{repo}")


_HANDLERS: dict[str, Callable[[Action, str], ActionResult]] = {
    "copy_skill": _do_copy_skill,
    "link_skill_harness": _do_link_skill_harness,
    "install_agent_hook": _do_install_agent_hook,
    "install_dispatcher": _do_install_dispatcher,
    "install_ci": _do_install_ci,
    "register_mcp": _do_register_mcp,
    "apply_harness": _do_apply_harness,
    "register_hook_bridge": _do_register_hook_bridge,
    "provision_schedule": _do_provision_schedule,
    "provision_agents_symlink": _do_provision_agents_symlink,
    "provision_github_ruleset": _do_provision_github_ruleset,
    "provision_tmux": _do_provision_tmux,
}
