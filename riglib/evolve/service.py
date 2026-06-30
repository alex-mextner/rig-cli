"""Service seam for `rig evolve`.

This mirrors `rig config-web`: parser construction stays stdlib-only, lifecycle verbs lazy-load
the shared `agenttools-service` library, and the daemon target is an internal `_serve` CLI verb.
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from riglib import errors
from riglib.evolve.web import DEFAULT_PORT, HOST, EvolveApp

if TYPE_CHECKING:  # pragma: no cover
    from agenttools_service import Service, ServiceManager

SERVICE_NAME = "evolve"
SERVICE_TOOL = "rig"
SERVE_VERB = "_serve"
LIFECYCLE_VERBS = ("run", "start", "stop", "status", "enable", "disable")

_VERB_HELP = {
    "run": "run evolve in the foreground (this shell), blocking",
    "start": "start evolve in the background (detached daemon)",
    "stop": "stop the background evolve instance",
    "status": "show whether evolve is running (pid/port/url)",
    "enable": "install OS autostart for evolve AND start it now",
    "disable": "remove OS autostart for evolve AND stop it",
}

_INSTALL_HINT = (
    f"uv pip install --python {shlex.quote(sys.executable)} "
    "-e <agent-tools>/lib/agenttools_daemon "
    "-e <agent-tools>/lib/agenttools_service"
)


def _service_name(repo_root: Path) -> str:
    digest = hashlib.sha1(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{SERVICE_NAME}-{digest}"


def _load_service_module() -> Any:
    try:
        import agenttools_service  # noqa: PLC0415
    except ImportError as exc:
        raise errors.MissingDepError(
            what="evolve needs the shared service manager 'agenttools-service'",
            why="rig evolve delegates run/start/stop/status/enable/disable to the shared service manager.",
            fix=_INSTALL_HINT,
        ) from exc
    return agenttools_service


def _riglib_parent() -> Path:
    return Path(__file__).resolve().parents[2]


def _serve_argv(repo_root: Path, port: int) -> list[str]:
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(_riglib_parent())!r}); "
        "from riglib.cli import main; raise SystemExit(main())"
    )
    return [
        sys.executable,
        "-c",
        bootstrap,
        "evolve",
        SERVE_VERB,
        "--port",
        str(port),
        "-C",
        str(repo_root),
    ]


def build_service(repo_root: Path, *, port: int = DEFAULT_PORT, svc_mod: Any = None) -> "Service":
    svc_mod = svc_mod or _load_service_module()
    return svc_mod.Service(
        name=_service_name(repo_root),
        argv=_serve_argv(repo_root, port),
        port=port,
        host=HOST,
        tool=SERVICE_TOOL,
        description="rig evolve — local project evolution portal",
    )


def _manager(repo_root: Path, port: int, *, svc_mod: Any = None) -> "ServiceManager":
    svc_mod = svc_mod or _load_service_module()
    return svc_mod.ServiceManager(build_service(repo_root, port=port, svc_mod=svc_mod))


def _repo_root(args: argparse.Namespace) -> Path:
    from riglib.detect import detect_environment

    return detect_environment(Path(getattr(args, "cwd", ".")).resolve()).repo_root


def register(subparsers: "argparse._SubParsersAction[Any]") -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "evolve",
        help="local project evolution portal (run|start|stop|status|enable|disable)",
        description="Serve the local project evolution portal: git histogram, proportional code treemap, "
        "and ecosystem-provider health. A bare `rig evolve` prints help, never launches.",
    )
    _add_target_args(parser)
    parser.set_defaults(_evolve_parser=parser)
    verbs = parser.add_subparsers(dest="evolve_verb", metavar="<verb>")
    serve = verbs.add_parser(SERVE_VERB, help="(internal) run the foreground server; used by run/start")
    _add_target_args(serve, suppress_default=True)
    for verb in LIFECYCLE_VERBS:
        vp = verbs.add_parser(verb, help=_VERB_HELP[verb])
        _add_target_args(vp, suppress_default=True)
    return parser


def _add_target_args(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    cwd_default: Any = argparse.SUPPRESS if suppress_default else "."
    port_default: Any = argparse.SUPPRESS if suppress_default else DEFAULT_PORT
    parser.add_argument("-C", "--cwd", default=cwd_default, help="repo root to inspect (default: cwd)")
    parser.add_argument("--port", type=_tcp_port, default=port_default, help=f"port to serve on (default: {DEFAULT_PORT})")


def _tcp_port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {raw!r}") from None
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {port}")
    return port


def dispatch_cli(args: argparse.Namespace) -> int:
    verb = getattr(args, "evolve_verb", None)
    parser = getattr(args, "_evolve_parser", None)
    if not verb:
        if parser is not None:
            parser.print_help()
        return 0
    port = int(getattr(args, "port", DEFAULT_PORT))
    if verb == SERVE_VERB:
        try:
            EvolveApp(repo_root=_repo_root(args)).serve(port=port, open_browser=False)
        except OSError as exc:
            raise errors.ConfigError(
                what=f"evolve could not start on port {port}",
                why=str(exc),
                fix="try a different --port, or `rig evolve status` / `stop` to free it.",
            ) from exc
        return 0
    svc_mod = _load_service_module()
    manager = _manager(_repo_root(args), port, svc_mod=svc_mod)
    return int(svc_mod.run_action(manager, verb))
