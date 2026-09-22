"""Sandbox bootstrap helpers.

This module contains the lower-level sandbox creation and attachment logic.
The RemoteAgentRuntimeManager delegates here for agent session types; chat
sessions use a separate path.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


async def check_server_health(sandbox: Any, *, timeout: float = 10.0) -> bool:
    """Check if the Claude enhanced server is responding on port 8000."""
    commands = getattr(sandbox, "commands", None)
    if commands is None:
        return False
    try:
        import asyncio

        result = await asyncio.wait_for(
            commands.run("curl -sf http://localhost:8000/health || echo FAIL"),
            timeout=timeout,
        )
        text = str(getattr(result, "output", "") or "")
        logs = getattr(result, "logs", None)
        if logs:
            stdout = getattr(logs, "stdout", None)
            if stdout:
                text = "".join(str(getattr(item, "text", item)) for item in stdout)
        return "FAIL" not in text and len(text.strip()) > 0
    except Exception as exc:
        logger.warning("health check failed: %s", exc)
        return False


async def reset_server_session(sandbox: Any, *, timeout: float = 10.0) -> bool:
    """Reset the Claude enhanced server session state."""
    commands = getattr(sandbox, "commands", None)
    if commands is None:
        return False
    try:
        import asyncio

        result = await asyncio.wait_for(
            commands.run(
                'curl -sf -X POST http://localhost:8000/reset -H "Content-Type: application/json" -d \'{"reset": true}\' || echo FAIL'
            ),
            timeout=timeout,
        )
        text = str(getattr(result, "output", "") or "")
        logs = getattr(result, "logs", None)
        if logs:
            stdout = getattr(logs, "stdout", None)
            if stdout:
                text = "".join(str(getattr(item, "text", item)) for item in stdout)
        return "FAIL" not in text
    except Exception as exc:
        logger.warning("server session reset failed: %s", exc)
        return False
