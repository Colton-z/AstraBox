"""A2A tasks projected from durable Agent input receipts and native response frames."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import API_READ_SCOPE, API_WRITE_SCOPE, UserContext
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
    settled_input_failure,
    settled_input_outcome,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_TOOL_APPROVAL,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_engine_fifo_messages,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    SDK_RESPONSE_RESULT_BOUNDARY,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _payload_is_turn_terminal,
)
from astrabox.core.service.orchestrator.session_message_view import project_session_messages
from astrabox.core.service.orchestrator.session_public_projection import (
    project_public_message,
    project_public_pending_interaction,
)
from astrabox.deploy.public_path import public_path
from astrabox.persistence.repository.agent_task_reference_repository import (
    AgentTaskReferenceRepository,
)
from astrabox.persistence.repository.interaction_snapshot_repository import (
    InteractionSnapshotRepository,
)
from astrabox.persistence.repository.session_event_repository import SessionEventRepository

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


@dataclass
class _TaskJournal:
    """A connection's read cache of the canonical immutable Session journal."""

    events: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)

    @property
    def through_seq(self) -> int:
        return max(
            [int(row["event_seq"]) for row in self.events]
            + [int(row["frame_seq"]) for row in self.frames],
            default=0,
        )

    def before(self, through_seq: int) -> _TaskJournal:
        return _TaskJournal(
            events=[row for row in self.events if int(row["event_seq"]) <= through_seq],
            frames=[row for row in self.frames if int(row["frame_seq"]) <= through_seq],
        )


def _pending_interaction(journal: _TaskJournal, turn_id: str) -> dict[str, Any] | None:
    pending: dict[str, Any] | None = None
    closed: set[str] = set()
    answers = {
        str(row.get("causation_id") or ""): str(
            (row.get("payload") or {}).get("interaction_id") or ""
        )
        for row in journal.events
        if row.get("event_type") == "command.accepted"
        and (row.get("payload") or {}).get("command_type") == "AnswerInteraction"
    }
    facts = [(int(row["event_seq"]), row) for row in journal.events]
    facts.extend((int(row["frame_seq"]), row) for row in journal.frames)
    for _, row in sorted(facts, key=lambda fact: fact[0]):
        if str(row.get("turn_id") or "") != turn_id:
            continue
        payload = row.get("payload") or {}
        kind = row.get("event_type")
        interaction_id = ""
        if kind == "interaction.answer_persisted":
            interaction_id = str(payload.get("interaction_id") or "")
        elif kind == "dispatch.confirmed":
            interaction_id = answers.get(str(row.get("causation_id") or ""), "")
        elif payload.get("type") == "start":
            interaction_id = str(
                (payload.get("messageMetadata") or {}).get("continues_interaction") or ""
            )
        if interaction_id:
            closed.add(interaction_id)
            if pending and pending.get("interaction_id") == interaction_id:
                pending = None
        if kind in {"turn.completed", "turn.failed", "turn.recovered"}:
            pending = None
        candidate = None
        if kind == "turn.awaiting_interaction":
            candidate = payload
        elif payload.get("type") == "data-interaction":
            candidate = payload.get("data")
        if isinstance(candidate, dict) and candidate.get("interaction_id") not in closed:
            pending = candidate
    return pending


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
    def __init__(self, *, agent_service: Any) -> None:
        self._agents = agent_service
        self._platform = agent_service._platform
        self._references = AgentTaskReferenceRepository()
        self._events = SessionEventRepository()
        self._interactions = InteractionSnapshotRepository()

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
        session = await self._platform.get_session(user, session_id)
        if session.get("agent_id") != agent_id or session.get("session_kind") != "agent_chat":
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
        self, user: UserContext, agent_id: str, params: a2a.SendMessageRequest
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
                row = await self._reserve(user, agent_id, message, kind="task")
                session_id = await self._context_session(user, agent_id, str(row["context_id"]))
                await self._references.bind_session(str(row["_id"]), session_id)
                row["session_id"] = session_id
                await self._session(user, agent_id, session_id)
                command = await self._command(row)
                if command is None:
                    try:
                        await self._platform.dispatch_turn_input(
                            user,
                            session_id,
                            content,
                            client_message_id=str(row["_id"]),
                            admission_only=True,
                        )
                    except APIError:
                        if await self._command(row) is None:
                            raise
                        if (
                            await settled_input_outcome(
                                self._events, session_id, f"{session_id}:{row['_id']}"
                            )
                            is None
                        ):
                            raise
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
                "_id": reference_id,
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

    async def _reserve(
        self,
        user: UserContext,
        agent_id: str,
        message: a2a.Message,
        *,
        kind: str,
    ) -> dict[str, Any]:
        identity = json.dumps(
            [user.org_id, user.user_id, agent_id, message.message_id], ensure_ascii=False
        )
        reference_id = "a2a-" + hashlib.sha256(identity.encode()).hexdigest()
        fingerprint = hashlib.sha256(message.SerializeToString(deterministic=True)).hexdigest()
        row = await self._references.reserve(
            {
                "_id": reference_id,
                "kind": kind,
                "fingerprint": fingerprint,
                "user_id": user.user_id,
                "org_id": user.org_id,
                "agent_id": agent_id,
                "message_id": message.message_id,
                "context_id": message.context_id or "context-" + reference_id,
                "task_id": message.task_id,
                "created_at": utcnow_iso(),
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
        interaction = await self._interactions.get_interaction(session_id, interaction_id)
        if not interaction or str(interaction.get("turn_id") or "") != str(
            task.metadata["turn_id"]
        ):
            raise InvalidParamsError(message="Interaction does not belong to this task")
        if interaction.get("interaction_state") == "ANSWERED":
            if interaction.get("response") != data["answer"]:
                raise InvalidParamsError(message="Interaction already has a different answer")
            await self._references.bind_session(str(reservation["_id"]), session_id)
            return task
        if task.status.state not in {
            a2a.TASK_STATE_INPUT_REQUIRED,
            a2a.TASK_STATE_AUTH_REQUIRED,
        }:
            raise InvalidParamsError(message="Task has no pending interaction")
        await self._platform.answer_pending_interaction(
            user, session_id, interaction_id, data["answer"]
        )
        await self._references.bind_session(str(reservation["_id"]), session_id)
        return await self._project(user, agent_id, row)

    async def _command(self, row: dict[str, Any]) -> dict[str, Any] | None:
        return await self._events.find_command_by_client_message_id(
            str(row["session_id"]),
            client_message_id=str(row["_id"]),
        )

    async def _read_journal(self, session_id: str, *, after_seq: int = 0) -> _TaskJournal:
        latest_events, latest_frames = await asyncio.gather(
            self._events.list_events(session_id, newest_first=True, limit=1),
            self._events.list_frames(session_id, scope="turn", newest_first=True, limit=1),
        )
        through_seq = max(
            [int(row["event_seq"]) for row in latest_events]
            + [int(row["frame_seq"]) for row in latest_frames],
            default=0,
        )
        journal = _TaskJournal()
        for frames in (False, True):
            cursor = after_seq
            while cursor < through_seq:
                page = await (
                    self._events.list_frames(
                        session_id, scope="turn", after_seq=cursor, before_seq=through_seq + 1, limit=500
                    )
                    if frames
                    else self._events.list_events(
                        session_id, after_seq=cursor, before_seq=through_seq + 1, limit=500
                    )
                )
                if not page:
                    break
                if frames:
                    journal.frames.extend(page)
                else:
                    journal.events.extend(page)
                cursor = int(page[-1]["frame_seq" if frames else "event_seq"])
        return journal

    async def _project(self, user: UserContext, agent_id: str, row: dict[str, Any]) -> a2a.Task:
        session_id = str(row.get("session_id") or "")
        session = await self._session(user, agent_id, session_id)
        journal = await self._read_journal(session_id)
        return self._project_journal(user, agent_id, row, session, journal)

    def _project_journal(
        self,
        user: UserContext,
        agent_id: str,
        row: dict[str, Any],
        session: dict[str, Any],
        journal: _TaskJournal,
    ) -> a2a.Task:
        session_id = str(row.get("session_id") or "")
        context_id = str(row["context_id"])
        command = next(
            (
                event
                for event in journal.events
                if event.get("event_type") == "command.accepted"
                and (event.get("payload") or {}).get("client_message_id") == row["_id"]
            ),
            None,
        )
        state = a2a.TASK_STATE_SUBMITTED
        history: list[a2a.Message] = []
        artifacts: list[a2a.Artifact] = []
        status_message = None
        timestamp = str(row["created_at"])
        turn_id = ""
        command_id = ""
        input_id = ""
        if command:
            payload = command["payload"]
            command_id = str(command["causation_id"])
            turn_id = str(command.get("turn_id") or "")
            input_id = str(payload["input_id"])
            timestamp = str(command.get("occurred_at") or timestamp)
            history.append(
                a2a.Message(
                    message_id=str(row["message_id"]),
                    context_id=context_id,
                    task_id=str(row["_id"]),
                    role=a2a.ROLE_USER,
                    parts=[a2a.Part(text=str(payload["content"]))],
                )
            )
            frames = [
                frame
                for frame in journal.frames
                if int(frame["frame_seq"]) > int(command["event_seq"])
            ]
            selected: list[dict[str, Any]] = []
            consuming = False
            for frame in frames:
                part = frame.get("payload") or {}
                if part.get("type") == "data-input-consumed":
                    consuming = (part.get("data") or {}).get("inputId") == input_id
                    if consuming:
                        selected = [frame]
                        turn_id = str(frame.get("turn_id") or turn_id)
                    continue
                if consuming:
                    selected.append(frame)
            response = None
            terminal_seq = 0
            if selected:
                state = a2a.TASK_STATE_WORKING
                timestamp = str(selected[-1].get("created_at") or timestamp)
                messages = build_active_engine_fifo_messages(
                    session_id=session_id,
                    turn_id=turn_id,
                    frames=selected,
                    default_message_seq=int(command["event_seq"]),
                    user_id=user.user_id,
                )
                response = next(
                    (
                        item
                        for item in messages
                        if item.get("message_id") == input_response_message_id(input_id)
                    ),
                    None,
                )
                terminals = [
                    f
                    for f in selected
                    if (
                        _payload_is_turn_terminal(f.get("payload") or {})
                        or (f.get("payload") or {}).get(SDK_RESPONSE_RESULT_BOUNDARY) is True
                    )
                ]
                if terminals:
                    terminal_seq = int(terminals[-1]["frame_seq"])
                    last_payload = terminals[-1]["payload"]
                    failed = (
                        last_payload.get("type") == "error"
                        or (last_payload.get("data") or {}).get("is_error") is True
                        or last_payload.get("finishReason") == "error"
                    )
                    # FIFO result boundaries settle one logical input. A whole
                    # turn's finish waits for its canonical terminal payload.
                    if last_payload.get(SDK_RESPONSE_RESULT_BOUNDARY) is True:
                        state = a2a.TASK_STATE_FAILED if failed else a2a.TASK_STATE_COMPLETED
            terminal_events = [
                event
                for event in reversed(journal.events)
                if event.get("turn_id") == turn_id
                and event.get("event_type") in {"turn.completed", "turn.failed", "turn.recovered"}
            ][:1]
            if state not in _TERMINAL and terminal_events:
                terminal = terminal_events[0]
                terminal_payload = terminal.get("payload") or {}
                if selected or terminal.get("causation_id") == command_id:
                    state = (
                        a2a.TASK_STATE_FAILED
                        if terminal["event_type"] == "turn.failed"
                        else a2a.TASK_STATE_COMPLETED
                    )
                    timestamp = str(terminal.get("occurred_at") or timestamp)
                    terminal_seq = int(terminal["event_seq"])
                    if terminal_payload.get("error_text"):
                        status_message = a2a.Message(
                            message_id=f"{row['_id']}:failure",
                            role=a2a.ROLE_AGENT,
                            parts=[a2a.Part(text=str(terminal_payload["error_text"]))],
                        )
            if state in _TERMINAL and any(
                (frame.get("payload") or {}).get("type") == "abort" for frame in selected
            ):
                state = a2a.TASK_STATE_CANCELED
            if state == a2a.TASK_STATE_FAILED and status_message is None:
                failure_text = ""
                if terminal_events and (
                    selected or terminal_events[0].get("causation_id") == command_id
                ):
                    failure_text = str(
                        (terminal_events[0].get("payload") or {}).get("error_text") or ""
                    )
                if not failure_text:
                    for frame in reversed(selected):
                        error_payload = frame.get("payload") or {}
                        if error_payload.get("type") == "error":
                            failure_text = str(error_payload.get("errorText") or "")
                            if failure_text:
                                break
                        error_data = error_payload.get("data") or {}
                        if (
                            error_payload.get("type") == "data-result"
                            and error_data.get("is_error") is True
                        ):
                            failure_text = str(error_data.get("result") or "")
                            if failure_text:
                                break
                if failure_text:
                    status_message = a2a.Message(
                        message_id=f"{row['_id']}:failure",
                        role=a2a.ROLE_AGENT,
                        parts=[a2a.Part(text=failure_text)],
                    )
            if terminal_seq and state == a2a.TASK_STATE_COMPLETED:
                interruptions = [
                    event
                    for event in journal.events
                    if event.get("turn_id") == turn_id
                    and event.get("event_type") == "turn.interrupt_requested"
                ][:1]
                if interruptions:
                    interrupt_seq = int(interruptions[0]["event_seq"])
                    start_seq = (
                        int(selected[0]["frame_seq"]) if selected else int(command["event_seq"])
                    )
                    if start_seq < interrupt_seq <= terminal_seq:
                        state = a2a.TASK_STATE_CANCELED
            if terminal_events and selected:
                messages = project_session_messages(
                    events=journal.events, frames=journal.frames, user_id=user.user_id
                )
                response = next(
                    (
                        item
                        for item in messages
                        if item.get("message_id") == input_response_message_id(input_id)
                    ),
                    response,
                )
            if response:
                response = project_public_message(response)
                parts = []
                if response.get("content"):
                    parts.append(a2a.Part(text=str(response["content"])))
                if response.get("blocks"):
                    parts.append(_data({"blocks": response["blocks"]}))
                if parts:
                    artifacts.append(
                        a2a.Artifact(
                            artifact_id=f"{row['_id']}:result", name="Agent result", parts=parts
                        )
                    )
                    history.append(
                        a2a.Message(
                            message_id=str(response["message_id"]),
                            context_id=context_id,
                            task_id=str(row["_id"]),
                            role=a2a.ROLE_AGENT,
                            parts=parts,
                        )
                    )
            failure = settled_input_failure(
                next(
                    (
                        event
                        for event in journal.events
                        if event.get("event_type") == "turn.failed"
                        and event.get("causation_id") == command_id
                    ),
                    None,
                )
            )
            if failure is not None:
                state = a2a.TASK_STATE_FAILED
                status_message = a2a.Message(
                    message_id=f"{row['_id']}:failure",
                    role=a2a.ROLE_AGENT,
                    parts=[a2a.Part(text=failure.message)],
                )
            pending = _pending_interaction(journal, turn_id)
            if (
                state not in _TERMINAL
                and isinstance(pending, dict)
                and str(pending.get("turn_id") or "") == turn_id
                and selected
                and consuming
            ):
                public_pending = project_public_pending_interaction(pending)
                assert public_pending is not None
                state = (
                    a2a.TASK_STATE_AUTH_REQUIRED
                    if pending.get("presentation") == PRESENTATION_TOOL_APPROVAL
                    else a2a.TASK_STATE_INPUT_REQUIRED
                )
                status_message = a2a.Message(
                    message_id=str(pending.get("interaction_id") or ""),
                    role=a2a.ROLE_AGENT,
                    task_id=str(row["_id"]),
                    context_id=context_id,
                    parts=[
                        a2a.Part(
                            text=_interaction_text(
                                public_pending, agent_id=agent_id, task_id=str(row["_id"])
                            )
                        ),
                        _data(public_pending),
                    ],
                )
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
                    message_id=f"{row['_id']}:status",
                    role=a2a.ROLE_AGENT,
                    task_id=str(row["_id"]),
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
            status_message.task_id = str(row["_id"])
        metadata = {
            "agent_id": agent_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "command_id": command_id,
            "input_id": input_id,
            "session_url_path": public_path(f"/sessions/{session_id}"),
        }
        for key in ("deployment_id", "run_id", "source_type"):
            if session.get(key) is not None:
                metadata[key] = str(session[key])
        return a2a.Task(
            id=str(row["_id"]),
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
        async with _protocol_errors():
            await self._authorize(user, agent_id)
            validate_history_length(params)
            size = params.page_size or 50
            validate_page_size(size)
            cursor = decode_list_tasks_cursor(params.page_token) if params.page_token else None
            if params.page_token and cursor is None:
                raise InvalidParamsError(message="Invalid page token")
            rows = await self._references.list_tasks(
                user_id=user.user_id,
                org_id=user.org_id,
                agent_id=agent_id,
                context_id=params.context_id,
            )
            tasks = []
            for row in rows:
                try:
                    task = await self._project(user, agent_id, row)
                except APIError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                if params.status and task.status.state != params.status:
                    continue
                if (
                    params.HasField("status_timestamp_after")
                    and task.status.timestamp.ToNanoseconds()
                    < params.status_timestamp_after.ToNanoseconds()
                ):
                    continue
                tasks.append(task)

            def position(task: a2a.Task) -> tuple[int, str]:
                return task.status.timestamp.ToNanoseconds(), task.id

            tasks.sort(key=position, reverse=True)
            remaining = [
                task
                for task in tasks
                if cursor is None or position(task) < (cursor.timestamp_ns or 0, cursor.task_id)
            ]
            page = remaining[:size]
            for task in page:
                if not params.include_artifacts:
                    task.ClearField("artifacts")
            return a2a.ListTasksResponse(
                tasks=[apply_history_length(task, params) for task in page],
                page_size=size,
                total_size=len(tasks),
                next_page_token=encode_list_tasks_cursor(
                    ListTasksCursor(
                        timestamp_ns=page[-1].status.timestamp.ToNanoseconds(),
                        task_id=page[-1].id,
                    )
                )
                if len(remaining) > size
                else "",
            )

    async def cancel(
        self, user: UserContext, agent_id: str, params: a2a.CancelTaskRequest
    ) -> a2a.Task:
        async with _protocol_errors():
            await self._authorize(user, agent_id, write=True)
            row = await self._reference(user, agent_id, params.id)
            await self._session(user, agent_id, str(row["session_id"]))
            raise TaskNotCancelableError(message="Task-scoped cancellation is unavailable")

    async def _wait(
        self,
        user: UserContext,
        agent_id: str,
        task_id: str,
        *,
        write: bool = False,
    ) -> a2a.Task:
        while True:
            await self._authorize(user, agent_id, write=write)
            row = await self._reference(user, agent_id, task_id)
            task = await self._project(user, agent_id, row)
            if task.status.state in _RETURNABLE:
                return task
            await asyncio.sleep(0.5)

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
            session_id = str(row["session_id"])
            session = await self._session(user, agent_id, session_id)
            journal = await self._read_journal(session_id)
            previous = self._project_journal(user, agent_id, row, session, journal)
            if reject_terminal and previous.status.state in _TERMINAL:
                raise UnsupportedOperationError(message="A terminal task cannot be subscribed to")
            initial = previous
            modes: list[str] = []
            if configuration is not None:
                initial = apply_history_length(initial, configuration)
                modes = list(configuration.accepted_output_modes)
            yield _output_view(initial, modes)
            while previous.status.state not in _TERMINAL:
                cursor = journal.through_seq
                await asyncio.sleep(0.5)
                await self._authorize(user, agent_id)
                session = await self._session(user, agent_id, session_id)
                updates = await self._read_journal(session_id, after_seq=cursor)
                journal.events.extend(updates.events)
                journal.frames.extend(updates.frames)
                # Every reader projects each immutable fact in the same sequence,
                # irrespective of how many facts arrived between its polls.
                boundaries = sorted(
                    {int(event["event_seq"]) for event in updates.events}
                    | {int(frame["frame_seq"]) for frame in updates.frames}
                )
                for boundary in boundaries:
                    canonical = self._project_journal(
                        user, agent_id, row, session, journal.before(boundary)
                    )
                    task = _output_view(canonical, modes)
                    old = _output_view(previous, modes)
                    old_artifacts = {item.artifact_id: item for item in old.artifacts}
                    for artifact in task.artifacts:
                        if old_artifacts.get(artifact.artifact_id) != artifact:
                            yield a2a.TaskArtifactUpdateEvent(
                                task_id=task.id,
                                context_id=task.context_id,
                                artifact=artifact,
                                append=False,
                                last_chunk=task.status.state in _TERMINAL,
                            )
                    if task.status != old.status:
                        yield a2a.TaskStatusUpdateEvent(
                            task_id=task.id,
                            context_id=task.context_id,
                            status=task.status,
                            metadata=task.metadata,
                        )
                    previous = canonical
                    if previous.status.state in _TERMINAL:
                        return

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
