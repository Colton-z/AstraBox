"""Read-only MCP with real startup identity and connection receipts."""
import json
import os
from pathlib import Path
import sys
import time
from mcp.server.fastmcp import FastMCP

kind = sys.argv[1]
home = Path.home()
journal = home / ".session-mcp-receipts.jsonl"


def record(event, **fields):
    with journal.open("a") as stream:
        stream.write(json.dumps({"event": event, "kind": kind, "pid": os.getpid(),
                                 "at": time.time(), **fields}) + "\n")


record("start")
if kind == "shared":
    identity = {"principal_id": "shared", "session_id": None}
else:
    path = os.environ.get("MCP_IDENTITY_MATERIAL")
    if not path or not Path(path).is_file() or (home / ".session-mcp-deny").exists():
        record("authorization_required")
        raise RuntimeError("Personal authorization required")
    identity = json.loads(Path(path).read_text())
    if identity["revision"] != os.environ.get("MCP_IDENTITY_REVISION"):
        record("stale_environment")
        raise RuntimeError("Identity material and process environment differ")
    time.sleep(max(0, identity.pop("not_before") - time.time()))
record("identity_bound", **identity)
server = FastMCP("fixture-" + kind)


@server.tool()
def whoami() -> dict:
    """Return this connection's identity without changing any business data."""
    record("call", **identity)
    return {"kind": kind, **identity}


server.run(transport="stdio")
