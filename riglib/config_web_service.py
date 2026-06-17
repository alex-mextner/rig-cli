"""config-web service seam — wires the config-web server into the SHARED service manager.

What this is
------------
The bridge between :mod:`riglib.config_web` (the stdlib HTTP app) and ``agenttools_service``
— the ONE reusable service-manager every long-running server in the agent-tools ecosystem
shares (the ``review`` dashboard, ``tg-ctl``, the daemon-supervisor, and now config-web). It
builds the :class:`~agenttools_service.Service` descriptor for config-web and wires the
``rig config-web run|start|stop|status|enable|disable`` verbs through the library's argparse
helper — so config-web gets identical lifecycle semantics to every other service, and the
OS-autostart machinery (macOS launchd LaunchAgent / Linux systemd ``--user`` unit, with a
no-systemd fallback) lives ONCE in the shared lib, never copied per tool.

The ACTUAL ``agenttools_service`` interface this builds against
---------------------------------------------------------------
The shared lib models a service as a SUBPROCESS COMMAND, not a callable:

- ``Service(name, argv, port=, host=, tool=, description=)`` — ``argv`` is the command that
  runs the server **in the foreground** (the thing ``run`` blocks on and ``start`` detaches);
  the lib derives the pidfile/logfile/autostart-unit paths under ``$XDG_STATE_HOME`` /
  ``$XDG_CACHE_HOME`` (``~/.local/state`` / ``~/.cache``) from ``tool`` + ``name``.
- ``ServiceManager(service)`` — ``.run()`` foreground/blocking; ``.start()`` background
  detached; ``.status()``; ``.stop()``; ``.enable()`` (install OS autostart + start now);
  ``.disable()`` (remove autostart + stop). Idempotent + removable.
- ``add_service_subcommands(subparsers, manager_factory=, service_name=)`` wires the
  ``run|start|stop|status|enable|disable`` verbs into an argparse **subparsers** object,
  tagging each with the action + a lazy manager factory.
- ``dispatch(args, on_no_subcommand=)`` runs the chosen action (printing a one-line result),
  or calls ``on_no_subcommand`` when no verb was given — a bare invocation prints HELP and
  NEVER launches.

Because ``argv`` is a subprocess command, the foreground server is reached via an INTERNAL
CLI verb — ``rig config-web _serve --port N -C <root>`` — which calls
:meth:`riglib.config_web.ConfigWebApp.serve` and blocks. ``run``/``start``/``enable`` exec
exactly that command. The argv[0] is the running Python interpreter
(``sys.executable -m riglib …``): absolute (launchd runs jobs with a minimal PATH, so a bare
``rig`` may not resolve — see ``render_launchd_plist``'s PATH caveat) and not dependent on a
``rig`` symlink being on PATH.

How it is reached at runtime
----------------------------
``riglib.cli`` calls :func:`register` to attach the ``config-web`` subparser + its verbs, then
routes a parsed ``config-web`` invocation to :func:`dispatch_cli`. Both lazy-import
``agenttools_service`` so ``rig --help`` / ``rig doctor`` and the rest of the CLI keep working
even when the (sibling) service library is not installed — a missing library yields a clear,
actionable :class:`riglib.errors.MissingDepError` (exit 127), not an import crash.

Dependency note
---------------
``agenttools-service`` is a SIBLING agent-tools nested lib (it depends on
``agenttools-daemon``, also stdlib-only). It is deliberately NOT a declared dependency in
``pyproject.toml``: neither lib is published to PyPI (they live under the agent-tools checkout
at ``lib/agenttools_service`` / ``lib/agenttools_daemon``), so a versioned requirement would
make ``pip install`` / ``uv run`` unresolvable for everyone. config-web LAZY-imports it instead;
until it is installed the import fails closed with the install command (see ``_INSTALL_HINT``).
config-web carries NO copy of the autostart machinery — sharing the lib is the point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import errors
from .config_web import DEFAULT_PORT, HOST, ConfigWebApp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agenttools_service import Service, ServiceManager

# The service id BASE. The lib slugs the per-repo service name into the pidfile/logfile + autostart
# label/unit (``com.agenttools.rig.config-web-<hash>`` / ``agenttools-rig-config-web-<hash>``).
SERVICE_NAME = "config-web"
SERVICE_TOOL = "rig"


def _service_name(repo_root: Path) -> str:
    """A PER-REPO service name so config-web for different repos never share a pidfile/autostart.

    The daemon argv bakes in a specific repo's ``-C``, so a single fixed ``config-web`` name would
    make ``start`` for repo A and repo B collide on the same pidfile/unit — ``status``/``stop`` would
    then act on whichever repo started last (a cross-repo footgun codex flagged). Append a short
    stable hash of the resolved repo path to the base name; the result stays slug-safe
    (``[A-Za-z0-9_.-]``) so the lib can derive a valid launchd label / systemd unit from it.
    """
    import hashlib

    digest = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{SERVICE_NAME}-{digest}"

# The lifecycle verbs the shared lib wires (mirrored from agenttools_service.SUBCOMMANDS only
# so HELP + the dispatch routing can name them WITHOUT importing the lib at parser-build time —
# parser construction runs on every `rig` invocation and must stay import-light).
LIFECYCLE_VERBS = ("run", "start", "stop", "status", "enable", "disable")

# Per-verb help (so `rig config-web --help` documents each without the lib). These mirror the
# shared lib's semantics; the lib still owns the actual behaviour at dispatch time.
_VERB_HELP = {
    "run": "run config-web in the foreground (this shell), blocking",
    "start": "start config-web in the background (detached daemon)",
    "stop": "stop the background config-web instance",
    "status": "show whether config-web is running (pid/port/url)",
    "enable": "install OS autostart for config-web AND start it now",
    "disable": "remove OS autostart for config-web AND stop it",
}

# The hidden verb the daemon's argv targets: it runs the foreground server and blocks. Not part
# of LIFECYCLE_VERBS — it is the implementation `run`/`start`/`enable` exec, not a user verb.
SERVE_VERB = "_serve"

_INSTALL_HINT = (
    "pip install -e <agent-tools>/lib/agenttools_daemon "
    "<agent-tools>/lib/agenttools_service   "
    "(both are agent-tools nested libs, not on PyPI; daemon is service's dependency)"
)


def _load_service_module() -> Any:
    """Import ``agenttools_service`` lazily, or raise a structured :class:`MissingDepError`.

    Lazy by design: the lib is a sibling package that may not be installed, and ``rig --help`` /
    ``rig doctor`` must not crash when it is absent. The raised error renders rig's standard
    what/why/fix block (exit 127), not a bare ImportError traceback.
    """
    try:
        import agenttools_service  # noqa: PLC0415 (lazy by design — the lib may be absent)
    except ImportError as exc:
        raise errors.MissingDepError(
            what="config-web needs the shared service manager 'agenttools-service'",
            why="agenttools-service (an agent-tools nested lib) is not importable in this "
            "environment; config-web delegates its whole lifecycle + OS-autostart to it.",
            fix=_INSTALL_HINT,
        ) from exc
    return agenttools_service


def _riglib_parent() -> Path:
    """The directory that CONTAINS the ``riglib`` package (so it can go on a subprocess sys.path).

    This module lives at ``<parent>/riglib/config_web_service.py``; its grandparent is ``<parent>``.
    Resolved so a daemon launched from any cwd can import ``riglib`` even when rig is installed as a
    SYMLINK into ``~/.local/bin`` (the checked-out repo IS the binary — there is no pip-installed
    ``riglib`` on the default ``sys.path``). For a real pip/pipx install the package is already
    importable, and prepending its own parent is a harmless no-op.
    """
    return Path(__file__).resolve().parent.parent


def _serve_argv(repo_root: Path, port: int) -> list[str]:
    """The foreground command the daemon runs: ``<python> -c <bootstrap> config-web _serve …``.

    argv[0] is :data:`sys.executable` (an ABSOLUTE interpreter path) so a launchd LaunchAgent —
    which runs with a minimal PATH that may not contain a ``rig`` shim — still starts. We do NOT use
    ``-m riglib``: a daemon runs from an arbitrary cwd, and when rig is installed as a SYMLINK (the
    checked-out repo IS the binary, not a pip package) ``riglib`` is not on the default
    ``sys.path``, so ``-m riglib`` would fail with ``No module named riglib``. Instead a tiny ``-c``
    bootstrap prepends the package's parent dir to ``sys.path`` (mirroring what ``bin/rig`` does for
    the symlink install) and then calls the CLI ``main`` — working for BOTH a symlink checkout and a
    real install. ``repo_root`` is captured as an absolute ``-C`` so a daemonized instance serves
    the SAME repo's config the CLI was launched against, regardless of the daemon's own cwd.
    """
    # The bootstrap reads the remaining argv (config-web _serve …) via sys.argv[1:] after `-c`.
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(_riglib_parent())!r}); "
        "from riglib.cli import main; raise SystemExit(main())"
    )
    return [
        sys.executable,
        "-c",
        bootstrap,
        "config-web",
        SERVE_VERB,
        "--port",
        str(port),
        "-C",
        str(repo_root),
    ]


def build_service(repo_root: Path, *, port: int = DEFAULT_PORT, svc_mod: Any = None) -> "Service":
    """Build the config-web :class:`~agenttools_service.Service` descriptor (real lib API).

    The service's ``argv`` is the internal ``_serve`` command (see :func:`_serve_argv`); the
    shared lib derives the pidfile/logfile/autostart unit from ``tool``/``name`` and the URL
    from ``host``/``port``. Raises :class:`MissingDepError` if the lib is not installed. ``svc_mod``
    lets a caller pass an already-loaded module so the import happens once per CLI invocation.
    """
    svc_mod = svc_mod or _load_service_module()
    return svc_mod.Service(
        name=_service_name(repo_root),
        argv=_serve_argv(repo_root, port),
        port=port,
        host=HOST,
        tool=SERVICE_TOOL,
        description="rig config-web — local web UI to view/edit the reconciled rig config",
    )


def _manager(repo_root: Path, port: int, *, svc_mod: Any = None) -> "ServiceManager":
    """Build the :class:`~agenttools_service.ServiceManager` wrapping the config-web service.

    ``svc_mod`` reuses an already-loaded lib module (avoids a redundant second import on the
    dispatch path where the caller already loaded it).
    """
    svc_mod = svc_mod or _load_service_module()
    return svc_mod.ServiceManager(build_service(repo_root, port=port, svc_mod=svc_mod))


def _repo_root(args: argparse.Namespace) -> Path:
    """Resolve ``-C/--cwd`` to the REPO ROOT, like ``config set``/``setup`` — not the literal cwd.

    Running ``rig config-web`` from a SUBDIRECTORY must still serve the repo's root ``rig.yaml``
    (otherwise the page looks at ``subdir/rig.yaml`` — absent — and rejects every repo edit). We
    detect the git root the same way the rest of the CLI does. Outside a git repo,
    ``detect_environment`` falls back to the directory itself, so a non-repo cwd still works.
    """
    from .detect import detect_environment  # lazy: keeps module import light

    return detect_environment(Path(getattr(args, "cwd", ".")).resolve()).repo_root


def register(subparsers: "argparse._SubParsersAction[Any]") -> argparse.ArgumentParser:
    """Add the ``config-web`` subparser + its lifecycle verbs (run/start/stop/…) to the CLI.

    Registers the verbs as plain argparse choices and does NOT import ``agenttools_service`` here —
    parser construction runs on EVERY ``rig`` invocation (including ``rig --help`` / ``rig
    doctor``), and the repo's hard rule is zero optional/sibling imports at that time. The lib is
    imported lazily in :func:`dispatch_cli`, only when a lifecycle verb actually runs; absent, that
    verb raises the actionable :class:`MissingDepError`. A bare ``rig config-web`` (no verb) prints
    HELP — never launches.

    ``-C/--cwd`` and ``--port`` live on the config-web parser AND on each verb subparser, so
    ``rig config-web start --port 9000`` and ``rig config-web --port 9000 start`` both work
    (argparse can't see a parent-level option after a subcommand token, so the verb carries its
    own copy — same dest, so dispatch reads one value).
    """
    cw = subparsers.add_parser(
        "config-web",
        help="local web UI to view/edit the reconciled rig config "
        "(run|start|stop|status|enable|disable)",
        description=(
            "Serve a local web UI to view and edit the reconciled rig config (the cascade of the "
            "global ~/.config/rig/config.yaml + the repo ./rig.yaml). Edits route to the owning "
            "layer, exactly like `rig config set` / the wizard; reconcile with `rig apply`. "
            "Lifecycle is the shared agenttools-service manager (identical run/start/stop/status/"
            "enable/disable to every ecosystem service); OS autostart is launchd (macOS) / systemd "
            "--user (Linux). A bare `rig config-web` with no verb prints this help, never launches."
        ),
    )
    _add_target_args(cw)
    cw.set_defaults(_config_web_parser=cw)

    verb_subs = cw.add_subparsers(dest="config_web_verb", metavar="<verb>")

    # The internal foreground-server verb the daemon's argv targets. Registered so
    # `<python> -c <bootstrap> config-web _serve …` parses; it bypasses the service manager entirely
    # and blocks in ConfigWebApp.serve. argparse can't fully hide a subparser (passing
    # help=SUPPRESS leaks a literal "==SUPPRESS==" row), so it carries a terse "internal" help and
    # its leading underscore marks it as not-a-user-verb.
    serve = verb_subs.add_parser(SERVE_VERB, help="(internal) run the foreground server; used by run/start")
    # suppress_default like the lifecycle verbs so a pre-verb `--port`/`-C` is not clobbered back to
    # the default by the _serve subparser (consistency; the daemon argv puts the flags AFTER _serve,
    # but the parent-first form must work too).
    _add_target_args(serve, suppress_default=True)

    # The lifecycle verbs as bare parsers (no lib import). dispatch_cli routes each through the
    # shared lib's run_action at call time. suppress_default=True so a verb-level -C/--port default
    # does not clobber a value parsed at the config-web level (`rig config-web -C /repo status`
    # would otherwise silently fall back to cwd="."/port=8787 — argparse can't see a parent-level
    # option after the subcommand token, so the verb carries its own SUPPRESS copy of the dest).
    for verb in LIFECYCLE_VERBS:
        vp = verb_subs.add_parser(verb, help=_VERB_HELP[verb])
        _add_target_args(vp, suppress_default=True)
    return cw


def _add_target_args(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    """Add the shared ``-C/--cwd`` + ``--port`` to a parser (declared once so they never drift).

    ``suppress_default=True`` (for the per-verb subparser copies) uses ``default=SUPPRESS`` so the
    dest is set ONLY when the flag is actually present — it must not clobber a value already parsed
    at the config-web level (where the real defaults live). See :func:`register` for why.
    """
    cwd_default: Any = argparse.SUPPRESS if suppress_default else "."
    port_default: Any = argparse.SUPPRESS if suppress_default else DEFAULT_PORT
    parser.add_argument(
        "-C", "--cwd", default=cwd_default, help="repo root to serve config for (default: cwd)"
    )
    parser.add_argument(
        "--port", type=_tcp_port, default=port_default,
        help=f"port to serve on, 1..65535 (default: {DEFAULT_PORT})",
    )


def _tcp_port(raw: str) -> int:
    """An argparse type for a TCP port: a 1..65535 int. Rejects 0 / out-of-range / non-numeric at
    PARSE time with a clean usage error, so a bad ``--port 0`` / ``--port 70000`` never reaches the
    server (where it would raise a raw OverflowError/ValueError or fail the child process)."""
    try:
        port = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {raw!r}") from None
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {port}")
    return port


def dispatch_cli(args: argparse.Namespace) -> int:
    """Run a parsed ``config-web`` invocation. Returns a process exit code.

    Routing:
    - no verb            → print HELP, return 0 (NEVER launch — the hard rule).
    - the ``_serve`` verb → run the foreground server in THIS process (the daemon's target),
      blocking until interrupted; this is what ``run``/``start``/``enable`` exec.
    - a lifecycle verb   → build the shared-lib :class:`ServiceManager` and run the matching
      operation via the lib's :func:`run_action` (prints a one-line result).

    The shared lib is imported LAZILY here (not at parser-build time), so ``rig --help`` stays
    free of the optional sibling import; absent, a lifecycle verb raises the actionable
    :class:`MissingDepError`.
    """
    verb = getattr(args, "config_web_verb", None)
    parser = getattr(args, "_config_web_parser", None)

    if not verb:
        # bare command → HELP, never launch.
        if parser is not None:
            parser.print_help()
        return 0

    if verb == SERVE_VERB:
        # The foreground server itself (the daemon's argv target). Block here. A bind failure (busy
        # port) raises OSError from serve(); convert it to a STRUCTURED RigError so `rig config-web
        # run`/`_serve` on an occupied port prints the standard what/why/fix block (exit 2), not a
        # raw traceback — `cli.main`'s top-level guard only renders RigError.
        port = int(getattr(args, "port", DEFAULT_PORT))
        try:
            ConfigWebApp(repo_root=_repo_root(args)).serve(port=port, open_browser=False)
        except OSError as exc:
            raise errors.ConfigError(
                what=f"config-web could not start on port {port}",
                why=str(exc),
                fix="try a different --port, or `rig config-web status` / `stop` to free it.",
            ) from exc
        return 0

    # A lifecycle verb. Import the shared lib now (raises MissingDepError if absent), build the
    # manager for the resolved repo/port, and run the action through the lib's run_action — the
    # SAME one-line-result + exit-code contract every ecosystem service uses. Reuse the loaded
    # module for the manager so the lib is imported once on this path.
    svc_mod = _load_service_module()
    manager = _manager(_repo_root(args), int(getattr(args, "port", DEFAULT_PORT)), svc_mod=svc_mod)
    return int(svc_mod.run_action(manager, verb))
