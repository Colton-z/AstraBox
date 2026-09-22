"""Assistant-conversation predicates used by lifecycle commands.

Startup ownership belongs to ``runtime_subject.py``. This mixin only identifies
shared Assistant conversations for delete, archive, end, and terminate policy.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    is_assistant_workspace_bootstrap,
)


class _AssistantWorkspaceMixin:
    """Identify Assistant workspace bootstrap and conversation Sessions."""

    @staticmethod
    def _is_assistant_workspace_bootstrap(session: dict[str, Any] | None) -> bool:
        return is_assistant_workspace_bootstrap(session)

    @staticmethod
    def _is_assistant_user_conversation(session: dict[str, Any] | None) -> bool:
        return is_assistant_user_conversation(session)

    @staticmethod
    def _assistant_workspace_sandbox_id(
        session: dict[str, Any] | None,
    ) -> str | None:
        workspace_ref = (session or {}).get("workspace_ref")
        if not isinstance(workspace_ref, dict):
            return None
        return (
            str(workspace_ref.get("sandbox_id") or (session or {}).get("sandbox_id") or "").strip()
            or None
        )
