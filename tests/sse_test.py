from __future__ import annotations

import json

from astrabox.api.sse import (
    format_sse_done,
    format_sse_event,
    format_sse_keepalive,
)


def test_sse_event_is_compact_unicode_safe_and_terminated() -> None:
    line = format_sse_event({"type": "text-delta", "delta": "café ☕"})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    assert "café ☕" in line
    body = line[len("data: ") : -2]
    assert ", " not in body and '": ' not in body
    assert json.loads(body) == {"type": "text-delta", "delta": "café ☕"}


def test_sse_sentinels() -> None:
    assert format_sse_done() == "data: [DONE]\n\n"
    assert format_sse_keepalive() == ": keepalive\n\n"
