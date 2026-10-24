"""Assistant interaction and schedule tools over existing platform services."""

from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import A2A_ERROR_MAPPING, A2AError, InternalError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.exceptions import HTTPException

from astrabox.common.utils.errors import APIError
from astrabox.deploy.public_path import public_path
from astrabox.core.service.orchestrator.agent.agent_collaboration import (
    collaboration_access, collaboration_peers,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.common.utils.user_context import (
    UserContext,
    reset_current_user_context,
    set_current_user_context,
)


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": description}


def agent_tools() -> list[dict[str, Any]]:
    agent = _string("The platform Agent id from its discovered A2A Agent Card.")
    deployment = _string("The personal schedule's deployment_id.")
    schedule = {"type": "object", "properties": {
        "cron": _string("Five-field cron expression."),
        "timezone": _string("IANA timezone, for example Asia/Shanghai."),
    }, "required": ["cron", "timezone"], "additionalProperties": False}
    definitions = [
        ("list_agent_conversations", "List the user's existing platform conversations by title. This inventory contains AstraBox Agent conversations, including those started in the console before any A2A exchange. Use it for broad questions such as which conversations the user has, as well as explicit Agent or A2A conversation questions. State that the returned scope is Agent conversations. Native session_search, a2a_list and a2a_history recall Hermes-local records, not this inventory. Search by Agent name or conversation title. Returns current names, stable Session IDs and A2A context IDs; use these IDs for messaging. Continue a page with next_cursor. Reading does not send a message or start a business conversation.", {
            "agent_id": agent, "agent_query": _string("Search the Agent's name or display name."),
            "query": _string("Search the conversation title."),
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "cursor": _string("The preceding page's next_cursor."),
        }, []),
        ("send_agent_message", "Send one message through A2A to an existing conversation from list_agent_conversations. Keeps that conversation and its native context; never starts a replacement conversation. Returns the A2A Task; use get_agent_task for a pending result. A busy runtime applies its own input policy.", {
            "agent_id": agent, "session_id": _string("The exact existing Session ID from discovery."),
            "message": _string("The message to deliver to the other conversation."),
            "message_id": _string("A unique stable message ID. Reuse it only for the identical message to the same conversation."),
        }, ["agent_id", "session_id", "message", "message_id"]),
        ("get_agent_task", "Read an A2A Task and its current public interaction contract. Use the explicit Task ID from a2a_call; inspect the declared options before answering. This does not approve anything.", {
            "agent_id": agent, "task_id": _string("The exact A2A Task ID from the peer response."),
        }, ["agent_id", "task_id"]),
        ("answer_agent_task", "Answer the specified A2A Task interaction after the user explicitly supplies the answer or authorization. Read get_agent_task first. Copy the contract's declared decision ID (for example Codex accept, not a generic approve); preserve its scope. Never infer approval from a new task or ordinary text. Returns the current Task; read get_agent_task for subsequent progress.", {
            "agent_id": agent, "task_id": _string("The exact A2A Task ID."),
            "interaction_id": _string("The exact pending interaction_id from get_agent_task."),
            "answer": {"type": "object", "description": "The answer required by the public interaction contract: decision with a declared option ID, or answers for a form. Do not rename supplier decisions."},
            "message_id": _string("A unique stable ID for this answer. Reuse it only when retrying the identical answer to the same Task."),
        }, ["agent_id", "task_id", "interaction_id", "answer", "message_id"]),
        ("list_schedules", "List this Assistant's personal Agent schedules.", {}, []),
        ("create_schedule", "Create an Agent schedule for this Assistant's owner. Each invocation has its own durable Run and Session. Use the owner's chosen timezone.", {
            "agent_id": agent, "name": _string("A short user-facing name."),
            "prompt": _string("The complete instruction for each invocation."), "schedule": schedule,
        }, ["agent_id", "name", "prompt", "schedule"]),
        ("update_schedule", "Update, pause or resume a personal schedule.", {"deployment_id": deployment,
            "patch": {"type": "object", "properties": {"enabled": {"type": "boolean"},
                "name": _string("User-facing name."), "prompt_prefix": _string("Complete invocation instruction."),
                "schedule": schedule}, "additionalProperties": False}}, ["deployment_id", "patch"]),
        ("delete_schedule", "Delete a personal schedule after the owner asks to remove it.", {"deployment_id": deployment}, ["deployment_id"]),
        ("list_schedule_runs", "Read the original Run records for a personal schedule, including Session links and failures.", {
            "deployment_id": deployment, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["deployment_id"]),
        ("run_schedule", "Run a personal schedule once without moving its calendar.", {"deployment_id": deployment}, ["deployment_id"]),
        ("replay_schedule_run", "Replay one prior Run as a new invocation with its captured instruction.", {
            "deployment_id": deployment, "run_id": _string("The Run to replay.")}, ["deployment_id", "run_id"]),
    ]
    return [{"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False,
    }} for name, description, properties, required in definitions]


def _validate(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """Refuse malformed MCP values before service dispatch."""
    def check(value: Any, spec: dict[str, Any], path: str) -> None:
        kind = spec.get("type")
        valid = {
            "object": isinstance(value, dict), "string": isinstance(value, str),
            "integer": type(value) is int, "boolean": type(value) is bool,
        }.get(kind, True)
        if not valid:
            raise APIError(code="INVALID_REQUEST", message=f"{path} must be {kind}", status_code=400)
        if kind == "string" and spec.get("minLength") and not value.strip():
            raise APIError(code="INVALID_REQUEST", message=f"{path} must not be blank", status_code=400)
        if kind == "integer" and not spec.get("minimum", value) <= value <= spec.get("maximum", value):
            raise APIError(code="INVALID_REQUEST", message=f"{path} is out of range", status_code=400)
        if kind == "object":
            properties = spec.get("properties", {})
            if set(spec.get("required", [])) - set(value) or (
                spec.get("additionalProperties") is False and set(value) - set(properties)
            ):
                raise APIError(code="INVALID_REQUEST", message=f"{path} has missing or unknown fields", status_code=400)
            for key, child in value.items():
                if key in properties:
                    check(child, properties[key], f"{path}.{key}")
    check(args, schema, "arguments")


async def call_agent_tool(
    *, name: str, args: dict[str, Any], user: UserContext, assistant_id: str,
    deployments: Any, source_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = next(tool["inputSchema"] for tool in agent_tools() if tool["name"] == name)
    _validate(args, schema)
    source_agent = (source_binding or {}).get("source_agent")
    if name == "list_agent_conversations":
        return await _list_conversations(args, user, source_agent=source_agent)
    if name in {"get_agent_task", "answer_agent_task", "send_agent_message"}:
        if source_agent is not None:
            mode, selected = collaboration_access(source_agent)
            if mode == "disabled" or (mode == "selected" and args["agent_id"] not in selected):
                raise APIError(code="NOT_FOUND", message="Agent is not available for collaboration", status_code=404)
        if name == "send_agent_message":
            if args["session_id"] == (source_binding or {}).get("session_id"):
                raise APIError(code="INVALID_REQUEST", message="Choose another conversation", status_code=400)
            return await _call_a2a_tool(name, args, user, source_binding=source_binding)
        return await _call_a2a_tool(name, args, user)
    if name == "list_schedules":
        return {"schedules": await deployments.list_personal_schedules(user, source_assistant_id=assistant_id)}
    if name == "create_schedule":
        return await deployments.create_personal_schedule(
            user, agent_id=args["agent_id"], source_assistant_id=assistant_id,
            name=args["name"], prompt_prefix=args["prompt"], schedule=args["schedule"],
        )
    if name == "update_schedule":
        return await deployments.update_personal_schedule(user, args["deployment_id"], patch=args["patch"])
    if name == "delete_schedule":
        await deployments.delete_personal_schedule(user, args["deployment_id"])
        return {"deployment_id": args["deployment_id"], "deleted": True}
    if name == "list_schedule_runs":
        return {"runs": await deployments.list_personal_schedule_runs(user, args["deployment_id"], limit=args.get("limit", 50))}
    if name == "run_schedule":
        return await deployments.trigger_personal_schedule(user, args["deployment_id"])
    if name == "replay_schedule_run":
        return await deployments.replay_personal_schedule_run(user, args["deployment_id"], args["run_id"])
    raise APIError(code="INVALID_REQUEST", message="unknown schedule tool", status_code=400)


async def _list_conversations(
    args: dict[str, Any], user: UserContext, *, source_agent: dict[str, Any] | None,
) -> dict[str, Any]:
    from astrabox.core.service.orchestrator.service_registry import get_agent_a2a_service

    peers = await collaboration_peers(user, source_agent=source_agent)
    needle = str(args.get("agent_query") or "").casefold()
    peers = [peer for peer in peers if (not args.get("agent_id") or peer["agent_id"] == args["agent_id"])
             and (not needle or needle in str(peer.get("name") or "").casefold()
                  or needle in str((peer.get("display_meta") or {}).get("display_name") or "").casefold())]
    by_id = {str(peer["agent_id"]): peer for peer in peers}
    page = await SessionRepository().list_user_sessions_page(
        user.user_id, agent_ids=list(by_id), query=str(args.get("query") or ""),
        limit=args.get("limit", 20), cursor=args.get("cursor"),
    )
    service = get_agent_a2a_service()
    conversations = []
    for session in page["sessions"]:
        agent_id = str(session["agent_id"])
        context_id = await service.context_for_session(user, agent_id, session["session_id"])
        peer = by_id[agent_id]
        conversations.append({
            "agent_id": agent_id,
            "agent_name": str((peer.get("display_meta") or {}).get("display_name") or peer["name"]),
            "session_id": session["session_id"], "title": session.get("title"),
            "context_id": context_id, "peer_key": "astrabox/" + agent_id,
            "state": session["state"], "source_type": session.get("source_type"),
            "updated_at": session["updated_at"],
            "session_url_path": public_path('/sessions/' + session["session_id"]),
        })
    return {"conversations": conversations, "has_more": page["has_more"], "next_cursor": page["next_cursor"]}


async def _call_a2a_tool(
    name: str, args: dict[str, Any], user: UserContext, *, source_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt explicit interaction tools to the same A2A service as native peers."""
    from astrabox.core.service.orchestrator.service_registry import get_agent_a2a_service

    service = get_agent_a2a_service()
    token = set_current_user_context(user)
    try:
        if name == "send_agent_message":
            context_id = await service.context_for_session(user, args["agent_id"], args["session_id"])
            if source_binding is None:
                raise APIError(code="MCP_BINDING_NOT_FOUND", message="Message sender binding is required", status_code=404)
            source_id = str(source_binding.get("session_id") or source_binding.get("assistant_id") or "")
            kind = "session" if source_binding.get("session_id") else "assistant"
            origin = {"kind": kind, "id": source_id, "title": source_binding.get("title"),
                      "url_path": public_path(f'/{"sessions" if kind == "session" else "assistants"}/{source_id}')}
            task = await service.send(user, args["agent_id"], a2a.SendMessageRequest(
                message=a2a.Message(
                    message_id=args["message_id"], context_id=context_id, role=a2a.ROLE_USER,
                    parts=[a2a.Part(text=args["message"])],
                ), configuration=a2a.SendMessageConfiguration(return_immediately=True),
            ), origin=origin)
        elif name == "get_agent_task":
            task = await service.get(user, args["agent_id"], a2a.GetTaskRequest(id=args["task_id"]))
        else:
            task = await service.send(user, args["agent_id"], a2a.SendMessageRequest(
                message=a2a.Message(
                    message_id=args["message_id"], task_id=args["task_id"], role=a2a.ROLE_USER,
                    parts=[a2a.Part(data=ParseDict({
                        "interaction_id": args["interaction_id"], "answer": args["answer"],
                    }, Value()), media_type="application/json")],
                ),
                configuration=a2a.SendMessageConfiguration(return_immediately=True),
            ))
        return {"task": MessageToDict(task)}
    except A2AError as exc:
        mapping = A2A_ERROR_MAPPING.get(type(exc), A2A_ERROR_MAPPING[InternalError])
        code = str((exc.data or {}).get("code") or "")
        if not code:
            if mapping.http_code == 404:
                code = "NOT_FOUND"
            elif mapping.http_code >= 500:
                code = "UNEXPECTED_SERVER_ERROR"
            else:
                code = "INVALID_REQUEST"
        raise APIError(
            code=code, message=f"{mapping.reason}: {exc.message}",
            status_code=mapping.http_code, cause_code=mapping.reason,
        ) from None
    except HTTPException as exc:
        if exc.status_code == 401:
            raise APIError(code="UNAUTHORIZED", message=str(exc.detail), status_code=401) from None
        raise APIError(code="FORBIDDEN", message=str(exc.detail), status_code=exc.status_code) from None
    finally:
        reset_current_user_context(token)
