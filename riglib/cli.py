"""rig CLI — argparse + subcommand dispatch only.

The thin entry point (``[project.scripts] rig = "riglib.cli:main"`` and the target of the
``bin/rig`` shim). It owns argument parsing and dispatch; all behavior lives in the sibling
modules. Heavy/optional imports (textual TUI, yaml) are done lazily inside the handler that
needs them so ``rig --help`` and ``rig doctor`` stay fast and dependency-light.

Subcommands:

    rig init     first-run onboarding — scaffold rig.yaml + wire the catalog in (the front door)
    rig apply    declarative reconcile: read rig.yaml, converge disk to it (idempotent)
    rig setup    interactive config wizard (no TTY → usage for init/apply/config)
    rig config   get/set ONE config key by dot path, then reconcile (get|set)
    rig status   detect + report drift in BOTH directions (config↔disk)
    rig doctor   detect + (offer to) install required/optional dependencies
    rig export   serialize default/current config to rig.yaml without a TUI
    rig stats    tool-adoption analytics over agent-harness session logs (sub: `show`)
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from . import __version__
from .layers import GLOBAL as _GLOBAL
from .layers import REPO as _REPO
from .layers import layer_for_category as _layer_for_category

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


def _fmt_scalar(value: object) -> str:
    """Render a coerced scalar in YAML/CLI casing (true/false/null), not Python's repr.

    Shared by `config get` (the printed value) and `config set` (the confirmation line) so a
    bool a user typed as ``false`` never echoes back as Python's ``False``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


# The GLOBAL/REPO layer headings, shared by the area summary and the per-item drift dump so the
# two renderers can never disagree on the wording.
_LAYER_HEADERS = {
    _GLOBAL: "GLOBAL — machine-wide (from ~/.config/rig/config.yaml)",
    _REPO: "REPO — this repository (from ./rig.yaml)",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rig",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="rig — the dev-environment umbrella driver. Set up a repo from a "
        "committed rig.yaml by applying agent-tools content (skills, hooks, CI gates, MCP).",
        # Exit codes are a PUBLIC CONTRACT (scripts/CI branch on them). Documented here per the
        # structured-exit-codes skill; the constants live in riglib/errors.py.
        epilog=(
            "exit codes:\n"
            "  0    success\n"
            "  1    internal error (an unexpected failure / bug)\n"
            "  2    invalid config (a malformed value, type, or unknown key)\n"
            "  3    drift (rig status found config↔disk drift)\n"
            "  4    unknown item (config names a catalog item that doesn't exist / was removed)\n"
            "  5    missing target (config references a path/binary that's gone on disk)\n"
            "  6    not a git repository (a repo-scoped command run outside a repo)\n"
            "  127  missing dependency (a required external tool isn't installed)\n"
            "\n"
            "  precedence: when `rig status` finds BOTH a missing target and config↔disk drift,\n"
            "  it prints both but exits 5 (missing-target outranks drift — the dead reference\n"
            "  fails at runtime, so it's the more urgent class).\n"
        ),
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

    ap = sub.add_parser(
        "apply",
        help="reconcile the repo to rig.yaml (idempotent)",
        description="Reconcile disk to rig.yaml (idempotent). apply only ADDS/UPDATES the "
        "items your config declares — it NEVER deletes on-disk extras. Items present on disk "
        "but not declared in any layer (e.g. a hand-added skill) are reported by `rig status` "
        "and left untouched; apply will not nuke them.",
    )
    ap.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ap.add_argument("--config", help="config file to apply (default: ./rig.yaml + global)")
    ap.add_argument("--dry-run", action="store_true", help="print the resolved plan, write nothing")
    ap.add_argument("--only", help="comma-separated categories to scope (e.g. skills,ci)")

    st = sub.add_parser(
        "status",
        help="report drift between config and disk, grouped by layer and managed area",
        description="Report drift between config and disk. In a git repo, status shows GLOBAL "
        "machine-wide areas and the REPO layer from ./rig.yaml; outside a git repo, it ignores "
        "auto-discovered ./rig.yaml, reports only GLOBAL areas, and marks the repo layer / "
        "rig.yaml as N/A.",
    )
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

    # `setup` IS the interactive wizard (not an alias for init/apply): in a TTY it shows what is
    # enabled across every reconciled area, edits the local rig.yaml AND the global config, then
    # applies; with no TTY it prints USAGE for init/apply/config (it never runs a half-wizard).
    sp = sub.add_parser(
        "setup",
        help="interactive config wizard (no TTY → prints usage for init/apply/config)",
        description="Interactive configuration wizard. In a terminal: show what is enabled across "
        "all reconciled areas, change options in the local rig.yaml AND the global "
        "~/.config/rig/config.yaml (each option carries an inline hint), then apply. "
        "Non-interactive (piped/no TTY): print usage for init/apply/config get|set.",
    )
    sp.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")

    _add_config_parser(sub)

    _add_stats_parser(sub)

    return p


def _add_config_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig config get|set <dot.path>` — read/edit ONE nested key, then reconcile.

    The recommended way to change a single setting without hand-editing YAML. `get` reads a
    nested key by dotted path from the single target file (./rig.yaml, or the global config with
    --global) — NOT the cascade; `set` coerces the value conservatively, writes it, and then
    runs the SAME apply engine `rig apply` uses so the change takes effect immediately (with a
    full rollback if the write or the catalog-backed plan build fails). --global targets
    ~/.config/rig/config.yaml; --json (get) emits the JSON value; --no-apply (set) writes the
    key and prints the plan only. The dot-path engine lives in riglib.config.
    """
    cp = sub.add_parser("config", help="get/set a single config key (dot path), then reconcile")
    cp.set_defaults(_config_parser=cp)
    csub = cp.add_subparsers(dest="config_command", metavar="<get|set>")

    def _add_config_target_args(parser: argparse.ArgumentParser) -> None:
        # -C/--cwd and --global are identical on get and set; declare them once so the two
        # never drift apart (same help text, same dest).
        parser.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
        parser.add_argument(
            "--global", dest="is_global", action="store_true",
            help="target the global config (~/.config/rig/config.yaml) instead of ./rig.yaml",
        )

    cg = csub.add_parser("get", help="read a nested config key (e.g. harness.auto_mode)")
    cg.add_argument("path", help="dot path into the config tree (e.g. ci.items.secret-scan.tier)")
    _add_config_target_args(cg)
    cg.add_argument("--json", action="store_true", help="emit the value as JSON")

    cs = csub.add_parser("set", help="write a nested config key, then reconcile (apply)")
    cs.add_argument("path", help="dot path into the config tree (e.g. harness.auto_mode)")
    cs.add_argument("value", help="value to set (coerced: true/false/int/float/null, else string)")
    _add_config_target_args(cs)
    cs.add_argument("--no-apply", action="store_true",
                    help="write the key but skip the reconcile (print the resulting plan only)")


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
        "config": cmd_config,
        "install-skill": cmd_install_skill,
        "setup": cmd_setup_wizard,  # setup = the interactive config wizard (distinct from init)
        "stats": cmd_stats,
    }
    # The single top-level error handler (error-system v2): any structured RigError a command
    # raises is rendered as the consistent what/why/fix block + its stable per-class exit code.
    # A non-RigError (a real bug) propagates so its traceback is visible.
    from . import errors

    return errors.guard(lambda: handlers[args.command](args))


# ── shared helpers ────────────────────────────────────────────────────────────────
def _load_plan(
    cwd: str,
    config: str | None,
    project_type_override: str | None = None,
    *,
    allow_repo_autodiscovery_in_non_git: bool = True,
):
    """Load config + catalog + build a plan. Returns (plan, loaded, env)."""
    from .catalog import Catalog
    from .config import load
    from .detect import detect_environment
    from .plan import build

    env = detect_environment(Path(cwd).resolve())
    explicit = _resolve_explicit_config(env, config)
    include_repo = _include_repo_config(env, explicit, allow_repo_autodiscovery_in_non_git)
    loaded = load(env.repo_root, explicit_config=explicit, include_repo=include_repo)
    catalog = Catalog.scan(loaded.agent_tools_source)
    ptype = project_type_override or env.project_type
    plan = build(loaded, catalog, project_type=ptype)
    return plan, loaded, env


def _resolve_explicit_config(env, config: str | None) -> Path | None:
    """Resolve ``--config`` the same way for every command path.

    A relative ``--config`` is relative to the detected git root, so the command works the
    same from the root or any subdirectory.
    """
    if not config:
        return None
    cp = Path(config)
    return (cp if cp.is_absolute() else env.repo_root / cp).resolve()


def _include_repo_config(env, explicit: Path | None, allow_repo_autodiscovery_in_non_git: bool) -> bool:
    """Whether this command context should load the repo/config layer."""
    if env.is_git_repo:
        return True
    if explicit is not None:
        return True
    return allow_repo_autodiscovery_in_non_git


def _is_global_action(action) -> bool:
    """Whether an install action belongs to the GLOBAL status layer."""
    return _layer_for_category(action.category) == _GLOBAL


def _validate_layer_in_isolation(layer_path: Path) -> None:
    """Build a plan over ONE config file alone (no cascade), so a catalog-backed value the file
    DECLARES can't be masked by a layer that merges over it.

    A `rig config set … --global` edit is otherwise only validated by the merged cascade
    (`_load_plan`), where a repo's `rig.yaml` overriding the just-broken global key (e.g. a bad
    `agent_tools_source`, or a global catalog item ref) hides the breakage — the write persists a
    config that fails in every OTHER repo. Loading the file as the sole explicit layer with
    `include_global=False` (its own directory has no `rig.yaml` to overlay) surfaces those errors
    against the file itself. Raises ConfigError / CatalogError / PlanError on rejection.

    Scope: only what the file ITSELF declares. A global file legitimately omits
    `agent_tools_source` (the checkout is supplied per-repo or via env), so we only run the
    catalog-backed plan build when this file pins its own `agent_tools_source` — there is nothing
    global-specific to catalog-validate otherwise, and demanding an independently-resolvable
    checkout would reject a valid deferring global config. Schema (`config.load`) still runs either
    way.
    """
    from .catalog import Catalog
    from .config import load
    from .plan import build

    loaded = load(layer_path.parent, explicit_config=layer_path, include_global=False)
    if loaded.agent_tools_source is None:
        return  # no own catalog coordinate to mask — schema validation already ran in load()
    catalog = Catalog.scan(loaded.agent_tools_source)
    build(loaded, catalog)


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


def _scan_missing_targets() -> list:
    """Scan the harness settings.json for hook commands pointing at files that are gone.

    Resolves the claude-code harness settings file under HOME (``~/.claude/settings.json``) and
    returns the missing-target findings. Kept a thin wrapper so cmd_status reads cleanly and a
    future multi-harness expansion has one place to add settings paths.
    """
    from .missing_target import scan_settings_hooks

    settings = Path(_os.path.expanduser("~/.claude/settings.json"))
    return scan_settings_hooks(settings)


def _print_non_git_note() -> None:
    """The one-line 'this is not a git repository' note shown under the status header.

    Single source for the wording so the normal status path and the catalog-failed fallback
    (``_print_non_git_status``) can never drift apart — the smoke greps for "not a git
    repository", and only one of two phrasings would be a regression waiting to happen.
    """
    print(_dim("  (not a git repository — repo layer / rig.yaml N/A here)"))


def _print_non_git_status(env, config: str | None = None) -> int:
    """Render a minimal `rig status` for a non-git dir when the catalog couldn't be resolved.

    The catalog scan feeds the REPO layer (what this repo would install); a non-git dir has no
    repo layer, so its failure is irrelevant here. We still owe the user the header + the
    explicit "not a git repository" note (never the "should be committed" nag). Returns 0 — a
    non-git dir is a valid place to ask "what's my status?", not an error.

    Config loading precedes the (failed) catalog scan, so the layer provenance is still
    reportable; we re-load it here rather than thread `loaded` through the exception path.
    """
    from .config import ConfigError, load

    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  stack: {env.stack}  type: {env.project_type}")
    layers = "(none — built-in defaults)"
    try:
        explicit = _resolve_explicit_config(env, config)
        loaded = load(
            env.repo_root,
            explicit_config=explicit,
            include_repo=_include_repo_config(
                env,
                explicit,
                allow_repo_autodiscovery_in_non_git=False,
            ),
        )
        layers = ", ".join(loaded.layers) or layers
    except ConfigError:
        pass  # a malformed config still shouldn't stop us reporting "not a git repository"
    print(f"  config layers: {layers}")
    _print_non_git_note()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import errors
    from .catalog import CatalogError
    from .config import ConfigError
    from .drift import detect
    from .plan import PlanError, resolve_category_target

    try:
        plan, loaded, env = _load_plan(
            args.cwd,
            args.config,
            allow_repo_autodiscovery_in_non_git=False,
        )
    except CatalogError as exc:
        # The catalog only matters for the REPO layer (what this repo would install). A non-git
        # dir (e.g. ~) has no repo layer at all, so an unresolved agent-tools checkout must NOT
        # mask the one thing status owes that user: "you're not in a git repository". Detecting
        # the env is independent of (and cheap relative to) the catalog scan that just failed.
        from .detect import detect_environment

        env = detect_environment(Path(args.cwd).resolve())
        if not env.is_git_repo:
            return _print_non_git_status(env, args.config)
        # A real repo with a broken/missing agent-tools source IS a genuine config failure.
        print(_err(f"error: {exc}"))
        return 2
    except (ConfigError, PlanError) as exc:
        print(_err(f"error: {exc}"))
        return 2

    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  stack: {env.stack}  type: {env.project_type}")
    cfg_src = ", ".join(loaded.layers) or "(none — built-in defaults)"
    print(f"  config layers: {cfg_src}")

    # The REPO layer only exists inside a git repository. In a non-git dir (e.g. ~) there is no
    # repo at all — do NOT nag "no rig.yaml, should be committed" (that advice applies only in a
    # real repo). Show that the repo layer is N/A and report the GLOBAL layer only.
    if not env.is_git_repo:
        _print_non_git_note()
        # plan.build still emits default-on repo actions from built-in defaults even when no
        # repo config file was loaded. Outside git those actions have no layer to report under,
        # so status must drop them before drift detection as well as from the summary.
        global_actions = []
        repo_actions = []
        for action in plan.actions:
            (global_actions if _is_global_action(action) else repo_actions).append(action)
        plan.actions = global_actions
        if repo_actions:
            print(_dim(
                "  (repo-scoped areas are N/A outside a git repository: "
                f"{len(repo_actions)} action(s) not evaluated)"
            ))
    elif loaded.repo_path is None:
        # A REAL repo with no committed rig.yaml: make the fix PROMINENT, not a one-liner
        # buried above a long drift dump.
        print()
        print(_warn(_bold("  ▸ no committed rig.yaml in this repository")))
        print(_warn("    rig.yaml is the committed source of truth for this repo's setup."))
        print(_ok("    fix: run `rig init` to create one"))

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
    # CI + MCP are REPO-LOCAL — only scan them when this IS a repo (a non-git dir has no repo
    # layer; scanning a cwd-relative .github there would be meaningless).
    if env.is_git_repo:
        d = resolve_category_target(loaded, "ci")  # CI: scan unconditionally (repo-local)
        if d:
            scan_ci_dirs.append(d)
    d = resolve_category_target(loaded, "mcp")  # MCP: scan unconditionally (global)
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
    # disabled-but-installed global-excludes block: config opted the gitignore category out, but a
    # prior apply may have left the rig-managed block in the global excludes file. apply won't
    # remove it, so surface it as disk→config drift (mirrors the disabled-dispatcher scan; this is
    # a GLOBAL, machine-wide artifact, not repo-local).
    gi_cfg = loaded.data.get("gitignore")
    if isinstance(gi_cfg, dict) and gi_cfg.get("enabled") is False:
        from .config import GITIGNORE_DEFAULT_EXCLUDESFILE
        from .drift import check_disabled_global_excludes
        from .plan import Action

        gi_opts: dict[str, object] = {"xdg_default": GITIGNORE_DEFAULT_EXCLUDESFILE}
        override = gi_cfg.get("excludesfile")
        if isinstance(override, str) and override:
            gi_opts["excludesfile"] = override
        check_disabled_global_excludes(
            Action(
                kind="provision_global_excludes",
                category="gitignore",
                item="block",
                source=loaded.repo_root,
                target=loaded.repo_root,
                options=gi_opts,
            ),
            report,
        )
    # AREA SUMMARY — the headline: every reconciled area (grouped by layer) with its in-sync vs
    # drift counts, so the user sees the FULL picture of what rig manages, not a skill-dominated
    # wall of drift lines. Printed in BOTH the in-sync and drift cases (the per-item drift dump
    # below is the detail; this is the at-a-glance overview). The report is complete here — the
    # disabled-dispatcher/disabled-global-excludes augmentations have run; schedule/tg-ctl drift
    # is already in the report from detect(). Printed FIRST so it is the headline, with the richer
    # per-area detail lines (schedule cron time, tg-ctl launchd label) following it.
    _render_area_summary(plan, report, env)

    # richer detail for two GLOBAL areas the summary counts but can't fully describe: the
    # model-freshness schedule (the daily cron time) and the tg-ctl inbound daemon (its launchd
    # boot label / unsupported-off-darwin state). Printed under the summary as its detail.
    _print_schedule_status(plan, report)
    _print_tg_ctl_status(plan, report)

    # missing-target: a hook command in the harness settings.json that points at a file gone
    # from disk (the dead-rtk-hook case) surfaces PROACTIVELY here, before it bites at runtime
    # as a generic "PreToolUse error". This is independent of config↔disk drift.
    dead_targets = _scan_missing_targets()
    if dead_targets:
        print()
        print(_err(_bold(f"  ▸ missing targets ({len(dead_targets)}) — config points at files that are gone:")))
        for f in dead_targets:
            print(f"    {_err('✗')} {f.what}")
            print(f"      {_dim('why:')} {f.why}")
            print(f"      {_ok('fix:')} {f.fix}")

    if report.in_sync and not dead_targets:
        print(_ok("\n  in sync — config and disk agree"))
        return 0

    if not report.in_sync:
        # render the full drift report whenever there IS drift — the user must SEE every problem,
        # regardless of which exit code we ultimately surface below.
        _render_drift_by_layer(report, loaded, env)
        # LOUD reassurance: apply NEVER deletes on-disk-not-declared items. A user must not fear
        # `rig apply` will nuke a hand-added skill (it won't — extras are surfaced, never removed).
        print()
        print(_ok("  ✔ safe: `rig apply` NEVER deletes on-disk extras — items present on disk but"))
        print(_ok("    not declared in any layer are left for you to decide. apply only ADDS/UPDATES."))
        print(_dim("\n  run `rig apply` to converge config→disk (extras above are left as-is)"))

    # EXIT-CODE PRECEDENCE — a dead target OUTRANKS ordinary drift. A missing hook script will
    # FAIL at runtime (a generic "PreToolUse error"); drift is merely "config and disk disagree".
    # The more-actionable, higher-severity class wins so a script following the stable exit-code
    # contract sees EXIT_MISSING_TARGET (5) even when drift is also present — the drift exit (3)
    # must never MASK it. Both are printed above; only the exit code is single-valued.
    if dead_targets:
        if not report.in_sync:
            print(_dim(
                f"\n  exit: missing-target ({errors.EXIT_MISSING_TARGET}) takes precedence over "
                f"config↔disk drift ({errors.EXIT_DRIFT}) — the dead reference fails at runtime; "
                "fix it first, then re-run."
            ))
        else:
            print(_dim("\n  (no config↔disk drift, but a missing target above needs attention)"))
        return errors.EXIT_MISSING_TARGET
    return errors.EXIT_DRIFT


def _declaring_config(category: str, loaded) -> str:
    """Name the config FILE that declares a drift item's layer (or 'not declared in any layer').

    GLOBAL categories are declared in ``~/.config/rig/config.yaml``; REPO categories in the
    repo's ``./rig.yaml``. If that layer file isn't loaded (the category drifted from a default
    or an orphaned on-disk extra with no backing config), say so plainly instead of pointing at
    a file that doesn't declare it.
    """
    from .layers import GLOBAL, layer_for_category

    layer = layer_for_category(category)
    path = loaded.global_path if layer == GLOBAL else loaded.repo_path
    return str(path) if path is not None else "not declared in any layer"


def _area_state_line(area, plan, report) -> str:
    """The rendered state for ONE area: "not configured" / "in sync" / "drift (…)".

    The reliable, unit-stable numbers are the DRIFT counts (config→disk + disk→config), so those
    are what the line reports. We deliberately do NOT print an "in-sync item count": a plan action
    and a drift item are not 1:1 across areas (the gitignore area is ONE action but TWO drift
    checks — the excludesfile setting AND the managed block — and a skill is one copy action + one
    harness-link action, two of each), so ``declared − missing`` mixes units and would either lie
    or need clamping. "in sync" / "drift (N …)" answers the real question — is this area out of
    sync, and by how much — without that fragile arithmetic. The detailed per-item dump below
    enumerates exactly which items drift.

    ``tg_ctl`` is the one CONFIGURED area whose installed-ness is platform-gated: off macOS the
    provisioner is a no-op, so detect() reports no drift even though nothing is actually installed
    — a naive "no drift → in sync" would mislead. A configured tg_ctl off Darwin is rendered
    "unsupported" (matching the dedicated tg-ctl detail line), never a false "in sync". A DISABLED
    tg_ctl (no action in the plan) still falls through to "not configured" like any other off
    area — the platform note only applies once the area is actually turned on.
    """
    from .areas import area_matches_action, area_matches_drift

    configured = any(area_matches_action(area, a.category, a.options) for a in plan.actions)
    missing = sum(
        1 for d in (report.by_direction("missing") + report.by_direction("modified"))
        if area_matches_drift(area, d.category, d.item, d.direction)
    )
    extra = sum(
        1 for d in report.by_direction("extra")
        if area_matches_drift(area, d.category, d.item, d.direction)
    )

    # tg_ctl off Darwin: the provisioner is a no-op so detect() finds no drift even though nothing
    # installed → render "unsupported" instead of a false "in sync". But ONLY when there really is
    # no drift: if some path ever DID surface drift off Darwin (a stale launchd plist from a prior
    # macOS run), "unsupported" must not mask it — fall through to the drift branch below.
    if area.key == "tg_ctl" and configured and missing == 0 and extra == 0:
        from .drift import _on_darwin

        if not _on_darwin():
            return _dim("unsupported (macOS-only; no-op off darwin)")

    if not configured and missing == 0 and extra == 0:
        return _dim("not configured")
    if missing == 0 and extra == 0:
        return _ok("in sync")
    parts = []
    if missing:
        # "missing/modified" because the count folds both directions of config→disk drift: a
        # declared item absent from disk AND one present-but-changed. Naming only "missing" would
        # mislabel a modified-on-disk file as deleted (matches _render_drift_by_layer's wording).
        parts.append(f"{missing} declared-but-missing/modified")
    if extra:
        parts.append(f"{extra} on-disk-not-declared")
    return _warn(f"drift ({', '.join(parts)})")


def _render_area_summary(plan, report, env) -> None:
    """Print the AREA SUMMARY: every reconciled area, grouped by layer, with in-sync vs drift.

    The pre-summary status rendered ONLY drifting items as a flat dump dominated by skill rows
    (one per skill + a harness-link row each), so it read as "mostly skills" and every other
    area was buried or — when in sync — invisible. This summary enumerates the full area
    registry (:mod:`riglib.areas`) so the user sees, at a glance, the WHOLE set of things rig
    manages and where each stands: configured-and-in-sync, drifted (with a count), or not
    configured. The detailed per-item drift dump still follows below for the actual problems.

    REPO-layer areas are gated to git repos (a non-git dir has no repo layer); in a non-git dir
    only the GLOBAL areas show.
    """
    from .areas import areas_for_layer
    from .layers import GLOBAL, REPO

    print()
    print(_bold("  areas rig manages:"))
    # only render (and only count) the layers that will actually show — a non-git dir has no
    # REPO layer, so its REPO areas are neither computed nor printed.
    layers = (GLOBAL,) if not env.is_git_repo else (GLOBAL, REPO)
    for layer in layers:
        print(_dim(f"    {_LAYER_HEADERS[layer]}"))
        for area in areas_for_layer(layer):
            print(f"      {area.label}: {_area_state_line(area, plan, report)}")


def _render_drift_by_layer(report, loaded, env) -> None:
    """Render drift GROUPED by GLOBAL vs REPO, each item naming its declaring config file.

    The pre-v2 status flattened machine-wide GLOBAL drift (skills/hooks/harness/mcp) together
    with this repo's REPO drift (CI/symlinks) into one undifferentiated dump — so a global
    skills drift looked like the repo's problem. Grouping by layer + naming the source file
    makes each item's ownership unambiguous.
    """
    from .layers import GLOBAL, REPO, layer_for_category

    # bucket every drift item by its owning layer, preserving missing-before-extra ordering.
    missing = report.by_direction("missing") + report.by_direction("modified")
    extra = report.by_direction("extra")
    buckets: dict[str, dict[str, list]] = {
        GLOBAL: {"missing": [], "extra": []},
        REPO: {"missing": [], "extra": []},
    }
    for d in missing:
        buckets[layer_for_category(d.category)]["missing"].append(d)
    for d in extra:
        buckets[layer_for_category(d.category)]["extra"].append(d)

    for layer in (GLOBAL, REPO):
        miss = buckets[layer]["missing"]
        ext = buckets[layer]["extra"]
        if not miss and not ext:
            continue
        # the REPO layer is meaningless outside a git repo — never print it there.
        if layer == REPO and not env.is_git_repo:
            continue
        print()
        print(_bold(f"  {_LAYER_HEADERS[layer]}"))
        if miss:
            print(_warn(f"    config→disk drift ({len(miss)}) — declared but missing/modified:"))
            for d in miss:
                src = _declaring_config(d.category, loaded)
                print(f"      {_warn('▸')} {d.category}/{d.item}: {d.detail}  {_dim('[declared in ' + src + ']')}")
        if ext:
            print(_warn(f"    disk→config drift ({len(ext)}) — on disk, not declared:"))
            for d in ext:
                src = _declaring_config(d.category, loaded)
                print(f"      {_warn('▸')} {d.category}/{d.item}: {d.detail}  {_dim('[' + str(d.target) + '; ' + src + ']')}")


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


def _print_tg_ctl_status(plan, report) -> None:
    """Report the tg-ctl inbound-daemon boot agent (GLOBAL): installed / drifted / disabled / unsupported.

    A per-machine LaunchAgent, so it lives in the GLOBAL section of status (not per-repo). Resolves
    the desired state through the SAME plan builder apply/drift use (so the label/boot flag never
    drift). Off macOS the provisioner is a no-op, so status says 'unsupported' — never a misleading
    'installed' (codex P2). Stays silent only when no ``provision_tg_ctl`` action is in the plan
    (default-on, so that is unusual — keeps the helper defensive)."""
    from .actions.runner import tg_ctl_plan_from_action
    from .drift import _on_darwin

    tg_actions = [a for a in plan.actions if a.kind == "provision_tg_ctl"]
    if not tg_actions:
        return
    tg = tg_ctl_plan_from_action(tg_actions[0])  # shared resolution → boot_label / boot_enabled
    if not _on_darwin():
        state = _dim("unsupported (macOS-only; no-op off darwin)")
    elif not tg.boot_enabled:
        state = _dim("disabled (tg_ctl.boot=false)")
    else:
        drifted = [d for d in report.items if d.category == "tg_ctl" and d.direction != "extra"]
        state = _warn(f"drifted ({drifted[0].detail})") if drifted else _ok("installed")
    print(f"\n  [GLOBAL] tg-ctl inbound daemon: {state}  " + _dim(f"(launchd boot agent, '{tg.boot_label}')"))


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

    # missing-target: proactively flag a dead hook reference in the harness settings.json (the
    # rtk-hook case) so it's caught here, not at runtime as a generic harness error.
    dead_targets = _scan_missing_targets()
    if dead_targets:
        print(_err(_bold(f"\n  ▸ missing targets ({len(dead_targets)}) — config points at files that are gone:")))
        for f in dead_targets:
            print(f"    {_err('✗')} {f.what}")
            print(f"      {_ok('fix:')} {f.fix}")

    missing_req = report.missing_required
    if not missing_req and not (args.optional and report.missing_optional) and not dead_targets:
        print(_ok("\n  all required dependencies present"))
        return 0
    # a dead target (but no missing dep) is still a problem worth a non-zero exit.
    if not missing_req and not (args.optional and report.missing_optional) and dead_targets:
        from . import errors

        print(_warn("\n  a missing target above needs attention (re-run `rig apply` or remove the stale entry)"))
        return errors.EXIT_MISSING_TARGET

    if not args.yes:
        from . import errors

        print(_warn("\n  missing dependencies above. Re-run with --yes to install them"))
        print(_dim("  (add --optional to also install optional deps)"))
        # honor the documented exit-code contract: a missing REQUIRED dep is the 127 class
        # (shell convention). A shortfall of only OPTIONAL deps (under --optional) is advisory,
        # not a hard "required tool absent", so it stays the generic non-zero.
        return errors.EXIT_MISSING_DEP if missing_req else 1

    print(_bold("\n  installing missing dependencies..."))
    results = bootstrap(report, assume_yes=True, include_optional=args.optional)
    failed = [name for name, rc in results if rc not in (0,)]
    for name, rc in results:
        print(f"    {_ok('✔') if rc == 0 else _err('✗')} {name} (rc={rc})")
    if failed:
        from . import errors

        # an install that left a REQUIRED dep absent is still the missing-dependency class;
        # a failed optional install is advisory (generic non-zero).
        req_names = {st.dep.name for st in report.statuses if st.dep.required and not st.present}
        return errors.EXIT_MISSING_DEP if (set(failed) & req_names) else 1
    return 0


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


def _config_target(args: argparse.Namespace, *, need_repo: bool) -> tuple[Path, Path | None]:
    """Resolve (config_file, repo_root) for a `config get|set` invocation.

    --global → the XDG global config; otherwise the per-repo ./rig.yaml at the detected git
    root (so the command works from any subdirectory, like apply/status).

    ``need_repo`` gates the repo lookup: ``set`` always reconciles the repo afterwards (even a
    --global edit must converge the repo in front of you), so it needs the root. A ``get
    --global`` does NOT touch any repo, so it must NOT require one — ``detect_environment``
    is skipped, and ``rig config get … --global`` works outside any git repo.
    """
    from .config import global_config_path, repo_config_path
    from .detect import detect_environment

    if args.is_global and not need_repo:
        return global_config_path(), None
    env = detect_environment(Path(args.cwd).resolve())
    target = global_config_path() if args.is_global else repo_config_path(env.repo_root)
    return target, env.repo_root


def _read_target_yaml(path: Path):
    """Parse a single config file to a dict (NOT the cascade), or None if it doesn't exist.

    Delegates to the loader's single-file reader, which already wraps YAML errors and rejects
    a non-mapping top level as a fail-closed :class:`ConfigError`.
    """
    from .config import read_yaml_file

    if not path.is_file():
        return None
    return read_yaml_file(path)


def cmd_config(args: argparse.Namespace) -> int:
    if not getattr(args, "config_command", None):
        print(_err("error: `rig config` needs a subcommand: get or set"))
        print(_dim("  rig config get <dot.path>           # read a key"))
        print(_dim("  rig config set <dot.path> <value>   # write a key, then reconcile"))
        return 2
    if args.config_command == "get":
        return _cmd_config_get(args)
    return _cmd_config_set(args)


def _cmd_config_get(args: argparse.Namespace) -> int:
    from .config import ConfigError, get_path

    try:
        target, _repo_root = _config_target(args, need_repo=False)
        data = _read_target_yaml(target)
        if data is None:
            raise ConfigError(f"config file not found: {target}")
        value = get_path(data, args.path)
    except ConfigError as exc:
        # diagnostics go to STDERR so `get --json | jq` keeps a clean stdout (the exit code
        # already signals failure); this matters most for the machine-readable --json path.
        print(_err(f"error: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # symmetric with set: anything unexpected (e.g. environment detection failing outside a
        # git repo on a non-global get) must fail closed with a message, not a traceback.
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    if args.json:
        import json

        # default=str so a YAML date/datetime (or any non-JSON scalar) serializes instead of
        # throwing an unhandled TypeError — get must always fail soft on output.
        print(json.dumps(value, default=str))
    elif isinstance(value, (dict, list)):
        # a subtree: dump it as YAML so the structure is readable (and re-feedable to set's
        # scalar children). Lazy yaml import keeps the no-yaml path light.
        import yaml

        print(yaml.safe_dump(value, sort_keys=False, default_flow_style=False).rstrip())
    else:
        print(_fmt_scalar(value))  # YAML/CLI casing for bool/null, str otherwise
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    from .actions import run_plan
    from .catalog import CatalogError
    from .config import ConfigError, coerce_scalar, set_path, validate
    from .plan import PlanError
    from .state import SetupState

    try:
        target, _repo_root = _config_target(args, need_repo=True)
    except ConfigError as exc:
        print(_err(f"error: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — environment detection failing must fail soft
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    # Refuse a repo-local `set` when ./rig.yaml does not exist yet: starting from {} and
    # reconciling would let built-in defaults mutate disk with no committed source of truth —
    # the same hazard `rig apply`'s no-config guard prevents. `rig init` (or `rig export`)
    # creates the file first. (--global may legitimately create the machine-wide file, so it is
    # not guarded here; its second gate still validates over the cascade.)
    if not args.is_global and not target.is_file():
        print(_err(f"error: no {target} — run `rig init` (or `rig export -o rig.yaml`) first."))
        print(_dim("  `config set` edits an existing committed config; it does not bootstrap one."))
        return 2

    # `scope` is a removed key (the cascade is location-based). Refuse to SET it — the
    # recommended editor must never (re)introduce a dead setting, even though the loader still
    # tolerates it in old committed files.
    if args.path == "scope" or args.path.startswith("scope."):
        print(_err("error: `scope` is a removed setting — the cascade is by location "
                   "(global vs repo), not a flag. Nothing to set."), file=sys.stderr)
        return 2

    try:
        data = _read_target_yaml(target) or {}
        # drop any legacy `scope` already in the file (mirrors config.load): we re-serialize the
        # whole file, so leaving it would re-emit a key the schema no longer recognizes.
        data.pop("scope", None)
        value = coerce_scalar(args.value)
        set_path(data, args.path, value)
        # First gate: schema validation of the WHOLE edited tree (enum/type checks). This
        # catches e.g. harness.auto_mode="yes" before anything touches disk.
        validate(data)
    except ConfigError as exc:
        print(_err(f"error: {exc}"))
        return 2
    except Exception as exc:  # noqa: BLE001
        # nothing has touched disk yet (read/coerce/validate only). Anything unexpected here
        # must still fail closed with a message, not a traceback.
        print(_err(f"error: {type(exc).__name__}: {exc}"))
        return 2

    # Capture the previous bytes BEFORE writing so a write IO error OR a SECOND, deeper
    # validation failure (catalog-backed: a bad agent_tools_source or an unknown CI item —
    # these live in plan.build(), not config.validate()) can fully ROLL BACK. The file must be
    # untouched on ANY failure, exactly as the docs promise. The repo file carries the
    # committed-source-of-truth header; the global file is a plain machine-wide dump.
    original = target.read_text(encoding="utf-8") if target.is_file() else None
    parent_existed = target.parent.exists()  # so rollback only removes a dir WE created

    def _rollback() -> bool:
        """Restore the file to its pre-set state. Returns True on success, False if the restore
        itself failed (so the caller can tell the user the file is NOT actually untouched,
        rather than printing a false reassurance)."""
        try:
            if original is None:
                target.unlink(missing_ok=True)  # we created the file; remove our partial write
                # only remove the parent if WE created it (a fresh ~/.config/rig for --global) —
                # never rmdir a pre-existing dir like the repo root.
                if not parent_existed:
                    with contextlib.suppress(OSError):
                        target.parent.rmdir()  # no-op if non-empty (e.g. a repo root has .git)
            else:
                target.write_text(original, encoding="utf-8")  # restore prior contents
        except OSError:
            return False
        return True

    # Write, then second gate: build the plan from the on-disk cascade (so a --global edit is
    # validated together with the repo layer it merges into). A write IO error or a plan
    # failure means the edit didn't take → roll back and fail closed.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        state = SetupState.from_dict(data)
        if args.is_global:
            target.write_text(state.to_yaml(), encoding="utf-8")
            # Validate the global file ALONE first: the cascade plan below merges the repo
            # overlay over it, which can mask a catalog-backed error in the global layer (a repo
            # `rig.yaml` overriding the just-broken key). Check the written file in isolation so a
            # globally-broken config never persists just because THIS repo happens to override it.
            _validate_layer_in_isolation(target)
        else:
            state.write(target)
        plan, _loaded, _env = _load_plan(args.cwd, config=None)
    except Exception as exc:  # noqa: BLE001
        # the write happened (or partially did) — ANY failure from here (catalog/plan rejection,
        # a write IO error, or a serializer error on data the first gate let through) must roll
        # the file back and fail closed. Never leave a written-but-unreconcilable config behind.
        restored = _rollback()
        kind = "" if isinstance(exc, (ConfigError, CatalogError, PlanError, OSError)) else f"{type(exc).__name__}: "
        print(_err(f"error: {kind}{exc}"))
        if restored:
            print(_dim(f"  (config not changed — {target} left untouched)"))
        else:
            # the restore itself failed — be honest: the file may hold the rejected edit.
            print(_err(f"  WARNING: could not restore {target} — it may contain the rejected edit"))
        return 2

    print(_ok(f"set {args.path} = {_fmt_scalar(value)}  → {target}"))

    # RECONCILE: a config change only matters once the disk reflects it. Run the SAME apply
    # engine `rig apply` uses (scoped to the repo in front of you — a --global edit still has
    # to converge this repo). --no-apply writes the key and prints the plan only.
    _print_plan(plan)
    if args.no_apply:
        print(_dim("\n(--no-apply: config written, nothing reconciled)"))
        return 0
    report = run_plan(plan)
    _print_results(report)
    return 1 if report.errors else 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


def cmd_setup_wizard(args: argparse.Namespace) -> int:
    """`rig setup` — the interactive config wizard, or the non-interactive USAGE pointer.

    In a TTY: open the wizard (show state across all reconciled areas → change options in the
    local rig.yaml AND the global config → apply). With no TTY (piped/redirected): print usage
    for init/apply/config and exit 0 — never a half-wizard the user can't answer.
    """
    from . import setup_wizard

    if not setup_wizard.is_interactive():
        return setup_wizard.print_non_interactive_usage()

    from .detect import detect_environment

    repo_root = detect_environment(Path(args.cwd).resolve()).repo_root

    def _apply(root: Path) -> int:
        # reuse the SAME engine as `rig apply` (one executor, never forked for the wizard).
        # Build the namespace through the REAL `apply` subparser so it always carries every
        # attribute cmd_apply reads — no hand-kept field list that can drift from the parser.
        apply_args = build_parser().parse_args(["apply", "-C", str(root)])
        return cmd_apply(apply_args)

    # color hook so the wizard's rendered state matches the rest of the CLI's NO_COLOR handling.
    return setup_wizard.run_setup(repo_root, apply_fn=_apply, color=_c)


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
