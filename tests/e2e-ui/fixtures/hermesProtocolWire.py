"""Fixture-image wire faults; the supplier ring and SessionDB remain untouched.

The isolated acceptance image imports this serializer from ``tui_gateway.ws``.
Flags live outside the task workspace and affect only serialized copies.
"""

from copy import deepcopy
import json
from pathlib import Path

from tui_gateway.transport import serialize_frame as native_serialize_frame

ROOT = Path("/tmp/astrabox-hermes-protocol-fault")


def serialize_frame(obj, peer, log):
    wire = deepcopy(obj)

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("type") in {"tool.complete", "message.complete"} and ROOT.is_dir():
                with (ROOT / "native-events.jsonl").open("a") as journal:
                    journal.write(json.dumps(value) + "\n")
            if value.get("type") == "tool.complete" and (ROOT / "strip-classification").exists():
                value["payload"].pop("is_error", None)
            if "per_session_exclusive_submit" in value and (ROOT / "hide-capability").exists():
                value.pop("execution_tool_completion_is_error", None)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    visit(wire)
    return native_serialize_frame(wire, peer, log)
