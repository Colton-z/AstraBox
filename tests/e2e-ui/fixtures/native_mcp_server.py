"""Real HTTP MCP fixture serving exactly one selected tool for one E2E case.

The MCP SDK owns protocol handling, cancellation and the ``_meta`` wire shape.
``--tool hang`` records an invocation and withholds its response: the fixture
never produces a timeout error, only Claude's configured deadline can.
``--tool large_output`` returns a deterministic head/body/tail text of the
requested size and advertises ``anthropic/maxResultSizeChars``: the fixture
never writes a persisted-output wrapper, only Claude Code can. Each run
registers one tool, so discovery from the client sandbox names exactly the
tool its case is about.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

HANG_DELAY_SECONDS = 120
LARGE_OUTPUT_MAX_RESULT_SIZE_CHARS = 4096
LARGE_OUTPUT_FILLER = "m"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tool", choices=("hang", "large_output"), required=True)
    args = parser.parse_args()
    server = MCPServer(f"e2e-native-{args.tool}")

    def record(**fields: object) -> None:
        with args.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"tool": args.tool, **fields, "timestamp": time.time()}) + "\n")

    if args.tool == "hang":

        @server.tool(structured_output=False)
        async def hang(delay_seconds: int) -> str:
            """Accept a call and withhold the answer for delay_seconds seconds."""
            if delay_seconds != HANG_DELAY_SECONDS:
                raise ValueError(f"this fixture requires delay_seconds={HANG_DELAY_SECONDS}")
            record(phase="started", delay_seconds=delay_seconds)
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                record(phase="cancelled", delay_seconds=delay_seconds)
                raise
            record(phase="returned", delay_seconds=delay_seconds)
            return "unexpected delayed response"

    else:

        @server.tool(
            structured_output=False,
            meta={"anthropic/maxResultSizeChars": LARGE_OUTPUT_MAX_RESULT_SIZE_CHARS},
        )
        async def large_output(head_marker: str, tail_marker: str, output_size: int, ctx: Context) -> str:
            """Return head_marker, filler and tail_marker totalling output_size characters."""
            prefix = head_marker + "\n"
            suffix = "\n" + tail_marker
            if output_size < len(prefix) + len(suffix) + 1:
                raise ValueError(f"output_size={output_size} cannot hold both markers")
            response = (
                prefix + LARGE_OUTPUT_FILLER * (output_size - len(prefix) - len(suffix)) + suffix
            )
            record(
                phase="returned",
                head_marker=head_marker,
                tail_marker=tail_marker,
                output_size=output_size,
                output_chars=len(response),
                max_result_size_chars=LARGE_OUTPUT_MAX_RESULT_SIZE_CHARS,
                response=response,
                request_id=ctx.request_id,
                request_method=ctx.request_context.method,
                # Observe only the non-secret E2E header from this real call.
                # The expected value is never supplied to the fixture server.
                request_headers={
                    key.lower(): value for key, value in (ctx.headers or {}).items()
                    if key.lower() == "x-astrabox-e2e-extra"
                },
            )
            return response

    args.log.touch(exist_ok=False)
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=args.port,
        json_response=True,
    )


if __name__ == "__main__":
    main()
