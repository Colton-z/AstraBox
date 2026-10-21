"""A2A tasks over Agent conversations, read from the runtime's recorded facts.

A task is one input sent to an Agent conversation under the task's id. Its
state is that input's: submitted until the runtime starts the response that
answers it, then the response's own state (working, waiting on a person,
ended with the runtime's outcome), or failed when the input was refused. The
task's result is the response's browser parts, folded into the message the
conversation's page shows. A stream re-reads the task each time the session's
reader commits output, which is when a response's state changes are recorded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from a2a.server.events.event_queue import Event
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import (
    A2AError,
    ContentTypeNotSupportedError,
    InternalError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import (
    ListTasksCursor,
    apply_history_length,
    decode_list_tasks_cursor,
    encode_list_tasks_cursor,
    validate_history_length,
    validate_page_size,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp
from starlette.exceptions import HTTPException

from astrabox import __version__
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso
from astrabox.common.utils.user_context import API_READ_SCOPE, API_WRITE_SCOPE, UserContext
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_TOOL_APPROVAL,
    public_interaction_view,
)
from astrabox.core.service.orchestrator.session_runtime.feed import Notice, Resync, Subscription
from astrabox.core.service.orchestrator.session_runtime.ui_message import _apply_approval_decisions, fold_message
from astrabox.core.service.orchestrator.session_runtime.views import public_message
from astrabox.deploy.public_path import public_path
from astrabox.persistence.repository.agent_task_reference_repository import (
    AgentTaskReferenceRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

logger = get_logger(__name__)
_TERMINAL = frozenset(
    {
        a2a.TASK_STATE_COMPLETED,
        a2a.TASK_STATE_FAILED,
        a2a.TASK_STATE_CANCELED,
        a2a.TASK_STATE_REJECTED,
    }
)
_RETURNABLE = _TERMINAL | {a2a.TASK_STATE_INPUT_REQUIRED, a2a.TASK_STATE_AUTH_REQUIRED}
#: A response's ``outcome`` (``engine/runtime.py``) as the task state it ends in.
_ENDED = {
    "completed": a2a.TASK_STATE_COMPLETED,
    "failed": a2a.TASK_STATE_FAILED,
    "cancelled": a2a.TASK_STATE_CANCELED,
}
#: Where the task's input came from, on the input the platform records.
_SOURCE = "a2a"


def _response_text(parts: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        str(part.get("text") or "") for part in parts if part.get("type") == "text" and part.get("text")
    )


def _failure_text(response: dict[str, Any], chunks: list[dict[str, Any]]) -> str:
    """Why a response failed: the runtime's error, else the error its parts showed."""
    error = response.get("error")
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        return str(error["message"])
    for chunk in reversed(chunks):
        if chunk.get("type") == "error" and str(chunk.get("errorText") or "").strip():
            return str(chunk["errorText"])
    return ""


def _output_view(task: a2a.Task, modes: list[str]) -> a2a.Task:
    if not modes or {"text/plain", "application/json"} <= set(modes):
        return task
    result = a2a.Task()
    result.CopyFrom(task)
    parts_groups = [artifact.parts for artifact in result.artifacts]
    parts_groups.extend(
        message.parts for message in result.history if message.role == a2a.ROLE_AGENT
    )
    if result.status.HasField("message"):
        parts_groups.append(result.status.message.parts)
    for parts in parts_groups:
        converted = []
        for part in parts:
            if "text/plain" in modes and part.HasField("data"):
                converted.append(
                    a2a.Part(text=json.dumps(MessageToDict(part.data), ensure_ascii=False))
                )
            elif "application/json" in modes and part.HasField("text"):
                converted.append(_data({"text": part.text}))
            else:
                converted.append(part)
        del parts[:]
        parts.extend(converted)
    return result


def _data(value: Any) -> a2a.Part:
    return a2a.Part(data=ParseDict(value, Value()), media_type="application/json")


def _interaction_text(pending: dict[str, Any], *, agent_id: str, task_id: str) -> str:
    lines = [
        "Agent needs your input. Read get_agent_task, then use answer_agent_task with the user's explicit answer or authorization. Ordinary a2a_call text does not answer this interaction. You can also answer in the Agent session below.",
        f"Agent ID: {agent_id}",
        f"Task ID: {task_id}",
        f"Interaction ID: {pending['interaction_id']}",
    ]
    for key in ("prompt", "body"):
        if isinstance(pending.get(key), str) and pending[key].strip():
            lines.append(pending[key])
    for question in pending.get("questions") or []:
        if not isinstance(question, dict):
            continue
        if isinstance(question.get("question"), str) and question["question"].strip():
            lines.append(question["question"])
        for option in question.get("options") or []:
            if isinstance(option, dict) and option.get("label"):
                label = str(option["label"])
                description = str(option.get("description") or "")
                lines.append(f"- {label}: {description}" if description else f"- {label}")
    for option in pending.get("options") or []:
        if isinstance(option, dict) and option.get("id"):
            lines.append(
                f"- decision: {option['id']} (denial: {str(option.get('denial')).lower()})"
            )
    if pending.get("presentation") == PRESENTATION_TOOL_APPROVAL:
        lines.append("Allowed decision values: approve, reject.")
    if pending.get("tool_name"):
        lines.append(str(pending["tool_name"]))
    return "\n\n".join(lines)


def _timestamp(value: str) -> Timestamp:
    stamp = Timestamp()
    stamp.FromJsonString(value)
    return stamp


#: A ListTasks page token names its last task's creation time in nanoseconds;
#: the store keeps microseconds, so both conversions are exact.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _cursor_ns(created_at: str) -> int:
    return (parse_iso(created_at) - _EPOCH) // timedelta(microseconds=1) * 1000


def _cursor_time(timestamp_ns: int) -> datetime:
    """Raises ``OverflowError`` for a time no datetime can hold."""
    return _EPOCH + timedelta(microseconds=timestamp_ns // 1000)


@asynccontextmanager
async def _protocol_errors() -> AsyncIterator[None]:
    try:
        yield
    except (A2AError, HTTPException):
        raise
    except APIError as exc:
        if exc.status_code in {401, 403}:
            raise HTTPException(exc.status_code, "Agent operation is not authorized") from None
        if exc.status_code == 404:
            raise TaskNotFoundError from None
        if exc.status_code in {400, 409, 410, 422}:
            raise InvalidParamsError(
                message="Agent operation refused", data={"code": exc.code}
            ) from None
        logger.exception("A2A platform operation failed code=%s", exc.code)
        raise InternalError(message="Agent operation failed") from None
    except Exception:
        logger.exception("A2A projection failed")
        raise InternalError(message="Agent operation failed") from None


class AgentA2AService:
    """A2A over the platform's Agent conversations.

    The conversation's runtime service is the platform's ``session_runtime``
    and its process-wide session feed ``_session_feed``.
    """

    def __init__(self, *, agent_service: Any) -> None:
        self._agents = agent_service
        self._platform = agent_service._platform
        self._references = AgentTaskReferenceRepository()
        self._sessions = SessionRepository()
        self._facts = SessionRuntimeRepository()

    async def _authorize(
        self, user: UserContext, agent_id: str, *, write: bool = False
    ) -> dict[str, Any]:
        scope = API_WRITE_SCOPE if write else API_READ_SCOPE
        if user.api_scopes is not None and scope not in user.api_scopes:
            raise HTTPException(403, f"API token requires scope {scope}")
        agent = await self._agents._agent_repo.get_agent(agent_id)
        if not agent or agent.get("deleted") or not can_view_agent(agent, user.user_id, user.roles):
            raise TaskNotFoundError
        return agent

    async def _session(self, user: UserContext, agent_id: str, session_id: str) -> dict[str, Any]:
        session = await self._sessions.get_owned_session(session_id, user.user_id)
        if (
            session is None
            or session.get("agent_id") != agent_id
            or session.get("session_kind") != "agent_chat"
        ):
            raise TaskNotFoundError
        return session

    async def _reference(self, user: UserContext, agent_id: str, task_id: str) -> dict[str, Any]:
        row = await self._references.get(task_id)
        if (
            not row
            or row.get("kind") != "task"
            or (row.get("user_id"), row.get("org_id"), row.get("agent_id"))
            != (user.user_id, user.org_id, agent_id)
        ):
            raise TaskNotFoundError
        return row

    async def send(
        self, user: UserContext, agent_id: str, params: a2a.SendMessageRequest,
        *, origin: dict[str, Any] | None = None,
    ) -> a2a.Task:
        async with _protocol_errors():
            await self._authorize(user, agent_id, write=True)
            validate_proto_required_fields(params)
            message = params.message
            config = params.configuration
            validate_history_length(config)
            if (
                params.tenant
                or params.metadata
                or message.metadata
                or message.extensions
                or message.reference_task_ids
            ):
                raise InvalidParamsError(
                    message="Tenant, metadata, extensions and task references are not supported"
                )
            if config.HasField("task_push_notification_config"):
                raise PushNotificationNotSupportedError
            if config.accepted_output_modes and not {"text/plain", "application/json"} & set(
                config.accepted_output_modes
            ):
                raise ContentTypeNotSupportedError(
                    message="Task results support text/plain and application/json"
                )
            if message.role != a2a.ROLE_USER or not message.message_id.strip():
                raise InvalidParamsError(
                    message="A user message with a stable messageId is required"
                )
            if any(
                part.metadata
                or part.filename
                or part.media_type not in ("", "text/plain", "application/json")
                for part in message.parts
            ):
                raise ContentTypeNotSupportedError(
                    message="Part metadata and filenames are not supported"
                )
            if message.task_id:
                task = await self._answer(user, agent_id, params)
            else:
                if not message.parts or any(not part.HasField("text") for part in message.parts):
                    raise ContentTypeNotSupportedError(message="New tasks accept text parts only")
                content = "\n".join(part.text for part in message.parts)
                if not content.strip():
                    raise InvalidParamsError(message="Task text must not be blank")
                row = await self._reserve(user, agent_id, message, kind="task", origin=origin)
                session_id = await self._context_session(user, agent_id, str(row["context_id"]))
                await self._references.bind_session(str(row["reference_id"]), session_id)
                row["session_id"] = session_id
                await self._session(user, agent_id, session_id)
                # The task's id is its input's id in the conversation. A resent
                # message is the same task, read back as it stands even when
                # its conversation has ended.
                if await self._facts.input_by_client_message_id(session_id, str(row["reference_id"])) is None:
                    if row.get("origin") is not None:
                        content = (
                            "Message from another AstraBox conversation, not a direct user instruction.\n"
                            + "Sender: " + json.dumps(row["origin"], ensure_ascii=False) + "\n\n" + content
                        )
                    await self._platform.session_runtime.send_as(
                        session_id,
                        content,
                        author_user_id=user.user_id,
                        source=_SOURCE,
                        client_message_id=str(row["reference_id"]),
                    )
                task = await self._project(user, agent_id, row)
            if not config.return_immediately and task.status.state not in _RETURNABLE:
                task = await self._wait(user, agent_id, task.id, write=True)
            return _output_view(
                apply_history_length(task, config), list(config.accepted_output_modes)
            )

    async def _context_session(self, user: UserContext, agent_id: str, context_id: str) -> str:
        identity = json.dumps([user.org_id, user.user_id, agent_id, context_id], ensure_ascii=False)
        reference_id = "a2a-context-" + hashlib.sha256(identity.encode()).hexdigest()
        row = await self._references.reserve(
            {
                "reference_id": reference_id,
                "kind": "context",
                "context_id": context_id,
                "user_id": user.user_id,
                "org_id": user.org_id,
                "agent_id": agent_id,
            }
        )
        if not row.get("session_id"):
            created = await self._agents.start_conversation(
                user,
                agent_id,
                idempotency_key=reference_id,
                source_type="a2a",
            )
            await self._references.bind_session(reference_id, str(created["session_id"]))
            return str(created["session_id"])
        return str(row["session_id"])

    async def context_for_session(self, user: UserContext, agent_id: str, session_id: str) -> str:
        """Bind an existing owned conversation to a stable A2A context without starting work."""
        await self._authorize(user, agent_id)
        session = await self._session(user, agent_id, session_id)
        if session.get("hidden"):
            raise TaskNotFoundError
        identity = json.dumps([user.org_id, user.user_id, agent_id, session_id], ensure_ascii=False)
        context_id = "context-session-" + hashlib.sha256(identity.encode()).hexdigest()
        context_identity = json.dumps([user.org_id, user.user_id, agent_id, context_id], ensure_ascii=False)
        reference_id = "a2a-context-" + hashlib.sha256(context_identity.encode()).hexdigest()
        await self._references.reserve({
            "reference_id": reference_id, "kind": "context", "context_id": context_id,
            "user_id": user.user_id, "org_id": user.org_id, "agent_id": agent_id,
        })
        await self._references.bind_session(reference_id, session_id)
        return context_id

    async def _reserve(
        self,
        user: UserContext,
        agent_id: str,
        message: a2a.Message,
        *,
        kind: str,
        origin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = json.dumps(
            [user.org_id, user.user_id, agent_id, message.message_id], ensure_ascii=False
        )
        reference_id = "a2a-" + hashlib.sha256(identity.encode()).hexdigest()
        digest = hashlib.sha256(message.SerializeToString(deterministic=True))
        if origin is not None:
            digest.update(b"\0" + json.dumps(
                {key: origin[key] for key in ("kind", "id")}, sort_keys=True,
            ).encode())
        fingerprint = digest.hexdigest()
        row = await self._references.reserve(
            {
                "reference_id": reference_id,
                "kind": kind,
                "fingerprint": fingerprint,
                "origin": origin,
                "user_id": user.user_id,
                "org_id": user.org_id,
                "agent_id": agent_id,
                "message_id": message.message_id,
                "context_id": message.context_id or "context-" + reference_id,
                "task_id": message.task_id,
            }
        )
        if row.get("fingerprint") != fingerprint or row.get("kind") != kind:
            raise InvalidParamsError(message="messageId already identifies a different request")
        return row

    async def _answer(
        self,
        user: UserContext,
        agent_id: str,
        params: a2a.SendMessageRequest,
    ) -> a2a.Task:
        message = params.message
        row = await self._reference(user, agent_id, message.task_id)
        if message.context_id and message.context_id != row.get("context_id"):
            raise InvalidParamsError(message="Task and context do not match")
        task = await self._project(user, agent_id, row)
        reservation = await self._reserve(user, agent_id, message, kind="answer")
        session_id = str(row["session_id"])
        if reservation.get("session_id") == session_id:
            return task
        if task.status.state in _TERMINAL:
            raise UnsupportedOperationError(message="A terminal task cannot accept another message")
        if len(message.parts) != 1 or not message.parts[0].HasField("data"):
            raise ContentTypeNotSupportedError(
                message="Task continuation requires an interaction_id and answer data part"
            )
        data = MessageToDict(message.parts[0].data)
        if (
            not isinstance(data, dict)
            or set(data) != {"interaction_id", "answer"}
            or not isinstance(data["answer"], dict)
        ):
            raise InvalidParamsError(message="Expected interaction_id and answer object")
        interaction_id = data["interaction_id"]
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            raise InvalidParamsError(message="interaction_id is required")
        interaction = await self._facts.get_interaction(session_id, interaction_id)
        if not interaction or interaction["response_id"] != task.metadata["response_id"]:
            raise InvalidParamsError(message="Interaction does not belong to this task")
        if interaction.get("answer") is not None:
            if interaction["answer"] != data["answer"]:
                raise InvalidParamsError(message="Interaction already has a different answer")
            await self._references.bind_session(str(reservation["reference_id"]), session_id)
            return task
        if task.status.state not in {
            a2a.TASK_STATE_INPUT_REQUIRED,
            a2a.TASK_STATE_AUTH_REQUIRED,
        }:
            raise InvalidParamsError(message="Task has no pending interaction")
        await self._platform.session_runtime.answer(user, session_id, interaction_id, data["answer"])
        await self._references.bind_session(str(reservation["reference_id"]), session_id)
        return await self._project(user, agent_id, row)

    async def _project(self, user: UserContext, agent_id: str, row: dict[str, Any]) -> a2a.Task:
        """The task as the runtime's recorded facts describe it now."""
        session_id = str(row.get("session_id") or "")
        session = await self._session(user, agent_id, session_id)
        sent = await self._facts.input_by_client_message_id(session_id, str(row["reference_id"]))
        response_id = str((sent or {}).get("response_id") or "")
        response = await self._facts.get_response(session_id, response_id) if response_id else None
        interactions: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        answers: list[dict[str, Any]] = []
        if response is not None:
            if response["status"] == "waiting":
                interactions = await self._facts.open_interactions_for_response(session_id, response_id)
            chunks = [
                output["part"]
                for output in await self._facts.output_for_responses(session_id, [response_id])
            ]
            answers = await self._facts.approval_answers(session_id, [response_id])
        return self._task(agent_id, row, session, sent, response, interactions, chunks, answers)

    def _task(
        self,
        agent_id: str,
        row: dict[str, Any],
        session: dict[str, Any],
        sent: dict[str, Any] | None,
        response: dict[str, Any] | None,
        interactions: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        answers: list[dict[str, Any]],
    ) -> a2a.Task:
        session_id = str(session["session_id"])
        context_id = str(row["context_id"])
        task_id = str(row["reference_id"])
        state = a2a.TASK_STATE_SUBMITTED
        history: list[a2a.Message] = []
        artifacts: list[a2a.Artifact] = []
        status_message: a2a.Message | None = None
        timestamp = str(row["created_at"])
        if sent is not None:
            timestamp = str(sent["created_at"])
            history.append(
                a2a.Message(
                    message_id=str(row["message_id"]),
                    context_id=context_id,
                    task_id=task_id,
                    role=a2a.ROLE_USER,
                    parts=[a2a.Part(text=str(sent["content"]))],
                )
            )
            if sent["state"] == "failed":
                state = a2a.TASK_STATE_FAILED
                failure = sent.get("failure") or {}
                status_message = a2a.Message(
                    message_id=f"{task_id}:failure",
                    role=a2a.ROLE_AGENT,
                    parts=[a2a.Part(text=str(failure.get("message") or failure.get("code") or ""))],
                )
        if response is not None:
            response_id = str(response["response_id"])
            timestamp = str(response.get("ended_at") or response["started_at"])
            # The caller sees the response as its owner's page does: the
            # public projection of each part, never an interaction's private half.
            message = fold_message(response_id, chunks)
            _apply_approval_decisions(message, {str(answer["interaction_id"]): answer["answer"] for answer in answers})
            message = public_message(message, shared=False)
            parts: list[a2a.Part] = []
            text = _response_text(message["parts"])
            if text:
                parts.append(a2a.Part(text=text))
            if message["parts"]:
                # The parts the conversation's page shows for this response.
                parts.append(_data({"parts": message["parts"]}))
            if parts:
                artifacts.append(
                    a2a.Artifact(artifact_id=f"{task_id}:result", name="Agent result", parts=parts)
                )
                history.append(
                    a2a.Message(
                        message_id=response_id,
                        context_id=context_id,
                        task_id=task_id,
                        role=a2a.ROLE_AGENT,
                        parts=parts,
                    )
                )
            if response["status"] == "ended":
                state = _ENDED.get(str(response.get("outcome") or ""), a2a.TASK_STATE_FAILED)
                failure_text = _failure_text(response, chunks) if state == a2a.TASK_STATE_FAILED else ""
                if failure_text:
                    status_message = a2a.Message(
                        message_id=f"{task_id}:failure",
                        role=a2a.ROLE_AGENT,
                        parts=[a2a.Part(text=failure_text)],
                    )
            elif response["status"] == "waiting" and interactions:
                pending = public_interaction_view(interactions[0]["contract"])
                assert pending is not None
                state = (
                    a2a.TASK_STATE_AUTH_REQUIRED
                    if pending.get("presentation") == PRESENTATION_TOOL_APPROVAL
                    else a2a.TASK_STATE_INPUT_REQUIRED
                )
                status_message = a2a.Message(
                    message_id=str(pending.get("interaction_id") or ""),
                    role=a2a.ROLE_AGENT,
                    task_id=task_id,
                    context_id=context_id,
                    parts=[
                        a2a.Part(text=_interaction_text(pending, agent_id=agent_id, task_id=task_id)),
                        _data(pending),
                    ],
                )
            else:
                state = a2a.TASK_STATE_WORKING
        if state in _RETURNABLE:
            notices = (
                [part.text for part in status_message.parts if part.HasField("text")]
                if status_message is not None
                else []
            )
            if state == a2a.TASK_STATE_FAILED:
                notices.insert(0, "Agent task failed.")
            elif state == a2a.TASK_STATE_CANCELED:
                notices.insert(0, "Agent task was canceled.")
            notices.append(f"[View Agent session]({public_path(f'/sessions/{session_id}')})")
            notice = "\n\n".join(notices)
            if status_message is None:
                status_message = a2a.Message(
                    message_id=f"{task_id}:status",
                    role=a2a.ROLE_AGENT,
                    task_id=task_id,
                    context_id=context_id,
                )
            data_parts = [part for part in status_message.parts if not part.HasField("text")]
            status_message.ClearField("parts")
            status_message.parts.extend([a2a.Part(text=notice), *data_parts])
            # Hermes renders data parts too, so the first nonempty artifact can
            # hide status even when the Agent has produced no text yet.
            first_artifact = next((artifact for artifact in artifacts if artifact.parts), None)
            if first_artifact is not None:
                first_text = next(
                    (
                        part
                        for part in first_artifact.parts
                        if part.HasField("text") and part.text
                    ),
                    None,
                )
                if first_text is not None:
                    first_text.text += "\n\n" + notice
                else:
                    first_artifact.parts.insert(0, a2a.Part(text=notice))
        if status_message is not None:
            status_message.context_id = context_id
            status_message.task_id = task_id
        metadata: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "session_title": str(session.get("title") or ""),
            "input_id": str((sent or {}).get("input_id") or ""),
            "response_id": str((response or {}).get("response_id") or ""),
            "session_url_path": public_path(f"/sessions/{session_id}"),
        }
        if session.get("source_type") is not None:
            metadata["source_type"] = str(session["source_type"])
        if row.get("origin") is not None:
            metadata["origin"] = row["origin"]
        return a2a.Task(
            id=task_id,
            context_id=context_id,
            status=a2a.TaskStatus(
                state=state, timestamp=_timestamp(timestamp), message=status_message
            ),
            history=history,
            artifacts=artifacts,
            metadata=metadata,
        )

    async def get(self, user: UserContext, agent_id: str, params: a2a.GetTaskRequest) -> a2a.Task:
        async with _protocol_errors():
            await self._authorize(user, agent_id)
            validate_history_length(params)
            row = await self._reference(user, agent_id, params.id)
            return apply_history_length(await self._project(user, agent_id, row), params)

    async def list(
        self, user: UserContext, agent_id: str, params: a2a.ListTasksRequest
    ) -> a2a.ListTasksResponse:
        """One page of the caller's tasks for this Agent, newest created first.

        Tasks are paged in the store by creation time, so a page reads only
        its own tasks. The status filters are applied to the page's tasks
        after projection: a filtered page can hold fewer than ``page_size``
        tasks while ``next_page_token`` still leads to more, and
        ``total_size`` counts the tasks before those filters.
        """
        async with _protocol_errors():
            await self._authorize(user, agent_id)
            validate_history_length(params)
            size = params.page_size or 50
            validate_page_size(size)
            after: tuple[datetime, str] | None = None
            if params.page_token:
                cursor = decode_list_tasks_cursor(params.page_token)
                if cursor is None or cursor.timestamp_ns is None:
                    raise InvalidParamsError(message="Invalid page token")
                try:
                    after = (_cursor_time(cursor.timestamp_ns), cursor.task_id)
                except OverflowError:
                    raise InvalidParamsError(message="Invalid page token") from None
            # One row past the page says whether another page follows.
            rows = await self._references.list_tasks(
                user_id=user.user_id,
                org_id=user.org_id,
                agent_id=agent_id,
                context_id=params.context_id,
                after=after,
                limit=size + 1,
            )
            page = rows[:size]
            tasks = []
            for row in page:
                try:
                    task = await self._project(user, agent_id, row)
                except TaskNotFoundError:
                    # The task's conversation was deleted.
                    continue
                if params.status and task.status.state != params.status:
                    continue
                if (
                    params.HasField("status_timestamp_after")
                    and task.status.timestamp.ToNanoseconds()
                    < params.status_timestamp_after.ToNanoseconds()
                ):
                    continue
                if not params.include_artifacts:
                    task.ClearField("artifacts")
                tasks.append(apply_history_length(task, params))
            next_page_token = ""
            if len(rows) > size:
                next_page_token = encode_list_tasks_cursor(
                    ListTasksCursor(
                        timestamp_ns=_cursor_ns(str(page[-1]["created_at"])),
                        task_id=str(page[-1]["reference_id"]),
                    )
                )
            return a2a.ListTasksResponse(
                tasks=tasks,
                page_size=size,
                total_size=await self._references.count_tasks(
                    user_id=user.user_id,
                    org_id=user.org_id,
                    agent_id=agent_id,
                    context_id=params.context_id,
                ),
                next_page_token=next_page_token,
            )

    async def cancel(
        self, user: UserContext, agent_id: str, params: a2a.CancelTaskRequest
    ) -> a2a.Task:
        async with _protocol_errors():
            await self._authorize(user, agent_id, write=True)
            row = await self._reference(user, agent_id, params.id)
            await self._session(user, agent_id, str(row["session_id"]))
            raise TaskNotCancelableError(message="Task-scoped cancellation is unavailable")

    def _changes(self, session_id: str) -> Subscription:
        """The session's notices; a task is read again after each one that commits output."""
        return self._platform._session_feed.subscribe(session_id)

    async def _wait(
        self,
        user: UserContext,
        agent_id: str,
        task_id: str,
        *,
        write: bool = False,
    ) -> a2a.Task:
        row = await self._reference(user, agent_id, task_id)
        with self._changes(str(row["session_id"])) as changes:
            while True:
                await self._authorize(user, agent_id, write=write)
                task = await self._project(user, agent_id, row)
                if task.status.state in _RETURNABLE:
                    return task
                await _committed(changes)

    async def stream(
        self, user: UserContext, agent_id: str, params: a2a.SendMessageRequest
    ) -> AsyncGenerator[Event, None]:
        immediate = a2a.SendMessageRequest()
        immediate.CopyFrom(params)
        immediate.configuration.return_immediately = True
        task = await self.send(user, agent_id, immediate)
        async for event in self._watch(user, agent_id, task.id, configuration=params.configuration):
            yield event

    async def subscribe(
        self, user: UserContext, agent_id: str, params: a2a.SubscribeToTaskRequest
    ) -> AsyncGenerator[Event, None]:
        async with _protocol_errors():
            if params.tenant:
                raise InvalidParamsError(message="Tenant routing is not supported")
            async for event in self._watch(user, agent_id, params.id, reject_terminal=True):
                yield event

    async def _watch(
        self,
        user: UserContext,
        agent_id: str,
        task_id: str,
        *,
        configuration: a2a.SendMessageConfiguration | None = None,
        reject_terminal: bool = False,
    ) -> AsyncGenerator[Event, None]:
        async with _protocol_errors():
            await self._authorize(user, agent_id)
            row = await self._reference(user, agent_id, task_id)
            # Subscribed before the first read, so nothing committed after it
            # goes unseen.
            with self._changes(str(row["session_id"])) as changes:
                previous = await self._project(user, agent_id, row)
                if reject_terminal and previous.status.state in _TERMINAL:
                    raise UnsupportedOperationError(message="A terminal task cannot be subscribed to")
                modes: list[str] = []
                initial = previous
                if configuration is not None:
                    initial = apply_history_length(initial, configuration)
                    modes = list(configuration.accepted_output_modes)
                yield _output_view(initial, modes)
                while previous.status.state not in _TERMINAL:
                    await _committed(changes)
                    await self._authorize(user, agent_id)
                    task = await self._project(user, agent_id, row)
                    current, old = _output_view(task, modes), _output_view(previous, modes)
                    old_artifacts = {item.artifact_id: item for item in old.artifacts}
                    for artifact in current.artifacts:
                        if old_artifacts.get(artifact.artifact_id) != artifact:
                            yield a2a.TaskArtifactUpdateEvent(
                                task_id=current.id,
                                context_id=current.context_id,
                                artifact=artifact,
                                append=False,
                                last_chunk=current.status.state in _TERMINAL,
                            )
                    if current.status != old.status:
                        yield a2a.TaskStatusUpdateEvent(
                            task_id=current.id,
                            context_id=current.context_id,
                            status=current.status,
                            metadata=current.metadata,
                        )
                    previous = task

    async def card(self, user: UserContext, agent_id: str, rpc_url: str) -> a2a.AgentCard:
        async with _protocol_errors():
            agent = await self._authorize(user, agent_id)
            return a2a.AgentCard(
                name=str(agent.get("name") or agent_id),
                description=str(agent.get("description") or "AstraBox Agent"),
                version=__version__,
                supported_interfaces=[
                    a2a.AgentInterface(
                        url=rpc_url,
                        protocol_binding="JSONRPC",
                        protocol_version="1.0",
                    )
                ],
                capabilities=a2a.AgentCapabilities(streaming=True, extended_agent_card=True),
                default_input_modes=["text/plain", "application/json"],
                default_output_modes=["text/plain", "application/json"],
                skills=[
                    a2a.AgentSkill(
                        id="execute",
                        name="Execute task",
                        tags=["agent"],
                        description="Send text to start work. Answer input-required tasks with a data part containing interaction_id and answer.",
                    )
                ],
            )


async def _committed(changes: Subscription) -> None:
    """Wait until the session's reader commits output, or the feed may have missed some."""
    while True:
        item = await changes.get()
        if isinstance(item, Resync) or (isinstance(item, Notice) and item.kind == "output"):
            return
