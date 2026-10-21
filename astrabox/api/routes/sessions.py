"""Session CRUD/lifecycle — the ``/api/v1/sessions`` resource family.

Covers list/create/get/delete/archive/recover, message history and history
blocks, child runs, file changes, the read marker, the permission mode, and
the two sandbox/shell routes (``sandbox/terminate``, ``webshell``). Handler
bodies rely on shared trace/error machinery from
:mod:`astrabox.api.routes._shared`.

Sending (``inputs``), following (``stream``), answering an interaction,
interrupting, ending the conversation and the web terminal live in
:mod:`astrabox.api.routes.turns`.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.api.documentation import object_schema, query_parameter, request_body
from astrabox.api.routes._shared import (
    _resolve_user,
    _svc,
)
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError

router = APIRouter()


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``. Public read models are closed:
# service projections build an allowlisted DTO, and Pydantic refuses a field
# that was not deliberately added to that contract.
#
# Two boundaries decide what is spelled out below and what stays an open
# object:
#
# * Platform vocabulary — sandbox lifecycle, transport and orchestration state
#   — is named, because this package owns it.
# * Engine vocabulary is carried as ``dict``/``list`` of open values.
#   ``pending_interaction``, ``engine_capabilities``, the parts of a UI message
#   and slash-command declarations are defined by the agent SDK or its adapter
#   and cross this boundary verbatim; re-declaring them here would create the parallel
#   platform-owned copy that CLAUDE.md's "Engine semantics belong to the engine"
#   forbids, and it would drift the moment the vendor adds a member.
#
# Only ``session_id`` is required on a session payload. The list answers a
# summary of each row and the detail adds what the runtime is doing, so a field
# declared required would narrow the reads a route can serve; Pydantic answers
# 500 for a required field the payload lacks.


class ResponseView(BaseModel):
    """A response the runtime produced, as its reader recorded it.

    ``status`` is ``running``, ``waiting`` or ``ended``; ``outcome`` is
    ``completed``, ``failed`` or ``cancelled`` once ended, and
    ``native_reason`` is the engine's own word for the ending, carried
    verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    response_id: str
    origin: str | None = None
    status: str | None = None
    outcome: str | None = None
    native_reason: str | None = None
    # The engine's report of a failure, carried as it said it.
    error: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None


class HeldInputView(BaseModel):
    """An input the platform still holds: accepted, not yet durably taken by the runtime."""

    model_config = ConfigDict(extra="forbid")

    input_id: str
    client_message_id: str | None = None
    content: str | None = None
    state: str | None = None
    created_at: str | None = None


class SessionRecord(BaseModel):
    """One session, as the read paths project it.

    The list summarizes each row (``session_runtime/views.py``
    ``session_summary``): its state there comes from the row's lifecycle and
    the runtime status the reader last cached. The detail adds what the
    runtime is doing from its recorded facts: the responses in progress, the
    last one that ended, the interaction waiting on the owner, the inputs the
    platform still holds, and ``output_seq``, the output position to follow the
    session's stream from.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    user_id: str | None = None
    state: str | None = None
    #: ``idle``, ``working`` or ``waiting``, as the runtime last reported.
    runtime_status: str | None = None
    permission_mode: str | None = None
    model_name: str | None = None
    sandbox_id: str | None = None
    terminal_cwd: str | None = None
    title: str | None = None
    source_type: str | None = None
    agent_id: str | None = None
    #: The referenced Agent's lifecycle, independent of the Session's runtime.
    agent_state: str | None = None
    assistant_id: str | None = None
    deployment_name: str | None = None
    session_kind: str | None = None
    engine_kind: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    #: When the conversation last gained a reply, and when its owner last
    #: read it: newer output than ``read_at`` is unread.
    last_output_at: str | None = None
    read_at: str | None = None
    runtime_unavailable: bool | None = None
    engine_available: bool | None = None
    last_error: str | None = None
    startup_progress: str | None = None
    recovery_reason: str | None = None
    # Platform vocabulary, not the engine's: which workspace surfaces this
    # session's Agent declares. Kept beside `engine_capabilities` and outside
    # it, because an engine has no opinion about what a console shows.
    workspace_panels: dict[str, bool] | None = None
    # Engine vocabulary — see the boundary note above.
    engine_capabilities: dict[str, Any] | None = None
    pending_interaction: dict[str, Any] | None = None
    slash_commands: list[Any] | None = None
    slash_command_details: list[dict[str, Any]] | None = None
    current_responses: list[ResponseView] | None = None
    last_response: ResponseView | None = None
    held_inputs: list[HeldInputView] | None = None
    output_seq: int | None = None


class SessionListPage(BaseModel):
    """One page of summarized sessions, newest first.

    ``next_cursor`` is the value to pass back as ``cursor``; it is ``null`` on
    the last page, which ``has_more`` reports independently.
    """

    model_config = ConfigDict(extra="forbid")

    sessions: list[SessionRecord]
    has_more: bool
    next_cursor: str | None = None


class SessionMessagePage(BaseModel):
    """One page of a session's conversation as AI SDK UI messages, oldest first.

    A user message is an input the session accepted; an assistant message is
    one response, its stored parts folded the way the AI SDK folds them live.
    ``before`` is the cursor for the next older page. The first page also
    carries ``output_seq``, the output position its parts were read through,
    to follow the stream from, and the interaction waiting on the owner. A
    share link's page carries neither ``output_seq`` nor the tool input of an
    interaction.
    """

    model_config = ConfigDict(extra="forbid")

    # UI message parts are the engine adapter's translation — see the
    # boundary note above.
    messages: list[dict[str, Any]]
    has_more: bool
    before: str | None = None
    output_seq: int | None = None
    pending_interaction: dict[str, Any] | None = None


class SessionHistoryBlockPage(BaseModel):
    """One page of the conversation with each ended response's work folded.

    The messages of :class:`SessionMessagePage`, where the work of a response
    that had ended is replaced by ``data-process-block`` parts that
    :class:`SessionHistoryBlockDetails` reopens. ``next_cursor`` is what to send
    back as ``before``; it pins every older page to the output position the
    first page was read at (``output_seq``).
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    has_more: bool
    output_seq: int | None = None
    pending_interaction: dict[str, Any] | None = None
    paging_mode: Literal["blocks"]
    next_cursor: str | None = None
    block_count: int


class SessionHistoryBlockDetails(BaseModel):
    """The parts one folded ``data-process-block`` header stands for.

    One message carrying the folded parts in their original order.
    ``has_more`` is always ``false``: a header's contents are whole, and there
    is nothing further to page through.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    has_more: bool


class ProcessSummaryState(BaseModel):
    """The generated label for one response's folded work.

    ``status`` is ``generating`` while a writer holds the claim, ``completed``
    once the label exists, and ``failed`` when the model call did not produce
    one — a failed label can be asked for again with ``retry=true``.
    ``disabled`` means label generation is switched off, not a failed request.
    ``response_completed`` distinguishes work that ran to an answer from work
    that stopped short.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    summary: str | None = None
    error: str | None = None
    response_completed: bool | None = None


class ChildRunRecord(BaseModel):
    """One engine-owned child run projected at Session scope."""

    model_config = ConfigDict(extra="forbid")

    child_run_id: str
    engine_kind: str
    depth: int
    engine_event: str | None = None
    engine_status: str | None = None
    engine_reason: str | None = None
    closed: bool
    active: bool = Field(
        description="The engine reports active work, independently of resource closure."
    )
    operations: list[str]
    tool_call_ids: list[str] = Field(
        default_factory=list,
        description="Tool invocations associated with this child by its engine.",
    )
    parent_child_run_id: str | None = None
    description: str | None = None
    task_type: str | None = None
    last_tool_name: str | None = None
    summary: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None


class SessionChildRunPage(BaseModel):
    """All child runs visible in one Session, in tree order."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    child_runs: list[ChildRunRecord]


class ChildRunMessagePage(BaseModel):
    """The engine-owned transcript of one Session child run."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    child_run_id: str
    messages: list[dict[str, Any]]


class ChildRunStopResult(BaseModel):
    """Acknowledgement that an engine-owned child run was asked to stop."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    child_run_id: str
    status: str


class PermissionModeUpdate(BaseModel):
    """The outcome of setting a session's permission mode.

    ``applied`` reports whether the running engine took the new mode, not
    whether the request was accepted: a session with no live runtime records
    the mode and answers ``false``.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    permission_mode: str | None = None
    applied: bool | None = None
    runtime_applied: bool | None = None
    changed: bool | None = None
    previous_permission_mode: str | None = None
    event_seq: int | None = None


class SandboxTermination(BaseModel):
    """The outcome of reclaiming a session's sandbox.

    The conversation survives — history is durable and the next turn rebuilds a
    sandbox. ``killed`` distinguishes a sandbox this call destroyed from one
    that was already gone.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    status: str | None = None
    sandbox_id: str | None = None
    killed: bool | None = None


class SessionDeletion(BaseModel):
    """Confirmation that a session was soft-deleted. Deletion is final."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    deleted: bool | None = None
    status: str | None = None
    sandbox_id: str | None = None
    killed: bool | None = None


class SessionArchival(BaseModel):
    """Confirmation that a session was archived.

    Archiving terminates the sandbox as well, so the fields of
    :class:`SandboxTermination` accompany the archive flag.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    archived: bool | None = None
    status: str | None = None
    sandbox_id: str | None = None
    killed: bool | None = None


class WebshellAccess(BaseModel):
    """The web-shell URL for a session's sandbox.

    Answered only by a backend that offers a web shell; the rest refuse with
    ``WEB_SHELL_UNSUPPORTED`` (``astrabox/seams/sandbox.py``).
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    sandbox_id: str
    url: str


class SessionFileChangeResponse(BaseModel):
    """One ended response and every file change it made; empty when it changed none."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    parts: list[dict[str, Any]]


class SessionFileChanges(BaseModel):
    """The responses that ended after ``after_seq``, through ``through_seq``.

    ``through_seq`` is the next ``after_seq``. Each response replaces what a
    reader held for it.
    """

    model_config = ConfigDict(extra="forbid")

    through_seq: int
    responses: list[SessionFileChangeResponse]


@router.get(
    "/api/v1/sessions",
    # Two shapes, and both are live: the console's `listSessions` reads the
    # bare array, `listSessionsPage` sends `page=1` and reads the page object
    # (frontend/src/api.ts).
    response_model=ApiEnvelope[SessionListPage] | ApiEnvelope[list[SessionRecord]],
    response_model_exclude_unset=True,
    openapi_extra={
        "parameters": [
            query_parameter(
                "page",
                "integer",
                "Set to 1 for a cursor page; omitted returns the complete visible list.",
                enum=[1],
            ),
            query_parameter(
                "limit",
                "integer",
                "Page size, clamped to 1..100; used only with page=1.",
                default=50,
            ),
            query_parameter("cursor", "string", "Opaque next_cursor from the preceding page."),
        ]
    },
)
async def list_sessions(request: Request):
    user = await _resolve_user(request)
    if str(request.query_params.get("page") or "").strip() == "1":
        limit = min(max(int(request.query_params.get("limit", "50")), 1), 100)
        cursor = request.query_params.get("cursor")
        page = await _svc().list_sessions_page(user, limit=limit, cursor=cursor)
        return success_response(page)
    sessions = await _svc().list_sessions(user)
    return success_response(sessions)


@router.get(
    "/api/v1/sessions/{session_id}",
    response_model=ApiEnvelope[SessionRecord],
    response_model_exclude_unset=True,
)
async def get_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    session = await _svc().get_session(user, session_id)
    return success_response(session)


@router.get(
    "/api/v1/sessions/{session_id}/messages",
    response_model=ApiEnvelope[SessionMessagePage],
    response_model_exclude_unset=True,
    openapi_extra={
        "parameters": [
            query_parameter("before", "string", "Opaque cursor for older messages."),
            query_parameter("limit", "integer", "Message page size, capped at 50.", default=20),
        ]
    },
)
async def get_messages(session_id: str, request: Request):
    user = await _resolve_user(request)
    before = request.query_params.get("before")
    limit = min(int(request.query_params.get("limit", "20")), 50)
    result = await _svc().get_messages(user, session_id, before=before, limit=limit)
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/file-changes",
    response_model=ApiEnvelope[SessionFileChanges],
)
async def get_session_file_changes(
    session_id: str, request: Request, after_seq: int = Query(default=0, ge=0),
):
    user = await _resolve_user(request)
    return success_response(await _svc().get_file_changes(user, session_id, after_seq=after_seq))


@router.get(
    "/api/v1/sessions/{session_id}/history-blocks",
    response_model=ApiEnvelope[SessionHistoryBlockPage],
    response_model_exclude_unset=True,
    openapi_extra={
        "parameters": [
            query_parameter("before", "string", "Opaque cursor for older history blocks."),
            query_parameter("limit", "integer", "History page size, capped at 50.", default=50),
        ]
    },
)
async def get_session_history_blocks(session_id: str, request: Request):
    """Read the conversation with each ended response's work folded away.

    ``before`` is a ``next_cursor`` from an earlier page; omitting it starts at
    the newest entries and pins the output position every later page reuses.
    """

    user = await _resolve_user(request)
    before = request.query_params.get("before")
    limit = min(int(request.query_params.get("limit", "50")), 50)
    result = await _svc().get_history_blocks(
        user,
        session_id,
        before=before,
        limit=limit,
    )
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/history-blocks/{block_id}",
    response_model=ApiEnvelope[SessionHistoryBlockDetails],
    response_model_exclude_unset=True,
    openapi_extra={
        "parameters": [
            query_parameter(
                "cursor",
                "string",
                "Recorded process-block position from the header.",
                required=True,
            )
        ]
    },
)
async def get_session_history_block_details(
    session_id: str,
    block_id: str,
    request: Request,
):
    """Reopen one folded ``data-process-block`` and return the parts it replaced.

    ``cursor`` is the one the header carried, and is required: it pins the read
    to the output position of the page that folded the parts.
    """

    user = await _resolve_user(request)
    cursor = str(request.query_params.get("cursor") or "").strip()
    if not cursor:
        raise APIError(
            code="INVALID_REQUEST",
            message="cursor is required",
            status_code=400,
        )
    result = await _svc().get_history_block_details(
        user,
        session_id,
        block_id,
        cursor=cursor,
    )
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/messages/{message_id}/process-summary",
    response_model=ApiEnvelope[ProcessSummaryState],
    response_model_exclude_unset=True,
    openapi_extra={
        "parameters": [
            query_parameter(
                "retry",
                "string",
                "Set to true to request regeneration after a recorded summary failure.",
            )
        ]
    },
)
async def generate_session_process_summary(
    session_id: str,
    message_id: str,
    request: Request,
):
    """Write, or return, the label for one response's folded work.

    Answers with the stored label when there already is one. ``retry=true``
    asks again for a label whose generation failed; a completed one is returned
    unchanged.
    """

    user = await _resolve_user(request)
    retry = str(request.query_params.get("retry") or "").strip().lower() == "true"
    result = await _svc().generate_process_summary(
        user,
        session_id,
        message_id,
        retry_failed=retry,
    )
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/child-runs",
    response_model=ApiEnvelope[SessionChildRunPage],
    response_model_exclude_unset=True,
)
async def list_session_child_runs(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().list_session_child_runs(user, session_id)
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages",
    response_model=ApiEnvelope[ChildRunMessagePage],
    response_model_exclude_unset=True,
)
async def get_session_child_run_messages(
    session_id: str,
    child_run_id: str,
    request: Request,
):
    user = await _resolve_user(request)
    result = await _svc().get_session_child_run_messages(
        user,
        session_id,
        child_run_id,
    )
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop",
    response_model=ApiEnvelope[ChildRunStopResult],
    response_model_exclude_unset=True,
)
async def stop_session_child_run(
    session_id: str,
    child_run_id: str,
    request: Request,
):
    user = await _resolve_user(request)
    result = await _svc().stop_session_child_run(user, session_id, child_run_id)
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/permission-mode",
    response_model=ApiEnvelope[PermissionModeUpdate],
    response_model_exclude_unset=True,
    openapi_extra=request_body(
        object_schema(
            {
                "permission_mode": {
                    "type": "string",
                    "description": "One mode from the current runtime capability manifest.",
                }
            },
            required=("permission_mode",),
        )
    ),
)
async def update_session_permission_mode(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    user = await _resolve_user(request)
    permission_mode = payload.get("permission_mode")
    if permission_mode is None:
        raise APIError(
            code="INVALID_REQUEST",
            message="permission_mode is required",
            status_code=400,
        )
    permission_mode = str(permission_mode).strip()
    if not permission_mode:
        raise APIError(
            code="INVALID_REQUEST",
            message="permission_mode is required",
            status_code=400,
        )
    result = await _svc().update_session_permission_mode(
        user,
        session_id,
        permission_mode,
    )
    return success_response(result)


# Sessions are created only as Agent conversations through
# `POST /api/v1/agents/{id}/conversations`, or as Assistant conversations. See
# ``docs/domain-model.md`` §2 defines both creation paths.


@router.post(
    "/api/v1/sessions/{session_id}/sandbox/terminate",
    response_model=ApiEnvelope[SandboxTermination],
    response_model_exclude_unset=True,
)
async def terminate_sandbox(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().terminate_sandbox(user, session_id)
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/recover",
    # Recovery answers with the re-read session, not with a report of what it
    # did: the worker's outcome is consumed internally and
    # `SessionKernelService.recover_session` returns `get_session(...)`, so a
    # caller sees the recovered state directly.
    response_model=ApiEnvelope[SessionRecord],
    response_model_exclude_unset=True,
)
async def recover_session(session_id: str, request: Request):
    service = _svc()
    user = await _resolve_user(request)
    result = await service.recover_session(user, session_id)
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/webshell",
    response_model=ApiEnvelope[WebshellAccess],
    response_model_exclude_unset=True,
)
async def get_webshell(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().get_webshell_url(user, session_id)
    return success_response(result)


@router.delete(
    "/api/v1/sessions/{session_id}",
    response_model=ApiEnvelope[SessionDeletion],
    response_model_exclude_unset=True,
)
async def delete_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().delete_session(user, session_id)
    return success_response(result)


class SessionRead(BaseModel):
    """When the owner last read a conversation."""

    session_id: str
    read_at: str


@router.post(
    "/api/v1/sessions/{session_id}/read",
    response_model=ApiEnvelope[SessionRead],
)
async def mark_session_read(session_id: str, request: Request):
    # The console calls this while the owner has the conversation open, so
    # output that arrived meanwhile is not shown as unread.
    user = await _resolve_user(request)
    result = await _svc().mark_session_read(user, session_id)
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/archive",
    response_model=ApiEnvelope[SessionArchival],
    response_model_exclude_unset=True,
)
async def archive_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().archive_session(user, session_id)
    return success_response(result)
