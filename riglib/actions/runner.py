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
    "install_agent_hook": _do_install_agent_hook,
    "install_dispatcher": _do_install_dispatcher,
    "install_ci": _do_install_ci,
    "register_mcp": _do_register_mcp,
    "apply_harness": _do_apply_harness,
}
