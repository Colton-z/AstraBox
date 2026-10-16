"""A conversation's runtime as the console and API clients use it.

Sending, answering and interrupting record the request and return a receipt;
the session's reader hands it to the runtime. ``GET …/stream`` is the only
way output reaches a client: what it missed after ``after_seq``, then live,
one UI message per response. ``conversation/end`` and the session's web
terminal (``terminal/stream``, ``terminal/interrupt``) are also mounted here.

Mounted by :func:`astrabox.api.app.create_app` via ``include_router``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.api.sse import (
    format_sse_done,
    format_sse_event,
    format_sse_keepalive,
)
from astrabox.api.routes._shared import (
    _resolve_user,
    _svc,
)
from astrabox.core.service.orchestrator.engine.input_content import (
    read_turn_input_content,
    turn_input_is_empty,
)
from astrabox.api.routes.response_envelope import ApiEnvelope

logger = get_logger(__name__)

router = APIRouter()

_UNEXPECTED_STREAM_MESSAGE = "The session stream stopped unexpectedly."
_UNEXPECTED_TERMINAL_STREAM_MESSAGE = "The terminal stream stopped unexpectedly."
#: Seconds of silence before the stream writes a keep-alive comment.
_KEEPALIVE_S = 15.0


def _public_stream_error_text(exc: BaseException, *, unexpected: str) -> str:
    """Return only text explicitly safe for an unaudited streaming response."""

    if isinstance(exc, APIError):
        return exc.user_message
    return unexpected


class InputReceipt(BaseModel):
    """An accepted input. Its response arrives on the session's stream.

    ``duplicate`` is true when the same ``client_message_id`` was accepted
    before; the receipt then describes that earlier input, including its
    recorded refusal in ``failure`` when it failed.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    input_id: str
    client_message_id: str
    accepted_at: str
    state: str
    failure: dict[str, Any] | None = None
    duplicate: bool


class InteractionAnswerReceipt(BaseModel):
    """An answer stored for the runtime; the response continues on the stream."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    interaction_id: str
    answered: bool


class InterruptReceipt(BaseModel):
    """``accepted`` when a reader will pass the interrupt on; ``idle`` when nothing runs."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: str


class TerminalInterruptResult(BaseModel):
    """``completed`` once the terminal's running command has ended; ``idle`` when none ran."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: str


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
    "/api/v1/sessions/{session_id}/inputs",
    response_model=ApiEnvelope[InputReceipt],
)
async def send_input(session_id: str, request: Request, payload: dict[str, Any] = Body(...)):
    """Accept a message for the conversation."""
    # The inbound half of the platform's delay starts here (see the reader's "input delivered").
    logger.info("input received session=%s at=%.3f", session_id, time.time())
    user = await _resolve_user(request)
    content, content_blocks = read_turn_input_content(payload.get("content") or "")
    if turn_input_is_empty(content, content_blocks):
        raise APIError(code="INVALID_REQUEST", message="content is required", status_code=400)
    client_message_id = payload.get("client_message_id")
    if client_message_id is not None and not isinstance(client_message_id, str):
        raise APIError(code="INVALID_REQUEST", message="client_message_id must be a string", status_code=400)
    receipt = await _svc().session_runtime.send(
        user,
        session_id,
        content,
        content_blocks=content_blocks,
        client_message_id=client_message_id,
    )
    return success_response(receipt)


@router.post(
    "/api/v1/sessions/{session_id}/interactions/{interaction_id}/answer",
    response_model=ApiEnvelope[InteractionAnswerReceipt],
)
async def answer_interaction(
    session_id: str, interaction_id: str, request: Request, payload: dict[str, Any] = Body(...)
):
    user = await _resolve_user(request)
    answer = payload.get("answer")
    if not isinstance(answer, dict):
        raise APIError(code="INVALID_REQUEST", message="answer must be an object", status_code=400)
    result = await _svc().session_runtime.answer(user, session_id, interaction_id, answer)
    return success_response(result)


@router.post(
    "/api/v1/sessions/{session_id}/interrupt",
    response_model=ApiEnvelope[InterruptReceipt],
)
async def interrupt_session(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().session_runtime.interrupt(user, session_id)
    return success_response(result)


@router.get("/api/v1/sessions/{session_id}/stream")
async def follow_stream(session_id: str, request: Request):
    """The session's output after ``after_seq``, then live, through the end of one response.

    Each part is an AI SDK UI message chunk; ``data-resume-cursor`` parts carry
    the position to reconnect from.
    """
    user = await _resolve_user(request)
    raw_after_seq = str(request.query_params.get("after_seq", "0") or "0").strip()
    try:
        after_seq = int(raw_after_seq)
    except ValueError as exc:
        raise APIError(code="INVALID_REQUEST", message="after_seq must be an integer", status_code=400) from exc
    if after_seq < 0:
        raise APIError(code="INVALID_REQUEST", message="after_seq must not be negative", status_code=400)
    parts = await _svc().session_runtime.follow(user, session_id, after_seq=after_seq)
    return StreamingResponse(
        _sse(parts, session_id=session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


async def _sse(parts: AsyncIterator[dict[str, Any]], *, session_id: str) -> AsyncIterator[str]:
    """Write parts as server-sent events, with keep-alives while the session is quiet."""
    iterator = parts.__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=_KEEPALIVE_S)
            if not done:
                yield format_sse_keepalive()
                continue
            task, pending = pending, None
            try:
                part = task.result()
            except StopAsyncIteration:
                break
            except APIError as exc:
                yield format_sse_event({"type": "error", "errorText": exc.user_message})
                break
            except Exception:
                logger.exception("session stream failed session=%s", session_id)
                yield format_sse_event({"type": "error", "errorText": _UNEXPECTED_STREAM_MESSAGE})
                break
            yield format_sse_event(part)
        yield format_sse_done()
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        with contextlib.suppress(Exception):
            await parts.aclose()  # type: ignore[attr-defined]


@router.post(
    "/api/v1/sessions/{session_id}/conversation/end",
    response_model=ApiEnvelope[ConversationEndResult],
    response_model_exclude_unset=True,
)
async def end_conversation(session_id: str, request: Request):
    user = await _resolve_user(request)
    result = await _svc().end_conversation(user, session_id)
    return success_response(result)


# ── Terminal endpoints ───────────────────────────────────────────────

_TERMINAL_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/api/v1/sessions/{session_id}/terminal/stream")
async def terminal_stream(
    session_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    """Run one command in the session's terminal and stream its events as SSE.

    The events are ``ack`` (carrying the ``command_id``), ``stdout``,
    ``stderr``, ``cwd`` and a final ``exit``. The command runs on when the
    stream closes; ``GET`` on this path follows it again. WebSocket is
    intentionally avoided; the terminal uses HTTP SSE.
    """
    user = await _resolve_user(request)
    command = str(payload.get("command") or "").strip()
    cwd = payload.get("working_directory") or payload.get("cwd")
    if not command:
        raise APIError(code="INVALID_REQUEST", message="command is empty", status_code=400)
    events = await _svc().terminal.run(user, session_id, command, cwd)
    return StreamingResponse(
        _terminal_sse(events, session_id=session_id),
        media_type="text/event-stream",
        headers=_TERMINAL_STREAM_HEADERS,
    )


@router.get("/api/v1/sessions/{session_id}/terminal/stream")
async def follow_terminal_stream(session_id: str, request: Request):
    """Follow a terminal command again: its recorded events after ``after_seq``, then live.

    The stream ends with the command's ``exit``; each event carries its
    ``seq``. Without ``command_id`` it follows the command the terminal runs
    now and answers ``204`` when none runs.
    """
    user = await _resolve_user(request)
    command_id = str(request.query_params.get("command_id") or "").strip() or None
    raw_after_seq = str(request.query_params.get("after_seq", "-1") or "-1").strip()
    try:
        after_seq = int(raw_after_seq)
    except ValueError as exc:
        raise APIError(code="INVALID_REQUEST", message="after_seq must be an integer", status_code=400) from exc
    events = await _svc().terminal.follow(user, session_id, command_id=command_id, after_seq=after_seq)
    if events is None:
        return Response(status_code=204)
    return StreamingResponse(
        _terminal_sse(events, session_id=session_id),
        media_type="text/event-stream",
        headers=_TERMINAL_STREAM_HEADERS,
    )


@router.post(
    "/api/v1/sessions/{session_id}/terminal/interrupt",
    response_model=ApiEnvelope[TerminalInterruptResult],
)
async def interrupt_terminal(session_id: str, request: Request):
    """Stop the command the session's terminal runs; answers once the command has ended."""
    user = await _resolve_user(request)
    result = await _svc().terminal.interrupt(user, session_id)
    return success_response(result)


async def _terminal_sse(events: AsyncIterator[dict[str, Any]], *, session_id: str) -> AsyncIterator[str]:
    """Write terminal events as server-sent events, with keep-alives while the command is quiet.

    A failure after the stream started becomes a ``stderr`` event and ends the
    stream. It carries no ``exit``: the command's status is the box's to report.
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=_KEEPALIVE_S)
            if not done:
                yield format_sse_keepalive()
                continue
            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                break
            except Exception as exc:
                if not isinstance(exc, APIError):
                    logger.exception("terminal stream failed session=%s", session_id)
                public_error = _public_stream_error_text(exc, unexpected=_UNEXPECTED_TERMINAL_STREAM_MESSAGE)
                yield format_sse_event({"type": "stderr", "text": f"{public_error}\n"})
                break
            yield format_sse_event(event)
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        with contextlib.suppress(Exception):
            await events.aclose()  # type: ignore[attr-defined]
