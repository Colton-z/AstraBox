"""``/api/v1/admin/*`` — the console-identity admin schema/dashboard/ops family.

Some handlers below delegate their body to a module-level helper function
that :mod:`astrabox.api.routes.admin_api` also calls for its overlapping
``/api/v1/admin-api/*`` operations, so both surfaces share one implementation.
That import edge is one-directional: ``admin_api.py`` imports these helpers
from here, never the reverse.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.http_headers import build_attachment_headers
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.environment_schema import get_environment_schema
from astrabox.core.service.orchestrator.agent_schema import get_agent_schema
from astrabox.api.routes._shared import _resolve_user, _svc
from astrabox.deploy.admin_integrations import configured_management_links

logger = get_logger(__name__)

router = APIRouter()


# ── Response payloads ────────────────────────────────────────────────
#
# One model per route, declared as ``ApiEnvelope[...]`` on the decorator so the
# generated OpenAPI client sees the body instead of an untyped object. The
# obligations such a declaration carries — ``extra="allow"`` on the payload
# model, ``response_model_exclude_unset=True`` on the route, and a declared
# type only where the write path fixes it — are stated in
# :mod:`astrabox.api.routes.response_envelope`.
#
# Every field declared below is one its handler always writes, so a caller can
# rely on it being present. A payload that carries a stored row stays
# ``dict[str, Any]``: those fields belong to the repository that wrote them,
# and naming a subset here would publish this surface as the owner of a shape
# it does not control.


class AdminFormSchema(BaseModel):
    """The editable schema an admin form renders.

    One grammar, two producers:
    :func:`~astrabox.core.service.orchestrator.agent_schema.get_agent_schema`
    and
    :func:`~astrabox.core.service.orchestrator.environment_schema.get_environment_schema`.
    ``groups`` and ``fields`` are the form's own vocabulary (key, type, enum,
    default, help), which the console reads and this module does not interpret.
    """

    model_config = ConfigDict(extra="allow")

    version: int
    groups: list[dict[str, Any]]
    fields: list[dict[str, Any]]


class AdminEnvironmentModels(BaseModel):
    """Model ids an environment's provider can offer.

    Empty when the provider publishes no authoritative catalog; the console
    then accepts a free-text model id.
    """

    model_config = ConfigDict(extra="allow")

    models: list[str]


class AdminOverviewProcessHealth(BaseModel):
    """The overview's four-number digest of :class:`AdminProcessHealth`."""

    model_config = ConfigDict(extra="allow")

    severity: str | None
    thread_count: int | None
    fd_count: int | None
    pending_task_count: int | None


class AdminSystemOverview(BaseModel):
    """Process facts plus the session distribution, counted at the source.

    ``session_state_counts`` and ``total_sessions`` are scoped to the sessions
    the caller administers, so they move with who is asking.
    """

    model_config = ConfigDict(extra="allow")

    machine_id: str
    server_env: str
    persistence_backend: str
    active_runtimes: int
    mcp_proxy_base_url: str
    runtime_session_ids: list[str]
    process_health: AdminOverviewProcessHealth
    session_state_counts: dict[str, int]
    total_sessions: int


class AdminSessionPagination(BaseModel):
    """Paging counters for a session listing, counted at the source."""

    model_config = ConfigDict(extra="allow")

    page: int
    page_size: int
    total_items: int
    total_pages: int


class AdminSessionPage(BaseModel):
    """One page of the sessions the caller administers."""

    model_config = ConfigDict(extra="allow")

    items: list[dict[str, Any]]
    pagination: AdminSessionPagination


class AdminNavigationSummary(BaseModel):
    """Collection totals displayed in the management rail."""

    model_config = ConfigDict(extra="allow")

    agents: int
    environments: int
    sessions: int


class AdminErrorsPage(BaseModel):
    """Recent failures, session rows and captured runtime errors together.

    ``counts`` tallies the returned page by source and severity; ``limit`` is
    the clamped limit that produced it, not the one that was asked for.
    """

    model_config = ConfigDict(extra="allow")

    errors: list[dict[str, Any]]
    counts: dict[str, int]
    limit: int


class AdminThread(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    daemon: bool
    alive: bool
    ident: int | None
    native_id: int | None


class AdminThreads(BaseModel):
    """Thread totals, and the first 200 threads themselves."""

    model_config = ConfigDict(extra="allow")

    count: int
    non_daemon_count: int
    items: list[AdminThread]


class AdminFileDescriptors(BaseModel):
    """Open descriptors against this process's own limit.

    Every field is null on a platform that does not publish it; a null is
    "unknown", which a caller must not render as 0.
    """

    model_config = ConfigDict(extra="allow")

    count: int | None
    soft_limit: int | None
    hard_limit: int | None
    usage_ratio: float | None


class AdminPendingTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    coro: str


class AdminAsyncioTasks(BaseModel):
    """Task totals, and the first 100 pending tasks themselves."""

    model_config = ConfigDict(extra="allow")

    task_count: int
    pending_task_count: int
    sample_pending_tasks: list[AdminPendingTask]


class AdminMemory(BaseModel):
    """Resident memory, null where the platform does not report it."""

    model_config = ConfigDict(extra="allow")

    rss_mb: float | None
    max_rss_mb: float | None


class AdminRuntimeRow(BaseModel):
    """One session runtime this process is holding open.

    The three flags are null when the runtime does not carry that object at
    all, which is a different answer from False.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    sandbox_id: str | None
    current_task_done: bool | None
    lock_locked: bool | None
    owner_loop_running: bool | None


class AdminRuntimes(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int
    current_task_count: int
    locked_count: int
    items: list[AdminRuntimeRow]


class AdminProcessHealth(BaseModel):
    """A live reading of the process answering the request.

    Nothing here is stored: every field is measured when the request arrives,
    so two calls a second apart legitimately disagree. ``severity`` is the
    server's own verdict over these numbers — a caller renders it rather than
    recomputing it, so one deployment's thresholds stay in one place.
    """

    model_config = ConfigDict(extra="allow")

    severity: str
    machine_id: str
    pid: int
    captured_at: str
    threads: AdminThreads
    file_descriptors: AdminFileDescriptors
    asyncio: AdminAsyncioTasks
    memory: AdminMemory
    load_avg: list[float]
    runtimes: AdminRuntimes


class AdminSessionTrace(BaseModel):
    """One turn of one session, message summaries beside AI-SDK frames.

    ``message_limit`` and ``frame_limit`` are the clamped limits that produced
    this answer. ``truncated_frame_count`` is how many frames the limit left
    out, which is what tells a reader the turn is longer than what is shown.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    state: str
    current_turn_id: str | None
    selected_turn_id: str | None
    message_limit: int
    frame_limit: int
    messages: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    frames: list[dict[str, Any]]
    truncated_frame_count: int


class AdminKillResult(BaseModel):
    """What an admin kill did to one session's sandbox.

    ``killed`` is True only for a confirmed destruction — something other than
    the delete call itself reported the box gone. ``destruction`` carries the
    verdict's own name and ``destruction_detail`` the reason, so an unconfirmed
    kill is legible as unconfirmed rather than as a failure or a success.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    sandbox_id: str | None
    killed: bool
    destruction: str
    destruction_detail: str


class AdminEvictResult(BaseModel):
    """The session whose cached runtime was dropped from this process."""

    model_config = ConfigDict(extra="allow")

    evicted: str


class AdminLogsPage(BaseModel):
    """A tail of one log file on the host answering the request.

    ``available_files`` is what this host actually has, which is empty when the
    deployment logs to stderr; ``log_path`` and ``current_file`` name the file
    the tail came from, and ``count`` is the number of lines after filtering.
    """

    model_config = ConfigDict(extra="allow")

    machine_id: str
    log_path: str | None
    current_file: str
    available_files: list[str]
    lines: list[str]
    count: int


# ── Admin endpoints ──────────────────────────────────────────────────


@router.get(
    "/api/v1/admin/agent-schema",
    response_model=ApiEnvelope[AdminFormSchema],
    response_model_exclude_unset=True,
)
async def get_agent_schema_endpoint(request: Request):
    """Return the authoritative editable schema for the Agent form."""
    await _resolve_user(request)
    return success_response(get_agent_schema())


@router.get(
    "/api/v1/admin/environment-schema",
    response_model=ApiEnvelope[AdminFormSchema],
    response_model_exclude_unset=True,
)
async def get_environment_schema_endpoint(request: Request):
    """Return the authoritative editable schema for the environment form."""
    await _resolve_user(request)
    return success_response(get_environment_schema())


@router.get(
    "/api/v1/admin/environments",
    # An environment preset is a stored document whose fields are the
    # environment schema's, not this route's; the schema itself is served by
    # `/api/v1/admin/environment-schema`.
    response_model=ApiEnvelope[list[dict[str, Any]]],
    response_model_exclude_unset=True,
)
async def list_admin_environments(request: Request):
    """List all environment presets (including disabled entries)."""
    user = await _resolve_user(request)
    result = await _svc().list_environment_configs(user)
    return success_response(result)


@router.put(
    "/api/v1/admin/environments/{name}",
    response_model=ApiEnvelope[dict[str, Any]],
    response_model_exclude_unset=True,
)
async def upsert_admin_environment(
    name: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """Upsert a single environment preset by name."""
    user = await _resolve_user(request)
    result = await _svc().upsert_environment_config(user, name, payload)
    return success_response(result)


@router.get(
    "/api/v1/admin/environments/{name}/models",
    response_model=ApiEnvelope[AdminEnvironmentModels],
    response_model_exclude_unset=True,
)
async def list_environment_models(name: str, request: Request):
    """Model ids the console can offer for agents on this environment.

    Enumerated by the environment's selected model provider. A provider with
    no authoritative catalog returns an empty list and the console accepts a
    free-text model id.
    """
    await _resolve_user(request)
    models = await _svc().list_environment_models(name)
    return success_response({"models": models})


# ── Admin dashboard endpoints ───────────────────────────────────────


@router.get(
    "/api/v1/admin/navigation-summary",
    response_model=ApiEnvelope[AdminNavigationSummary],
    response_model_exclude_unset=True,
)
async def admin_navigation_summary(request: Request):
    user = await _resolve_user(request)
    return success_response(await _svc().admin_navigation_summary(user))


@router.get("/api/v1/admin/integrations")
async def admin_integrations(request: Request):
    """List management consoles enabled for this deployment."""
    await _resolve_user(request)
    return success_response({"services": configured_management_links()})


@router.get(
    "/api/v1/admin/system/overview",
    response_model=ApiEnvelope[AdminSystemOverview],
    response_model_exclude_unset=True,
)
async def admin_system_overview(request: Request):
    # Process facts, plus the session distribution counted at the source.
    # Tallying the distribution from a fetched page instead would report a
    # deployment past the page size as `total_sessions: 500` forever, with the
    # newest five hundred conversations standing in for all of them.
    #
    # A comment rather than a docstring: FastAPI publishes a handler's docstring
    # as the endpoint's OpenAPI `description`, and this counting constraint is
    # an implementation fact, not part of the contract.
    user = await _resolve_user(request)
    overview = _svc().admin_system_overview()
    totals = await _svc().admin_session_totals(user)
    overview["session_state_counts"] = totals["by_state"]
    overview["total_sessions"] = totals["total"]
    return success_response(overview)


async def _global_sessions_payload(request: Request) -> list[dict[str, Any]]:
    """The admin-api's unscoped session dump — no user, no owner filtering.

    Kept flat and capped rather than paged: this is the ops/automation surface,
    whose caller is a script holding a bearer token, not the console. The
    console's own listing is paged and owner-scoped and is a different route
    (`/api/v1/admin/sessions/all`); the two are deliberately not the same shape.
    """
    limit = int(request.query_params.get("limit", "200") or "200")
    return await _svc().admin_list_global_sessions(limit=min(limit, 500))


def _session_filters(request: Request) -> dict[str, Any]:
    """The narrowing every session surface shares: agent, and a start-time window.

    One reader, so the list and the export of that list cannot drift into
    describing different sets.
    """
    q = request.query_params
    return {
        "agent_id": str(q.get("agent_id", "") or "").strip() or None,
        "since": str(q.get("since", "") or "").strip() or None,
        "until": str(q.get("until", "") or "").strip() or None,
    }


@router.get(
    "/api/v1/admin/sessions/all",
    response_model=ApiEnvelope[AdminSessionPage],
    response_model_exclude_unset=True,
)
async def admin_list_all_sessions(request: Request):
    # One page of the caller's sessions, narrowed in the query — paged and
    # filtered at the source rather than in the client. A deployment with a
    # hundred agents and a thousand conversations a day each has six figures of
    # rows: fetching a fixed page and filtering it to the caller's agents
    # afterwards would return a sliver of one page, with nothing to say how
    # much had been dropped.
    #
    # Comment, not docstring, for the reason given on the overview above.
    user = await _resolve_user(request)
    try:
        page = int(request.query_params.get("page", "1") or "1")
        page_size = int(request.query_params.get("page_size", "50") or "50")
    except ValueError as exc:
        raise APIError(
            code="INVALID_REQUEST",
            message="page and page_size must be integers",
            status_code=400,
        ) from exc
    result = await _svc().admin_list_sessions_page(
        user, page=page, page_size=page_size, **_session_filters(request)
    )
    return success_response(result)


async def _errors_payload(limit_param: str) -> dict[str, Any]:
    """The ``errors`` payload: parse+clamp ``limit_param``, call ``admin_list_errors``."""
    limit = int(limit_param or "200")
    return await _svc().admin_list_errors(limit=min(limit, 500))


@router.get(
    "/api/v1/admin/errors",
    response_model=ApiEnvelope[AdminErrorsPage],
    response_model_exclude_unset=True,
)
async def admin_list_errors(request: Request):
    _ = await _resolve_user(request)
    errors = await _errors_payload(request.query_params.get("limit", "200"))
    return success_response(errors)


def _process_health_payload() -> dict[str, Any]:
    return _svc().admin_process_health()


@router.get(
    "/api/v1/admin/process/health",
    response_model=ApiEnvelope[AdminProcessHealth],
    response_model_exclude_unset=True,
)
async def admin_process_health(request: Request):
    _ = await _resolve_user(request)
    return success_response(_process_health_payload())


@router.get(
    "/api/v1/admin/sessions/{session_id}/detail",
    # The sanitized session row, whose fields belong to the sessions
    # repository; the two states it carries are documented on
    # ``AdminService.admin_get_session_detail``.
    response_model=ApiEnvelope[dict[str, Any]],
    response_model_exclude_unset=True,
)
async def admin_session_detail(session_id: str, request: Request):
    user = await _resolve_user(request)
    detail = await _svc().admin_get_session_detail(user, session_id)
    return success_response(detail)


@router.get(
    "/api/v1/admin/sessions/{session_id}/trace",
    response_model=ApiEnvelope[AdminSessionTrace],
    response_model_exclude_unset=True,
)
async def admin_session_trace(session_id: str, request: Request):
    user = await _resolve_user(request)
    turn_id = str(request.query_params.get("turn_id", "") or "").strip() or None
    try:
        message_limit = int(request.query_params.get("message_limit", "100") or "100")
        frame_limit = int(request.query_params.get("frame_limit", "500") or "500")
    except ValueError as exc:
        raise APIError(
            code="INVALID_REQUEST",
            message="message_limit and frame_limit must be integers",
            status_code=400,
        ) from exc
    trace = await _svc().admin_get_session_trace(
        user,
        session_id,
        turn_id=turn_id,
        message_limit=message_limit,
        frame_limit=frame_limit,
    )
    return success_response(trace)


class _TarSink:
    """A file object for ``tarfile`` that hands each write straight to a generator.

    ``tarfile.open(mode="w|gz")`` is the streaming mode: it writes members
    sequentially and never seeks, so with a sink that buffers only what has not
    been yielded yet, the archive is produced without ever existing whole.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


@router.get("/api/v1/admin/sessions/transcripts")
async def admin_batch_session_transcripts(request: Request):
    """Stream every transcript the caller may manage as one gzip tar archive.

    Each session uses the SDK's directory layout, so untarring under
    ``~/.claude/projects/`` makes its transcript resumable. The service applies
    owner scope and the requested agent/date filters in the session query.

    The route writes and releases one session's files at a time instead of
    assembling the complete archive in memory. Sessions without mirrored
    transcript entries are omitted.
    """
    user = await _resolve_user(request)
    filters = _session_filters(request)

    async def _archive() -> AsyncIterator[bytes]:
        sink = _TarSink()
        with tarfile.open(fileobj=sink, mode="w|gz") as archive:  # type: ignore[call-overload]
            async for group in _svc().admin_iter_batch_transcript_files(user, **filters):
                for entry_file in group["files"]:
                    info = tarfile.TarInfo(name=entry_file["path"])
                    info.size = len(entry_file["jsonl"])
                    archive.addfile(info, io.BytesIO(entry_file["jsonl"]))
                chunk = sink.drain()
                if chunk:
                    yield chunk
        tail = sink.drain()
        if tail:
            yield tail

    stamp = utcnow_iso()[:10]
    agent_id = filters["agent_id"]
    name = f"transcripts-{agent_id}-{stamp}" if agent_id else f"transcripts-{stamp}"
    return StreamingResponse(
        _archive(),
        media_type="application/gzip",
        headers=build_attachment_headers(f"{name}.tar.gz"),
    )


@router.get("/api/v1/admin/sessions/{session_id}/transcript")
async def admin_session_transcript(session_id: str, request: Request):
    """The session's transcript as the Claude Agent SDK stores it.

    A single ``<sdk-session-id>.jsonl`` when the session has only its main
    conversation: dropped into any directory under ``~/.claude/projects/``,
    ``claude --resume <sdk-session-id>`` continues it. A ``.tar.gz`` preserving
    the SDK's directory layout when subagent transcripts exist, because those
    are separate files and shipping only the main one would silently drop them.

    The media type therefore follows the artifact rather than being fixed: the
    two cases are different files, and answering `.jsonl` for a session with
    subagents would either misname the archive or drop the subagent
    transcripts.
    """
    user = await _resolve_user(request)
    files = await _svc().admin_session_transcript_files(user, session_id)
    if not files:
        raise APIError(
            code="NO_TRANSCRIPT",
            message="this session has no mirrored transcript",
            status_code=404,
        )

    main = next((f for f in files if f["subpath"] is None), files[0])
    if len(files) == 1:
        return Response(
            content=files[0]["jsonl"],
            # The SDK writes JSONL; ndjson is that format's registered type.
            media_type="application/x-ndjson",
            headers=build_attachment_headers(f"{main['sdk_session_id']}.jsonl"),
        )

    buffer = io.BytesIO()
    # Streaming tar mode: members are written and compressed as they are added,
    # so the archive is never assembled twice in memory.
    with tarfile.open(fileobj=buffer, mode="w|gz") as archive:
        for entry_file in files:
            info = tarfile.TarInfo(name=entry_file["path"])
            info.size = len(entry_file["jsonl"])
            archive.addfile(info, io.BytesIO(entry_file["jsonl"]))
    return Response(
        content=buffer.getvalue(),
        media_type="application/gzip",
        headers=build_attachment_headers(f"{main['sdk_session_id']}.tar.gz"),
    )


@router.post(
    "/api/v1/admin/sessions/{session_id}/kill",
    response_model=ApiEnvelope[AdminKillResult],
    response_model_exclude_unset=True,
)
async def admin_kill_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    logger.warning(
        "admin kill session: session=%s operator=%s",
        session_id,
        user.user_id,
    )
    result = await _svc().admin_kill_session(user, session_id)
    return success_response(result)


@router.post(
    "/api/v1/admin/sessions/{session_id}/evict-runtime",
    response_model=ApiEnvelope[AdminEvictResult],
    response_model_exclude_unset=True,
)
async def admin_evict_runtime(session_id: str, request: Request):
    """Evict cached runtime from memory (simulates request hitting a different machine)."""
    user = await _resolve_user(request)
    logger.info("admin evict runtime: session=%s operator=%s", session_id, user.user_id)
    await _svc()._runtime_manager.evict_runtime(session_id)
    return success_response({"evicted": session_id})


@router.get(
    "/api/v1/admin/logs",
    response_model=ApiEnvelope[AdminLogsPage],
    response_model_exclude_unset=True,
)
async def admin_logs(request: Request):
    import socket
    import glob as _glob
    _ = await _resolve_user(request)
    params = request.query_params
    lines = min(int(params.get("lines", "200") or "200"), 2000)
    level_filter = str(params.get("level", "") or "").strip().upper()
    keyword = str(params.get("keyword", "") or "").strip()
    requested_file = str(params.get("file", "") or "").strip()

    log_dirs: list[str] = []
    env_log_path = os.getenv("ASTRABOX_LOGGING_PATH", "")
    if env_log_path:
        log_dirs.append(env_log_path)
    log_dirs.append(os.path.join(os.getcwd(), "logs"))
    log_dirs.extend(["/home/log/default", "/home/log/error", "/home/log"])

    available: dict[str, str] = {}  # label -> path
    for d in log_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(_glob.glob(os.path.join(d, "**", "*.log"), recursive=True)):
            if os.path.isfile(f):
                label = os.path.relpath(f, d) if d != "/home/log" else f
                if label not in available:
                    available[label] = f

    used_path: str | None = None
    if requested_file and requested_file in available:
        used_path = available[requested_file]
    else:
        # Default: prefer module business log, then nohup, then framework default
        for prefer in ["astrabox/default.log", "nohup.log", "default.log"]:
            if prefer in available:
                used_path = available[prefer]
                break
        if used_path is None and available:
            used_path = next(iter(available.values()))

    result_lines: list[str] = []
    if used_path:
        try:
            with open(used_path, "r", errors="replace") as fh:
                all_lines = fh.readlines()
            tail = all_lines[-lines * 3:] if len(all_lines) > lines * 3 else all_lines
            for line in tail:
                if level_filter and level_filter not in line.upper():
                    continue
                if keyword and keyword.lower() not in line.lower():
                    continue
                result_lines.append(line.rstrip("\n"))
            result_lines = result_lines[-lines:]
        except Exception:
            pass

    used_label = ""
    for label, path in available.items():
        if path == used_path:
            used_label = label
            break

    return success_response({
        "machine_id": socket.gethostname(),
        "log_path": used_path,
        "current_file": used_label,
        "available_files": sorted(available.keys()),
        "lines": result_lines,
        "count": len(result_lines),
    })
