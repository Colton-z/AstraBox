"""Stateless MCP tool handlers backed by the same services as the web UI.

Conversation operations go through the platform's session runtime service,
the one the console's own routes use:

- list_agents → agent_service.list_agents()
- create_conversation → agent_service.start_conversation()
- send_message → session_runtime.send_as() (the message is accepted; the
  session's reader hands it to the runtime)
- get_status → the session's lifecycle, the runtime's recorded status and
  last response, and its latest history
- answer_interaction → session_runtime.answer()
- cancel_task → session_runtime.interrupt()
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    require_scope_for_tool,
)
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
)
from astrabox.core.service.orchestrator.agent.agent_service import _AGENT_STARTUP_TIMEOUT_SECONDS
from astrabox.deploy.public_path import public_path
from astrabox.persistence.repository.session_repository import SessionRepository

logger = get_logger(__name__)

#: Where an MCP client's message came from, on the input the platform records.
_SOURCE = "mcp"
#: How many of the conversation's latest messages ``get_status`` returns.
_RECENT_MESSAGES = 10


class AgentMCPService:
    """The Agent MCP facade; the conversation runtime is the platform's ``session_runtime``."""

    def __init__(self, *, agent_service: Any) -> None:
        self._svc = agent_service
        self._sessions = SessionRepository()

    @property
    def _runtime(self) -> Any:
        return self._svc._platform.session_runtime

    async def handle_tools_list(self) -> dict[str, Any]:
        """Return the stable platform-level Agent MCP tool catalogue."""

        return {
            "tools": [
                {
                    "name": "list_agents",
                    "description": (
                        "List the AstraBox Agents the current caller may use."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "create_conversation",
                    "description": (
                        "Create a new conversation for an Agent and return the "
                        "AstraBox platform session_id plus session_url_path."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "Agent id returned by list_agents.",
                            },
                        },
                        "required": ["agent_id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "send_message",
                    "description": "Send one message to an existing conversation session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "Agent id returned by list_agents.",
                            },
                            "session_id": {
                                "type": "string",
                                "description": "AstraBox platform session_id returned by create_conversation, not MCP transport session id.",
                            },
                            "instruction": {"type": "string"},
                        },
                        "required": ["agent_id", "session_id", "instruction"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "get_status",
                    "description": "Get the current state and recent messages for an existing conversation session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "Agent id returned by list_agents.",
                            },
                            "session_id": {
                                "type": "string",
                                "description": "AstraBox platform session_id returned by create_conversation, not MCP transport session id.",
                            },
                        },
                        "required": ["agent_id", "session_id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "answer_interaction",
                    "description": "Answer a pending interaction on an existing conversation session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "Agent id returned by list_agents.",
                            },
                            "session_id": {
                                "type": "string",
                                "description": "AstraBox platform session_id returned by create_conversation, not MCP transport session id.",
                            },
                            "interaction_id": {"type": "string"},
                            "answer": {"type": "object"},
                        },
                        "required": [
                            "agent_id",
                            "session_id",
                            "interaction_id",
                            "answer",
                        ],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "cancel_task",
                    "description": "Interrupt the currently running task on an existing conversation session.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "Agent id returned by list_agents.",
                            },
                            "session_id": {
                                "type": "string",
                                "description": "AstraBox platform session_id returned by create_conversation, not MCP transport session id.",
                            },
                        },
                        "required": ["agent_id", "session_id"],
                        "additionalProperties": False,
                    },
                },
            ]
        }

    async def handle_tool_call(
        self,
        user: UserContext,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Run one tool for this caller.

        The scope gate is here rather than on the transport because this is the
        one place every tool passes through. A check on the route would cover
        the methods it knows about, and a tool reached another way would not
        meet it.
        """
        require_scope_for_tool(scope, tool_name)
        if tool_name == "list_agents":
            return await self._handle_list_agents(user, arguments)

        handler = {
            "create_conversation": self._handle_create_conversation,
            "send_message": self._handle_send_message,
            "get_status": self._handle_get_status,
            "answer_interaction": self._handle_answer_interaction,
            "cancel_task": self._handle_cancel_task,
        }.get(tool_name)
        if handler is None:
            raise APIError(
                code="UNKNOWN_TOOL",
                message=f"unknown tool: {tool_name}",
                status_code=400,
            )

        agent_id = str(arguments.get("agent_id") or "").strip()
        if not agent_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="agent_id is required. Call list_agents first.",
                status_code=400,
            )

        # HTTP identity identifies a user, not an Agent. Re-evaluate the live
        # Agent ACL on every call so an ACL edit takes effect without changing
        # the caller's credential.
        await self._ensure_agent_visible(user, agent_id)
        return await handler(user, agent_id, arguments)

    async def _handle_list_agents(
        self,
        user: UserContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        _ = args
        return {"agents": await self._svc.list_agents(user)}

    # ------------------------------------------------------------------
    # create_conversation
    # ------------------------------------------------------------------

    async def _handle_create_conversation(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        _ = args
        await self._ensure_active_agent(user, agent_id)
        result = await self._svc.start_conversation(user, agent_id)
        payload = _platform_session_payload(str(result["session_id"]))
        payload["agent_id"] = agent_id
        return payload

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def _handle_send_message(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        instruction = str(args.get("instruction", ""))
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            return {"error": "session_id is required. Call create_conversation(agent_id) first to get a session_id."}
        if not instruction.strip():
            # Refused by name here: a tool call that named the argument anything
            # else arrives empty, and the caller is told which argument it got
            # wrong instead of seeing an empty message in the conversation.
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    "instruction is required and must not be blank — it carries the "
                    "message to send; a tool call naming it anything else arrives here empty"
                ),
                status_code=400,
            )

        await self._ensure_active_agent(user, agent_id)
        session = await self._get_bound_session(user, agent_id, session_id)
        self._normalize_mcp_state(str(session.get("state") or ""))
        # The platform holds the message until the conversation's runtime
        # takes it, including while the runtime is still starting. An MCP
        # client never resends a call, so each send is its own message.
        receipt = await self._runtime.send_as(
            session_id,
            instruction,
            author_user_id=user.user_id,
            source=_SOURCE,
            client_message_id=str(uuid.uuid4()),
        )
        payload = _platform_session_payload(session_id)
        payload["state"] = "SUBMITTED"
        payload["input_id"] = str(receipt["input_id"])
        return payload

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------

    async def _handle_get_status(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """The conversation's state, the runtime's own status and last response, and its latest messages.

        ``state`` is READY, BUSY or WAITING_INPUT as the conversation's page
        shows it; ``runtime_status`` is what the runtime last reported
        (``idle``, ``working``, ``waiting``). ``recent_messages`` include a
        response still in progress, as far as it has been recorded.
        """
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)

        session = await self._get_bound_session(user, agent_id, session_id)
        view = await self._runtime.runtime_view(session)
        result: dict[str, Any] = _platform_session_payload(session_id)
        result["state"] = self._normalize_mcp_state(str(view["state"] or ""))
        result["runtime_status"] = view["runtime_status"]
        pending = view["pending_interaction"]
        if isinstance(pending, dict) and pending:
            result["state"] = "WAITING_INPUT"
            result["pending_interaction"] = self._format_pending(pending)
        last = view["last_response"]
        if isinstance(last, dict):
            result["last_response"] = {
                key: last.get(key)
                for key in ("response_id", "outcome", "native_reason", "error", "ended_at")
            }
        history = await self._runtime.history_for(session_id, limit=_RECENT_MESSAGES)
        result["recent_messages"] = [_recent_message(message) for message in history["messages"]]
        return result

    # ------------------------------------------------------------------
    # answer_interaction
    # ------------------------------------------------------------------

    async def _handle_answer_interaction(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        interaction_id = str(args.get("interaction_id", ""))
        answer = args.get("answer", {})

        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)
        await self._get_bound_session(user, agent_id, session_id)

        await self._runtime.answer(user, session_id, interaction_id, answer)
        payload = _platform_session_payload(session_id)
        payload["state"] = "SUBMITTED"
        return payload

    # ------------------------------------------------------------------
    # cancel_task
    # ------------------------------------------------------------------

    async def _handle_cancel_task(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Ask the runtime to stop; ``cancelled`` is whether a reader will pass the stop on."""
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)
        await self._get_bound_session(user, agent_id, session_id)
        try:
            receipt = await self._runtime.interrupt(user, session_id)
        except Exception as exc:
            logger.error("mcp cancel_task failed: %s", exc)
            payload = _platform_session_payload(session_id)
            payload.update({"cancelled": False, "error": str(exc)})
            return payload
        payload = _platform_session_payload(session_id)
        payload["cancelled"] = receipt["status"] == "accepted"
        payload["status"] = receipt["status"]
        return payload

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_active_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._ensure_agent_visible(user, agent_id)
        state = agent.get("state", "")
        if state != "ACTIVE":
            raise APIError(code="AGENT_NOT_ACTIVE", message=f"agent not active (state={state})", status_code=409)
        return agent

    async def _ensure_agent_exists(self, agent_id: str) -> dict[str, Any]:
        agent = await self._svc._agent_repo.get_agent(agent_id)
        if agent is None:
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        return agent

    async def _ensure_agent_visible(
        self,
        user: UserContext,
        agent_id: str,
    ) -> dict[str, Any]:
        agent = await self._ensure_agent_exists(agent_id)
        if not can_view_agent(agent, user.user_id, user.roles):
            # Match the regular Agent read surface: invisible and absent are
            # indistinguishable, so a caller cannot enumerate private Agent ids.
            raise APIError(
                code="AGENT_NOT_FOUND",
                message="agent not found",
                status_code=404,
            )
        return agent

    async def _get_bound_session(
        self,
        user: UserContext,
        agent_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        session = await self._sessions.get_owned_session(session_id, user.user_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        session_kind = str(session.get("session_kind") or "").strip()
        session_agent_id = str(session.get("agent_id") or "").strip()
        if session_kind != "agent_chat" or session_agent_id != agent_id:
            raise APIError(
                code="AGENT_SESSION_MISMATCH",
                message="session does not belong to this agent",
                status_code=409,
            )
        return session

    @staticmethod
    def _normalize_mcp_state(state: str) -> str:
        """Map the conversation's shown state to the MCP contract: READY / BUSY / WAITING_INPUT.

        Raises APIError for an ended conversation, which needs a new one.
        """
        if state in ("READY", ""):
            return "READY"
        if state == "WAITING_INPUT":
            return "WAITING_INPUT"
        if state in ("TERMINATED", "DELETED"):
            raise APIError(
                code="SESSION_TERMINATED",
                message="session terminated, call create_conversation to start a new one",
                status_code=410,
            )
        return "BUSY"

    @staticmethod
    def _format_pending(contract: dict[str, Any]) -> dict[str, Any]:
        pi: dict[str, Any] = {
            "interaction_id": contract.get("interaction_id"),
            "tool_name": contract.get("tool_name"),
        }
        if str(contract.get("presentation") or "").strip() == PRESENTATION_FORM:
            questions = contract.get("questions") or []
            pi["questions"] = [
                {
                    "id": q.get("id"),
                    "question": q.get("question") or q.get("header"),
                    "multi_select": q.get("multi_select", False),
                    "allow_free_text": q.get("allow_free_text"),
                    "allow_empty_text": q.get("allow_empty_text"),
                    "options": [
                        o.get("label") for o in (q.get("options") or [])
                        if isinstance(o, dict) and o.get("label")
                    ],
                }
                for q in questions if isinstance(q, dict)
            ]
        return pi


def _recent_message(message: dict[str, Any]) -> dict[str, Any]:
    """One history message as ``get_status`` reports it: who said what, and when."""
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    content = "\n\n".join(
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    )
    recent = {
        "message_id": message.get("id"),
        "role": message.get("role"),
        "content": content,
        "created_at": metadata.get("created_at") or metadata.get("started_at"),
    }
    if metadata.get("response_id"):
        recent["response_id"] = metadata["response_id"]
    return recent


def _platform_session_payload(session_id: str) -> dict[str, str]:
    """Return the AstraBox business session identifier, not MCP transport state."""
    return {
        "session_id": session_id,
        "platform_session_id": session_id,
        "session_url_path": public_path(f"/sessions/{session_id}"),
    }
