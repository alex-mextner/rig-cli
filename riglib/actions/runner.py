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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


_HANDLERS: dict[str, Callable[[Action, str], ActionResult]] = {
    "copy_skill": _do_copy_skill,
    "link_skill_harness": _do_link_skill_harness,
    "install_agent_hook": _do_install_agent_hook,
    "install_dispatcher": _do_install_dispatcher,
    "install_ci": _do_install_ci,
    "register_mcp": _do_register_mcp,
    "apply_harness": _do_apply_harness,
    "provision_schedule": _do_provision_schedule,
}
