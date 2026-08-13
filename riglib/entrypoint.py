"""Console entrypoint that routes fleet commands before delegating to the legacy CLI parser."""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "fleet":
        from .fleet import main as fleet_main

        return fleet_main(args[1:])

    from .cli import main as cli_main

    return cli_main(args)
