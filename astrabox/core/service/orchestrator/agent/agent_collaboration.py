"""Agent-controlled access to the caller's own A2A conversations."""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext

CONVERSATION_TOOLS = frozenset({
    "list_agent_conversations", "send_agent_message", "get_agent_task", "answer_agent_task",
})


def collaboration_access(agent: dict[str, Any]) -> tuple[str, frozenset[str]]:
    """An administrator's ceiling; it never grants another person's Sessions."""
    mode = agent.get("a2a_access", "disabled")
    ids = agent.get("a2a_agent_ids", [])
    if not isinstance(mode, str) or mode not in {"disabled", "all", "selected"} or not isinstance(ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in ids
    ):
        raise APIError(code="INVALID_REQUEST", message="Invalid Agent A2A access configuration", status_code=400)
    return str(mode), frozenset(value.strip() for value in ids)


async def collaboration_peers(
    user: UserContext, *, source_agent: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Find the person's accessible Agents within the configured collaboration scope."""
    from astrabox.core.service.orchestrator.service_registry import get_agent_service

    mode, selected = collaboration_access(source_agent) if source_agent is not None else ("all", frozenset())
    if mode == "disabled":
        return []
    agents = await get_agent_service().list_agents(user)
    return [agent for agent in agents if agent.get("can_start_conversation") is True
            and (mode == "all" or str(agent["agent_id"]) in selected)]
