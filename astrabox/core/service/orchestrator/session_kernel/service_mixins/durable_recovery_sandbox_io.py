"""Sandbox endpoint resolution for durable recovery of
:class:`SessionKernelService`."""
from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


class DurableRecoverySandboxIOMixin:
    """Sandbox endpoint resolution for :class:`SessionKernelService`."""

    async def _resolve_sandbox_endpoint(self, session: dict[str, Any]) -> str:
        session = await self._reconcile_runtime_binding(session, persist=False)
        sandbox_endpoint = str(session.get("sandbox_endpoint") or "").strip() or None
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        if (
            str(session.get("session_kind") or "").strip() == "agent_chat"
            and not sandbox_id
            and not sandbox_endpoint
        ):
            return ""
        return (
            await self._runtime_manager._resolve_validated_sandbox_endpoint(
                session_id=str(session.get("session_id") or ""),
                sandbox_endpoint=sandbox_endpoint,
                sandbox_id=sandbox_id,
                reason="session_kernel_endpoint",
            )
            or ""
        ).strip()
