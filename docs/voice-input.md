# Voice input

Voice input records a short message, transcribes it, and appends the text to
the conversation draft. The user can edit the text
before sending it to the Agent. Recordings stop automatically after two minutes;
the maximum upload is 25 MiB. Cancel discards the recording or ignores a pending
transcript. Cancellation cannot undo processing already started by a provider.

If transcription fails, the microphone stops and the recording stays in the
current page's memory. Choose **Retry transcription** to submit the same audio
to the same model without recording again, or **Discard recording** to clear
it. The existing draft remains editable; resolve or discard the recording
before sending. A successful retry appends the transcript to the latest draft
once and clears the retained audio. There are no automatic retries.

Audio is not saved to browser storage or persisted by AstraBox. Refreshing,
leaving the conversation, or closing the page clears it. It is also discarded
if the conversation can no longer accept input or its configured voice route
is removed. Retrying resubmits the audio to the provider and may incur another
transcription charge, including when an earlier response was lost.

The microphone control is adapted from Vercel AI Elements' Apache-2.0
[SpeechInput](https://elements.ai-sdk.dev/components/speech-input). It always
uses the selected gateway model. Its source and adaptation notes are in
`frontend/src/components/voice/SpeechInput.tsx`; the pristine registry components
remain under `components/ai-elements`.

## Configure transcription models

Configure one or more audio-transcription routes on the existing LiteLLM
gateway, using its model management interface or configuration. For example:

```yaml
model_list:
  - model_name: voice-cloud
    litellm_params:
      model: openai/whisper-1
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      mode: audio_transcription
  - model_name: voice-local
    litellm_params:
      model: openai/your-transcription-model
      api_base: os.environ/VOICE_LOCAL_BASE_URL
      api_key: os.environ/VOICE_LOCAL_API_KEY
    model_info:
      mode: audio_transcription
```

The second route is optional. A self-hosted service must implement the
OpenAI-compatible audio transcription endpoint, accept the browser's recording
format, and be reachable **from the gateway**. A chat-only endpoint is insufficient;
`localhost` on the cloud gateway does not refer to the user's computer.

Expose the route names to the AstraBox server with a JSON list:

```sh
ASTRABOX_SPEECH_INPUT_MODELS='["voice-cloud", "voice-local"]'
```

Alternatively set `astrabox.speech_input.models` in `app.yml`. Restart the
server after changing this setting. An empty list disables the microphone
control. The first model is the default; when several are configured, the user
can choose beside the microphone, and the browser remembers the choice.
Supply provider secrets to LiteLLM using your existing deployment mechanism.
AstraBox does not deploy speech models or require a separate media service.

## Request path

The browser uploads multipart audio to
`POST /api/v1/sessions/{session_id}/speech-input`. AstraBox checks session
ownership, validates the selected route and recording size, resolves the
existing model endpoint provider, and calls `/v1/audio/transcriptions` using
the server-reachable gateway address and the session's inference credential.
Providers without session credentials use their declared server credential.
The selected provider must expose the compatible transcription API.

The browser receives only the transcript. Audio is not added to the sandbox,
conversation history, or workspace. Gateway and model-provider retention
policies still apply. The endpoint uses the platform's configured
`model_endpoint_provider`; the selected speech model is independent of the
Agent's chat model.

Recording needs a microphone and a secure browser context (HTTPS or localhost).
This flow transcribes after recording stops; it does not provide live partial
transcripts or a spoken conversation with the Agent.
