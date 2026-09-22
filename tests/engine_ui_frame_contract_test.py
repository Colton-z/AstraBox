"""Retain the Assistant frame check until its own live tool-card coverage lands.

Agent runtimes use the real four-engine Diff/tool-card E2E instead of this
source scan. Hermes is the separate Assistant path, outside that acceptance.
"""

from __future__ import annotations

import ast
from pathlib import Path

HERMES_CLIENT = Path(__file__).resolve().parents[1] / "astrabox/core/service/orchestrator/engine/hermes_client.py"

#: Frame types whose part type the SDK decides from `dynamic`. `tool-input-error`
#: is included because the SDK reads the flag on that frame as well, regardless
#: of which frame types an individual adapter emits.
DYNAMIC_BEARING_FRAMES = frozenset(
    {"tool-input-start", "tool-input-available", "tool-input-error"}
)


def _frame_literals(source: str) -> list[tuple[int, str, set[str]]]:
    """Every dict literal that names a frame `type`, as (line, type, keys)."""

    found: list[tuple[int, str, set[str]]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys: set[str] = set()
        frame_type: str | None = None
        literal = True
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                literal = False
                break
            keys.add(key.value)
            if key.value == "type" and isinstance(value, ast.Constant):
                frame_type = value.value if isinstance(value.value, str) else None
        if literal and frame_type:
            found.append((node.lineno, frame_type, keys))
    return found


def test_assistant_tool_input_frames_declare_themselves_dynamic() -> None:
    frames = [
        (line, frame_type, keys)
        for line, frame_type, keys in _frame_literals(HERMES_CLIENT.read_text(encoding="utf-8"))
        if frame_type in DYNAMIC_BEARING_FRAMES
    ]
    assert frames, "Assistant tool-card coverage must not silently disappear"
    missing = [
        f"{HERMES_CLIENT.name}:{line} {frame_type}"
        for line, frame_type, keys in frames
        if "dynamic" not in keys
    ]
    assert not missing, (
        "these tool frames reach the browser as a part no console reader "
        f"accepts: {missing}. Add `\"dynamic\": True`."
    )
