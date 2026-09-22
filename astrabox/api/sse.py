"""Server-Sent Event framing for the AI SDK HTTP stream."""

from __future__ import annotations

import json
from typing import Any


def format_sse_event(event: dict[str, Any]) -> str:
    """Encode one AI SDK frame as a compact, Unicode-safe SSE record."""

    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def format_sse_done() -> str:
    """Return the AI SDK stream terminal sentinel."""

    return "data: [DONE]\n\n"


def format_sse_keepalive() -> str:
    """Return an SSE comment that keeps an idle transport open."""

    return ": keepalive\n\n"
