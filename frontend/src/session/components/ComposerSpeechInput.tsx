import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';
import { toast } from 'sonner';
import { ChevronDownIcon } from 'lucide-react';
import { ApiError, getSpeechInputOptions, transcribeSpeechInput } from '@/api';
import { SpeechInput, type SpeechInputState } from '@/components/voice/SpeechInput';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const MODEL_PREFERENCE = 'astrabox.voice-input.model';

export function ComposerSpeechInput({
  sessionId, disabled, onTranscriptionChange, onStateChange,
}: {
  sessionId: string;
  disabled: boolean;
  onTranscriptionChange: (text: string) => void;
  onStateChange: (state: SpeechInputState) => void;
}) {
  const { t } = useTranslation();
  const { data, error, mutate } = useSWR(
    ['speech-input', sessionId],
    () => getSpeechInputOptions(sessionId),
    { revalidateOnFocus: false },
  );
  const [state, setState] = useState<SpeechInputState>('idle');
  const [preferredModel, setPreferredModel] = useState(() => {
    try { return localStorage.getItem(MODEL_PREFERENCE) ?? ''; } catch { return ''; }
  });
  const models = data?.models ?? [];
  const selectedModel = models.includes(preferredModel) ? preferredModel : models[0];
  useEffect(() => {
    onStateChange(selectedModel ? state : 'idle');
  }, [selectedModel, state, onStateChange]);

  if (error && !data) {
    return <Button type="button" variant="ghost" size="sm" onClick={() => { void mutate(); }}>
      {t('chat:voice.retry_options')}
    </Button>;
  }
  if (!data || !selectedModel) return null;

  const handleError = (cause: unknown) => {
    let key = 'failed';
    if (cause instanceof DOMException && cause.name === 'NotAllowedError') key = 'permission_denied';
    else if (cause instanceof DOMException && cause.name === 'NotFoundError') key = 'no_microphone';
    else if (cause instanceof Error && cause.message === 'recording-empty') key = 'empty';
    else if ((cause instanceof Error && cause.message === 'recording-too-large')
      || (cause instanceof ApiError && cause.code === 'SPEECH_INPUT_TOO_LARGE')) key = 'too_large';
    else if (cause instanceof ApiError && cause.code === 'SPEECH_INPUT_TIMEOUT') key = 'timeout';
    else if (cause instanceof ApiError && cause.code === 'SPEECH_INPUT_NOT_CONFIGURED') key = 'not_configured';
    else if (cause instanceof ApiError && cause.code === 'SPEECH_INPUT_INVALID_AUDIO') key = 'invalid_audio';
    else if (cause instanceof ApiError) key = 'transcription_failed';
    toast.error(t(`chat:voice.${key}`));
  };

  return (
    <div className="flex items-center gap-0.5">
      <SpeechInput
        key={selectedModel}
        disabled={disabled}
        maxAudioBytes={data.max_audio_bytes}
        maxRecordingSeconds={data.max_recording_seconds}
        labels={{
          idle: t('chat:voice.start'), requesting: t('chat:voice.requesting'),
          recording: t('chat:voice.stop'), transcribing: t('chat:voice.transcribing'),
          failed: t('chat:voice.retry'), discard: t('chat:voice.discard'),
          retry: t('chat:voice.retry_short'),
          cancel: t('chat:voice.cancel'), unsupported: t('chat:voice.unsupported'),
        }}
        onStateChange={setState}
        onAudioRecorded={(audio, signal) => transcribeSpeechInput(sessionId, selectedModel, audio, signal)}
        onTranscriptionChange={(text) => {
          if (text.trim()) onTranscriptionChange(text);
          else toast.info(t('chat:voice.empty'));
        }}
        onError={handleError}
      />
      {models.length > 1 && (
        <DropdownMenu>
          <DropdownMenuTrigger render={
            <Button type="button" variant="ghost" size="icon-xs"
              disabled={disabled || state !== 'idle'}
              aria-label={t('chat:voice.choose_model')} title={selectedModel} />
          }>
            <ChevronDownIcon />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuRadioGroup value={selectedModel} onValueChange={(value) => {
              setPreferredModel(value);
              try { localStorage.setItem(MODEL_PREFERENCE, value); } catch { /* Storage can be unavailable. */ }
            }}>
              {models.map((model) => <DropdownMenuRadioItem key={model} value={model} closeOnClick>{model}</DropdownMenuRadioItem>)}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
