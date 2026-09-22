"""Stateless MCP tool handlers backed by the same services as the web UI.

Conversation operations go through the platform service API, which dispatches
to SessionKernelService → command → turn_worker → projector.

- list_agents → agent_service.list_agents()
- create_conversation → agent_service.start_conversation()
- send_message → platform_service.stream_message_events_ds()
- get_status → platform_service.get_session() (projection-backed)
- answer_interaction → platform_service.answer_pending_interaction()
- cancel_task → platform_service.interrupt()
"""

from __future__ import annotations

import asyncio
import contextlib
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

logger = get_logger(__name__)

_SESSION_CREATION_POLL_SECONDS = 0.25


class AgentMCPService:
    def __init__(self, *, agent_service: Any) -> None:
        self._svc = agent_service

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
    # send_message dispatch
    # ------------------------------------------------------------------

    async def _handle_send_message(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        instruction = str(args.get("instruction", ""))
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            return {"error": "session_id is required. Call create_conversation(agent_id) first to get a session_id."}
        if not instruction.strip():
            # Refused here because the content is carried, unexamined, into the
            # turn worker: the emptiness is first noticed after a turn has been
            # created and dispatched, so the caller gets a FAILED turn and a
            # "content is empty" from four layers down instead of being told
            # which argument it got wrong.
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
        session = await self._auto_recover_if_needed(user, agent_id, session)
        session = await self._wait_for_creation(user, agent_id, session)
        self._normalize_mcp_state(str(session.get("state") or ""))

        # The input path requires a client_message_id: it is the outbox/dedup
        # identity a browser mints per send. An MCP client has no outbox and
        # never retries a delivered call through this facade, so the facade
        # mints one per send — leaving it out is not an option, because the
        # dispatch refuses unstamped input outright (INVALID_REQUEST four
        # layers down).
        agen = self._svc._platform.stream_message_events_ds(
            user, session_id, instruction,
            client_message_id=str(uuid.uuid4()),
        )
        try:
            first = await asyncio.wait_for(anext(agen), timeout=30.0)
            sess_id = session_id
        except StopAsyncIteration:
            payload = _platform_session_payload(session_id)
            payload.update({"state": "FAILED", "error": "empty stream"})
            return payload
        except APIError as ae:
            if ae.code == "SESSION_BUSY":
                payload = _platform_session_payload(session_id)
                payload.update({"state": "AGENT_BUSY", "message": ae.message})
                return payload
            msg_lower = (ae.message or "").lower()
            if "pending interaction" in msg_lower or "pending question" in msg_lower:
                payload = _platform_session_payload(session_id)
                payload.update({"state": "WAITING_INPUT", "message": ae.message})
                return payload
            raise
        except Exception as exc:
            try:
                await agen.aclose()
            except Exception:
                pass
            payload = _platform_session_payload(session_id)
            payload.update({"state": "FAILED", "error": str(exc)})
            return payload

        # Spawned through the platform's managed spawner, not `ensure_future`.
        # That helper is what detaches a task from the request's context — a
        # bare task inherits the MCP call's scope and is cancelled when the tool
        # returns, which is this instant — and it holds a strong reference, so
        # the task cannot be collected while it waits on the engine. Both are
        # needed: the tool answers SUBMITTED and returns immediately, so this
        # drain outlives its caller by design.
        self._svc._platform._spawn_background_task(
            self._drain_stream(agen, session_id),
            name=f"mcp-drain-{session_id}",
        )
        payload = _platform_session_payload(sess_id)
        payload["state"] = "SUBMITTED"
        return payload

    async def _drain_stream(self, agen: Any, session_id: str) -> None:
        """Consume the turn stream to its end on behalf of a caller that left.

        Logged at both ends, because the two ways this fails are silent and look
        identical from outside: a turn that sits in PROCESSING for ever tells
        nobody whether this ran and the engine never answered, or whether this
        never ran at all.

        A stalled turn is not by itself evidence that this drain is at fault, and
        the cheapest thing to rule out first is whether the box existed yet: a
        sandbox created after the turn was accepted cannot have run it, and the
        session then looks exactly like a dispatch that went nowhere. Compare the
        sandbox's creation time against the turn's acceptance time before reading
        anything into these lines.
        """
        logger.info("mcp background drain started session=%s", session_id)
        events = 0
        try:
            async for _ in agen:
                events += 1
        except asyncio.CancelledError:
            # CancelledError is not an Exception. Swallowed silently, a
            # cancelled drain is indistinguishable from an engine that went
            # quiet.
            logger.warning(
                "mcp background drain cancelled session=%s after %d event(s)",
                session_id,
                events,
            )
            raise
        except Exception as exc:
            logger.error("mcp background drain failed session=%s: %s", session_id, exc)
        else:
            logger.info(
                "mcp background drain finished session=%s events=%d", session_id, events
            )
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    # ------------------------------------------------------------------
    # get_status — projection-backed read via platform API
    # ------------------------------------------------------------------

    async def _handle_get_status(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)

        session = await self._get_bound_session(user, agent_id, session_id)
        session = await self._auto_recover_if_needed(user, agent_id, session)
        state = str(session.get("state") or "")
        state = self._normalize_mcp_state(state)

        result: dict[str, Any] = _platform_session_payload(session_id)
        result["state"] = state

        pending = session.get("pending_interaction")
        if isinstance(pending, dict) and pending:
            result["state"] = "WAITING_INPUT"
            result["pending_interaction"] = self._format_pending(pending)

        turn_id = session.get("current_turn_id")
        if turn_id:
            result["current_turn_id"] = str(turn_id)

        messages_result = await self._svc._platform.get_messages(
            user, session_id, limit=10,
        )
        result["recent_messages"] = [
            {
                "role": m.get("role"),
                "content": m.get("content") or m.get("text", ""),
                "turn_id": m.get("turn_id"),
                "created_at": m.get("created_at"),
            }
            for m in (messages_result.get("messages") or [])
        ]

        return result

    # ------------------------------------------------------------------
    # answer_interaction dispatch
    # ------------------------------------------------------------------

    async def _handle_answer_interaction(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        interaction_id = str(args.get("interaction_id", ""))
        answer = args.get("answer", {})

        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)
        await self._get_bound_session(user, agent_id, session_id)

        await self._svc._platform.answer_pending_interaction(
            user, session_id, interaction_id, answer,
        )
        payload = _platform_session_payload(session_id)
        payload["state"] = "SUBMITTED"
        return payload

    # ------------------------------------------------------------------
    # cancel_task dispatch
    # ------------------------------------------------------------------

    async def _handle_cancel_task(self, user: UserContext, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise APIError(code="NO_SESSION", message="session_id is required. Call create_conversation first.", status_code=409)
        await self._get_bound_session(user, agent_id, session_id)
        try:
            await self._svc._platform.interrupt(user, session_id)
            payload = _platform_session_payload(session_id)
            payload["cancelled"] = True
            return payload
        except Exception as exc:
            logger.error("mcp cancel_task failed: %s", exc)
            payload = _platform_session_payload(session_id)
            payload.update({"cancelled": False, "error": str(exc)})
            return payload

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_active_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._ensure_agent_visible(user, agent_id)
        state = agent.get("state", "")
        if state == "HIBERNATING":
            await self._svc.wake_agent(user, agent_id)
            for _ in range(max(1, int(_AGENT_STARTUP_TIMEOUT_SECONDS / 2))):
                await asyncio.sleep(2.0)
                agent = await self._svc._agent_repo.get_agent(agent_id)
                if agent and agent.get("state") == "ACTIVE":
                    return agent
            raise APIError(code="AGENT_NOT_READY", message="agent failed to wake up in time", status_code=503)
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
        session = await self._svc._platform.get_session(user, session_id)
        session_kind = str(session.get("session_kind") or "").strip()
        session_agent_id = str(session.get("agent_id") or "").strip()
        if session_kind != "agent_chat" or session_agent_id != agent_id:
            raise APIError(
                code="AGENT_SESSION_MISMATCH",
                message="session does not belong to this agent",
                status_code=409,
            )
        return session

    async def _wait_for_creation(
        self,
        user: UserContext,
        agent_id: str,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        """Let an immediate post-create message wait for provisioning.

        The web conversation endpoint intentionally returns while the session
        is still ``CREATING``. MCP callers have no live session subscription
        between ``create_conversation`` and ``send_message``, so requiring them
        to discover and retry that internal race makes the documented tool
        sequence unreliable. Wait only for creation; an already-running turn
        remains a normal ``AGENT_BUSY`` response.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _AGENT_STARTUP_TIMEOUT_SECONDS
        current = session
        while str(current.get("state") or "") == "CREATING":
            remaining = deadline - loop.time()
            if remaining <= 0:
                return current
            await asyncio.sleep(min(_SESSION_CREATION_POLL_SECONDS, remaining))
            current = await self._get_bound_session(
                user,
                agent_id,
                str(current.get("session_id") or ""),
            )
        return current

    @staticmethod
    def _normalize_mcp_state(state: str) -> str:
        """Map any platform session state to MCP contract: READY / BUSY / WAITING_INPUT.

        Raises APIError for terminal states that need client action.
        """
        if state in ("READY", ""):
            return "READY"
        if state in ("PROCESSING", "INTERRUPTING", "CREATING"):
            return "BUSY"
        if state == "WAITING_INPUT":
            return "WAITING_INPUT"
        if state in ("TERMINATED", "DELETED"):
            raise APIError(
                code="SESSION_TERMINATED",
                message="session terminated, call create_conversation to start a new one",
                status_code=410,
            )
        if state == "RECOVERY_REQUIRED":
            raise APIError(
                code="SESSION_RECOVERY_REQUIRED",
                message="agent runtime lost, retry after a short delay or wake the agent",
                status_code=503,
            )
        return "BUSY"

    async def _auto_recover_if_needed(
        self,
        user: Any,
        agent_id: str,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = str(session.get("session_id") or "").strip()
        if str(session.get("state") or "") == "RECOVERY_REQUIRED":
            try:
                await self._svc._platform.recover_session(user, session_id)
            except Exception as exc:
                logger.warning("mcp auto-recover failed session=%s: %s", session_id, exc)
            return await self._get_bound_session(user, agent_id, session_id)
        return session

    @staticmethod
    def _format_pending(pending: dict[str, Any]) -> dict[str, Any]:
        pi: dict[str, Any] = {
            "interaction_id": pending.get("interaction_id"),
            "tool_name": pending.get("tool_name"),
        }
        inner = pending.get("pending_interaction") or pending
        if str(inner.get("presentation") or "").strip() == PRESENTATION_FORM:
            questions = inner.get("questions") or []
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


def _platform_session_payload(session_id: str) -> dict[str, str]:
    """Return the AstraBox business session identifier, not MCP transport state."""
    return {
        "session_id": session_id,
        "platform_session_id": session_id,
        "session_url_path": f"/sessions/{session_id}",
    }
