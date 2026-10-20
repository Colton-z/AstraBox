"""A local stdio MCP fixture; only real tools/call requests write receipts."""

import json
import os
import sys
from pathlib import Path

name, directory = sys.argv[1:]
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": name, "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "record", "description": "Record the supplied acceptance marker locally.",
            "inputSchema": {"type": "object", "properties": {"marker": {"type": "string"}},
                            "required": ["marker"], "additionalProperties": False},
        }]}
    elif method == "tools/call" and message["params"]["name"] == "record":
        marker = message["params"]["arguments"]["marker"]
        receipt = {"server": name, "marker": marker, "uid": os.geteuid()}
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        with (target / f"{name}.jsonl").open("a") as output:
            output.write(json.dumps(receipt) + "\n")
        result = {"content": [{"type": "text", "text": json.dumps(receipt)}]}
    elif method == "ping":
        result = {}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": "Unknown fixture method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
