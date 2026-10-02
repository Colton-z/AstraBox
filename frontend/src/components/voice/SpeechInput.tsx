// Adapted from Vercel AI Elements SpeechInput (Apache-2.0):
// https://github.com/vercel/ai-elements/blob/1310612d305d6c2694a920ff87e38a74ec859e61/packages/elements/src/speech-input.tsx
// Keeps upstream's MediaRecorder flow and record/stop/spinner UI. Product
// adaptations: always use the selected gateway, preserve the recording's MIME
// type, retain failed uploads for retry, and discard/abort work on cancellation
// or unmount.
// This adapted component lives outside the pristine ai-elements registry tree.
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/utils';
import { MicIcon, RotateCcwIcon, SquareIcon, XIcon } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

export type SpeechInputState = 'idle' | 'requesting' | 'recording' | 'transcribing' | 'failed';

type Recording = {
  controller: AbortController;
  stream?: MediaStream;
  recorder?: MediaRecorder;
  timer?: ReturnType<typeof setTimeout>;
  audio?: Blob;
  transcribing?: boolean;
};

function releaseRecording(recording: Recording) {
  clearTimeout(recording.timer);
  if (recording.recorder && recording.recorder.state !== 'inactive') {
    recording.recorder.stop();
  }
  recording.stream?.getTracks().forEach((track) => track.stop());
}

export function SpeechInput({
  disabled = false,
  maxAudioBytes,
  maxRecordingSeconds,
  labels,
  onAudioRecorded,
  onTranscriptionChange,
  onError,
  onStateChange,
}: {
  disabled?: boolean;
  maxAudioBytes: number;
  maxRecordingSeconds: number;
  labels: Record<SpeechInputState | 'retry' | 'cancel' | 'discard' | 'unsupported', string>;
  onAudioRecorded: (audio: Blob, signal: AbortSignal) => Promise<string>;
  onTranscriptionChange: (text: string) => void;
  onError: (error: unknown) => void;
  onStateChange?: (state: SpeechInputState) => void;
}) {
  const [state, setState] = useState<SpeechInputState>('idle');
  const recordingRef = useRef<Recording | null>(null);
  const callbacks = useRef({ onAudioRecorded, onTranscriptionChange, onError });
  callbacks.current = { onAudioRecorded, onTranscriptionChange, onError };
  const supported = typeof MediaRecorder !== 'undefined'
    && typeof navigator.mediaDevices?.getUserMedia === 'function';

  const discard = useCallback(() => {
    const recording = recordingRef.current;
    recordingRef.current = null;
    if (recording) {
      recording.controller.abort();
      releaseRecording(recording);
      recording.audio = undefined;
    }
  }, []);

  const cancel = useCallback(() => {
    discard();
    setState('idle');
  }, [discard]);

  useEffect(() => discard, [discard]);
  useEffect(() => {
    if (disabled) cancel();
  }, [disabled, cancel]);
  useEffect(() => { onStateChange?.(state); }, [state, onStateChange]);

  const transcribe = useCallback(async (recording: Recording) => {
    if (recordingRef.current !== recording || !recording.audio || recording.transcribing) return;
    recording.transcribing = true;
    const controller = new AbortController();
    recording.controller = controller;
    setState('transcribing');
    let transcript: string;
    try {
      transcript = await callbacks.current.onAudioRecorded(recording.audio, controller.signal);
    } catch (error) {
      if (recordingRef.current === recording) {
        setState('failed');
        callbacks.current.onError(error);
      }
      return;
    } finally {
      recording.transcribing = false;
    }
    if (recordingRef.current === recording) {
      recordingRef.current = null;
      recording.audio = undefined;
      setState('idle');
      callbacks.current.onTranscriptionChange(transcript);
    }
  }, []);

  const startMediaRecorder = useCallback(async () => {
    if (recordingRef.current || disabled || !supported) return;
    const recording: Recording = { controller: new AbortController() };
    recordingRef.current = recording;
    setState('requesting');
    const isCurrent = () => recordingRef.current === recording;
    const fail = (error: unknown) => {
      if (!isCurrent()) return;
      cancel();
      callbacks.current.onError(error);
    };
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recording.stream = stream;
      if (!isCurrent()) {
        releaseRecording(recording);
        return;
      }
      // Prefer widely accepted formats, while allowing Safari's MP4 recorder.
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
        .find((type) => MediaRecorder.isTypeSupported(type));
      const mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      recording.recorder = mediaRecorder;
      const audioChunks: Blob[] = [];
      let audioBytes = 0;

      const handleDataAvailable = (event: BlobEvent) => {
        if (!isCurrent() || event.data.size === 0) return;
        audioBytes += event.data.size;
        if (audioBytes > maxAudioBytes) {
          fail(new Error('recording-too-large'));
          return;
        }
        audioChunks.push(event.data);
      };
      const handleStop = () => {
        releaseRecording(recording);
        recording.stream = undefined;
        recording.recorder = undefined;
        if (!isCurrent()) return;
        const audioBlob = new Blob(audioChunks, {
          type: mediaRecorder.mimeType || audioChunks[0]?.type,
        });
        if (!audioBlob.size) {
          fail(new Error('recording-empty'));
          return;
        }
        audioChunks.length = 0;
        recording.audio = audioBlob;
        void transcribe(recording);
      };
      mediaRecorder.addEventListener('dataavailable', handleDataAvailable);
      mediaRecorder.addEventListener('stop', () => { void handleStop(); });
      mediaRecorder.addEventListener('error', () => fail(new Error('recording-failed')));
      mediaRecorder.start(1000);
      setState('recording');
      recording.timer = setTimeout(() => {
        if (isCurrent() && mediaRecorder.state === 'recording') mediaRecorder.stop();
      }, maxRecordingSeconds * 1000);
    } catch (error) {
      fail(error);
    }
  }, [cancel, disabled, maxAudioBytes, maxRecordingSeconds, supported, transcribe]);

  const toggleListening = () => {
    const recorder = recordingRef.current?.recorder;
    if (state === 'recording' && recorder?.state === 'recording') {
      setState('transcribing');
      recorder.stop();
    } else if (state === 'idle') {
      void startMediaRecorder();
    } else if (state === 'failed' && recordingRef.current && !disabled) {
      void transcribe(recordingRef.current);
    }
  };
  const isListening = state === 'recording';
  const isProcessing = state === 'requesting' || state === 'transcribing';
  const label = supported ? labels[state] : labels.unsupported;
  const cancelLabel = state === 'failed' ? labels.discard : labels.cancel;

  return (
    <div className="relative inline-flex items-center gap-1">
      <div className="relative inline-flex items-center justify-center">
        {isListening && <div aria-hidden className="pointer-events-none absolute inset-0 animate-ping rounded-full border border-destructive/30 motion-reduce:animate-none" />}
        <Button
          type="button"
          size={state === 'failed' ? 'sm' : 'icon'}
          variant="ghost"
          className={cn('relative h-8 rounded-lg', state !== 'failed' && 'w-8', isListening && 'bg-destructive/10 text-destructive')}
          disabled={disabled || !supported || isProcessing}
          aria-label={label}
          title={label}
          aria-pressed={isListening}
          onClick={toggleListening}
        >
          {isProcessing ? <Spinner /> : isListening ? <SquareIcon className="size-4" />
            : state === 'failed' ? <><RotateCcwIcon className="size-4" />{labels.retry}</> : <MicIcon className="size-4" />}
        </Button>
      </div>
      {state !== 'idle' && (
        <Button type="button" size="icon" variant="ghost" aria-label={cancelLabel} title={cancelLabel} onClick={cancel}>
          <XIcon className="size-4" />
        </Button>
      )}
      <span className="sr-only" role="status">{state === 'idle' ? '' : label}</span>
    </div>
  );
}
