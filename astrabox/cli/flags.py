"""The flags every client subcommand shares.

Defined once as an argparse parent so ``--endpoint``, ``--token`` and
``--output`` land *after* the subcommand (``astrabox get agents -o json``)
rather than before it. A caller should not have to remember which flags are
global and which are the verb's.
"""

from __future__ import annotations

import argparse

from astrabox.cli.output import FORMAT_TABLE, OUTPUT_FORMATS


def connection_flags() -> argparse.ArgumentParser:
    """A parent parser carrying the deployment address, credential and format."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--endpoint",
        help=(
            "Deployment base URL (default: $ASTRABOX_ENDPOINT, else "
            "http://127.0.0.1:$ASTRABOX_SERVER_HOST_PORT with 8088 as the port's "
            "own default)."
        ),
    )
    parent.add_argument(
        "--token",
        help="Bearer token (default: $ASTRABOX_TOKEN, or an OAuth client-credentials exchange).",
    )
    parent.add_argument(
        "-o",
        "--output",
        choices=OUTPUT_FORMATS,
        default=FORMAT_TABLE,
        help="Output format (default: %(default)s).",
    )
    return parent


__all__ = ["connection_flags"]
