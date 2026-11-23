"""Probe one port and print one JSON line `{"chip", "mac", "error"}`; run by the bench server.

`python -m rustyfarian_bench.probe_port <port>`. It is a module, not a CLI subcommand, so the
consumer-facing CLI stays as it is. The server only passes ports from its own listing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from . import device


def main(argv: Sequence[str]) -> int:
    chip: str | None = None
    mac: str | None = None
    error: str | None = None
    if len(argv) != 1 or argv[0].startswith("-"):
        error = "usage: probe_port PORT"
    else:
        try:
            found, failure = device.probe(argv[0])
        except device.MissingTool as e:
            error = str(e)
        else:
            if found is None:
                error = failure or "no chip answered"
            else:
                chip, mac = found.chip, found.mac
    print(json.dumps({"chip": chip, "mac": mac, "error": error}), flush=True)
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
