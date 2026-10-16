"""What the console and share links read about a session.

:class:`SessionReads` serves the session list, one session's detail, its
history (as UI messages, and folded into history blocks), the label of a folded
response, the files its responses changed, and its child runs. Every read is
built from the ``sessions`` row and the facts the session's reader recorded
(``persistence/repository/session_runtime_repository.py``); none asks a runtime.

An owner's read checks ownership first. A share link's read answers the same
history without what only the owner may see: who wrote each message and how it
reached the session, the action a refused input asks of its owner, and the tool
input an open approval asks about.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.capabilities import resolve_session_capabilities
from astrabox.core.service.orchestrator.engine.child_runs import ChildRunProjectionError
from astrabox.core.service.orchestrator.engine.registry import EngineKindNotRegistered
from astrabox.core.service.orchestrator.session_runtime import child_runs
from astrabox.core.service.orchestrator.session_runtime.file_changes import (
    FileChangeCursorError,
    file_changes,
)
from astrabox.core.service.orchestrator.session_runtime.ui_message import public_part
from astrabox.core.service.orchestrator.session_runtime.history_blocks import (
    HistoryCursorError,
    load_block_parts,
    load_history_page,
)
from astrabox.core.service.orchestrator.session_runtime.process_summaries import ProcessSummaries
from astrabox.core.service.orchestrator.session_runtime.service import (
    SessionRuntimeService,
    interaction_view,
    runtime_view_state,
)
from astrabox.core.service.orchestrator.slash_commands import (
    normalize_slash_command_details,
    normalize_slash_commands,
)
from astrabox.persistence.repository.keyset import InvalidListCursor
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.seams.runtime_preparation import validated_user_action

#: A session as its owner's list shows it: the row, and the state the row's
#: cached runtime status gives, so a page of sessions is one query.
_SUMMARY_FIELDS = (
    "session_id",
    "user_id",
    "title",
    "runtime_status",
    "permission_mode",
    "session_kind",
    "engine_kind",
    "agent_id",
    "agent_state",
    "assistant_id",
    "deployment_name",
    "source_type",
    "sandbox_id",
    "expires_at",
    "created_at",
    "updated_at",
    "last_output_at",
    "read_at",
    "runtime_unavailable",
    "recovery_reason",
    "last_error",
    "startup_progress",
)
_DETAIL_FIELDS = ("model_name", "terminal_cwd")
_NO_ACTIVITY: dict[str, list[Any]] = {"open_interactions": [], "open_responses": [], "held_inputs": []}
#: What an input's author and route say; a share link does not carry it.
_OWNER_INPUT_METADATA = ("author_user_id", "source")


def session_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row of the session list."""
    view = {key: row.get(key) for key in _SUMMARY_FIELDS}
    view["state"] = runtime_view_state(dict(row), _NO_ACTIVITY)
    return view


def session_detail_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row half of one session's detail; the runtime half is :meth:`SessionRuntimeService.runtime_view`."""
    view = session_summary(row)
    view.update({key: row.get(key) for key in _DETAIL_FIELDS})
    view["slash_commands"] = normalize_slash_commands(row.get("slash_commands"))
    view["slash_command_details"] = normalize_slash_command_details(row.get("slash_command_details"))
    try:
        resolve_session_capabilities(dict(row))
    except EngineKindNotRegistered:
        # A session outlives a removed engine plugin; its owner can still read it.
        view["engine_available"] = False
    else:
        view["engine_available"] = True
    return view


def public_message(message: Mapping[str, Any], *, shared: bool) -> dict[str, Any]:
    """One UI message as a page receives it."""
    parts = [projected for part in message.get("parts") or [] if (projected := public_part(part, shared=shared))]
    metadata = dict(message.get("metadata") or {})
    failure = metadata.get("failure")
    if isinstance(failure, dict):
        failure = dict(failure)
        action = validated_user_action(failure.pop("user_action", None))
        if action and not shared:
            failure["user_action"] = action
        metadata["failure"] = failure
    if shared:
        for key in _OWNER_INPUT_METADATA:
            metadata.pop(key, None)
    return {"id": message["id"], "role": message["role"], "parts": parts, "metadata": metadata}


class SessionReads:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        facts: SessionRuntimeRepository,
        reads: SessionReadRepository,
        runtime: SessionRuntimeService,
        summaries: ProcessSummaries,
    ) -> None:
        self._sessions = sessions
        self._facts = facts
        self._reads = reads
        self._runtime = runtime
        self._summaries = summaries

    async def _owned(self, user: UserContext, session_id: str) -> dict[str, Any]:
        row = await self._sessions.get_owned_session(session_id, str(user.user_id))
        if row is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return row

    # -- sessions ------------------------------------------------------------

    async def sessions(self, user: UserContext) -> list[dict[str, Any]]:
        """The owner's newest sessions."""
        return [session_summary(row) for row in await self._sessions.list_user_sessions(str(user.user_id))]

    async def sessions_page(self, user: UserContext, *, limit: int, cursor: str | None) -> dict[str, Any]:
        """One page of the owner's sessions, newest activity first."""
        try:
            page = await self._sessions.list_user_sessions_page(str(user.user_id), limit=limit, cursor=cursor)
        except InvalidListCursor as exc:
            raise APIError(code="INVALID_REQUEST", message="cursor is not one this list returned", status_code=400) from exc
        return {
            "sessions": [session_summary(row) for row in page["sessions"]],
            "has_more": bool(page["has_more"]),
            "next_cursor": page["next_cursor"],
        }

    async def detail(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """One session with what its runtime is doing and the output position to follow from."""
        row = await self._owned(user, session_id)
        return {**session_detail_fields(row), **await self._runtime.runtime_view(row)}

    # -- history -------------------------------------------------------------

    async def messages(
        self, user: UserContext, session_id: str, *, before: str | None, limit: int
    ) -> dict[str, Any]:
        await self._owned(user, session_id)
        return await self._messages(session_id, before=before, limit=limit, shared=False)

    async def history_blocks(
        self, user: UserContext, session_id: str, *, before: str | None, limit: int
    ) -> dict[str, Any]:
        await self._owned(user, session_id)
        return await self._history_blocks(session_id, before=before, limit=limit, shared=False)

    async def history_block_details(
        self, user: UserContext, session_id: str, block_id: str, *, cursor: str
    ) -> dict[str, Any]:
        await self._owned(user, session_id)
        return await self._block_details(session_id, block_id, cursor=cursor, shared=False)

    async def process_summary(
        self, user: UserContext, session_id: str, response_id: str, *, retry_failed: bool
    ) -> dict[str, Any]:
        """The label of one response's folded work (see :class:`ProcessSummaries`)."""
        await self._owned(user, session_id)
        return await self._summaries.request(session_id, response_id, retry_failed=retry_failed)

    async def _messages(
        self, session_id: str, *, before: str | None, limit: int, shared: bool
    ) -> dict[str, Any]:
        """One page of UI messages; the first page also names the interaction waiting on its reader."""
        page = await self._runtime.history_for(session_id, before=before, limit=limit)
        result: dict[str, Any] = {
            "messages": [public_message(message, shared=shared) for message in page["messages"]],
            "has_more": page["has_more"],
            "before": page["before"],
        }
        if before is None:
            await self._first_page(result, session_id, output_seq=page["output_seq"], shared=shared)
        return result

    async def _history_blocks(
        self, session_id: str, *, before: str | None, limit: int, shared: bool
    ) -> dict[str, Any]:
        try:
            page = await load_history_page(self._facts, self._reads, session_id, cursor=before, limit=limit)
        except HistoryCursorError as exc:
            raise APIError(code="INVALID_REQUEST", message=str(exc), status_code=400) from exc
        labels = await self._summaries.labels(session_id, list(page.labelled))
        for response_id, header in page.labelled.items():
            header["summary"] = labels.get(response_id)
        result: dict[str, Any] = {
            "messages": [public_message(message, shared=shared) for message in page.messages],
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
            "paging_mode": "blocks",
            "block_count": len(page.messages),
        }
        if before is None:
            await self._first_page(result, session_id, output_seq=page.through_seq, shared=shared)
        return result

    async def _block_details(
        self, session_id: str, block_id: str, *, cursor: str, shared: bool
    ) -> dict[str, Any]:
        """The parts one folded header stands for, as one message."""
        try:
            found = await load_block_parts(self._facts, self._reads, session_id, block_id, cursor=cursor)
        except HistoryCursorError as exc:
            raise APIError(code="INVALID_REQUEST", message=str(exc), status_code=400) from exc
        if found is None:
            raise APIError(code="NOT_FOUND", message="process block not found at this cursor", status_code=404)
        loaded, parts = found
        message = {
            "id": block_id,
            "role": "assistant",
            "parts": parts,
            "metadata": {**loaded.message["metadata"], "history_block_id": block_id},
        }
        return {"messages": [public_message(message, shared=shared)], "has_more": False}

    async def _first_page(
        self, result: dict[str, Any], session_id: str, *, output_seq: int, shared: bool
    ) -> None:
        """What the newest page adds: where to follow the stream from, and the interaction waiting on its reader.

        The interaction is read after the page: one still open then is shown
        here, and one opened later arrives on the stream after ``output_seq``.
        A share link has no stream to follow.
        """
        if not shared:
            result["output_seq"] = output_seq
        result["pending_interaction"] = await self._waiting_interaction(session_id, shared=shared)

    async def _waiting_interaction(self, session_id: str, *, shared: bool) -> dict[str, Any] | None:
        row = await self._reads.waiting_interaction(session_id)
        return interaction_view(row, include_tool_input=not shared) if row is not None else None

    # -- file changes and child runs -----------------------------------------

    async def file_changes(self, user: UserContext, session_id: str, *, after_seq: int) -> dict[str, Any]:
        await self._owned(user, session_id)
        try:
            return await file_changes(self._facts, self._reads, session_id, after_seq=after_seq)
        except FileChangeCursorError as exc:
            raise APIError(code="INVALID_REQUEST", message=str(exc), status_code=400) from exc

    async def child_runs(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self._owned(user, session_id)
        try:
            runs = await child_runs.list_child_runs(self._reads, session_id)
        except ChildRunProjectionError as exc:
            raise _projection_error(exc) from exc
        return {"session_id": session_id, "child_runs": runs}

    async def child_run_messages(self, user: UserContext, session_id: str, child_run_id: str) -> dict[str, Any]:
        await self._owned(user, session_id)
        try:
            messages = await child_runs.child_run_messages(self._reads, session_id, child_run_id)
        except ChildRunProjectionError as exc:
            raise _projection_error(exc) from exc
        if messages is None:
            raise _child_run_not_found(child_run_id)
        return {"session_id": session_id, "child_run_id": child_run_id, "messages": messages}

    async def child_run_stop_control(self, user: UserContext, session_id: str, child_run_id: str) -> str:
        """The engine's control reference for stopping one child, after checking it can be stopped now.

        Handing the stop to the runtime is the session reader's: it holds the
        runtime's only connection.
        """
        row = await self._owned(user, session_id)
        if row.get("state") != SessionState.READY.value:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message=f"the session is {str(row.get('state') or '').lower()} and has no runtime to stop a child in",
                status_code=409,
            )
        try:
            entry = await child_runs.find_child_run(self._reads, session_id, child_run_id)
        except ChildRunProjectionError as exc:
            raise _projection_error(exc) from exc
        if entry is None:
            raise _child_run_not_found(child_run_id)
        if entry["closed"]:
            raise APIError(
                code="CHILD_RUN_ALREADY_TERMINAL",
                message=f"Child run '{child_run_id}' is already terminal",
                status_code=409,
            )
        control_ref = str(entry.get("_control_ref") or "")
        if "stop" not in entry["operations"] or not control_ref:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message=f"Child run '{child_run_id}' is not currently stoppable",
                status_code=409,
            )
        return control_ref

    # -- share links ---------------------------------------------------------

    async def shared_session(self, session_id: str, *, allow_download: bool) -> dict[str, Any]:
        """What a share link shows about the session around its transcript."""
        row = await self._sessions.get_session(session_id)
        if row is None:
            raise APIError(code="SHARE_NOT_FOUND", message="share link not found or revoked", status_code=404)
        return {
            "title": row.get("title"),
            "pending_interaction": await self._waiting_interaction(session_id, shared=True),
            "share_allow_download": bool(allow_download),
        }

    async def shared_messages(self, session_id: str, *, before: str | None, limit: int) -> dict[str, Any]:
        return await self._messages(session_id, before=before, limit=limit, shared=True)

    async def shared_history_blocks(self, session_id: str, *, before: str | None, limit: int) -> dict[str, Any]:
        return await self._history_blocks(session_id, before=before, limit=limit, shared=True)

    async def shared_history_block_details(self, session_id: str, block_id: str, *, cursor: str) -> dict[str, Any]:
        return await self._block_details(session_id, block_id, cursor=cursor, shared=True)


def _projection_error(exc: ChildRunProjectionError) -> APIError:
    return APIError(code="CHILD_RUN_PROJECTION_INVALID", message=str(exc), status_code=409)


def _child_run_not_found(child_run_id: str) -> APIError:
    return APIError(code="CHILD_RUN_NOT_FOUND", message=f"Child run '{child_run_id}' was not found", status_code=404)
