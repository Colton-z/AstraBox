"""Session-owned voice input; audio is transcribed without entering a sandbox."""

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes._shared import _resolve_user, _svc
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.speech_input import speech_input_options, transcribe_speech_input

router = APIRouter()


class SpeechInputOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str]
    max_audio_bytes: int
    max_recording_seconds: int


class SpeechInputTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


@router.get(
    "/api/v1/sessions/{session_id}/speech-input",
    response_model=ApiEnvelope[SpeechInputOptions],
    response_model_exclude_unset=True,
)
async def get_speech_input_options(session_id: str, request: Request):
    user = await _resolve_user(request)
    await _svc().must_own_session(user, session_id)
    return success_response(speech_input_options(load_astrabox_settings()))


@router.post(
    "/api/v1/sessions/{session_id}/speech-input",
    response_model=ApiEnvelope[SpeechInputTranscript],
    response_model_exclude_unset=True,
)
async def transcribe_session_speech(
    session_id: str,
    request: Request,
    model: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        user = await _resolve_user(request)
        await _svc().must_own_session(user, session_id)
        text = await transcribe_speech_input(
            settings=load_astrabox_settings(),
            session_id=session_id,
            user_id=user.user_id,
            model=model,
            audio=file,
        )
        return success_response({"text": text})
    finally:
        await file.close()
