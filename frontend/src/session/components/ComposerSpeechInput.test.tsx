// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { SWRConfig } from 'swr';
import i18n from '@/i18n';
import { getSpeechInputOptions, transcribeSpeechInput } from '@/api';
import { ComposerSpeechInput } from './ComposerSpeechInput';

vi.mock('@/api', () => ({
  ApiError: class extends Error {},
  getSpeechInputOptions: vi.fn(),
  transcribeSpeechInput: vi.fn().mockResolvedValue('transcribed words'),
}));
vi.mock('@/components/voice/SpeechInput', () => ({
  SpeechInput: ({ onAudioRecorded, onTranscriptionChange }: {
    onAudioRecorded: (audio: Blob, signal: AbortSignal) => Promise<string>;
    onTranscriptionChange: (text: string) => void;
  }) => <button onClick={async () => {
    const text = await onAudioRecorded(new Blob(['audio'], { type: 'audio/mp4' }), new AbortController().signal);
    onTranscriptionChange(text);
  }}>Record</button>,
}));

beforeAll(async () => { await i18n.changeLanguage('en'); });
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks(); });

function renderVoice() {
  const transcript = vi.fn();
  render(<SWRConfig value={{ provider: () => new Map() }}>
    <ComposerSpeechInput sessionId="session-voice" disabled={false} onStateChange={vi.fn()} onTranscriptionChange={transcript} />
  </SWRConfig>);
  return transcript;
}

describe('composer voice model selection', () => {
  it('uses the remembered self-hosted route and returns text to the composer', async () => {
    localStorage.setItem('astrabox.voice-input.model', 'voice-local');
    vi.mocked(getSpeechInputOptions).mockResolvedValue({ models: ['voice-cloud', 'voice-local'], max_audio_bytes: 1024, max_recording_seconds: 120 });
    const transcript = renderVoice();
    fireEvent.click(await screen.findByRole('button', { name: 'Record' }));
    await waitFor(() => expect(transcript).toHaveBeenCalledWith('transcribed words'));
    expect(transcribeSpeechInput).toHaveBeenCalledWith('session-voice', 'voice-local', expect.any(Blob), expect.any(AbortSignal));
    expect(screen.getByRole('button', { name: 'Choose transcription model' })).toBeTruthy();
  });

  it('falls back to the configured default when a remembered route is removed', async () => {
    localStorage.setItem('astrabox.voice-input.model', 'removed-route');
    vi.mocked(getSpeechInputOptions).mockResolvedValue({ models: ['voice-cloud'], max_audio_bytes: 1024, max_recording_seconds: 120 });
    renderVoice();
    fireEvent.click(await screen.findByRole('button', { name: 'Record' }));
    await waitFor(() => expect(transcribeSpeechInput).toHaveBeenCalledWith('session-voice', 'voice-cloud', expect.any(Blob), expect.any(AbortSignal)));
  });

  it('hides recording when no transcription models are configured', async () => {
    vi.mocked(getSpeechInputOptions).mockResolvedValue({ models: [], max_audio_bytes: 1024, max_recording_seconds: 120 });
    renderVoice();
    await waitFor(() => expect(getSpeechInputOptions).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Record' })).toBeNull();
  });
});
