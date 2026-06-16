"""rig CLI — argparse + subcommand dispatch only.

The thin entry point (``[project.scripts] rig = "riglib.cli:main"`` and the target of the
``bin/rig`` shim). It owns argument parsing and dispatch; all behavior lives in the sibling
modules. Heavy/optional imports (textual TUI, yaml) are done lazily inside the handler that
needs them so ``rig --help`` and ``rig doctor`` stay fast and dependency-light.

Subcommands:

    rig init     first-run onboarding — scaffold rig.yaml + wire the catalog in (the front door)
    rig apply    declarative reconcile: read rig.yaml, converge disk to it (idempotent)
    rig status   detect + report drift in BOTH directions (config↔disk)
    rig doctor   detect + (offer to) install required/optional dependencies
    rig export   serialize default/current config to rig.yaml without a TUI
    rig stats    tool-adoption analytics over agent-harness session logs (sub: `show`)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

# ── tiny output helpers (no color dep; honor NO_COLOR) ───────────────────────────
import os as _os

_USE_COLOR = sys.stdout.isatty() and not _os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _ok(s: str) -> str:
    return _c("32", s)


def _warn(s: str) -> str:
    return _c("33", s)


def _err(s: str) -> str:
    return _c("31", s)


def _dim(s: str) -> str:
    return _c("2", s)


def _bold(s: str) -> str:
    return _c("1", s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rig",
        description="rig — the dev-environment umbrella driver. Set up a repo from a "
        "committed rig.yaml by applying agent-tools content (skills, hooks, CI gates, MCP).",
    )
    p.add_argument("--version", action="version", version=f"rig {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def _add_setup_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")
        parser.add_argument("--config", help="apply this config file headlessly (non-interactive)")
        parser.add_argument("--yes", action="store_true", help="non-interactive; assume yes")
        # NOTE: there is intentionally NO --no-write-config flag. rig.yaml is the committed
        # source of truth and is NOT optional (AGENTS.md). Use --dry-run for a no-write preview.
        parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

    # `init` is the canonical first-run onboarding command (the front door). init/apply are
    # the two real commands; interactivity (TUI/semi/--yes) is orthogonal to both.
    ip = sub.add_parser("init", help="first-run onboarding: scaffold rig.yaml + wire the catalog in (the front door)")
    _add_setup_args(ip)

    ap = sub.add_parser("apply", help="reconcile the repo to rig.yaml (idempotent)")
    ap.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ap.add_argument("--config", help="config file to apply (default: ./rig.yaml + global)")
    ap.add_argument("--dry-run", action="store_true", help="print the resolved plan, write nothing")
    ap.add_argument("--only", help="comma-separated categories to scope (e.g. skills,ci)")

    st = sub.add_parser("status", help="report drift between rig.yaml and disk (both ways)")
    st.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    st.add_argument("--config", help="config file (default: ./rig.yaml + global)")

    dp = sub.add_parser("doctor", help="detect + (offer to) install dependencies")
    dp.add_argument("--yes", action="store_true", help="install missing deps non-interactively")
    dp.add_argument("--optional", action="store_true", help="also install optional deps")

    ep = sub.add_parser("export", help="write a rig.yaml from default/current config")
    ep.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ep.add_argument("-o", "--output", default="rig.yaml", help="output path (default: rig.yaml)")
    ep.add_argument("--force", action="store_true", help="overwrite an existing file")

    sub.add_parser("install-skill", help="register the rig agent skill with harnesses")

    _add_stats_parser(sub)

    return p


def _add_stats_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig stats <action>` — analytics over agent-harness session logs.

    Nested sub-actions (currently just `show`) so the surface has room to grow
    (`rig stats export`, `rig stats compare`, …) without reshaping the top-level CLI.
    """
    sp = sub.add_parser("stats", help="tool-adoption analytics over agent-harness session logs")
    # stash the subparser so `cmd_stats` can print ITS help on a bare `rig stats` (one source
    # of truth for the usage text — no hand-retyped duplicate).
    sp.set_defaults(_stats_parser=sp)
    sact = sp.add_subparsers(dest="stats_action", metavar="<action>")

    show = sact.add_parser("show", help="report how often each tool is invoked, by category/repo/harness")
    show.add_argument(
        "--harness", action="append", metavar="NAME",
        # kept generic (not an exhaustive list) so it can't drift as parsers are added; the
        # authoritative set is data-driven from the registered sources at run time.
        help="limit to a harness (repeatable), e.g. claude-code / codex / gemini / opencode",
    )
    show.add_argument(
        "--repo", action="append", metavar="PATH",
        help="limit to a repo/cwd absolute path (repeatable)",
    )
    show.add_argument("--since", metavar="YYYY-MM-DD", help="only invocations on/after this date")
    show.add_argument("--until", metavar="YYYY-MM-DD", help="only invocations on/before this date")
    show.add_argument(
        "--format", choices=("json", "tui", "web"), default="tui",
        help="output mode (default: tui — rich terminal UI)",
    )
    show.add_argument("--web-port", type=int, default=0, help="port for --format web (default: auto)")
    show.add_argument(
        "--baseline", action="store_true",
        help="narrow the report to the baseline-vs-ours buckets (drop external/other noise)",
    )
    # hidden seam: tests/scripts can point the whole pipeline at a sandbox HOME.
    show.add_argument("--home", help=argparse.SUPPRESS)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "init": cmd_setup,  # init = the canonical onboarding command (the front door)
        "apply": cmd_apply,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "export": cmd_export,
        "install-skill": cmd_install_skill,
        "stats": cmd_stats,
    }
    return handlers[args.command](args)


# ── shared helpers ────────────────────────────────────────────────────────────────
def _load_plan(cwd: str, config: str | None, project_type_override: str | None = None):
    """Load config + catalog + build a plan. Returns (plan, loaded, env)."""
    from .catalog import Catalog
    from .config import load
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(Path(cwd).resolve())
    explicit = None
    if config:
        # a relative --config is relative to the detected git root (where rig.yaml lives),
        # so the command works the same from the root or any subdirectory.
        cp = Path(config)
        explicit = (cp if cp.is_absolute() else env.repo_root / cp).resolve()
    loaded = load(env.repo_root, explicit_config=explicit)
    catalog = Catalog.scan(loaded.agent_tools_source)
    ptype = project_type_override or env.project_type
    plan = build(loaded, catalog, project_type=ptype)
    return plan, loaded, env


def _print_plan(plan) -> None:
    print(_bold(f"\nPlan: {len(plan)} action(s)  [on_conflict={plan.on_conflict}]"))
    for a in plan.actions:
        print(f"  {_dim('•')} {a.category}/{a.item} → {a.target}")
    for note in plan.notes:
        print(f"  {_warn('note')}: {note}")


def _print_results(report) -> None:
    icon = {
        "created": _ok("✔"),
        "updated": _ok("✔"),
        "backed_up": _warn("↩"),
        "skipped": _dim("·"),
        "planned": _dim("•"),
        "error": _err("✗"),
    }
    for r in report.results:
        print(f"  {icon.get(r.status, '?')} {r.action.category}/{r.action.item}: {r.detail}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    print(_bold(f"\nSummary: {summary}"))


# ── commands ──────────────────────────────────────────────────────────────────────
def cmd_setup(args: argparse.Namespace) -> int:
    from .config import ConfigError

    # --dry-run is a headless preview ("print the plan, write nothing") — it must never
    # launch the interactive wizard, even when textual is installed.
    interactive = not (args.config or args.yes or args.dry_run)
    if interactive:
        # `.tui` re-exports run_wizard but imports textual ONLY inside the call, so this
        # import is cheap and stdlib-safe; the textual ImportError surfaces on invocation.
        from .tui import run_wizard

        try:
            return run_wizard(Path(args.cwd).resolve())
        except ImportError:
            print(
                _warn("textual not installed — falling back to a non-interactive default setup.\n")
                + _dim("  Install the wizard with: pip install 'rig-cli[tui]'\n")
                + _dim("  Or run headless: rig init --yes  /  rig init --config rig.yaml --yes")
            )
            return _setup_headless(args, use_default=True)

    return _setup_headless(args, use_default=args.config is None)


def _setup_headless(args: argparse.Namespace, *, use_default: bool) -> int:
    from .actions import run_plan
    from .catalog import Catalog, CatalogError
    from .config import ConfigError, LoadedConfig, load, validate
    from .detect import detect_environment
    from .plan import PlanError, build
    from .state import SetupState

    # detect_environment resolves repo_root to the git top-level; use THAT consistently for
    # config load, rig.yaml writes, and install targets, so setup and later apply/status
    # (which also resolve to the git root) operate on the same paths and reproduce.
    env = detect_environment(Path(args.cwd).resolve())
    repo_root = env.repo_root
    repo_yaml = repo_root / "rig.yaml"

    # describes how to persist rig.yaml AFTER the plan is proven valid (fail-closed: never
    # leave a new/invalid committed config behind a failed setup).
    pending_write = None  # ("generated", SetupState) | ("copy", src_path) | None
    try:
        if use_default:
            # don't silently clobber an existing customized rig.yaml with the default one.
            if repo_yaml.is_file() and not args.dry_run:
                print(_err(f"error: {repo_yaml} already exists."))
                print(_dim("  run `rig apply` to apply it, or delete it to regenerate a default."))
                return 2
            # honor a global-config-pinned agent_tools_source for the scan (the documented
            # cascade has a valid global layer), but do NOT pin it into the committed
            # rig.yaml — that would break the env/default fallback on other machines.
            global_source = load(repo_root, include_global=True).agent_tools_source
            try:
                Catalog.scan(global_source)  # verify an agent-tools checkout exists (fail early)
            except CatalogError as exc:
                print(_err(f"error: {exc}"))
                return 2
            state = SetupState.default(
                agent_tools_source=None, project_type=env.project_type
            )
            # Build the plan from the GENERATED state, not from disk: with --dry-run there
            # may be no rig.yaml on disk yet, so loading from disk would preview an
            # empty/stale plan instead of what setup actually decided. Carry the global
            # source into the in-memory config so the catalog scan below resolves it.
            validate(state.data)
            data = dict(state.data)
            if global_source:
                data["agent_tools_source"] = global_source
            loaded = LoadedConfig(data=data, repo_root=repo_root)
            pending_write = ("generated", state)
        else:
            # a relative --config is relative to the -C repo root (mirrors `rig apply`).
            cp = Path(args.config)
            explicit = (cp if cp.is_absolute() else repo_root / cp).resolve()
            loaded = load(repo_root, explicit_config=explicit)
            # rig.yaml is committed-by-default: setting up from an external template must
            # leave the repo with its own rig.yaml. Copy it in unless it already IS it.
            if explicit != repo_yaml.resolve():
                pending_write = ("copy", explicit)
        catalog = Catalog.scan(loaded.agent_tools_source)
        plan = build(loaded, catalog, project_type=env.project_type)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err(f"error: {exc}"))
        return 2

    # plan is valid → now persist rig.yaml (the committed source of truth, never optional)
    if pending_write and not args.dry_run:
        kind, payload = pending_write
        if kind == "generated":
            # (the generated path already refused an existing rig.yaml above)
            written = payload.write(repo_yaml)
            print(_ok(f"wrote {written}"))
        else:
            import shutil
            import time

            repo_yaml.parent.mkdir(parents=True, exist_ok=True)
            # never silently discard an existing committed config — back it up first.
            if repo_yaml.is_file():
                bak = repo_yaml.with_name(f"rig.yaml.rig-bak-{time.strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(str(repo_yaml), str(bak))
                print(_warn(f"  backed up existing rig.yaml → {bak}"))
            shutil.copyfile(payload, repo_yaml)
            print(_ok(f"wrote {repo_yaml}  (from {payload})"))

    _print_plan(plan)
    if args.dry_run:
        print(_dim("\n(dry-run: nothing written)"))
        return 0
    report = run_plan(plan)
    _print_results(report)
    return 1 if report.errors else 0


def cmd_apply(args: argparse.Namespace) -> int:
    from .actions import run_plan
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    try:
        plan, loaded, env = _load_plan(args.cwd, args.config)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err(f"error: {exc}"))
        return 2

    # fail-closed: with NO config layer (no --config, no ./rig.yaml, no global), the empty
    # config resolves to built-in defaults and would mutate HOME with no committed source
    # of truth. Refuse — `rig init`/`rig export` create the rig.yaml first.
    if not loaded.layers:
        print(_err("error: no rig.yaml found (and no --config / global config)."))
        print(_dim("  run `rig init` (or `rig export -o rig.yaml`) to create one first."))
        return 2

    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        plan.actions = [a for a in plan.actions if a.category in wanted]

    _print_plan(plan)
    if args.dry_run:
        print(_dim("\n(dry-run: nothing written)"))
        return 0
    report = run_plan(plan)
    _print_results(report)
    return 1 if report.errors else 0


def cmd_status(args: argparse.Namespace) -> int:
    from .catalog import CatalogError
    from .config import ConfigError
    from .drift import detect
    from .plan import PlanError, resolve_category_target

    try:
        plan, loaded, env = _load_plan(args.cwd, args.config)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err(f"error: {exc}"))
        return 2

    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  stack: {env.stack}  type: {env.project_type}")
    cfg_src = ", ".join(loaded.layers) or "(none — built-in defaults)"
    print(f"  config layers: {cfg_src}")
    if loaded.repo_path is None:
        print(_warn("  warning: no rig.yaml in this repo (it should be committed)"))

    # scan the configured target dirs for extras even if no action targets them.
    # CI + MCP targets are REPO-LOCAL with clear ownership → scan even when the category is
    # disabled, so a previously-applied gate/server left on disk surfaces as disk→config
    # drift. Skills/agent-hooks live in SHARED global dirs (~/.agents/skills, ~/.claude/
    # hooks) where other tools' entries legitimately coexist, so only scan them when the
    # category is enabled (flagging every global skill as "extra" would be noise).
    scan_skill_dirs = []
    scan_ci_dirs = []
    scan_mcp_files = []
    scan_hook_dirs = []
    if loaded.category("skills").get("enabled") is not False:
        d = resolve_category_target(loaded, "skills")
        if d:
            scan_skill_dirs.append(d)
        h = resolve_category_target(loaded, "agent_hooks")
        if h and loaded.category("agent_hooks").get("enabled") is not False:
            scan_hook_dirs.append(h)
    d = resolve_category_target(loaded, "ci")  # CI: scan unconditionally (repo-local)
    if d:
        scan_ci_dirs.append(d)
    d = resolve_category_target(loaded, "mcp")  # MCP: scan unconditionally
    if d:
        scan_mcp_files.append(d if d.suffix == ".json" else d / "mcp.json")
    report = detect(
        plan,
        scan_skill_dirs=scan_skill_dirs,
        scan_ci_dirs=scan_ci_dirs,
        scan_mcp_files=scan_mcp_files,
        scan_hook_dirs=scan_hook_dirs,
    )
    # disabled-but-installed dispatcher: config turned the dispatcher off, but a prior apply
    # may have left core.hooksPath pointing at the installed composer dir. apply won't delete
    # it, so surface it as disk→config drift.
    disp_cfg = loaded.category("git_hooks").get("dispatcher", {}) if isinstance(loaded.category("git_hooks"), dict) else {}
    if isinstance(disp_cfg, dict) and not disp_cfg.get("enabled"):
        from .drift import check_disabled_dispatcher

        check_disabled_dispatcher(loaded.repo_root, report)
    # surface the model-freshness schedule explicitly (installed / drifted / not configured),
    # so `rig status` answers "is the daily checker cron there?" at a glance.
    _print_schedule_status(plan, report)

    if report.in_sync:
        print(_ok("\n  in sync — config and disk agree"))
        return 0

    missing = report.by_direction("missing") + report.by_direction("modified")
    extra = report.by_direction("extra")
    if missing:
        print(_warn(f"\n  config→disk drift ({len(missing)}) — declared but missing/modified:"))
        for d in missing:
            print(f"    {_warn('▸')} {d.category}/{d.item}: {d.detail}")
    if extra:
        print(_warn(f"\n  disk→config drift ({len(extra)}) — on disk, not declared:"))
        for d in extra:
            print(f"    {_warn('▸')} {d.category}/{d.item}: {d.detail}  [{d.target}]")
    print(_dim("\n  run `rig apply` to converge config→disk (extras are left for you to decide)"))
    return 3


def _print_schedule_status(plan, report) -> None:
    """Report the model-freshness daily schedule: installed / drifted / not configured."""
    sched_actions = [a for a in plan.actions if a.kind == "provision_schedule"]
    if not sched_actions:
        return
    action = sched_actions[0]
    opts = action.options
    when = f"{int(opts.get('hour', 12)):02d}:{int(opts.get('minute', 0)):02d}"
    platform = opts.get("platform", "?")
    drifted = [d for d in report.items if d.category == "models"]
    if drifted:
        state = _warn(f"drifted ({drifted[0].detail})")
    else:
        state = _ok("installed")
    print(f"\n  model-freshness schedule: {state}  "
          + _dim(f"(daily {when}, {platform}, '{opts.get('label', '')}')"))


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import bootstrap, diagnose

    report = diagnose()
    print(_bold(f"rig doctor — {report.os.pretty}") + _dim(f"  (pkg manager: {report.os.package_manager or 'none detected'})"))
    print()
    for st in report.statuses:
        tag = "required" if st.dep.required else "optional"
        if st.present:
            print(f"  {_ok('✔')} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
        else:
            mark = _err("✗") if st.dep.required else _warn("○")
            print(f"  {mark} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
            if st.install_cmd:
                print(f"      install: {_dim(' '.join(st.install_cmd))}")
            else:
                print(f"      {_dim('install: (no package mapping for this OS — install manually)')}")

    missing_req = report.missing_required
    if not missing_req and not (args.optional and report.missing_optional):
        print(_ok("\n  all required dependencies present"))
        return 0

    if not args.yes:
        print(_warn("\n  missing dependencies above. Re-run with --yes to install them"))
        print(_dim("  (add --optional to also install optional deps)"))
        return 1

    print(_bold("\n  installing missing dependencies..."))
    results = bootstrap(report, assume_yes=True, include_optional=args.optional)
    failed = [name for name, rc in results if rc not in (0,)]
    for name, rc in results:
        print(f"    {_ok('✔') if rc == 0 else _err('✗')} {name} (rc={rc})")
    return 1 if failed else 0


def cmd_export(args: argparse.Namespace) -> int:
    from .detect import detect_environment
    from .state import SetupState

    env = detect_environment(Path(args.cwd).resolve())
    repo_root = env.repo_root  # write/anchor at the git root, like setup/apply/status
    # resolve the output path against the repo root (relative -o is relative to -C/git root,
    # not cwd) BEFORE the collision check, so the check and the write target the same file.
    out_raw = Path(args.output)
    out = out_raw if out_raw.is_absolute() else repo_root / out_raw
    if out.exists() and not args.force:
        print(_err(f"error: {out} exists (use --force to overwrite)"))
        return 2
    # exported rig.yaml stays portable: do NOT pin an auto-detected absolute source.
    state = SetupState.default(agent_tools_source=None, project_type=env.project_type)
    written = state.write(out)
    print(_ok(f"wrote {written}"))
    print(_dim("  edit it, then: rig apply"))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


def cmd_stats(args: argparse.Namespace) -> int:
    # `rig stats` with no action → print the stats subparser's own help (mirrors top-level
    # `rig` behaviour; no hand-retyped usage to drift from the real flags).
    if getattr(args, "stats_action", None) != "show":
        parser = getattr(args, "_stats_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
    from .stats import run as stats_run

    return stats_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
