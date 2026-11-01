"""Fixture-image wire faults; the supplier ring and SessionDB remain untouched.

The isolated acceptance image imports this serializer from ``tui_gateway.ws``,
which writes the gateway's canonical WebSocket. A native event observer records
completion even after the reader detaches. Flags live outside the task
workspace and affect only serialized copies.
"""

from copy import deepcopy
import json
from pathlib import Path

from gateway import session_controls
from tui_gateway.transport import serialize_frame as native_serialize_frame

ROOT = Path("/tmp/astrabox-hermes-protocol-fault")
_create = session_controls.AuthorityConnection._create


def capture_native(frame):
    event = frame.get("params", {})
    if event.get("type") in {"tool.complete", "message.complete"} and ROOT.is_dir():
        with (ROOT / "native-events.jsonl").open("a") as journal:
            journal.write(json.dumps(event) + "\n")


async def create(self, params, title):
    snapshot = await _create(self, params, title)
    # The native observer keeps seeing committed publications after the faulty
    # reader detaches; the WebSocket serializer sees subscribed traffic only.
    self.authority.sessions[snapshot["session_id"]].event_stream.observers.add(capture_native)
    return snapshot


session_controls.AuthorityConnection._create = create


def serialize_frame(obj, peer, log):
    wire = deepcopy(obj)

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("type") == "tool.complete" and (ROOT / "strip-classification").exists():
                value["payload"].pop("is_error", None)
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    visit(wire)
    return native_serialize_frame(wire, peer, log)
