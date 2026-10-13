"""Assistant interaction and schedule tools over existing platform services."""

from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import A2A_ERROR_MAPPING, A2AError, InternalError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.exceptions import HTTPException

from astrabox.common.utils.errors import APIError
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
    deployments: Any,
) -> dict[str, Any]:
    schema = next(tool["inputSchema"] for tool in agent_tools() if tool["name"] == name)
    _validate(args, schema)
    if name in {"get_agent_task", "answer_agent_task"}:
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


async def _call_a2a_tool(name: str, args: dict[str, Any], user: UserContext) -> dict[str, Any]:
    """Adapt explicit interaction tools to the same A2A service as native peers."""
    from astrabox.core.service.orchestrator.service_registry import get_agent_a2a_service

    service = get_agent_a2a_service()
    token = set_current_user_context(user)
    try:
        if name == "get_agent_task":
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
