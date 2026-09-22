"""Session CRUD/lifecycle — the ``/api/v1/sessions`` resource family.

Covers list/create/get/delete/archive/recover, message history,
permission-mode, and the two sandbox/shell routes (``sandbox/terminate``,
``webshell``). Handler bodies rely on shared trace/error machinery from
:mod:`astrabox.api.routes._shared`.

The streaming/turn family (``ai-stream`` POST/GET, ``interrupt``,
``interaction-respond``, ``conversation/end``, ``terminal/stream``) lives in
:mod:`astrabox.api.routes.turns`. ``permission-mode`` is served from this
module rather than with that family, despite its turn-flavored semantics.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.api.routes._shared import (
    _resolve_user,
    _svc,
)
from astrabox.api.routes.response_envelope import ApiEnvelope

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
#   ``pending_interaction``, ``engine_capabilities``, message content blocks and
#   slash-command declarations are defined by the agent SDK and cross this
#   boundary verbatim; re-declaring them here would create the parallel
#   platform-owned copy that CLAUDE.md's "Engine semantics belong to the engine"
#   forbids, and it would drift the moment the vendor adds a member.
#
# Only ``session_id`` is required on a session payload. Session reads are
# projections assembled from a stored document, a snapshot and several
# overlays, and every other member is conditional on which of those a given
# read had. Pydantic answers 500 for a required field the payload lacks, so
# each one declared required narrows the set of reads a route can serve.


class AgentRuntimeView(BaseModel):
    """The owning Agent's runtime posture, overlaid onto an ``agent_chat`` session.

    Present only for ``session_kind="agent_chat"``. An Agent whose catalog row
    is gone still gets a view, reporting ``state="DELETED"``, so the console can
    distinguish a deleted Agent from one that never resolved.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    state: str | None = None
    sandbox_id: str | None = None
    expires_at: str | None = None
    startup_progress: str | None = None
    last_error: str | None = None
    runtime_unavailable: bool | None = None


class BackgroundTaskState(BaseModel):
    """Active native child tasks and their pending result-collection metadata.

    Absent (``null``) when no child has engine-declared active work. Engines
    without detached-result manifests report zero pending manifests.
    """

    model_config = ConfigDict(extra="forbid")

    state: str
    pending_manifest_count: int
    pending_task_count: int
    opened_event_seq: int | None = None
    source_turn_id: str | None = None


class DeliveryFailure(BaseModel):
    """The last turn's input, when the sandbox never acknowledged receiving it.

    Synthesized only while ``delivery_state`` is ``NOT_RECEIVED``; the text is
    returned so the console can offer the message back for resending.
    """

    model_config = ConfigDict(extra="forbid")

    turn_id: str | None = None
    client_message_id: str | None = None
    text: str | None = None
    summary: str | None = None


class RuntimeBindingView(BaseModel):
    """Dispatch availability without its internal owner or sandbox locators."""

    model_config = ConfigDict(extra="forbid")

    state: str
    can_dispatch: bool
    reason_code: str | None = None
    reason_message: str | None = None


class SessionRecord(BaseModel):
    """One session, as the read paths project it.

    The stored session document is not an API DTO. The platform facade builds
    this closed projection field by field; runtime identity, engine resume
    handles, ownership wiring and share capabilities never cross this model.

    Which members are populated depends on the read. The paged list summarizes
    each row to the fields in ``_SESSION_LIST_SUMMARY_FIELDS``
    (``astrabox/core/service/orchestrator/session_kernel/service_mixins/session_read.py``);
    detail adds the per-session reads that are too expensive to run per row —
    ``pending_interaction``, ``delivery_failure``, ``pending_inputs`` and
    ``engine_capabilities``.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    user_id: str | None = None
    template_name: str | None = None
    state: str | None = None
    permission_mode: str | None = None
    model_name: str | None = None
    sandbox_id: str | None = None
    terminal_cwd: str | None = None
    title: str | None = None
    source_type: str | None = None
    agent_id: str | None = None
    deployment_name: str | None = None
    agent_runtime: AgentRuntimeView | None = None
    session_kind: str | None = None
    engine_kind: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    deleted: bool | None = None
    runtime_unavailable: bool | None = None
    runtime_warning: bool | None = None
    engine_available: bool | None = None
    last_error: str | None = None
    startup_progress: str | None = None
    current_turn_id: str | None = None
    last_turn_id: str | None = None
    last_turn_status: str | None = None
    last_turn_error: str | None = None
    last_turn_command_id: str | None = None
    delivery_state: str | None = None
    last_turn_failure_phase: str | None = None
    last_turn_terminal_reason: str | None = None
    recovery_policy: str | None = None
    recovery_reason: str | None = None
    background_task_state: BackgroundTaskState | None = None
    delivery_failure: DeliveryFailure | None = None
    # Platform vocabulary, not the engine's: which workspace surfaces this
    # session's Agent declares. Kept beside `engine_capabilities` and outside
    # it, because an engine has no opinion about what a console shows.
    workspace_panels: dict[str, bool] | None = None
    # Engine vocabulary — see the boundary note above.
    engine_capabilities: dict[str, Any] | None = None
    pending_interaction: dict[str, Any] | None = None
    slash_commands: list[Any] | None = None
    slash_command_details: list[dict[str, Any]] | None = None
    # Turn inputs accepted but not yet delivered to the engine. Claude Code
    # only: the field is added where the session's engine resolves to
    # ``claude_code``.
    pending_inputs: list[dict[str, Any]] | None = None
    runtime_binding: RuntimeBindingView | None = None


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
    """One page of a session's durable messages, plus the live-turn overlay.

    ``messages`` is durable-only. A turn still in flight is reported separately
    through ``active_turn_overlay``, and both it and ``session_frame_seq``
    accompany the first page alone — a caller paging backwards with ``before``
    receives ``null`` for them.
    """

    model_config = ConfigDict(extra="forbid")

    # Message bodies are engine content blocks — see the boundary note above.
    messages: list[dict[str, Any]]
    has_more: bool
    active_turn_overlay: dict[str, Any] | None = None
    session_frame_seq: int | None = None
    pending_interaction: dict[str, Any] | None = None


class SessionHistoryBlockPage(BaseModel):
    """One page of the transcript with each settled response's work folded.

    Same records as :class:`SessionMessagePage`, paginated by record instead of
    by timestamp: ``next_cursor`` is what to send back as ``before``, and it
    pins every later page to the history this page saw. A response that ran
    tools and then answered carries one ``process_block`` in place of that
    work, which :class:`SessionHistoryBlockDetails` reopens.
    """

    model_config = ConfigDict(extra="forbid")

    # Message bodies are engine content blocks — see the boundary note above.
    messages: list[dict[str, Any]]
    has_more: bool
    active_turn_overlay: dict[str, Any] | None = None
    session_frame_seq: int | None = None
    pending_interaction: dict[str, Any] | None = None
    paging_mode: Literal["blocks"]
    next_cursor: str | None = None
    block_count: int


class SessionHistoryBlockDetails(BaseModel):
    """The blocks one folded ``process_block`` header stands for.

    One message carrying the folded blocks in their original order.
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
    ``turn_completed`` distinguishes work that ran to an answer from work an
    interruption cut short.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    summary: str | None = None
    error: str | None = None
    turn_completed: bool | None = None


class ChildRunRecord(BaseModel):
    """One engine-owned child run projected at Session scope."""

    model_config = ConfigDict(extra="forbid")

    child_run_id: str
    engine_kind: str
    depth: int
    engine_event: str
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


@router.get(
    "/api/v1/sessions",
    # Two shapes, and both are live: the console's `listSessions` reads the
    # bare array, `listSessionsPage` sends `page=1` and reads the page object
    # (frontend/src/api.ts).
    response_model=ApiEnvelope[SessionListPage] | ApiEnvelope[list[SessionRecord]],
    response_model_exclude_unset=True,
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
)
async def get_messages(session_id: str, request: Request):
    user = await _resolve_user(request)
    before = request.query_params.get("before")
    limit = min(int(request.query_params.get("limit", "20")), 50)
    result = await _svc().get_messages(user, session_id, before=before, limit=limit)
    return success_response(result)


@router.get(
    "/api/v1/sessions/{session_id}/history-blocks",
    response_model=ApiEnvelope[SessionHistoryBlockPage],
    response_model_exclude_unset=True,
)
async def get_session_history_blocks(session_id: str, request: Request):
    """Read the transcript with each settled response's tool work folded away.

    ``before`` is a ``next_cursor`` from an earlier page; omitting it starts at
    the newest records and pins the checkpoint every later page reuses.
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
)
async def get_session_history_block_details(
    session_id: str,
    block_id: str,
    request: Request,
):
    """Reopen one folded ``process_block`` and return the blocks it replaced.

    ``cursor`` is the checkpoint the header carried, and is required: without
    it the blocks would be read from the record as it stands now rather than as
    the page that folded them saw it.
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


@router.post(
    "/api/v1/sessions/{session_id}/archive",
    response_model=ApiEnvelope[SessionArchival],
    response_model_exclude_unset=True,
)
async def archive_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().archive_session(user, session_id)
    return success_response(result)
