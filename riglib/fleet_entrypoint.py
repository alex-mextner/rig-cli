"""Console router for fleet surfaces plus the existing single-repository CLI."""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "fleet" and args[1] == "config":
        from .fleet_config import main as fleet_config_main

        return fleet_config_main(args[2:])
    if args and args[0] == "fleet":
        from .fleet import main as fleet_main

        return fleet_main(args[1:])

    from .cli import main as cli_main

    return cli_main(args)
