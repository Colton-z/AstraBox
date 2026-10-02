"""Voice input preserves session authority and the gateway's multipart wire."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from starlette.datastructures import Headers, UploadFile

from astrabox.api.routes import speech_input as routes
from astrabox.api.routes._shared import handle_api_error
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import AstraBoxRuntimeSettings
from astrabox.core.service import speech_input as speech
from astrabox.seams.model import ModelEndpoint


def _audio(content: bytes = b"\x00\xffrecorded-audio", media_type: str = "audio/mp4") -> UploadFile:
    return UploadFile(
        io.BytesIO(content),
        filename="browser-recording",
        size=len(content),
        headers=Headers({"content-type": media_type}),
    )


@pytest.fixture
def gateway(monkeypatch):
    calls = []
    provider = SimpleNamespace(
        server_endpoint=lambda **kwargs: ModelEndpoint(
            base_url="http://gateway-server.test/v1",
            api_key="server-management-key",
        ),
        ensure_session_credential=AsyncMock(return_value="session-inference-key"),
        request_headers=lambda **kwargs: {"langfuse_session_id": kwargs["context"].conversation_id},
    )
    monkeypatch.setattr(speech, "model_endpoint_for_name", lambda name: provider)

    def receive(request):
        calls.append(request)
        return httpx.Response(200, json={"text": " 请检查这个改动。 "})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        speech.httpx,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(receive),
            **kwargs,
        ),
    )
    settings = SimpleNamespace(
        speech_input_models=["voice-cloud", "voice-local"], model_endpoint_provider="test"
    )
    return settings, provider, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["voice-cloud", "voice-local"])
async def test_transcription_uses_session_key_and_preserves_audio_format(gateway, model):
    settings, provider, calls = gateway
    text = await speech.transcribe_speech_input(
        settings=settings,
        session_id="session-1",
        user_id="owner-1",
        model=model,
        audio=_audio(),
    )
    assert text == "请检查这个改动。"
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "http://gateway-server.test/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer session-inference-key"
    assert request.headers["langfuse_session_id"] == "session-1"
    assert b'filename="recording.m4a"' in request.content
    assert b"Content-Type: audio/mp4" in request.content
    assert b"\x00\xffrecorded-audio" in request.content
    assert model.encode() in request.content
    assert b"server-management-key" not in request.content
    context = provider.ensure_session_credential.call_args.kwargs["context"]
    assert (context.conversation_id, context.user_id) == ("session-1", "owner-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,content,media_type,status",
    [
        ("arbitrary-model", b"audio", "audio/webm", 400),
        ("voice-cloud", b"", "audio/webm", 415),
        ("voice-cloud", b"text", "text/plain", 415),
    ],
)
async def test_invalid_inputs_never_mint_keys_or_call_the_gateway(
    gateway, model, content, media_type, status
):
    settings, provider, calls = gateway
    with pytest.raises(APIError) as error:
        await speech.transcribe_speech_input(
            settings=settings,
            session_id="session-1",
            user_id="owner-1",
            model=model,
            audio=_audio(content, media_type),
        )
    assert error.value.status_code == status
    provider.ensure_session_credential.assert_not_called()
    assert calls == []


@pytest.mark.asyncio
async def test_upload_limit_is_enforced_even_without_declared_size(gateway, monkeypatch):
    settings, provider, calls = gateway
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 4)
    audio = _audio(b"12345")
    audio.size = None
    with pytest.raises(APIError, match="SPEECH_INPUT_TOO_LARGE"):
        await speech.transcribe_speech_input(
            settings=settings,
            session_id="session-1",
            user_id="owner-1",
            model="voice-cloud",
            audio=audio,
        )
    provider.ensure_session_credential.assert_not_called()
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="private upstream credential"),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json={"text": None}),
    ],
)
async def test_gateway_errors_are_not_exposed_or_retried(gateway, monkeypatch, response):
    settings, _, _ = gateway
    post = AsyncMock(return_value=response)
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(post=post)
    monkeypatch.setattr(speech.httpx, "AsyncClient", lambda **kwargs: manager)
    with pytest.raises(APIError) as error:
        await speech.transcribe_speech_input(
            settings=settings,
            session_id="session-1",
            user_id="owner-1",
            model="voice-cloud",
            audio=_audio(),
        )
    assert error.value.code == "SPEECH_INPUT_FAILED"
    assert "private upstream" not in str(error.value)
    assert post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_other_users_cannot_discover_or_transcribe_session_audio(monkeypatch, method):
    owner_check = AsyncMock(side_effect=APIError("SESSION_NOT_FOUND", "Not found", 404))
    transcribe = AsyncMock()
    monkeypatch.setattr(routes, "_svc", lambda: SimpleNamespace(must_own_session=owner_check))
    monkeypatch.setattr(
        routes, "_resolve_user", AsyncMock(return_value=SimpleNamespace(user_id="other"))
    )
    monkeypatch.setattr(routes, "transcribe_speech_input", transcribe)
    app = FastAPI()
    app.include_router(routes.router)
    app.add_exception_handler(APIError, handle_api_error)
    kwargs = (
        {
            "data": {"model": "voice-cloud"},
            "files": {"file": ("audio.webm", b"audio", "audio/webm")},
        }
        if method == "POST"
        else {}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, "/api/v1/sessions/not-owned/speech-input", **kwargs)
    assert response.status_code == 404
    transcribe.assert_not_called()


def test_settings_accept_ordered_model_choices_from_environment(monkeypatch):
    monkeypatch.setenv(
        "ASTRABOX_SPEECH_INPUT_MODELS", '["voice-cloud", "voice-local", "voice-cloud"]'
    )
    options = speech.speech_input_options(AstraBoxRuntimeSettings())
    assert options["models"] == ["voice-cloud", "voice-local"]
