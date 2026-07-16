"""rig CLI — argparse + subcommand dispatch only.

The thin entry point (``[project.scripts] rig = "riglib.cli:main"`` and the target of the
``bin/rig`` shim). It owns argument parsing and dispatch; all behavior lives in the sibling
modules. Heavy/optional imports (textual TUI, yaml) are done lazily inside the handler that
needs them so ``rig --help`` and ``rig doctor`` stay fast and dependency-light.

Subcommands:

    rig init     first-run onboarding — scaffold rig.yaml + wire the catalog in (the front door)
    rig apply    PREVIEW the reconcile (bare = `apply info`); `apply commit` executes it
    rig setup    interactive config wizard (no TTY → usage for init/apply/config)
    rig config   get/set ONE config key by dot path, then reconcile (get|set)
    rig status   detect + report drift in BOTH directions (config↔disk)
    rig doctor   detect + (offer to) install required/optional dependencies
    rig export   serialize default/current config to rig.yaml without a TUI
    rig stats    tool-adoption analytics over agent-harness session logs (sub: `show`)
    rig evolve   local project evolution portal (git histogram + code treemap)
    rig codex    safe Codex maintenance helpers
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import shlex
import sys
import time
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


def _err_block(exc: Exception) -> str:
    """Render an exception as the standard error block — 3-part for a config schema error.

    A :class:`riglib.config.ConfigError` carries WHAT / WHY / FIX + the SCHEMA PATH (roadmap §5):
    render the full block so a malformed config fails LOUDLY and points the user at the offending
    node in ``schema/rig.schema.json``. Any other exception (catalog/plan/OS) keeps the existing
    one-line ``error: <msg>`` shape. Used by every ``except`` site so config rejections read the
    same whether they come from ``apply``, ``config set``, ``status``, or ``init``.
    """
    from .config import ConfigError as _ConfigError
    from .config import render_config_error

    if isinstance(exc, _ConfigError):
        return render_config_error(exc, color=_USE_COLOR)
    return _err(f"error: {exc}")


def _tui_importable() -> bool:
    """True when the wizard's deps (textual + rich) are importable.

    ``textual`` + ``rich`` are CORE runtime dependencies (pyproject ``[project].dependencies``),
    so every canonical install (`pipx install rig-cli`, `uv tool install rig-cli`, `pip install
    rig-cli`) brings them and this is normally True. It can be False only on a genuinely broken
    environment (a partial / corrupt install); when it is, `rig init` degrades to a one-line
    message + a non-destructive preview instead of crashing. Pure spec lookup (no import side
    effects) so the check never drags textual into the process when it is absent. ``find_spec``
    itself can raise (``ModuleNotFoundError`` on a missing parent package, ``ValueError`` on a
    half-written ``__spec__``), so it is caught: this predicate must never throw.
    """
    import importlib.util

    try:
        return all(importlib.util.find_spec(m) is not None for m in ("textual", "rich"))
    except (ImportError, ValueError):
        return False


def _tui_opted_out() -> bool:
    """The interactive TUI is suppressed via the ``RIG_NO_TUI`` env (any truthy value).

    The ``--no-tui`` flag is checked separately at the call site (it lives on ``args``); this is
    the env half, so automation can disable the wizard without passing a flag.
    """
    val = _os.environ.get("RIG_NO_TUI", "").strip().lower()
    return val not in ("", "0", "false", "no", "off")


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
            "  7    repo corrupt (a working checkout's git config is broken, e.g. core.bare=true)\n"
            "  8    Codex update failed (candidate failed probes or rollback needed attention)\n"
            "  127  missing dependency (a required external tool isn't installed)\n"
            "\n"
            "  precedence (doctor): `rig doctor` exits 7 (repo corrupt) ahead of any other class —\n"
            "  a broken .git (e.g. core.bare=true on a working checkout) makes every git-backed\n"
            "  check unreliable, so it is fixed first.\n"
            "  precedence (status): when `rig status` finds BOTH a missing target and config↔disk\n"
            "  drift, it prints both but exits 5 (missing-target outranks drift — the dead\n"
            "  reference fails at runtime, so it's the more urgent class).\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"rig {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def _add_setup_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-C", "--cwd", default=".", help="repo root to operate on (default: cwd)")
        parser.add_argument("--config", help="apply this config file headlessly (non-interactive)")
        parser.add_argument("--yes", action="store_true", help="non-interactive; assume yes")
        # The repo's stack preset (l1/lang[/framework]); overrides auto-detection. Written into the
        # scaffolded rig.yaml and used to select the by-stack skill set.
        parser.add_argument(
            "--stack",
            help="the repo stack preset l1/lang[/framework] (e.g. mobile/swift/swiftui, "
            "frontend/ts/react, backend/python); default: auto-detect from the repo",
        )
        # NOTE: there is intentionally NO --no-write-config flag. rig.yaml is the committed
        # source of truth and is NOT optional (AGENTS.md). Use --dry-run for a no-write preview.
        parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
        # init only SCAFFOLDS rig.yaml + PREVIEWS the plan; applying is a deliberate second step
        # (`rig apply commit`). --apply opts into the one-shot "scaffold AND apply now".
        parser.add_argument(
            "--apply", action="store_true",
            help="also apply the plan now (default: init only scaffolds rig.yaml; run `rig apply commit`)",
        )
        parser.add_argument(
            "--plan", action="store_true",
            help="list every planned action (default: a per-carrier summary for a large plan)",
        )
        # --notes mirrors `rig apply --notes`: expand the collapsed informational notes so the
        # collapse hint the shared plan renderer prints ("--notes to expand") is valid here too.
        parser.add_argument(
            "--notes", action="store_true",
            help="expand every informational note (default: collapse them behind a one-line count)",
        )
        # a bare interactive `rig init` launches the TUI wizard (textual ships WITH rig); --no-tui
        # (or RIG_NO_TUI=1) opts out of the wizard, keeping the non-destructive preview path.
        parser.add_argument(
            "--no-tui", action="store_true",
            help="don't launch the interactive TUI; show a preview only",
        )

    # `init` is the canonical first-run onboarding command (the front door). init/apply are
    # the two real commands; interactivity (TUI/semi/--yes) is orthogonal to both. init
    # SCAFFOLDS + PREVIEWS by default — `rig apply` (or `rig init --apply`) is what applies.
    ip = sub.add_parser("init", help="first-run onboarding: scaffold rig.yaml + preview the plan (front door; `rig apply` applies)")
    _add_setup_args(ip)

    ap = sub.add_parser(
        "apply",
        help="preview the reconcile (bare = `apply info`); `apply commit` executes it",
        description="Reconcile disk to rig.yaml (idempotent). PREVIEW-BY-DEFAULT: a bare "
        "`rig apply` is an alias for `rig apply info` — it builds + prints the plan and MUTATES "
        "NOTHING. Run `rig apply commit` to actually execute it. apply only ADDS/UPDATES the "
        "items your config declares — it NEVER deletes on-disk extras. Items present on disk "
        "but not declared in any layer (e.g. a hand-added skill) are reported by `rig status` "
        "and left untouched; apply will not nuke them.",
    )
    # info | commit as a positional (NOT argparse sub-subparsers) so every flag stays on the one
    # apply parser and works regardless of position — `rig apply commit -C x` and `rig apply -C x`
    # both parse cleanly, with no per-subparser namespace to keep in sync.
    ap.add_argument(
        "mode", nargs="?", choices=("info", "commit"), default=None,
        help="info: preview the plan, write nothing (default). commit: execute the plan.",
    )
    ap.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ap.add_argument("--config", help="config file to apply (default: ./rig.yaml + global)")
    ap.add_argument("--dry-run", action="store_true", help="force preview even under `commit`")
    ap.add_argument("--only", help="comma-separated categories to scope (e.g. skills,ci)")
    ap.add_argument(
        "--yes", action="store_true",
        help="non-interactive commit intent: a bare `apply --yes` executes (automation back-compat)",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="show already-in-sync (no-op) actions, which are collapsed behind a count by default",
    )
    ap.add_argument(
        "--notes", action="store_true",
        help="expand every informational note (default: collapse them behind a one-line count)",
    )
    ap.add_argument(
        "--plan", action="store_true",
        help="list every planned action (default: a per-carrier summary for a large plan)",
    )

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
    dp.add_argument(
        "--fix",
        action="store_true",
        help="auto-repair detected repo corruption (e.g. reset a wrong core.bare=true)",
    )

    ep = sub.add_parser("export", help="write a rig.yaml from default/current config")
    ep.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ep.add_argument("-o", "--output", default="rig.yaml", help="output path (default: rig.yaml)")
    ep.add_argument("--force", action="store_true", help="overwrite an existing file")

    scp = sub.add_parser(
        "schema",
        help="print the rig.yaml JSON Schema (or --check / --write that it matches on disk)",
        description="Emit the JSON Schema for rig.yaml + the global config (generated from the "
        "single in-code registry). Editors point at the committed schema/rig.schema.json for "
        "completion/validation; `--check` fails if the committed file is stale, `--write` "
        "regenerates it.",
    )
    scp.add_argument("--check", action="store_true",
                     help="exit non-zero if schema/rig.schema.json is missing or out of sync")
    scp.add_argument("--write", action="store_true",
                     help="(re)generate schema/rig.schema.json from the in-code registry")

    sub.add_parser("install-skill", help="register the rig agent skill with harnesses")

    # `spotlight-sweep` runs the Spotlight-exclude sweep once (the command the launchd re-sweep
    # agent invokes). It reads the merged `spotlight:` config and drops .metadata_never_index into
    # dependency/build dirs under the configured roots. Idempotent; no launchd mutation.
    ss = sub.add_parser(
        "spotlight-sweep",
        help="drop .metadata_never_index into dependency/build dirs under the configured roots (macOS Spotlight-exclude)",
    )
    ss.add_argument("-C", "--cwd", default=".", help="repo root (default: cwd)")
    ss.add_argument("--config", help="config file (default: ./rig.yaml + global)")

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

    # `config-web` — a local web UI over the SAME config engine as the wizard / `config set`.
    # Its lifecycle (run/start/stop/status/enable/disable + OS autostart) is delegated to the
    # SHARED agenttools-service manager; the wiring lives in config_web_service.register (NOT
    # here) so this module carries no copy of the service machinery. Lazy-imported so a missing
    # agenttools-service lib can't break `rig --help` / the rest of the CLI.
    from . import config_web_service

    config_web_service.register(sub)

    # `evolve` — local project evolution portal. Lifecycle is the same shared
    # agenttools-service manager pattern as config-web; registration stays lazy so `rig --help`
    # does not import optional service machinery.
    from .evolve import service as evolve_service

    evolve_service.register(sub)

    _add_stats_parser(sub)
    _add_codex_parser(sub)

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
    cs.add_argument(
        "--plan", action="store_true",
        help="list every planned action (default: a per-carrier summary for a large plan)",
    )


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


def _add_codex_parser(sub: "argparse._SubParsersAction") -> None:
    """`rig codex update` — wrap Codex updates with rollback-on-hang probes."""
    cp = sub.add_parser("codex", help="safe Codex maintenance helpers")
    cp.set_defaults(_codex_parser=cp)
    csub = cp.add_subparsers(dest="codex_command", metavar="<update>")

    up = csub.add_parser(
        "update",
        help="update Codex, then roll back if version/help/completion probes fail or hang",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes:\n  0 success\n  8 Codex update failed\n  127 Codex binary or updater command missing",
    )
    up.add_argument("--path", help="path to the codex binary (default: first codex on PATH)")
    up.add_argument(
        "--backup-dir",
        default=None,
        help="where to store last-known-good binary backups (default: ~/.cache/rig/codex-backups)",
    )
    up.add_argument(
        "--probe-timeout",
        type=float,
        default=None,
        help="seconds for each version/help/completion probe (default: 5)",
    )
    up.add_argument(
        "update_command",
        nargs=argparse.REMAINDER,
        help="optional updater command after -- (default: brew upgrade --cask codex for Homebrew codex)",
    )


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
        "schema": cmd_schema,
        "install-skill": cmd_install_skill,
        "spotlight-sweep": cmd_spotlight_sweep,
        "setup": cmd_setup_wizard,  # setup = the interactive config wizard (distinct from init)
        "config-web": cmd_config_web,  # web UI over the config engine; lifecycle via agenttools-service
        "evolve": cmd_evolve,  # project evolution portal; lifecycle via agenttools-service
        "stats": cmd_stats,
        "codex": cmd_codex,
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


# Above this many planned actions, `_print_plan` prints a per-carrier SUMMARY rather than the
# full wall of "category/item → target" lines (a fresh `rig init`/`apply` is ~120 actions, and
# a `config set` reconcile runs the SAME full engine). `--plan` always forces the full list; a
# genuinely small plan (a scoped `--only`, a near-empty config) stays fully inline — it is not a
# wall and the per-line detail is more useful than a count.
_PLAN_INLINE_MAX = 12

# Human labels for the plan summary, keyed by Action.category. An unlisted category falls back to
# its raw name (underscores hyphenated), so a newly added carrier can't silently break the summary.
_CATEGORY_LABELS = {
    "skills": "skills",
    "agent_hooks": "agent-hooks",
    "git_hooks": "git-hooks",
    "ci": "CI gates",
    "mcp": "MCP",
    "harness": "harness",
    "permissions": "permissions",
    "agents_md": "agents.md",
    "ship_delegator": "ship gate",
    "ship_env": "ship env (machine)",
    "github": "GitHub",
    "models": "models",
    "tmux": "tmux",
    "linters": "linters",
    "gitignore": "gitignore",
    "tools": "tools",
    "tg_ctl": "tg-ctl",
}


def _plan_summary_line(plan) -> str:
    """One-line "<n> <carrier>, …" count of the plan, most-numerous carrier first."""
    from collections import Counter

    counts = Counter(a.category for a in plan.actions)
    parts = [
        f"{n} {_CATEGORY_LABELS.get(cat, cat.replace('_', '-'))}"
        # most-numerous first, ties broken alphabetically for a stable, reproducible read.
        for cat, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ", ".join(parts)


# A note is a free-text string; these substrings mark a REAL GAP the user should see — something
# rig DECLINED to provision that the config asked for (a bridge/schedule/permission it could not
# wire) — as opposed to informational policy/wiring/status chatter. Deliberately NARROW: broad
# words ("has no", "not written", "missing", "cannot") also appear in intentional-state notes
# (export-only CI, a harness that reads a global instruction file instead of a skills dir), so
# they would cry wolf. Kept as one keyword set (single source of truth) rather than restructuring
# every `plan.notes.append` call site into a typed severity — centralized here and covered by
# tests. If a note class needs elevating, add its stable phrase here, not a generic word.
_NOTE_ATTENTION_MARKERS = (
    "skipped",  # "hook_bridge: skipped", "permissions: skipped", "models: schedule skipped"
    "dropped",  # "allow rules dropped for harness …"
    "not provisioned",
    "not wired",
    "no allowlist to provision",
    "no rig auto",  # "has no rig auto/permission-mode writer yet"
    "no supported agents-hooks bridge",
)


def _note_needs_attention(note: str) -> bool:
    """True when a plan note reports a real provisioning gap (elevated), not just information."""
    low = note.lower()
    return any(marker in low for marker in _NOTE_ATTENTION_MARKERS)


def _print_notes(notes: list[str], *, expand: bool = False) -> None:
    """Print plan notes: real-gap notes elevated to a ``⚠`` line, informational ones collapsed.

    By default the informational notes are hidden behind a one-line count (``notes: N
    informational, M need attention — --notes to expand``) so they never form a wall; the gap
    notes (e.g. the hook_bridge one) are always shown. ``expand=True`` (``--notes``) prints every
    note verbatim.
    """
    if not notes:
        return
    attention = [n for n in notes if _note_needs_attention(n)]
    info = [n for n in notes if not _note_needs_attention(n)]
    for note in attention:
        print(f"  {_warn('⚠')} {note}")
    if expand:
        for note in info:
            print(f"  {_dim('note')}: {note}")
        return
    if info:
        n, m = len(info), len(attention)
        parts = [f"{n} informational"]
        if m:
            parts.append(f"{m} need attention")
        print(_dim(f"  notes: {', '.join(parts)} — --notes to expand"))


def _print_plan(
    plan, *, full: bool = False, preview: bool = False, expand_notes: bool = False
) -> None:
    # `preview=True` marks the plan as NOT applied (the `rig init` scaffold/no-TUI paths): the
    # plan is what `rig apply` WOULD do, so it must never read as completed work.
    label = _dim("   — PREVIEW of `rig apply` (not applied)") if preview else ""
    print(_bold(f"\nPlan: {len(plan)} action(s)  [on_conflict={plan.on_conflict}]") + label)
    if full or len(plan.actions) <= _PLAN_INLINE_MAX:
        for a in plan.actions:
            print(f"  {_dim('•')} {a.category}/{a.item} → {a.target}")
    else:
        print(f"  {_plan_summary_line(plan)}")
        print(_dim(f"  (run with --plan to list all {len(plan.actions)} actions)"))
    _print_notes(plan.notes, expand=expand_notes)


# Result statuses that mean the action CHANGED disk (vs "skipped" = already in sync).
_CHANGED_STATUSES = ("created", "updated", "backed_up")


def _print_results(report, *, verbose: bool = False, list_hint: str | None = None) -> None:
    """Print apply results grouped by what CHANGED vs what was already in sync.

    Changed/error rows always print; already-in-sync (``skipped``) no-ops are collapsed behind a
    one-line count unless ``verbose``. ``list_hint`` (e.g. ``"-v"``) is the flag the current
    command exposes to reveal the collapsed rows — passed ONLY by front-ends that actually accept
    it (``rig apply commit``), so the hint never advertises a flag the caller lacks (``init
    --apply`` / ``config set`` reuse this renderer without a ``-v``). The trailing ``Summary:``
    line is unchanged so existing status/parity assertions keep working.
    """
    icon = {
        "created": _ok("✔"),
        "updated": _ok("✔"),
        "backed_up": _warn("↩"),
        "skipped": _dim("·"),
        "planned": _dim("•"),
        "error": _err("✗"),
    }
    in_sync = [r for r in report.results if r.status == "skipped"]
    changed = [r for r in report.results if r.status != "skipped"]
    rows = report.results if verbose else changed
    for r in rows:
        print(f"  {icon.get(r.status, '?')} {r.action.category}/{r.action.item}: {r.detail}")
    if in_sync and not verbose:
        hint = f" (run {list_hint} to list)" if list_hint else ""
        print(_dim(f"  {icon['skipped']} {len(in_sync)} already in sync{hint}"))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    print(_bold(f"\nSummary: {summary}"))


def _run_and_print_verify(plan, applied=None) -> bool:
    """Run the post-apply verify framework over ``plan`` and print each check. Returns ok/not-ok.

    Runs only checks for provisioners actually in the plan. Skipped checks (non-macOS / dry-run
    launchd) never fail the result — only a real FALSE does. This is the prove-it-worked step:
    every provisioner that declares a verify() is confirmed to have taken effect.
    """
    from .verify import verify_plan

    report = verify_plan(plan, applied)
    if not report.results:
        return True
    icon = {"pass": _ok("✔"), "FAIL": _err("✗"), "skipped": _dim("·")}
    print(_bold("\nVerify:"))
    for r in report.results:
        print(f"  {icon.get(r.state, '?')} {r.category}/{r.item}: {r.evidence}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    print(_bold(f"Verify summary: {summary}"))
    if not report.ok:
        print(_err(f"verify: {len(report.failures)} check(s) FAILED — provisioned artifact did not take effect"))
    return report.ok


# ── commands ──────────────────────────────────────────────────────────────────────
def cmd_spotlight_sweep(args: argparse.Namespace) -> int:
    """Run the Spotlight-exclude sweep once (the launchd re-sweep agent's command).

    Reads the merged ``spotlight:`` config, resolves roots + denylist, and drops the sentinel into
    every matched dependency/build dir. Idempotent; touches no launchd state. Exit 0 on success.
    Deliberately NOT gated by ``RIG_SPOTLIGHT_DRY_RUN`` (unlike the apply-time provisioner): this
    command IS the sweep — a dry-run of it would be a no-op.
    """
    from . import spotlight
    from .config import ConfigError, load

    try:
        env_root, explicit = _sweep_config_context(args)
        loaded = load(env_root, explicit_config=explicit, include_repo=True)
    except ConfigError as exc:
        print(_err_block(exc))
        return 2
    s = loaded.data.get("spotlight")
    if not isinstance(s, dict):
        s = {}
    # Honor the opt-out/default-off contract: if the config is absent or spotlight is disabled,
    # this is a NO-OP (the persistent launchd agent keeps invoking us after a config removal — it
    # must not keep writing sentinels once the user turned the feature off). Same guard as
    # plan._build_spotlight: an explicit `enabled: true` is required to sweep.
    if not s.get("enabled"):
        print("spotlight-sweep: disabled in config (spotlight.enabled is not true) — nothing to do")
        return 0
    roots = spotlight.resolve_roots(s.get("roots"))
    deny = spotlight.resolve_deny(s.get("deny"), s.get("extra"))
    max_depth = int(s.get("max_depth", spotlight.DEFAULT_MAX_DEPTH))
    result = spotlight.perform_sweep(roots, deny, max_depth)
    print(f"spotlight-sweep: {result.summary()}")
    for missing in result.roots_missing:
        print(_dim(f"  (root not present: {missing})"))
    return 0


def _sweep_config_context(args: argparse.Namespace) -> tuple[Path, Path | None]:
    """Resolve (repo_root, explicit_config) for the sweep, reusing the shared config-context helpers."""
    from .detect import detect_environment

    env = detect_environment(Path(args.cwd).resolve())
    explicit = _resolve_explicit_config(env, getattr(args, "config", None))
    return env.repo_root, explicit


def cmd_codex(args: argparse.Namespace) -> int:
    if not getattr(args, "codex_command", None):
        args._codex_parser.print_help()
        return 0
    if args.codex_command != "update":
        args._codex_parser.print_help()
        return 2

    from . import codex_update

    command = list(getattr(args, "update_command", []) or [])
    if command and command[0] == "--":
        command = command[1:]
    if args.probe_timeout is not None and (
        args.probe_timeout <= 0 or not math.isfinite(args.probe_timeout)
    ):
        print(_err("codex update: --probe-timeout must be positive"))
        return 2
    try:
        result = codex_update.safe_update(
            codex_path=args.path,
            update_command=command or None,
            backup_dir=args.backup_dir or codex_update.DEFAULT_BACKUP_DIR,
            probe_timeout_s=(
                args.probe_timeout
                if args.probe_timeout is not None
                else codex_update.DEFAULT_PROBE_TIMEOUT_S
            ),
        )
    except FileNotFoundError as exc:
        print(_err(f"codex update: {exc}"))
        return 127

    prefix = {
        "updated": _ok("updated"),
        "rolled_back": _warn("rolled back"),
        "error": _err("error"),
    }.get(result.status, result.status)
    print(f"codex update: {prefix}: {result.message}")
    if result.backup_path is not None:
        print(_dim(f"last-known-good backup: {result.backup_path}"))
    return result.exit_code


def cmd_setup(args: argparse.Namespace) -> int:
    # `rig init` front door. The split:
    #   • a bare `rig init` (no --config/--yes/--apply/--dry-run) is INTERACTIVE → the TUI
    #     wizard, the user's control surface.
    #   • any explicit signal (--config / --yes / --apply / --dry-run) is HEADLESS.
    #   • a bare `rig init` with NO way to run the wizard (no TTY, or textual not installed) has
    #     NO instructions AND no wizard, so it must NOT silently scaffold+apply — it shows a
    #     non-destructive PREVIEW (writes nothing, applies nothing) and how to proceed.
    from .setup_wizard import is_interactive

    interactive = not (args.config or args.yes or args.dry_run or getattr(args, "apply", False))
    if interactive:
        # the wizard needs a TTY on BOTH ends (stdin to read, stdout to draw). A piped / CI /
        # agent run has no TTY, so a fullscreen TUI would hang — fall to the preview instead of
        # doing anything surprising (mirrors `rig setup`'s non-interactive degradation).
        if not is_interactive():
            return _setup_preview_no_tui(args, reason="no-tty")
        # explicit opt-out (`--no-tui` / RIG_NO_TUI): just the non-destructive preview, so
        # automation that sets it never gets a surprise fullscreen wizard.
        if getattr(args, "no_tui", False) or _tui_opted_out():
            return _setup_preview_no_tui(args, reason="no-tui")
        # textual ships WITH rig (a CORE dependency), so a TTY `rig init` launches the wizard
        # directly — no install step, no hint. It is missing only on a genuinely broken env; that
        # degrades to a one-line message + the non-destructive preview rather than crashing.
        if not _tui_importable():
            return _setup_preview_no_tui(args, reason="no-textual")
        # `.tui` re-exports run_wizard but imports textual ONLY inside the call, so this
        # import is cheap and stdlib-safe; the textual ImportError surfaces on invocation.
        from .tui import run_wizard

        try:
            # thread --stack through so a TTY `rig init --stack …` is honored, not silently
            # dropped at the interactive boundary (the headless path already respects it).
            return run_wizard(Path(args.cwd).resolve(), stack=getattr(args, "stack", None))
        except ImportError:
            return _setup_preview_no_tui(args, reason="no-textual")

    return _setup_headless(args, use_default=args.config is None)


def _resolve_init_plan(args: argparse.Namespace, *, use_default: bool):
    """Resolve ``(plan, loaded, env, repo_yaml, pending_write)`` for `rig init`.

    Pure resolver: it scans the catalog and builds the plan but writes NOTHING and applies
    NOTHING. Raises ConfigError/CatalogError/PlanError on a bad config (the caller renders the
    error + maps it to an exit code). ``pending_write`` records how rig.yaml WOULD be persisted
    (``("generated", SetupState)`` | ``("copy", src_path)`` | ``None``), so the headless path can
    persist it fail-closed only AFTER the plan is proven valid.

    The tuple LEADS with ``plan`` to match :func:`_load_plan` (``(plan, loaded, env)``) — both
    plan-resolvers share one convention so a caller can't mix them up.
    """
    from .catalog import Catalog
    from .config import (
        ConfigError,
        LoadedConfig,
        load,
        read_yaml_file,
        resolve_init_stack,
        validate,
    )
    from .detect import detect_environment
    from .plan import build
    from .state import SetupState

    # detect_environment resolves repo_root to the git top-level; use THAT consistently for
    # config load, rig.yaml writes, and install targets, so setup and later apply/status
    # (which also resolve to the git root) operate on the same paths and reproduce.
    env = detect_environment(Path(args.cwd).resolve())
    repo_root = env.repo_root
    repo_yaml = repo_root / "rig.yaml"
    pending_write = None
    if use_default:
        # honor a global-config-pinned agent_tools_source for the scan (the documented cascade
        # has a valid global layer), but do NOT pin it into the committed rig.yaml — that would
        # break the env/default fallback on other machines.
        global_cfg = load(repo_root, include_global=True)
        global_source = global_cfg.agent_tools_source
        Catalog.scan(global_source)  # verify an agent-tools checkout exists (fail early)
        # Stack preset: an explicit --stack wins, else the global default (cascade), else a
        # best-guess from the repo files. Written into the committed rig.yaml so the by-stack
        # skills are selected; left unset (→ soft-require warning) when nothing is known. The
        # interactive TUI seeds its state through the SAME helper, so both front-ends agree.
        stack = resolve_init_stack(
            repo_root, explicit=getattr(args, "stack", None), global_stack=global_cfg.stack
        )
        state = SetupState.default(
            agent_tools_source=None, project_type=env.project_type, stack=stack
        )
        # Build the plan from the GENERATED state, not from disk: with --dry-run there may be no
        # rig.yaml on disk yet, so loading from disk would preview an empty/stale plan instead of
        # what setup decided. Carry the global source into the in-memory config so the catalog
        # scan below resolves it.
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
        template_data = read_yaml_file(explicit)
        if "mode" in template_data:
            raise ConfigError(
                "mode is a global-only config block",
                why=f"{explicit} was passed to `rig init --config`, which copies the template into ./rig.yaml",
                fix="move mode to ~/.config/rig/config.yaml and remove it from the init template",
                schema_path="mode",
            )
        loaded = load(repo_root, explicit_config=explicit)
        # rig.yaml is committed-by-default: setting up from an external template must leave the
        # repo with its own rig.yaml. Copy it in unless it already IS it.
        if explicit != repo_yaml.resolve():
            pending_write = ("copy", explicit)
    catalog = Catalog.scan(loaded.agent_tools_source)
    plan = build(loaded, catalog, project_type=env.project_type)
    return plan, loaded, env, repo_yaml, pending_write


def _persist_rig_yaml(pending_write, repo_yaml: Path) -> None:
    """Write the committed rig.yaml from a resolved ``pending_write`` (generated | copy)."""
    kind, payload = pending_write
    if kind == "generated":
        written = payload.write(repo_yaml)
        print(_ok(f"wrote {written}"))
        return
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


def _print_init_next_steps(*, config_exists: bool, reason: str) -> None:
    """The 'nothing was done — here is how to proceed' block for the no-TUI PREVIEW path.

    The last line is ``reason``-aware: under ``no-textual`` rig's environment is broken (textual
    ships WITH rig, so its absence means a corrupt install) and we point at reinstalling rig; under
    ``no-tui`` the user opted out (``--no-tui`` / ``RIG_NO_TUI``) so we point at dropping the flag;
    under ``no-tty`` (piped / CI / agent) the wizard can't run at all, so we point at a real terminal.
    """
    print(_bold("\nNothing was written and nothing was applied — this is a PREVIEW."))
    print("To proceed, pick one:")
    if config_exists:
        print(f"  {_dim('•')} rig.yaml already exists here  →  run `rig apply commit` to apply it")
    else:
        print(f"  {_dim('•')} rig init --yes           write rig.yaml (config only; then `rig apply commit`)")
        print(f"  {_dim('•')} rig init --yes --apply   write rig.yaml AND apply the plan in one step")
    if reason == "no-textual":
        print(f"  {_dim('•')} reinstall rig (`pipx install rig-cli` / `uv tool install rig-cli`), then re-run `rig init`")
    elif reason == "no-tui":
        print(f"  {_dim('•')} drop --no-tui (and unset RIG_NO_TUI), then re-run `rig init` to choose interactively")
    else:  # no-tty: the wizard needs a real terminal; installing textual would not help here
        print(f"  {_dim('•')} run `rig init` from an interactive terminal (TTY) to choose interactively")


def _setup_preview_no_tui(args: argparse.Namespace, *, reason: str) -> int:
    """`rig init` with no instructions and no way to run the wizard: a non-destructive PREVIEW.

    A bare `rig init` carries no user signal, so it must not "do a bunch of things". When the
    wizard can't run — ``reason="no-tty"`` (piped / CI / agent), ``reason="no-textual"`` (textual
    is missing from rig's environment — a broken install, since it ships WITH rig), or
    ``reason="no-tui"`` (the user opted out via ``--no-tui`` / ``RIG_NO_TUI``) — we WRITE NOTHING
    and APPLY NOTHING: we print what `rig apply` WOULD do and exactly how to proceed.
    """
    from .catalog import CatalogError
    from .config import ConfigError
    from .detect import detect_environment
    from .plan import PlanError

    if reason == "no-textual":
        # textual is a CORE dependency (ships with rig); its absence means a broken environment.
        # One line, NOT a multi-step install hint — the fix is to reinstall rig properly.
        print(_warn("textual is missing from rig's environment (a broken install) — showing a preview only."))
    elif reason == "no-tui":
        print(_warn("interactive TUI disabled (--no-tui / RIG_NO_TUI) — showing a preview only."))
    else:  # no-tty
        print(_warn("no TTY — can't launch the interactive setup wizard (piped / non-interactive run)."))
    config_exists = (detect_environment(Path(args.cwd).resolve()).repo_root / "rig.yaml").is_file()
    try:
        if config_exists:
            # a committed rig.yaml already exists: preview what `rig apply` WOULD do FROM it (the
            # advice below is "run rig apply"), not a default scaffold — the preview must match
            # the command we point the user at, or it misleads.
            plan, _loaded, _env = _load_plan(args.cwd, None)
        else:
            plan, _loaded, _env, _repo_yaml, _pending = _resolve_init_plan(args, use_default=True)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    _print_init_next_steps(config_exists=config_exists, reason=reason)
    return 0


def _preview_existing_config(args: argparse.Namespace, repo_yaml: Path) -> int:
    """Preview the plan of an ALREADY-committed rig.yaml (what `rig apply` would do), writing
    nothing. Used by the dry-run and no-TUI paths when a config already exists, so the preview
    matches the command the user is pointed at (`rig apply`) instead of a default scaffold."""
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    try:
        plan, _loaded, _env = _load_plan(args.cwd, None)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    print(_dim("\n(dry-run: nothing written, nothing applied)"))
    print(_dim(f"rig.yaml already exists at {repo_yaml} — run `rig apply commit` to apply it."))
    return 0


def _setup_headless(args: argparse.Namespace, *, use_default: bool) -> int:
    from .actions import run_plan
    from .catalog import CatalogError
    from .config import ConfigError
    from .detect import detect_environment
    from .plan import PlanError

    # The default-scaffold path (no --config) must reckon with an ALREADY-committed rig.yaml.
    if use_default:
        repo_yaml = detect_environment(Path(args.cwd).resolve()).repo_root / "rig.yaml"
        if repo_yaml.is_file():
            if args.dry_run:
                # `rig init --dry-run` on a configured repo: preview what `rig apply` WOULD do FROM
                # the committed rig.yaml (consistent with the no-TUI preview), not the default
                # scaffold the refusing `--yes` path would never write.
                return _preview_existing_config(args, repo_yaml)
            # clobber-guard, surfaced BEFORE the catalog scan: a generated default must never
            # overwrite a customized rig.yaml, and surfacing it here keeps the clear "already
            # exists → run rig apply" message even when the agent-tools catalog is unavailable
            # (preserving the pre-refactor error precedence). The explicit --config path instead
            # BACKS UP the old rig.yaml in _persist_rig_yaml — a template install is opt-in.
            print(_err(f"error: {repo_yaml} already exists."))
            print(_dim("  run `rig apply commit` to apply it, or delete it to regenerate a default."))
            return 2

    try:
        plan, _loaded, _env, repo_yaml, pending_write = _resolve_init_plan(
            args, use_default=use_default
        )
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2

    apply_now = getattr(args, "apply", False) and not args.dry_run

    # plan is valid → persist rig.yaml (the committed source of truth, never optional) unless dry-run.
    # pending_write is None only when --config already points AT the repo's own rig.yaml (nothing to
    # write) — track whether a write actually happened so the message below never claims a phantom one.
    wrote_config = bool(pending_write) and not args.dry_run
    if wrote_config:
        _persist_rig_yaml(pending_write, repo_yaml)

    # --apply: show the plan (NOT a preview — it IS being applied), then run it and report what
    # was DONE. Printing the plan first mirrors `rig apply` and makes `--plan` meaningful here too.
    if apply_now:
        _print_plan(plan, full=getattr(args, "plan", False), expand_notes=getattr(args, "notes", False))
        print(_bold(f"\nApplying {len(plan)} action(s)  [on_conflict={plan.on_conflict}]…"))
        report = run_plan(plan)
        _print_results(report)
        verify_ok = _run_and_print_verify(plan, report.results)
        return 1 if (report.errors or not verify_ok) else 0

    # default: the plan is a PREVIEW of `rig apply` — init only scaffolds the config.
    _print_plan(plan, full=getattr(args, "plan", False), preview=True, expand_notes=getattr(args, "notes", False))
    if args.dry_run:
        print(_dim("\n(dry-run: nothing written, nothing applied)"))
    elif wrote_config:
        print(_bold("\nDone — rig.yaml scaffolded (config only; NOTHING applied yet)."))
        print(_dim("Next: run `rig apply commit` to apply the plan above (or re-run init with --apply)."))
    else:
        # --config pointed at the repo's existing rig.yaml: nothing was written, nothing applied.
        print(_bold("\nNothing written (rig.yaml already in place) and NOTHING applied."))
        print(_dim("Next: run `rig apply commit` to apply the plan above (or re-run init with --apply)."))
    return 0


def _scope_categories(only: str) -> set[str]:
    """Parse ``--only`` into the set of ACTION categories to keep.

    ``ship_env`` is a drift/status-only category (the machine env file): it has no standalone
    action — it is reconciled by whichever action OWNS it. That is the ``ship_delegator`` action
    normally, and the ``gh_ship_alias`` action in the ci-only combo (delegator disabled,
    ``ci.items.ship.gh_alias`` on). This returns only the always-safe ``ship_delegator`` alias; the
    ci-only owner is pulled in by :func:`_action_in_only_scope`, which keys on the ACTION contract
    (a ``gh_ship_alias`` action that carries ``canonical_ship``) — a bare category add would also
    drag the option-less alias action that does NOT own ``ship_env`` and could clobber a user alias.
    """
    wanted = {s.strip() for s in only.split(",")}
    if "ship_env" in wanted:
        wanted.add("ship_delegator")
    return wanted


def _action_in_only_scope(action, wanted: set[str]) -> bool:
    """Whether ``action`` survives an ``--only`` filter for the ``wanted`` categories.

    A plain category match keeps the action. The one contract-level exception: ``ship_env`` has no
    standalone action, so scoping to it must ALSO keep the ci-only owner — a ``gh_ship_alias``
    action that carries ``canonical_ship`` (only then does it reconcile the env file). The
    option-less ``gh_ship_alias`` action (normal delegator-enabled plan) does NOT own ``ship_env``
    and is correctly excluded, so ``apply --only ship_env`` can never touch an unrelated alias.
    """
    if action.category in wanted:
        return True
    if "ship_env" in wanted and action.kind == "provision_gh_ship_alias":
        return bool(str(action.options.get("canonical_ship", "")).strip())
    return False


def cmd_apply(args: argparse.Namespace) -> int:
    from .catalog import CatalogError
    from .config import ConfigError
    from .plan import PlanError

    try:
        plan, loaded, env = _load_plan(args.cwd, args.config)
    except (ConfigError, CatalogError, PlanError) as exc:
        print(_err_block(exc))
        return 2

    # fail-closed: with NO config layer (no --config, no ./rig.yaml, no global), the empty
    # config resolves to built-in defaults and would mutate HOME with no committed source
    # of truth. Refuse — `rig init`/`rig export` create the rig.yaml first.
    if not loaded.layers:
        print(_err("error: no rig.yaml found (and no --config / global config)."))
        print(_dim("  run `rig init` (or `rig export -o rig.yaml`) to create one first."))
        return 2

    if args.only:
        wanted = _scope_categories(args.only)
        plan.actions = [a for a in plan.actions if _action_in_only_scope(a, wanted)]

    if _resolve_apply_mode(args) == "info":
        return _apply_info(plan, args)
    return _apply_commit(plan, args)


def _resolve_apply_mode(args: argparse.Namespace) -> str:
    """Decide preview vs execute. Preview-by-default; `commit` (or a bare `--yes`) executes.

    ``--dry-run`` always forces a preview (back-compat: `rig apply --dry-run` never mutated). A
    bare `rig apply` (``mode is None``) is a preview UNLESS ``--yes`` is given — automation that
    said `rig apply --yes` keeps executing (commit intent).
    """
    if getattr(args, "dry_run", False):
        return "info"
    if args.mode == "commit":
        return "commit"
    if args.mode == "info":
        return "info"
    return "commit" if getattr(args, "yes", False) else "info"


def _apply_info(plan, args: argparse.Namespace) -> int:
    """Preview: build + print the plan, mutate NOTHING, point at `rig apply commit`."""
    _print_plan(
        plan,
        full=getattr(args, "plan", False),
        preview=True,
        expand_notes=getattr(args, "notes", False),
    )
    print()
    if args.mode is None:
        print(_dim("(`rig apply` is an alias for `rig apply info` — preview only, nothing applied)"))
    else:
        print(_dim("(preview only — nothing applied)"))
    print(f"  run {_bold(_apply_commit_command(args))} to execute this plan")
    return 0


def _apply_commit_command(args: argparse.Namespace) -> str:
    """The exact `rig apply commit …` line to execute THIS preview — carrying the plan-defining
    flags the user passed (`-C`, `--config`, `--only`) so the suggested command reconciles the
    SAME repo/config/scope, never a broader default plan.
    """
    parts = ["rig apply commit"]
    cwd = getattr(args, "cwd", ".")
    if cwd and cwd != ".":
        parts.append(f"-C {shlex.quote(cwd)}")
    if getattr(args, "config", None):
        parts.append(f"--config {shlex.quote(args.config)}")
    if getattr(args, "only", None):
        parts.append(f"--only {shlex.quote(args.only)}")
    return " ".join(parts)


# Action kinds whose runners do slow, live-activation work (network clones, launchctl,
# tg-ctl/schedule reloads, gh API). During a commit these get a per-phase progress line so a
# long silent tail reads as progress, not a hang.
_LIVE_ACTIVATION_KINDS = frozenset({
    "provision_tmux",
    "provision_tg_ctl",
    "provision_schedule",
    "provision_spotlight",
    "provision_project_tool",
    "provision_tools",
    "provision_github_ruleset",
    "provision_github_merge",
    "provision_github_ghas",
    "provision_github_actions",
    "provision_github_browser",
})


def _apply_commit(plan, args: argparse.Namespace) -> int:
    """Execute the plan (today's `rig apply` behavior): per-phase progress on the slow live
    runners, a full log written to disk regardless of console verbosity, verify, then a single
    completion line that reflects the verify result."""
    from .actions import run_plan

    verbose = getattr(args, "verbose", False)
    _print_plan(
        plan,
        full=getattr(args, "plan", False),
        expand_notes=getattr(args, "notes", False),
    )
    live_phases = [a for a in plan.actions if a.kind in _LIVE_ACTIVATION_KINDS]
    if live_phases:
        cats = ", ".join(sorted({a.category for a in live_phases}))
        print(_dim(f"\napplying (live activation may take a moment: {cats})…"))

    def _on_start(action) -> None:
        # printed BEFORE the (slow) runner dispatches, so a hung clone/launchctl/gh call is
        # visible as an in-flight phase — silence during the op ≠ hang.
        if action.kind in _LIVE_ACTIVATION_KINDS:
            print(_dim(f"  → {action.category}/{action.item}…"), flush=True)

    started = time.monotonic()
    report = run_plan(plan, on_start=_on_start)
    elapsed = time.monotonic() - started
    _print_results(report, verbose=verbose, list_hint="-v")
    verify_ok = _run_and_print_verify(plan, report.results)
    log_path = _write_apply_log(plan, report, elapsed, verify_ok)
    # the completion line is the LAST thing printed and reflects verify: a green ✓ never precedes
    # a failed verify that flips the exit code to 1.
    _print_completion_line(report, elapsed, verify_ok=verify_ok, log_path=log_path)
    return 1 if (report.errors or not verify_ok) else 0


def _write_apply_log(plan, report, elapsed: float, verify_ok: bool) -> Path | None:
    """Write the FULL apply record (every action + result, regardless of console verbosity) to
    ``~/.cache/rig/apply-<UTC>.log`` and return its path. Best-effort: a log-write failure must
    never fail the apply, so any OSError degrades to ``None`` (no log path reported)."""
    import datetime

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(_os.path.expanduser("~/.cache/rig"))
    log_path = log_dir / f"apply-{stamp}.log"
    lines = [
        f"rig apply commit — {stamp}",
        f"on_conflict={plan.on_conflict}  actions={len(plan.actions)}  elapsed={elapsed:.2f}s",
        "",
        "PLAN:",
    ]
    lines += [f"  {a.category}/{a.item} → {a.target}" for a in plan.actions]
    if plan.notes:
        lines += ["", "NOTES:"] + [f"  {n}" for n in plan.notes]
    lines += ["", "RESULTS:"]
    lines += [f"  [{r.status}] {r.action.category}/{r.action.item}: {r.detail}" for r in report.results]
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.summary().items()))
    lines += [
        "",
        f"SUMMARY: {summary}",
        f"verify_ok={verify_ok}  errors={len(report.errors)}",
    ]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return log_path


def _print_completion_line(
    report, elapsed: float, *, verify_ok: bool = True, log_path: Path | None = None
) -> None:
    """The clear finish line on commit: ✓ applied N actions (C changed, M unchanged) in Xs.

    A verify FAILURE or an action error downgrades the ✓ to ✗ so the marker never lies about a
    run that exits non-zero. The full-detail log path is appended when one was written.
    """
    total = len(report.results)
    changed = report.changed
    unchanged = sum(1 for r in report.results if r.status == "skipped")
    errs = len(report.errors)
    failed = bool(errs) or not verify_ok
    icon = _err("✗") if failed else _ok("✓")
    reasons = []
    if errs:
        reasons.append(f"{errs} error(s)")
    if not verify_ok:
        reasons.append("verify FAILED")
    tail = f" — {_err(', '.join(reasons))}" if reasons else ""
    print(
        _bold(
            f"{icon} applied {total} action(s) "
            f"({changed} changed, {unchanged} unchanged) in {elapsed:.1f}s"
        )
        + tail
    )
    if log_path is not None:
        print(_dim(f"  full log: {log_path}"))


def _harness_settings_paths(plan) -> list[Path]:
    """The claude-code settings.json file(s) THIS config provisions hooks/permissions into.

    The actions that write the harness settings file (auto-mode, the command allowlist, the
    hook-bridge) all carry the RESOLVED settings path as ``action.target`` — honoring a
    ``harness.settings_path`` / ``permissions.settings_path`` override and the harness kind
    (e.g. a non-``auto`` mode writes the repo-local ``.claude/settings.json``, not the one
    under HOME). Resolve from THOSE so the missing-target scan inspects the file rig actually
    manages, not a hardcoded ``~/.claude/settings.json``. Deduped, order-preserving.

    Scoped to **claude-code** actions: the scanner (:func:`missing_target.scan_settings_hooks`)
    understands only the claude-code ``settings.json`` ``hooks`` shape. The allowlist write
    CAN target opencode (its ``~/.config/opencode/opencode.json`` has a different schema with no
    such ``hooks`` blocks), so an opencode ``provision_permissions`` action's target must NOT be
    scanned here — feeding it to the claude-hook scanner would misread an opencode file as a
    claude hooks file. We key off the harness kind every action carries in ``options['kind']``.
    """
    from .actions.runner import harness_settings_file

    kinds = {"apply_harness", "provision_permissions", "register_hook_bridge"}
    paths: list[Path] = []
    seen: set[Path] = set()
    for action in plan.actions:
        if action.kind not in kinds:
            continue
        if action.options.get("kind") != "claude-code":
            continue  # only the claude-code settings.json carries the hook shape we scan
        resolved = harness_settings_file(action)
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return paths


def _scan_missing_targets(settings_paths: list[Path] | None = None) -> list:
    """Scan the harness settings.json(s) for hook commands pointing at files that are gone.

    ``settings_paths`` are the resolved harness settings files THIS config manages (from
    :func:`_harness_settings_paths`). ``None`` — the ``rig doctor`` case, which loads no config —
    falls back to the claude-code default under HOME (``~/.claude/settings.json``). An EMPTY
    list is NOT the same as ``None``: it means this config provisions no harness settings file
    (e.g. harness + permissions both disabled), so there is nothing to scan — scanning the HOME
    default there would cry wolf on a file the config doesn't manage (the very false positive
    this scan exists to avoid). Findings are deduped across files by the per-finding ``what``
    line (``missing <kind>: <path>``) so the same dead target isn't reported twice.
    """
    from .missing_target import scan_settings_hooks

    if settings_paths is None:
        settings_paths = [Path(_os.path.expanduser("~/.claude/settings.json"))]
    findings: list = []
    seen: set[str] = set()
    for settings in settings_paths:
        for finding in scan_settings_hooks(settings):
            if finding.what in seen:
                continue
            seen.add(finding.what)
            findings.append(finding)
    return findings


def _scan_core_bare() -> list:
    """Scan the cwd repo + its worktrees for the core.bare corruption class.

    A working checkout with ``core.bare=true`` silently breaks every git op there + ship's
    main-refresh; this surfaces it as a structured finding with the one-line fix. Returns ``[]``
    when cwd is not a repo / git is absent (the scanner degrades to nothing, never raises).
    """
    from .core_bare import scan_repo

    try:
        cwd = Path.cwd()
    except OSError:
        return []  # cwd was deleted out from under us — honor the "never raises" contract
    return scan_repo(cwd)


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
    print(f"  toolchain: {env.stack}  type: {env.project_type}")
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


def _print_stack_preset(loaded) -> None:
    """Print the declared stack preset (or the soft-require warning + detected guess).

    Distinct from the toolchain line above: this is the by-stack curation axis. A declared stack
    is shown plainly; an absent one prints the migration-phase soft-require warning so the user
    knows to set it (per-repo stack is mandatory by policy, warned-not-enforced for now)."""
    from .config import stack_requirement_warning

    if loaded.stack:
        print(f"  stack-preset: {loaded.stack}")
        return
    warning = stack_requirement_warning(loaded)
    if warning:
        print(_warn(f"  {warning}"))


def cmd_status(args: argparse.Namespace) -> int:
    from . import errors
    from .catalog import CatalogError
    from .config import ConfigError
    from .drift import detect
    from .plan import PlanError, resolve_category_target, resolve_category_targets

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
        print(_err_block(exc))
        return 2

    print(_bold("rig status"))
    print(f"  repo: {env.repo_root}")
    print(f"  toolchain: {env.stack}  type: {env.project_type}")
    _print_stack_preset(loaded)
    cfg_src = ", ".join(loaded.layers) or "(none — built-in defaults)"
    print(f"  config layers: {cfg_src}")

    # The REPO layer only exists inside a git repository. In a non-git dir (e.g. ~) there is no
    # repo at all — do NOT nag "no rig.yaml, should be committed" (that advice applies only in a
    # real repo). Show that the repo layer is N/A and report the GLOBAL layer only.
    dropped_ship_delegator = []  # repo-scoped ship_delegator actions dropped outside git
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
        # ship_delegator carries the GLOBAL ship_env (machine env file) check, and apply does
        # NOT drop repo actions outside git — it still reconciles that file. Remember the
        # dropped actions so the env-file check can run after detect() (status/apply parity
        # for the machine-wide artifact; the repo-local delegator part stays dropped).
        dropped_ship_delegator = [a for a in repo_actions if a.category == "ship_delegator"]
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
    if loaded.category("agent_hooks").get("enabled") is not False:
        scan_hook_dirs.extend(resolve_category_targets(loaded, "agent_hooks"))
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
    if dropped_ship_delegator:
        # non-git cwd: the repo-scoped delegator actions were dropped above, but their GLOBAL
        # ship_env (machine env file) check must still run — apply reconciles that file here too.
        from .drift import check_ship_env_for_dropped_repo_action

        for action in dropped_ship_delegator:
            check_ship_env_for_dropped_repo_action(action, report)
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
    _render_area_summary(plan, report, env, extra_configured_actions=dropped_ship_delegator)

    # richer detail for two GLOBAL areas the summary counts but can't fully describe: the
    # model-freshness schedule (the daily cron time) and the tg-ctl inbound daemon (its launchd
    # boot label / unsupported-off-darwin state). Printed under the summary as its detail.
    _print_schedule_status(plan, report)
    _print_tg_ctl_status(plan, report)
    _print_tmux_autosave_status(plan)

    # missing-target: a hook command in the harness settings.json that points at a file gone
    # from disk (the dead-rtk-hook case) surfaces PROACTIVELY here, before it bites at runtime
    # as a generic "PreToolUse error". This is independent of config↔disk drift. Scan the
    # settings file(s) THIS config actually provisions (honoring a settings_path / harness-kind
    # override), not a hardcoded ~/.claude/settings.json.
    dead_targets = _scan_missing_targets(_harness_settings_paths(plan))
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
        print(_dim("\n  run `rig apply commit` to converge config→disk (extras above are left as-is)"))

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

    # `ship_env`/`gh_ship_alias` are GLOBAL machine artifacts with no key of their own — their
    # provenance is the layer that declares the delegator/alias combo. `source_for_key` tracks the
    # REAL declaring layer per key (repo OR global), so a ci-only combo declared purely in the global
    # cascade (`ship_delegator.enabled: false` + `ci.items.ship.gh_alias`) is attributed to the
    # global config even when an UNRELATED repo `rig.yaml` also exists — the case a bare repo-vs-
    # global lookup mislabels. Falls back to the primary config when the (default-on) delegator
    # declares no key, matching "would live in the repo rig.yaml".
    if category in ("ship_env", "gh_ship_alias"):
        return str(loaded.source_for_key("ship_delegator"))
    layer = layer_for_category(category)
    path = loaded.global_path if layer == GLOBAL else loaded.repo_path
    return str(path) if path is not None else "not declared in any layer"


def _area_state_line(area, plan, report, extra_configured_actions=()) -> str:
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

    # extra_configured_actions: plan-dropped actions (repo-scoped, non-git cwd) that still mark
    # their GLOBAL areas configured — see _render_area_summary. Configured-ness only, no counts.
    from itertools import chain

    configured = any(
        area_matches_action(area, a.category, a.options)
        for a in chain(plan.actions, extra_configured_actions)
    )
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


def _render_area_summary(plan, report, env, extra_configured_actions=()) -> None:
    """Print the AREA SUMMARY: every reconciled area, grouped by layer, with in-sync vs drift.

    The pre-summary status rendered ONLY drifting items as a flat dump dominated by skill rows
    (one per skill + a harness-link row each), so it read as "mostly skills" and every other
    area was buried or — when in sync — invisible. This summary enumerates the full area
    registry (:mod:`riglib.areas`) so the user sees, at a glance, the WHOLE set of things rig
    manages and where each stands: configured-and-in-sync, drifted (with a count), or not
    configured. The detailed per-item drift dump still follows below for the actual problems.

    REPO-layer areas are gated to git repos (a non-git dir has no repo layer); in a non-git dir
    only the GLOBAL areas show.

    ``extra_configured_actions`` are actions DROPPED from the plan (repo-scoped actions outside
    git) that must still mark their GLOBAL areas configured: the ship_delegator action carries
    the machine-wide ship_env artifact, and with it dropped a clean env file would render
    ``not configured`` even though apply manages the file from this cwd too. They feed only the
    configured-ness check, never drift counts (their repo-side drift stays out of a non-git run).
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
            line = _area_state_line(area, plan, report, extra_configured_actions)
            print(f"      {area.label}: {line}")


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


def _print_tmux_autosave_status(plan) -> None:
    """Report the independent tmux autosave agent's FRESHNESS (the observability half of #138).

    This is an ADVISORY runtime-health line, NOT config↔disk drift: a stale save is not something
    ``rig apply`` converges (the plist/script may be byte-perfect while the agent silently stopped
    firing), so it is surfaced here — printed under the area summary like the schedule / tg-ctl
    detail lines — and deliberately does NOT change the status exit code. It gives an OWNER to the
    question "is the newest save fresh?" that went unanswered for the month-long silent death.

    Reads the health-state file the wrapper atomically rewrites every run; ``assess_autosave_freshness``
    turns its mtime into a verdict. Silent when the saver isn't in the plan (no tmux action) or is
    disabled — no noise for repos that don't manage tmux persistence.
    """
    from riglib.actions.runner import tmux_plan_from_action
    from riglib.tmux import assess_autosave_freshness

    tmux_actions = [a for a in plan.actions if a.kind == "provision_tmux"]
    if not tmux_actions:
        return
    tplan = tmux_plan_from_action(tmux_actions[0])
    if not tplan.autosave_enabled:
        return  # nothing to report; the disabled-plist leftover (if any) is already drift
    # Only assess freshness where the agent is actually INSTALLED (its launchd plist is on disk).
    # On a machine that never applied tmux — or off darwin, where the plist is never written —
    # there is nothing to be fresh, so stay silent instead of nagging "no health record" on every
    # `rig status`. Once installed, a MISSING health record is a real signal (installed but never ran).
    if not tplan.autosave_plist_path.is_file():
        return
    health_path = tplan.autosave_health_path
    health_mtime: float | None = None
    health_result: str | None = None
    if health_path.is_file():
        try:
            health_mtime = health_path.stat().st_mtime
            data = json.loads(health_path.read_text(encoding="utf-8"))
            # a dict WITHOUT a "result" key (or "result": null) must stay None, not the string
            # "None" — str(None) is truthy and would print a confusing `result=None` note on the
            # exact corrupt/partial-write path this feature exists to surface.
            raw_result = data.get("result") if isinstance(data, dict) else None
            health_result = str(raw_result) if raw_result is not None else None
        except (OSError, ValueError):
            health_mtime = None  # unreadable/corrupt → treat as missing (flag it)
    verdict = assess_autosave_freshness(
        tplan, now_epoch=time.time(), health_mtime=health_mtime, health_result=health_result
    )
    # `disabled` is filtered upstream (the autosave_enabled early-return), so it never reaches here;
    # map the reachable states explicitly and fail closed on anything unforeseen rather than silently
    # mislabel a future/leaked state as "no health record".
    if verdict.state == "ok":
        state = _ok(f"fresh ({verdict.detail})")
    elif verdict.state == "stale":
        state = _warn(f"STALE ({verdict.detail})")
    elif verdict.state == "unhealthy":
        state = _warn(f"UNHEALTHY ({verdict.detail})")
    elif verdict.state == "missing":
        state = _warn(f"no health record ({verdict.detail})")
    else:
        raise AssertionError(f"unhandled autosave freshness state: {verdict.state!r}")
    print(f"\n  tmux autosave agent: {state}  " + _dim(f"('{tplan.autosave_label}')"))


def _print_dep_statuses(report) -> None:
    """Print the per-dependency present/absent lines + the install hint for each absent one."""
    for st in report.statuses:
        tag = "required" if st.dep.required else "optional"
        if st.present:
            print(f"  {_ok('✔')} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
            continue
        mark = _err("✗") if st.dep.required else _warn("○")
        print(f"  {mark} {st.dep.name:<12} {_dim('(' + tag + ')')}  {st.dep.why}")
        if st.install_cmd:
            print(f"      install: {_dim(' '.join(st.install_cmd))}")
        else:
            print(f"      {_dim('install: (no package mapping for this OS — install manually)')}")


def _handle_core_bare(do_fix: bool) -> bool:
    """Report (and, when ``do_fix``, repair) core.bare corruption in the cwd repo + worktrees.

    Returns True iff an UNFIXED corruption remains — the caller folds that into a non-zero exit
    (the repo-corrupt class takes precedence over a mere missing dependency). A clean scan prints
    nothing and returns False.
    """
    from .core_bare import finding_to_error, fix_core_bare

    findings = _scan_core_bare()
    if not findings:
        return False
    print(_err(_bold(f"\n  ▸ corrupted git config ({len(findings)}) — a working checkout claims core.bare=true:")))
    unfixed = False
    for finding in findings:
        err = finding_to_error(finding)
        print(f"    {_err('✗')} {err.what}")
        print(f"      {_dim('why:')} {err.why}")
        if do_fix and fix_core_bare(finding):
            print(f"      {_ok('fixed:')} set core.bare=false on {finding.path}")
            continue
        # not repaired: show the manual command. Suggest `--fix` only when it was NOT already
        # tried (a failed --fix would just fail again — don't advise re-running it). The command
        # writes the LOCAL scope; if `true` is sourced from a worktree/include scope it won't bite,
        # so always note that possibility rather than imply the one command is guaranteed.
        if do_fix:
            hint = f"{err.fix}   (if this doesn't help, `true` comes from a --worktree/[include] scope — clear it there)"
        else:
            hint = f"{err.fix}   (or re-run `rig doctor --fix`)"
        print(f"      {_ok('fix:')} {hint}")
        unfixed = True
    return unfixed


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import errors
    from .doctor import bootstrap, diagnose

    report = diagnose()
    print(_bold(f"rig doctor — {report.os.pretty}") + _dim(f"  (pkg manager: {report.os.package_manager or 'none detected'})"))
    print()
    _print_dep_statuses(report)

    # repo corruption: a working checkout with core.bare=true silently breaks every git op +
    # ship. This is the most severe class — surface it (and --fix it) ahead of any dep shortfall.
    repo_corrupt = _handle_core_bare(do_fix=args.fix)

    # missing-target: proactively flag a dead hook reference in the harness settings.json (the
    # rtk-hook case) so it's caught here, not at runtime as a generic harness error.
    dead_targets = _scan_missing_targets()
    if dead_targets:
        print(_err(_bold(f"\n  ▸ missing targets ({len(dead_targets)}) — config points at files that are gone:")))
        for f in dead_targets:
            print(f"    {_err('✗')} {f.what}")
            print(f"      {_ok('fix:')} {f.fix}")

    missing_req = report.missing_required
    only_deps_clean = not missing_req and not (args.optional and report.missing_optional)
    # repo corruption is the top-precedence failure: a broken .git makes every other git-backed
    # check unreliable. An UNFIXED corruption is non-zero regardless of the dependency picture.
    if repo_corrupt:
        # When --fix was already given but a repair failed, do NOT advise "re-run --fix" — it would
        # just fail again. The local-write manual command can also be ineffective if `true` comes
        # from a worktree/include scope (the very case fix_core_bare re-checks), so point at BOTH
        # the unwritable-config and the other-scope possibilities rather than a single command.
        if args.fix:
            print(_err("\n  the core.bare repair above FAILED — config may be unwritable, or `true` comes from a worktree/include scope; check it manually — git is broken there"))
        else:
            print(_err("\n  fix the corrupted core.bare above (or re-run `rig doctor --fix`) — git is broken there"))
        return errors.EXIT_REPO_CORRUPT
    if only_deps_clean and not dead_targets:
        print(_ok("\n  all required dependencies present"))
        return 0
    # a dead target (but no missing dep) is still a problem worth a non-zero exit.
    if only_deps_clean and dead_targets:
        print(_warn("\n  a missing target above needs attention (re-run `rig apply commit` or remove the stale entry)"))
        return errors.EXIT_MISSING_TARGET

    if not args.yes:
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


def cmd_schema(args: argparse.Namespace) -> int:
    """`rig schema` — print / verify / regenerate the rig.yaml JSON Schema.

    Default: print the generated schema to stdout (pipe it into a file or an editor config).
    ``--check``: exit 2 if the committed ``schema/rig.schema.json`` is missing or stale (the same
    drift the sync test catches — usable as a CI gate). ``--write``: regenerate the committed file.
    Bare print needs no repo; --check/--write operate on rig's OWN packaged schema file.
    """
    from . import config_schema

    if args.write:
        try:
            path = config_schema.write_schema_file()
        except OSError as exc:
            # a bare pip install puts the path under a (possibly read-only) site-packages; the
            # canonical symlink install always writes fine. Fail clearly, don't crash.
            print(_err(f"error: cannot write {config_schema.schema_file_path()}: {exc}"))
            print(_dim("  the schema file lives in the rig-cli checkout (symlink install); "
                       "run from there, or redirect: rig schema > schema/rig.schema.json"))
            return 2
        print(_ok(f"wrote {path}"))
        return 0
    if args.check:
        if config_schema.schema_file_in_sync():
            print(_ok(f"{config_schema.schema_file_path()} is in sync with the registry"))
            return 0
        print(_err(f"error: {config_schema.schema_file_path()} is missing or out of sync"))
        print(_dim("  regenerate it with: rig schema --write"))
        return 2
    print(config_schema.render_schema_json(), end="")
    return 0


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
    from .config import ConfigError, canonical_dot_path, get_path

    try:
        target, _repo_root = _config_target(args, need_repo=False)
        data = _read_target_yaml(target)
        if data is None:
            raise ConfigError(f"config file not found: {target}")
        config_path = canonical_dot_path(args.path)
        value = get_path(data, config_path)
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
    from .config import ConfigError, canonical_dot_path, coerce_scalar, set_path, validate
    from .layers import GLOBAL
    from .plan import PlanError
    from .schema import coerce as coerce_option
    from .schema import option_for_key, writable_layer_for_category
    from .state import SetupState

    try:
        target, _repo_root = _config_target(args, need_repo=True)
        config_path = canonical_dot_path(args.path)
    except ConfigError as exc:
        print(_err(f"error: {exc}"), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — environment detection failing must fail soft
        print(_err(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    category = config_path.split(".", 1)[0]
    if not args.is_global and writable_layer_for_category(category) == GLOBAL:
        print(_err(f"error: `{category}` is a global-only config block; use `--global`."), file=sys.stderr)
        print(_dim("  global-only settings are written to ~/.config/rig/config.yaml, never ./rig.yaml."),
              file=sys.stderr)
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
    if config_path == "scope" or config_path.startswith("scope."):
        print(_err("error: `scope` is a removed setting — the cascade is by location "
                   "(global vs repo), not a flag. Nothing to set."), file=sys.stderr)
        return 2

    try:
        data = _read_target_yaml(target) or {}
        # drop any legacy `scope` already in the file (mirrors config.load): we re-serialize the
        # whole file, so leaving it would re-emit a key the schema no longer recognizes.
        data.pop("scope", None)
        option = option_for_key(config_path)
        if option is not None:
            try:
                value = coerce_option(option, args.value)
            except ValueError as exc:
                raise ConfigError(str(exc), schema_path=config_path) from exc
        else:
            value = coerce_scalar(args.value)
        set_path(data, config_path, value)
        # First gate: schema validation of the WHOLE edited tree (enum/type checks). This
        # catches e.g. harness.auto_mode="yes" before anything touches disk.
        validate(data)
    except ConfigError as exc:
        print(_err_block(exc))
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
        # A ConfigError (e.g. the isolated-global validation) renders 3-part with the schema path;
        # a known catalog/plan/OS error stays one-line; an unexpected one is name-prefixed.
        if isinstance(exc, ConfigError):
            print(_err_block(exc))
        else:
            kind = "" if isinstance(exc, (CatalogError, PlanError, OSError)) else f"{type(exc).__name__}: "
            print(_err(f"error: {kind}{exc}"))
        if restored:
            print(_dim(f"  (config not changed — {target} left untouched)"))
        else:
            # the restore itself failed — be honest: the file may hold the rejected edit.
            print(_err(f"  WARNING: could not restore {target} — it may contain the rejected edit"))
        return 2

    print(_ok(f"set {config_path} = {_fmt_scalar(value)}  → {target}"))

    # RECONCILE: a config change only matters once the disk reflects it. Run the SAME apply
    # engine `rig apply` uses (scoped to the repo in front of you — a --global edit still has
    # to converge this repo). --no-apply writes the key and prints the plan only.
    _print_plan(plan, full=getattr(args, "plan", False))
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
        # `commit` is the EXECUTE subcommand — the wizard applies, so it must NOT preview.
        apply_args = build_parser().parse_args(["apply", "commit", "-C", str(root)])
        return cmd_apply(apply_args)

    # color hook so the wizard's rendered state matches the rest of the CLI's NO_COLOR handling.
    return setup_wizard.run_setup(repo_root, apply_fn=_apply, color=_c)


def cmd_config_web(args: argparse.Namespace) -> int:
    """`rig config-web …` — view/edit the reconciled rig config in a local web UI.

    A thin pass-through to :func:`riglib.config_web_service.dispatch_cli`, which routes the verb
    (run/start/stop/status/enable/disable + the internal `_serve`) through the SHARED
    agenttools-service manager. A bare `rig config-web` prints HELP and never launches a server.
    The service module is imported lazily so a missing agenttools-service lib only bites when a
    lifecycle verb is actually invoked (it raises a structured MissingDepError then), not at
    `rig --help` time.
    """
    from . import config_web_service

    return config_web_service.dispatch_cli(args)


def cmd_evolve(args: argparse.Namespace) -> int:
    """Dispatch `rig evolve` lifecycle/internal serve verbs."""
    from .evolve import service as evolve_service

    return evolve_service.dispatch_cli(args)


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
