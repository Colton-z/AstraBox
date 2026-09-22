"""Identifiers shared by terminal transports and their interrupt path."""

from __future__ import annotations

import uuid


ISOLATED_TERMINAL_EXECUTION_PREFIX = "isolated-run:"


def new_isolated_terminal_execution_id(session_id: str) -> str:
    """Return an opaque id for one streamed run inside an isolation session."""

    subject = str(session_id or "").strip() or "unknown"
    return f"{ISOLATED_TERMINAL_EXECUTION_PREFIX}{subject}:{uuid.uuid4().hex}"


def is_isolated_terminal_execution_id(value: object) -> bool:
    return str(value or "").startswith(ISOLATED_TERMINAL_EXECUTION_PREFIX)


__all__ = [
    "ISOLATED_TERMINAL_EXECUTION_PREFIX",
    "is_isolated_terminal_execution_id",
    "new_isolated_terminal_execution_id",
]
