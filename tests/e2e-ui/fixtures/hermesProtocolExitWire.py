"""Lose a native close acknowledgement in the old-contract exit fixture."""

from copy import deepcopy
import json
from pathlib import Path

from tui_gateway.transport import serialize_frame as native_serialize_frame

ROOT = Path("/tmp/astrabox-hermes-protocol-exit")


def serialize_frame(obj, peer, log):
    wire = deepcopy(obj)
    if isinstance(wire, dict):
        result = wire.get("result")
        if isinstance(result, dict) and "closed" in result and (ROOT / "lose-close-ack").exists():
            wire.pop("result")
            wire["error"] = {"code": -32090, "message": "E2E native close acknowledgement lost"}
    return native_serialize_frame(wire, peer, log)
