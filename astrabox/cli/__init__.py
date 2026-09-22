"""The ``astrabox`` console script: one parser over two command halves.

``astrabox = "astrabox.cli:main"`` (pyproject ``[project.scripts]``).

* **Operator commands** run a deployment from the inside —
  :mod:`astrabox.cli.serve` boots the FastAPI application and the verification
  commands exercise a live deployment. The container image's ENTRYPOINT is
  ``astrabox`` and its CMD is ``serve``, so ``docker run <image>`` lands there.
* **Client commands** configure a deployment that is already running, over its
  HTTP API — :mod:`astrabox.cli.resources` — and start, stop and probe the
  maintained local one — :mod:`astrabox.cli.stack`.

Every client command shares one failure contract: the exit code says what to
fix (:mod:`astrabox.cli.output`), and ``--output json`` prints one JSON object
for success and for failure alike, so a caller parsing stdout never has to
handle two shapes. ``docs/maintainers/design-developer-cli-2026-08.md`` records
why the surface is shaped this way.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from astrabox.cli.output import (
    EXIT_USAGE,
    FORMAT_TABLE,
    CliError,
    emit_failure,
)

#: Exit code for an interrupted run, matching the shell's SIGINT convention.
_EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``astrabox`` argument parser with its subcommands."""
    from astrabox.cli import mcp_server, resources, run, scaffold, serve, stack

    parser = argparse.ArgumentParser(
        prog="astrabox",
        description=(
            "AstraBox — open self-hosted agent runtime. "
            "Run a deployment, and configure one from a terminal or a script."
        ),
        epilog=(
            "Field names for an astrabox.yaml resource come from the "
            "deployment itself: `astrabox schema agent` and "
            "`astrabox schema environment` print the contract it enforces on "
            "write."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve.register(subparsers)
    stack.register(subparsers)
    scaffold.register(subparsers)
    resources.register(subparsers)
    run.register(subparsers)
    mcp_server.register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point.

    Parses ``argv`` (defaults to ``sys.argv[1:]``), dispatches to the selected
    subcommand handler, and returns its exit code. ``required=True`` on the
    subparser makes a bare ``astrabox`` invocation fail loud with usage text
    rather than silently doing nothing.

    A :class:`~astrabox.cli.output.CliError` from any handler is rendered in the
    format that handler was asked for and becomes the process exit code, so a
    caller reads one result shape whether the command succeeded or not.
    """
    # Fill process env from .env FIRST so the parser's --host/--port defaults
    # and every downstream os.getenv read see one config source; real env
    # vars still take precedence over .env values.
    from astrabox.config.settings import load_env_file_into_process_env

    load_env_file_into_process_env()

    parser = _build_parser()
    args = parser.parse_args(argv)
    output = str(getattr(args, "output", FORMAT_TABLE))
    try:
        return int(args.func(args))
    except CliError as error:
        emit_failure(error, output=output)
        return error.exit_code
    except KeyboardInterrupt:
        print("astrabox: interrupted", file=sys.stderr)
        return _EXIT_INTERRUPTED
    except SystemExit as exit_request:
        # `astrabox serve` refuses an unauthenticated non-loopback bind by
        # raising SystemExit with its reason; keep that message and map it onto
        # the usage exit code rather than letting a bare traceback escape.
        code = exit_request.code
        if isinstance(code, int):
            return code
        if code:
            print(str(code), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
