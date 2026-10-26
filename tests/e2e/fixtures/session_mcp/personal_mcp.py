"""Read-only MCP with real startup identity and connection receipts."""
import json
import os
from pathlib import Path
import sys
import time

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

# The same dependency-free fixture transport as fixtures/prepared_plugin/server.py.
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture-" + kind, "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "whoami", "description": "Read this connection's identity.",
                             "inputSchema": {"type": "object", "properties": {},
                                             "additionalProperties": False}}]}
    elif method == "tools/call" and message["params"]["name"] == "whoami":
        record("call", **identity)
        result = {"content": [{"type": "text", "text": json.dumps({"kind": kind, **identity})}]}
    elif method == "ping":
        result = {}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": "Unknown fixture method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
