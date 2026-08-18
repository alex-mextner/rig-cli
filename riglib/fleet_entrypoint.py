"""Console router for fleet surfaces plus the existing single-repository CLI."""

from __future__ import annotations

import sys
from typing import Sequence


def _fleet_subcommand_index(fleet_args: list[str]) -> int | None:
    """Index of the fleet-level subcommand token in ``fleet_args`` (the argv
    after "fleet"), skipping a leading ``--registry <value>`` / ``--registry=value``
    — the only fleet-level global option (see ``fleet.build_parser``). This
    only widens ROUTING: it lets ``rig fleet --registry F config ...`` reach
    ``config`` the same as ``rig fleet config --registry F ...`` does, since
    ``config`` owns its own parser once routed. It does NOT change argument
    order requirements for other subcommands — ``fleet.build_parser`` still
    defines ``--registry`` as a top-level option ahead of its subparsers, so
    e.g. ``rig fleet status --registry F`` still errors there exactly as
    before; only the routing decision (which module gets the args) is
    order-independent. Returns None if no subcommand token is present yet.
    """
    i = 0
    if i < len(fleet_args) and fleet_args[i] == "--registry":
        i += 2
    elif i < len(fleet_args) and fleet_args[i].startswith("--registry="):
        i += 1
    return i if i < len(fleet_args) else None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "fleet":
        rest = args[1:]
        idx = _fleet_subcommand_index(rest)
        if idx is not None and rest[idx] == "config":
            from .fleet_config import main as fleet_config_main

            # fleet_config.py owns its own --registry parsing, so hand it
            # everything except the "config" token itself — preserves a
            # --registry that appeared before "config" (fleet-level
            # ordering) same as one appearing after (config-level ordering).
            return fleet_config_main(rest[:idx] + rest[idx + 1 :])

        from .fleet import main as fleet_main

        return fleet_main(rest)

    from .cli import main as cli_main

    return cli_main(args)
