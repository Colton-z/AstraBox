"""The turn / streaming resource family — ``ai-stream``, ``interrupt``,
``interaction-respond``, ``conversation/end``, and ``terminal/stream``.

The queue/sentinel/producer locals in each handler are per-request closures,
not module state. Route-registration order here is not pinned by the
wire-contract tests (``tests/http_wire_contract_test.py``): they sort the
route table and serialize the OpenAPI schema with ``sort_keys=True`` before
comparing.

The admin schema/dashboard/ops routes live in :mod:`astrabox.api.routes.admin`,
and the sandbox-originated platform MCP surface lives in
:mod:`astrabox.api.routes.platform_mcp`.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.api.sse import (
    format_sse_done,
    format_sse_event,
    format_sse_keepalive,
)
from astrabox.api.routes._shared import (
    _resolve_user,
    _stream_first_event_timeout_seconds,
    _svc,
)
from astrabox.core.service.orchestrator.engine.input_content import (
    read_turn_input_content,
    turn_input_is_empty,
)
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.api.routes.stream_start import spawn_drain_cancelled_stream_start

logger = get_logger(__name__)

router = APIRouter()

_UNEXPECTED_AI_STREAM_MESSAGE = "The session stream stopped unexpectedly."
_UNEXPECTED_TERMINAL_STREAM_MESSAGE = "The terminal stream stopped unexpectedly."


def _public_stream_error_text(exc: BaseException, *, unexpected: str) -> str:
    """Return only text explicitly safe for an unaudited streaming response."""

    if isinstance(exc, APIError):
        return exc.user_message
    return unexpected


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.
#
# Only the JSON routes below carry one. The four streaming routes in this
# module answer ``text/event-stream`` from a ``StreamingResponse`` — there is no
# JSON body for a model to describe, and ``GET .../ai-stream`` additionally
# answers a bare 204 when there is nothing to resume. Declaring a 200 model on
# any of them would put a body into the generated client that the route never
# sends.


class TurnReceipt(BaseModel):
    """Acknowledgement that a turn was accepted. It carries no turn content.

    Sending and receiving are separate channels: these identifiers are what
    lets a caller recognise the turn's frames when they arrive on the session's
    stream. ``client_message_id`` echoes the caller's own key and is ``null``
    when the request did not supply one.
    """

    model_config = ConfigDict(extra="allow")

    turn_id: str | None = None
    command_id: str | None = None
    client_message_id: str | None = None
    accepted: bool | None = None


class InteractionAnswerReceipt(BaseModel):
    """Acknowledgement that an answer was matched to the pending interaction.

    The turn continues on the session's stream; nothing of its continuation is
    delivered on this response.
    """

    model_config = ConfigDict(extra="allow")

    interaction_id: str | None = None
    answered: bool | None = None
    turn_id: str | None = None


class TurnControlResult(BaseModel):
    """The outcome of an interrupt.

    ``status`` distinguishes the two things an interrupt can reach: a running
    terminal command (``completed``) or the conversation's turn (``accepted``,
    which the engine settles asynchronously).
    """

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    status: str | None = None


class ConversationEndResult(BaseModel):
    """The outcome of ending an Assistant conversation.

    Supported for Assistant conversations only; an Agent conversation is
    refused. The conversation's runtime is disposed, and its history survives.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    status: str | None = None
    sandbox_id: str | None = None
    killed: bool | None = None


@router.post(
    "/api/v1/sessions/{session_id}/turn-inputs",
    response_model=ApiEnvelope[TurnReceipt],
    response_model_exclude_unset=True,
)
async def append_turn_input(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """Accept a turn. The receipt comes back here; the content does not.

    Input and output are separate channels: this admits the message and
    answers with the identifiers that let a caller recognise the turn on the
    session's stream. Nothing is delivered on this response, so its lifetime
    is not tied to how long the engine takes or where it pauses.
    """
    user = await _resolve_user(request)
    await _svc().must_own_session(user, session_id)
    content, content_blocks = read_turn_input_content(payload.get("content") or "")
    if turn_input_is_empty(content, content_blocks):
        raise APIError(
            code="INVALID_REQUEST",
            message="content is required",
            status_code=400,
        )
    client_message_id = payload.get("client_message_id")
    if client_message_id is not None and not isinstance(client_message_id, str):
        raise APIError(
            code="INVALID_REQUEST",
            message="client_message_id must be a string",
            status_code=400,
        )
    permission_mode = payload.get("permission_mode")
    if permission_mode is not None and not isinstance(permission_mode, str):
        raise APIError(
            code="INVALID_REQUEST",
            message="permission_mode must be a string",
            status_code=400,
        )
    receipt = await _svc().dispatch_turn_input(
        user,
        session_id,
        content,
        content_blocks=content_blocks,
        permission_mode=permission_mode,
        client_message_id=str(client_message_id or "").strip() or None,
    )
    return success_response(receipt)


@router.post(
    "/api/v1/sessions/{session_id}/interaction-respond",
    response_model=ApiEnvelope[InteractionAnswerReceipt],
    response_model_exclude_unset=True,
)
async def interaction_respond(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    user = await _resolve_user(request)
    await _svc().must_own_session(user, session_id)
    interaction_id = str(payload.get("interaction_id") or "").strip()
    if not interaction_id:
        raise APIError(
            code="INVALID_REQUEST",
            message="interaction_id is required",
            status_code=400,
        )
    answer = payload.get("answer")
    if not isinstance(answer, dict):
        raise APIError(
            code="INVALID_REQUEST",
            message="answer must be an object",
            status_code=400,
        )
    result = await _svc().answer_pending_interaction(
        user,
        session_id,
        interaction_id,
        answer,
    )
    return success_response(result)


# ── Data Stream Protocol endpoints (AI SDK v2) ──────────────────────


@router.post("/api/v1/sessions/{session_id}/ai-stream")
async def send_message_ai_stream(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """Data Stream Protocol endpoint for Vercel AI SDK useChat."""
    service = _svc()
    user = await _resolve_user(request)

    # Authorize before reading any session data.
    await service.must_own_session(user, session_id)

    content = str(payload.get("content") or "")
    interaction_response = payload.get("interaction_response")
    permission_mode = payload.get("permission_mode")
    client_message_id = payload.get("client_message_id")

    if interaction_response is not None and not isinstance(interaction_response, dict):
        raise APIError(
            code="INVALID_REQUEST",
            message="interaction_response must be an object",
            status_code=400,
        )
    if client_message_id is not None and not isinstance(client_message_id, str):
        raise APIError(
            code="INVALID_REQUEST", message="client_message_id must be a string", status_code=400
        )

    # Auto-fill interaction_id from the session's pending interaction.
    if isinstance(interaction_response, dict) and not interaction_response.get("interaction_id"):
        try:
            session_detail = await service.get_session(user, session_id)
            pi = (session_detail or {}).get("pending_interaction")
            if isinstance(pi, dict) and pi.get("interaction_id"):
                interaction_response["interaction_id"] = pi["interaction_id"]
        except Exception:
            logger.debug("auto-fill interaction_id failed session=%s", session_id, exc_info=True)

    agen = service.stream_message_events_ds(
        user,
        session_id,
        content,
        interaction_response=interaction_response,
        permission_mode=permission_mode,
        client_message_id=str(client_message_id or "").strip() or None,
    )

    first: dict[str, Any] | None = None
    first_event_task = asyncio.create_task(anext(agen))
    try:
        first = await asyncio.wait_for(
            asyncio.shield(first_event_task),
            timeout=_stream_first_event_timeout_seconds(),
        )
    except asyncio.CancelledError:
        # The client disconnected before the turn's first event arrived.
        # first_event_task (shielded above) is not cancelled by this, and by
        # this point the command may already be durably dispatched with its
        # worker spawned -- both decoupled from this request's lifetime.
        # Cancelling first_event_task and closing agen here would abandon
        # that worker with nobody draining its output, wedging the session
        # in PROCESSING until reconciliation's staleness timeout reclaims
        # it, so draining of the same generator continues in the
        # background until the turn finishes.
        try:
            spawn_drain_cancelled_stream_start(
                session_id,
                agen,
                first_event_task,
                spawn_background_task=service._spawn_background_task,
            )
        except Exception:
            # Shutdown may have quiesced the platform before this cancelled
            # request can hand off its shielded first-event task. If ownership
            # is refused, the request must finish the structured cleanup itself.
            if not first_event_task.done():
                first_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_event_task
            with contextlib.suppress(Exception):
                await agen.aclose()
            raise
        return JSONResponse(
            content=error_response("REQUEST_CANCELLED", "request cancelled"),
            status_code=499,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "ai stream first event wait timed out; keep stream open session=%s timeout_s=%.1f",
            session_id,
            _stream_first_event_timeout_seconds(),
        )
    except StopAsyncIteration:
        with contextlib.suppress(Exception):
            await agen.aclose()
        return JSONResponse(
            content=error_response("AGENT_RUNTIME_ERROR", "empty ai stream"),
            status_code=500,
        )
    except APIError as exc:
        with contextlib.suppress(Exception):
            await agen.aclose()
        return JSONResponse(
            content=error_response(exc),
            status_code=exc.status_code,
        )
    except Exception:
        if not first_event_task.done():
            first_event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await first_event_task
        with contextlib.suppress(Exception):
            await agen.aclose()
        raise

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    client_disconnected = False

    async def _producer():
        try:
            if first is not None:
                if not client_disconnected:
                    await queue.put(first)
            else:
                with contextlib.suppress(StopAsyncIteration):
                    first_late = await first_event_task
                    if not client_disconnected:
                        await queue.put(first_late)
            async for event in agen:
                if client_disconnected:
                    continue
                await queue.put(event)
        except APIError as exc:
            if not client_disconnected:
                await queue.put(
                    {
                        "type": "error",
                        "errorText": _public_stream_error_text(
                            exc,
                            unexpected=_UNEXPECTED_AI_STREAM_MESSAGE,
                        ),
                    }
                )
        except Exception as exc:
            logger.exception("ai-stream producer failed session=%s", session_id)
            if not client_disconnected:
                await queue.put(
                    {
                        "type": "error",
                        "errorText": _public_stream_error_text(
                            exc,
                            unexpected=_UNEXPECTED_AI_STREAM_MESSAGE,
                        ),
                    }
                )
        finally:
            if not client_disconnected:
                await queue.put(sentinel)
            if not first_event_task.done():
                first_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_event_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agen.aclose()

    producer_task = asyncio.create_task(_producer())

    async def _generate():
        nonlocal client_disconnected
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield format_sse_keepalive()
                    continue
                if item is sentinel:
                    break
                yield format_sse_event(item)
            yield format_sse_done()
        except asyncio.CancelledError:
            client_disconnected = True
        finally:
            producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer_task

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


@router.get("/api/v1/sessions/{session_id}/ai-stream")
async def resume_ai_stream(session_id: str, request: Request):
    """Resume endpoint for AI SDK useChat({ resume: true }).

    Durable frames are stored in AI SDK stream format by the session
    kernel. This handler replays them directly as SSE.
    """
    user = await _resolve_user(request)
    raw_after_seq = str(request.query_params.get("after_seq", "-1") or "-1").strip() or "-1"
    try:
        after_seq = int(raw_after_seq)
    except ValueError as exc:
        raise APIError(
            code="INVALID_REQUEST",
            message="after_seq must be an integer",
            status_code=400,
        ) from exc
    raw_follow = str(request.query_params.get("follow", "") or "").strip().lower()
    if raw_follow not in {"", "session"}:
        raise APIError(
            code="INVALID_REQUEST",
            message="follow must be 'session' when present",
            status_code=400,
        )
    if raw_follow == "session":
        # The session output subscription may start idle, so it never returns
        # 204. Each response closes at one terminal turn and the client reopens
        # from the emitted durable cursor.
        agen: Any = _svc().follow_session_stream(user, session_id, after_seq=after_seq)
    else:
        agen = await _svc().stream_message_events_ds_resume(
            user,
            session_id,
            after_seq=after_seq,
        )
        if agen is None:
            return Response(status_code=204)
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    client_disconnected = False

    async def _producer():
        try:
            async for item in agen:
                if client_disconnected:
                    continue
                await queue.put(item)
        except Exception as exc:
            logger.warning("resume ai-stream producer failed session=%s: %s", session_id, exc)
            if not client_disconnected:
                await queue.put(
                    {
                        "type": "error",
                        "errorText": _public_stream_error_text(
                            exc,
                            unexpected=_UNEXPECTED_AI_STREAM_MESSAGE,
                        ),
                    }
                )
        finally:
            if not client_disconnected:
                await queue.put(sentinel)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agen.aclose()

    producer_task = asyncio.create_task(_producer())

    async def _generate():
        nonlocal client_disconnected
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield format_sse_keepalive()
                    continue
                if item is sentinel:
                    break
                yield format_sse_event(item)
            yield format_sse_done()
        except asyncio.CancelledError:
            client_disconnected = True
        finally:
            producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer_task

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


@router.post(
    "/api/v1/sessions/{session_id}/interrupt",
    response_model=ApiEnvelope[TurnControlResult],
    response_model_exclude_unset=True,
)
async def interrupt_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().interrupt(user, session_id)
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/conversation/end",
    response_model=ApiEnvelope[ConversationEndResult],
    response_model_exclude_unset=True,
)
async def end_conversation(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().end_conversation(user, session_id)
    return success_response(result)


# ── Terminal endpoint ────────────────────────────────────────────────


@router.post("/api/v1/sessions/{session_id}/terminal/stream")
async def terminal_stream(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """Run a single terminal command and stream output as SSE events.

    WebSocket is intentionally avoided; Terminal uses HTTP SSE.
    """
    service = _svc()
    user = await _resolve_user(request)
    command = str(payload.get("command") or "").strip()
    cwd = payload.get("working_directory") or payload.get("cwd")
    if not command:
        raise APIError(code="INVALID_REQUEST", message="command is empty", status_code=400)

    agen = service.run_terminal_command(user, session_id, command, cwd)

    first: dict[str, Any] | None = None
    first_event_task = asyncio.create_task(anext(agen))
    try:
        first = await asyncio.wait_for(
            asyncio.shield(first_event_task),
            timeout=_stream_first_event_timeout_seconds(),
        )
    except asyncio.CancelledError:
        if not first_event_task.done():
            first_event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await first_event_task
        with contextlib.suppress(Exception):
            await agen.aclose()
        return JSONResponse(
            content=error_response("REQUEST_CANCELLED", "request cancelled"),
            status_code=499,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "terminal stream first event wait timed out; keep stream open session=%s timeout_s=%.1f",
            session_id,
            _stream_first_event_timeout_seconds(),
        )
    except StopAsyncIteration:
        with contextlib.suppress(Exception):
            await agen.aclose()
        return JSONResponse(
            content=error_response("AGENT_RUNTIME_ERROR", "empty terminal stream"),
            status_code=500,
        )
    except APIError as exc:
        with contextlib.suppress(Exception):
            await agen.aclose()
        return JSONResponse(
            content=error_response(exc),
            status_code=exc.status_code,
        )
    except Exception:  # pragma: no cover
        if not first_event_task.done():
            first_event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await first_event_task
        with contextlib.suppress(Exception):
            await agen.aclose()
        raise

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def _producer() -> None:
        try:
            if first is not None:
                await queue.put(first)
            else:
                with contextlib.suppress(StopAsyncIteration):
                    first_late = await first_event_task
                    await queue.put(first_late)
            async for event in agen:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except APIError as exc:
            logger.warning(
                "terminal stream producer api error session=%s code=%s msg=%s",
                session_id,
                exc.code,
                exc.message,
            )
            public_error = _public_stream_error_text(
                exc,
                unexpected=_UNEXPECTED_TERMINAL_STREAM_MESSAGE,
            )
            await queue.put({"type": "stderr", "text": f"{exc.code}: {public_error}\n"})
            await queue.put({"type": "exit", "exit_code": 1})
        except Exception as exc:  # pragma: no cover
            logger.exception("terminal stream producer failed")
            public_error = _public_stream_error_text(
                exc,
                unexpected=_UNEXPECTED_TERMINAL_STREAM_MESSAGE,
            )
            await queue.put({"type": "stderr", "text": f"{public_error}\n"})
            await queue.put({"type": "exit", "exit_code": 1})
        finally:
            await queue.put(sentinel)
            if not first_event_task.done():
                first_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_event_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agen.aclose()

    producer_task = asyncio.create_task(_producer())

    async def _generate():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                if item is sentinel:
                    break

                yield f"data: {json.dumps(item, default=str)}\n\n"
        except asyncio.CancelledError:
            return
        finally:
            producer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer_task

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
