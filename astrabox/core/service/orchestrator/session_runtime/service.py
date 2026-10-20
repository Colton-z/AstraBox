"""What people and channels do with a conversation's runtime, as the API sees it.

Sending, answering and interrupting record the request and return; the
session's reader hands it to the runtime (see :mod:`.reader`). Following and
history read what the reader recorded.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import AgentState, SessionState
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    public_interaction_view,
    resolve_interaction_permission_mode,
    validate_interaction_response,
)
from astrabox.core.service.orchestrator.runtime.preparation import require_session_user_prerequisites
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.follow import follow_output
from astrabox.core.service.orchestrator.session_runtime.host import ReaderHost
from astrabox.core.service.orchestrator.session_runtime.ui_message import history_messages
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.seams.admission import ADMISSION_KIND_INPUT, AdmissionRequest, enforce_admission
from astrabox.seams.runtime_preparation import validated_user_action

_HISTORY_PAGE_LIMIT = 100


def _encode_before(before: tuple[str, int, int] | None) -> str | None:
    if before is None:
        return None
    raw = json.dumps(list(before), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_before(cursor: str | None) -> tuple[str, int, int] | None:
    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        at, kind, seq = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        return str(at), int(kind), int(seq)
    except (ValueError, TypeError) as exc:
        raise APIError(code="INVALID_REQUEST", message="invalid history cursor", status_code=400) from exc


def response_view(row: dict[str, Any]) -> dict[str, Any]:
    """A recorded response as a page shows it."""
    return {
        key: row.get(key)
        for key in ("response_id", "origin", "status", "outcome", "native_reason", "error", "started_at", "ended_at")
    }


def interaction_view(row: dict[str, Any], *, include_tool_input: bool) -> dict[str, Any] | None:
    """An open interaction as a page shows it: the contract's public half and the response it pauses.

    The adapter's replies and native answer keys stay on the server
    (:func:`~astrabox.core.service.orchestrator.engine.interaction_contract.public_interaction_view`).
    """
    view = public_interaction_view(row["contract"], include_tool_input=include_tool_input)
    if view is None:
        return None
    return {**view, "response_id": row["response_id"]}


def runtime_view_state(session: dict[str, Any], activity: dict[str, Any]) -> str:
    """The session's state as the page shows it.

    The lifecycle state while the session is not READY, recovery required
    while its runtime is unavailable, and otherwise what the runtime reports,
    with inputs the platform still holds counted as work.
    """
    lifecycle = str(session.get("state") or "")
    if lifecycle != SessionState.READY.value:
        return lifecycle
    if session.get("runtime_unavailable"):
        return SessionState.RECOVERY_REQUIRED.value
    status = str(session.get("runtime_status") or "idle")
    if status == "waiting" or activity["open_interactions"]:
        return "WAITING_INPUT"
    if status == "working" or activity["open_responses"] or activity["held_inputs"]:
        return "PROCESSING"
    return "READY"


class SessionRuntimeService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        repo: SessionRuntimeRepository,
        feed: SessionFeed,
        host: ReaderHost,
    ) -> None:
        self._sessions = sessions
        self._repo = repo
        self._feed = feed
        self._host = host

    # -- requests ----------------------------------------------------------

    def notification_health(self) -> dict[str, Any]:
        """The notification listener owned by this service, including its terminal state."""
        return self._feed.diagnostics()

    async def send(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        *,
        content_blocks: list[dict[str, Any]] | None = None,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Check a known startup prerequisite before accepting the owner's message."""
        session = await self._sessions.get_owned_session(session_id, str(user.user_id))
        if session is None:
            raise await self._not_accepting(session_id, str(user.user_id))
        if (
            session.get("state") == SessionState.READY.value
            and session.get("agent_state") not in {AgentState.DELETING.value, AgentState.DELETED.value}
            and validated_user_action(session.get("startup_user_action")) is not None
        ):
            await require_session_user_prerequisites(session)
        return await self._accept(
            session_id,
            content,
            content_blocks=content_blocks,
            client_message_id=client_message_id,
            owner_user_id=str(user.user_id),
            author_user_id=str(user.user_id),
            source="console",
        )

    async def send_as(
        self,
        session_id: str,
        content: str,
        *,
        author_user_id: str | None,
        source: str,
        client_message_id: str | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Accept a message a channel, schedule or agent call delivers; the caller authorized it."""
        return await self._accept(
            session_id,
            content,
            content_blocks=content_blocks,
            client_message_id=client_message_id,
            owner_user_id=None,
            author_user_id=author_user_id,
            source=source,
        )

    async def _accept(
        self,
        session_id: str,
        content: str,
        *,
        content_blocks: list[dict[str, Any]] | None,
        client_message_id: str | None,
        owner_user_id: str | None,
        author_user_id: str | None,
        source: str,
    ) -> dict[str, Any]:
        await enforce_admission(
            AdmissionRequest(
                kind=ADMISSION_KIND_INPUT,
                user_id=str(owner_user_id or author_user_id or ""),
                session_id=session_id,
            )
        )
        if content_blocks:
            session = (
                await self._sessions.get_owned_session(session_id, owner_user_id)
                if owner_user_id is not None
                else await self._sessions.get_session(session_id)
            )
            if session is None:
                raise await self._not_accepting(session_id, owner_user_id)
            manifest = session.get("engine_capabilities")
            declared_types = manifest.get("input_content_types") if isinstance(manifest, dict) else None
            if not isinstance(declared_types, list) or any(
                not isinstance(kind, str) or not kind.strip() for kind in declared_types
            ):
                raise APIError(
                    code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                    message=f"session has no verified engine input-content declaration (session={session_id})",
                    status_code=502,
                )
            unsupported_types = sorted({block["type"] for block in content_blocks} - set(declared_types))
            if unsupported_types:
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine {session.get('engine_kind')!r} does not accept turn content "
                        f"types {unsupported_types!r}"
                    ),
                    status_code=409,
                )
        input_id = str(uuid.uuid4())
        async with get_persistence().coordination.runtime_lock(session_id):
            accepted = await self._repo.accept_input(
                session_id=session_id,
                input_id=input_id,
                client_message_id=str(client_message_id or "").strip() or input_id,
                content=content,
                content_blocks=content_blocks,
                owner_user_id=owner_user_id,
                author_user_id=author_user_id,
                source=source,
            )
        if accepted is None:
            raise await self._not_accepting(session_id, owner_user_id)
        row, created = accepted
        await self._host.ensure(session_id)
        return {
            "session_id": session_id,
            "input_id": row["input_id"],
            "client_message_id": row["client_message_id"],
            "accepted_at": row["created_at"],
            "state": row["state"],
            "failure": row.get("failure"),
            "duplicate": not created,
        }

    async def _not_accepting(self, session_id: str, owner_user_id: str | None) -> APIError:
        session = await self._sessions.get_session_including_deleted(session_id)
        if session is None or (owner_user_id is not None and session.get("user_id") != owner_user_id):
            return APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        if not session.get("deleted") and session.get("state") not in (
            SessionState.TERMINATED.value, SessionState.DELETED.value,
        ):
            return APIError(
                code="AGENT_DELETED",
                message="the conversation's Agent was deleted, so it takes no new messages",
                status_code=409,
            )
        state = "deleted" if session.get("deleted") else "terminated"
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"conversation is unavailable because the session is {state}",
            status_code=409,
        )

    async def answer(
        self, user: UserContext, session_id: str, interaction_id: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        """Store a person's answer for the runtime; the reader hands it over."""
        await self._require_owner(user, session_id)
        interaction = await self._repo.get_interaction(session_id, interaction_id)
        if interaction is None:
            raise APIError(code="INTERACTION_NOT_FOUND", message="interaction not found", status_code=404)
        if interaction["status"] != "open" or interaction.get("answer") is not None:
            raise APIError(
                code="INTERACTION_NOT_OPEN",
                message=f"interaction is {interaction['status']}",
                status_code=409,
            )
        contract = dict(interaction["contract"])
        validate_interaction_response(contract, answer)
        if not await self._repo.request_answer(
            session_id, interaction_id, answer=answer, answered_by=str(user.user_id)
        ):
            raise APIError(code="INTERACTION_NOT_OPEN", message="interaction was answered or closed", status_code=409)
        mode = resolve_interaction_permission_mode(contract, answer)
        if mode:
            await self._sessions.set_permission_mode(session_id, mode)
        return {"session_id": session_id, "interaction_id": interaction_id, "answered": True}

    async def interrupt(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """Ask the runtime to stop a response, without withdrawing accepted inputs.

        A response that is running but whose runtime has no connected reader
        cannot be reached. Held inputs without an open response also refuse
        the operation; ``idle`` means neither was observed.
        """
        await self._require_owner(user, session_id)
        if not await self._repo.open_response_ids(session_id):
            held = await self._repo.open_input_ids(session_id)
            if held:
                raise APIError(
                    code="PENDING_INPUTS_NOT_CANCELLED",
                    message=(
                        "No response is running to interrupt. Accepted messages were not withdrawn "
                        "and may still run; archive the conversation to take it offline."
                    ),
                    status_code=409,
                    data={"input_ids": held},
                )
            return {"session_id": session_id, "status": "idle"}
        control = await self._repo.request_control(
            session_id, kind="interrupt", target=None, requested_by=str(user.user_id)
        )
        if control is None:
            raise APIError(
                code="INTERRUPT_UNAVAILABLE",
                message="the conversation's runtime is not connected, so nothing can stop the response",
                status_code=409,
            )
        return {"session_id": session_id, "status": "accepted"}

    async def stop_child_run(self, user: UserContext, session_id: str, control_ref: str) -> dict[str, Any]:
        """Ask the runtime to stop one sub-agent, named by the runtime's own handle."""
        await self._require_owner(user, session_id)
        control = await self._repo.request_control(
            session_id, kind="stop_child", target=control_ref, requested_by=str(user.user_id)
        )
        if control is None:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message="the conversation's runtime is not connected, so nothing can stop the sub-agent",
                status_code=409,
            )
        return {"session_id": session_id, "status": "accepted"}

    # -- reads -------------------------------------------------------------

    async def follow(self, user: UserContext, session_id: str, *, after_seq: int) -> AsyncIterator[dict[str, Any]]:
        await self._require_owner(user, session_id)
        return follow_output(self._feed, self._repo, session_id, after_seq=after_seq)

    async def history(
        self, user: UserContext, session_id: str, *, before: str | None = None, limit: int = _HISTORY_PAGE_LIMIT
    ) -> dict[str, Any]:
        """One page of the conversation as UI messages, with the log position it reflects.

        The page folds parts only through ``output_seq``; following from
        there adds the rest of an open response exactly once.
        """
        await self._require_owner(user, session_id)
        return await self.history_for(session_id, before=before, limit=limit)

    async def history_for(
        self, session_id: str, *, before: str | None = None, limit: int = _HISTORY_PAGE_LIMIT
    ) -> dict[str, Any]:
        output_seq, _cursor = await self._repo.resume_point(session_id)
        page = await self._repo.history_page(
            session_id, before=_decode_before(before), limit=max(1, min(int(limit), _HISTORY_PAGE_LIMIT))
        )
        response_ids = [str(r["response_id"]) for r in page["responses"]]
        outputs = await self._repo.output_for_responses(session_id, response_ids, through_seq=output_seq)
        interactions = await self._repo.response_interactions(session_id, response_ids)
        return {
            "messages": history_messages(page, outputs, interactions),
            "has_more": page["has_more"],
            "before": _encode_before(page["before"]),
            "output_seq": output_seq,
        }

    async def runtime_view(self, session: dict[str, Any]) -> dict[str, Any]:
        """The runtime half of the session detail, and the output position it reflects.

        ``output_seq`` is read before the facts: a page that follows the output
        from it hears every part recorded after this view, so nothing that
        happens between the two reads is missed.
        """
        session_id = str(session["session_id"])
        output_seq, _cursor = await self._repo.resume_point(session_id)
        activity = await self._repo.activity(session_id)
        waiting = [row for row in activity["open_interactions"] if row.get("answer") is None]
        last_response = activity["last_response"]
        return {
            "state": runtime_view_state(session, activity),
            "runtime_status": session.get("runtime_status"),
            "current_responses": [response_view(row) for row in activity["open_responses"]],
            "last_response": response_view(last_response) if last_response else None,
            "pending_interaction": interaction_view(waiting[0], include_tool_input=True) if waiting else None,
            "held_inputs": [
                {key: row.get(key) for key in ("input_id", "client_message_id", "content", "state", "created_at")}
                for row in activity["held_inputs"]
            ],
            "output_seq": output_seq,
        }

    async def _require_owner(self, user: UserContext, session_id: str) -> None:
        if await self._sessions.get_owner(session_id) != str(user.user_id):
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
