"""Short voice recordings transcribed through the existing model gateway.

The caller authenticates the session before invoking this service. Only gateway
route names configured by the deployment are accepted; browser input never
selects an upstream URL or supplies a provider credential.
"""

from __future__ import annotations

from typing import Any

import httpx
from starlette.datastructures import UploadFile

from astrabox.common.utils.errors import make_api_error
from astrabox.seams.model import (
    ModelEndpointConfigurationError,
    ModelRequestContext,
    model_endpoint_for_name,
)

MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_RECORDING_SECONDS = 120
_AUDIO_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
}


def speech_input_options(settings: Any) -> dict[str, Any]:
    models = list(
        dict.fromkeys(name.strip() for name in settings.speech_input_models if name.strip())
    )
    return {
        "models": models,
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "max_recording_seconds": MAX_RECORDING_SECONDS,
    }


async def transcribe_speech_input(
    *,
    settings: Any,
    session_id: str,
    user_id: str,
    model: str,
    audio: UploadFile,
) -> str:
    models = speech_input_options(settings)["models"]
    if not models:
        raise make_api_error(
            code="SPEECH_INPUT_NOT_CONFIGURED", message="Voice input is not configured."
        )
    if model not in models:
        raise make_api_error(
            code="INVALID_REQUEST",
            message="Select a configured transcription model.",
        )
    media_type = str(audio.content_type or "").split(";", 1)[0].lower().strip()
    extension = _AUDIO_EXTENSIONS.get(media_type)
    if extension is None:
        raise make_api_error(
            code="SPEECH_INPUT_INVALID_AUDIO", message="Unsupported recording format."
        )
    if audio.size is not None and audio.size > MAX_AUDIO_BYTES:
        raise make_api_error(code="SPEECH_INPUT_TOO_LARGE", message="The recording is too large.")
    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(content) > MAX_AUDIO_BYTES:
        raise make_api_error(code="SPEECH_INPUT_TOO_LARGE", message="The recording is too large.")
    if not content:
        raise make_api_error(code="SPEECH_INPUT_INVALID_AUDIO", message="The recording is empty.")

    provider = model_endpoint_for_name(settings.model_endpoint_provider or None)
    context = ModelRequestContext(conversation_id=session_id, user_id=user_id)
    try:
        endpoint = provider.server_endpoint(settings=settings)
    except ModelEndpointConfigurationError as exc:
        raise make_api_error(
            code="SPEECH_INPUT_NOT_CONFIGURED",
            message="The transcription gateway is not configured.",
        ) from exc
    # The server needs its own reachable gateway address, but model spend keeps
    # the same session identity as the Agent. Providers without session keys use
    # their declared server credential, as the model seam permits.
    credential = await provider.ensure_session_credential(context=context) or endpoint.api_key
    if not endpoint.base_url or not credential:
        raise make_api_error(
            code="SPEECH_INPUT_NOT_CONFIGURED",
            message="The transcription gateway is not configured.",
        )
    base_url = endpoint.base_url.rstrip("/")
    url = base_url + (
        "/audio/transcriptions" if base_url.endswith("/v1") else "/v1/audio/transcriptions"
    )
    headers = dict(provider.request_headers(endpoint=endpoint, context=context))
    headers["Authorization"] = f"Bearer {credential}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
            response = await client.post(
                url,
                headers=headers,
                data={"model": model, "response_format": "json"},
                files={"file": (f"recording.{extension}", content, media_type)},
            )
    except httpx.TimeoutException as exc:
        raise make_api_error(
            code="SPEECH_INPUT_TIMEOUT", message="Transcription timed out. Please try again."
        ) from exc
    except httpx.HTTPError as exc:
        raise make_api_error(
            code="SPEECH_INPUT_FAILED", message="The transcription service is unavailable."
        ) from exc
    # Upstream errors may include configuration or credentials. Keep them out
    # of the browser's error envelope; do not automatically replay paid calls.
    if not response.is_success:
        raise make_api_error(
            code="SPEECH_INPUT_FAILED",
            message="The transcription service could not process this recording.",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise make_api_error(
            code="SPEECH_INPUT_FAILED",
            message="The transcription service returned an invalid response.",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise make_api_error(
            code="SPEECH_INPUT_FAILED",
            message="The transcription service returned an invalid response.",
        )
    return payload["text"].strip()
