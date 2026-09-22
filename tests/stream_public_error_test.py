from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import StreamingResponse

from astrabox.api.routes import turns as turns_module
from astrabox.common.utils.errors import APIError


async def _failing_stream(exc: Exception) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "text-delta", "id": "text-1", "delta": "visible"}
    raise exc


async def _response_text(response: StreamingResponse) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


class _PostStreamService:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def must_own_session(self, *_args: object) -> None:
        return None

    def stream_message_events_ds(
        self, *_args: object, **_kwargs: object
    ) -> AsyncIterator[dict[str, Any]]:
        return _failing_stream(self._exc)


class _ResumeStreamService:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def stream_message_events_ds_resume(
        self, *_args: object, **_kwargs: object
    ) -> AsyncIterator[dict[str, Any]]:
        return _failing_stream(self._exc)


class _TerminalStreamService:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def run_terminal_command(
        self, *_args: object, **_kwargs: object
    ) -> AsyncIterator[dict[str, Any]]:
        return _failing_stream(self._exc)


@pytest.fixture
def resolve_user(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve_user(_request: object) -> object:
        return object()

    monkeypatch.setattr(turns_module, "_resolve_user", _resolve_user)


async def test_post_stream_uses_the_api_errors_public_message(
    monkeypatch: pytest.MonkeyPatch,
    resolve_user: None,
) -> None:
    private = "sandbox=https://private.invalid native_session=secret"
    service = _PostStreamService(
        APIError(
            code="AGENT_RUNTIME_ERROR",
            message=private,
            user_message="The agent runtime disconnected.",
            status_code=502,
        )
    )
    monkeypatch.setattr(turns_module, "_svc", lambda: service)

    response = await turns_module.send_message_ai_stream(
        "session-1",
        SimpleNamespace(),  # type: ignore[arg-type]
        {"content": "hello"},
    )
    assert isinstance(response, StreamingResponse)

    body = await _response_text(response)
    assert "The agent runtime disconnected." in body
    assert private not in body


async def test_post_stream_does_not_publish_an_unexpected_exceptions_text(
    monkeypatch: pytest.MonkeyPatch,
    resolve_user: None,
) -> None:
    private = "workspace=/home/private native_control=secret"
    monkeypatch.setattr(
        turns_module,
        "_svc",
        lambda: _PostStreamService(RuntimeError(private)),
    )

    response = await turns_module.send_message_ai_stream(
        "session-1",
        SimpleNamespace(),  # type: ignore[arg-type]
        {"content": "hello"},
    )
    assert isinstance(response, StreamingResponse)

    body = await _response_text(response)
    assert "The session stream stopped unexpectedly." in body
    assert private not in body


async def test_resume_stream_does_not_publish_an_unexpected_exceptions_text(
    monkeypatch: pytest.MonkeyPatch,
    resolve_user: None,
) -> None:
    private = "sandbox_id=private native_session=secret"
    monkeypatch.setattr(
        turns_module,
        "_svc",
        lambda: _ResumeStreamService(RuntimeError(private)),
    )

    response = await turns_module.resume_ai_stream(
        "session-1",
        SimpleNamespace(query_params={}),  # type: ignore[arg-type]
    )
    assert isinstance(response, StreamingResponse)

    body = await _response_text(response)
    assert "The session stream stopped unexpectedly." in body
    assert private not in body


async def test_terminal_stream_does_not_publish_an_unexpected_exceptions_text(
    monkeypatch: pytest.MonkeyPatch,
    resolve_user: None,
) -> None:
    private = "runner_url=https://private.invalid working_dir=/home/private"
    monkeypatch.setattr(
        turns_module,
        "_svc",
        lambda: _TerminalStreamService(RuntimeError(private)),
    )

    response = await turns_module.terminal_stream(
        "session-1",
        SimpleNamespace(),  # type: ignore[arg-type]
        {"command": "pwd"},
    )
    assert isinstance(response, StreamingResponse)

    body = await _response_text(response)
    assert "The terminal stream stopped unexpectedly." in body
    assert private not in body
