// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SpeechInput } from './SpeechInput';

class Recorder extends EventTarget {
  static isTypeSupported(type: string) { return type === 'audio/mp4'; }
  state = 'inactive';
  mimeType = 'audio/mp4';
  start() { this.state = 'recording'; }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    queueMicrotask(() => {
      const event = new Event('dataavailable');
      Object.defineProperty(event, 'data', { value: new Blob(['audio'], { type: this.mimeType }) });
      this.dispatchEvent(event);
      this.dispatchEvent(new Event('stop'));
    });
  }
}

const stopTrack = vi.fn();
const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
const getUserMedia = vi.fn();
const labels = { idle: 'Start', requesting: 'Permission', recording: 'Stop', transcribing: 'Transcribing', failed: 'Retry', retry: 'Retry', cancel: 'Cancel', discard: 'Discard', unsupported: 'Unsupported' };

function props() {
  return {
    labels, maxAudioBytes: 1024, maxRecordingSeconds: 120,
    onAudioRecorded: vi.fn().mockResolvedValue('transcribed words'),
    onTranscriptionChange: vi.fn(), onError: vi.fn(),
  };
}

beforeEach(() => {
  stopTrack.mockClear();
  getUserMedia.mockReset().mockResolvedValue(stream);
  vi.stubGlobal('MediaRecorder', Recorder);
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('gateway speech recording', () => {
  it('records even when browser speech recognition exists and sends the actual MP4 type', async () => {
    const recognition = vi.fn();
    vi.stubGlobal('SpeechRecognition', recognition);
    const callbacks = props();
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(callbacks.onTranscriptionChange).toHaveBeenCalledWith('transcribed words'));
    expect(recognition).not.toHaveBeenCalled();
    expect(callbacks.onAudioRecorded.mock.calls[0][0].type).toBe('audio/mp4');
    expect(stopTrack).toHaveBeenCalled();
  });

  it('discards a recording on cancel without uploading it', async () => {
    const callbacks = props();
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByRole('button', { name: 'Stop' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await act(async () => {});
    expect(callbacks.onAudioRecorded).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Start' })).toBeTruthy();
  });

  it('releases a microphone granted after the component has unmounted', async () => {
    let grant!: (stream: MediaStream) => void;
    getUserMedia.mockReturnValue(new Promise<MediaStream>((resolve) => { grant = resolve; }));
    const callbacks = props();
    const view = render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    view.unmount();
    await act(async () => { grant(stream); });
    expect(stopTrack).toHaveBeenCalled();
    expect(callbacks.onAudioRecorded).not.toHaveBeenCalled();
  });

  it('aborts transcription and ignores late responses after cancellation', async () => {
    let finish!: (text: string) => void;
    const callbacks = props();
    callbacks.onAudioRecorded.mockImplementation(() => new Promise<string>((resolve) => { finish = resolve; }));
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(callbacks.onAudioRecorded).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(callbacks.onAudioRecorded.mock.calls[0][1].aborted).toBe(true);
    await act(async () => { finish('late transcript'); });
    expect(callbacks.onTranscriptionChange).not.toHaveBeenCalled();
  });

  it('reports denied microphone access and permits another attempt', async () => {
    getUserMedia.mockRejectedValueOnce(new DOMException('Denied', 'NotAllowedError'));
    const callbacks = props();
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    await waitFor(() => expect(callbacks.onError).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    await screen.findByRole('button', { name: 'Stop' });
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });

  it('retains the same audio across failures and appends one result without recording again', async () => {
    const callbacks = props();
    callbacks.onAudioRecorded
      .mockRejectedValueOnce(new TypeError('Network disconnected'))
      .mockRejectedValueOnce(new Error('Upstream unavailable'));
    const view = render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(callbacks.onError).toHaveBeenCalledTimes(2));
    expect(callbacks.onTranscriptionChange).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalled();

    const latestDraftCallback = vi.fn();
    view.rerender(<SpeechInput {...callbacks} onTranscriptionChange={latestDraftCallback} />);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('button', { name: 'Start' });
    expect(latestDraftCallback).toHaveBeenCalledExactlyOnceWith('transcribed words');
    expect(callbacks.onTranscriptionChange).not.toHaveBeenCalled();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    const calls = callbacks.onAudioRecorded.mock.calls;
    expect(calls).toHaveLength(3);
    expect(calls[1][0]).toBe(calls[0][0]);
    expect(calls[2][0]).toBe(calls[0][0]);
    expect(new Set(calls.map((call) => call[1])).size).toBe(3);
    expect(screen.queryByRole('button', { name: 'Discard' })).toBeNull();
  });

  it('prevents duplicate retries and ignores a retry response after cancellation', async () => {
    let finish!: (text: string) => void;
    const callbacks = props();
    callbacks.onAudioRecorded.mockRejectedValueOnce(new Error('Timeout'))
      .mockImplementationOnce(() => new Promise<string>((resolve) => { finish = resolve; }));
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    const retry = await screen.findByRole('button', { name: 'Retry' });
    fireEvent.click(retry);
    fireEvent.click(retry);
    expect(callbacks.onAudioRecorded).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(callbacks.onAudioRecorded.mock.calls[1][1].aborted).toBe(true);
    await act(async () => { finish('late retry'); });
    expect(callbacks.onTranscriptionChange).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Start' })).toBeTruthy();
  });

  it('discards failed audio explicitly and records fresh audio on the next attempt', async () => {
    const callbacks = props();
    callbacks.onAudioRecorded.mockRejectedValueOnce(new Error('Timeout'));
    render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Discard' }));
    expect(callbacks.onAudioRecorded).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(callbacks.onTranscriptionChange).toHaveBeenCalledTimes(1));
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(callbacks.onAudioRecorded.mock.calls[1][0]).not.toBe(callbacks.onAudioRecorded.mock.calls[0][0]);
  });

  it.each(['unmount', 'disable'] as const)('aborts a pending retry on %s', async (action) => {
    let fail!: (error: Error) => void;
    const callbacks = props();
    callbacks.onAudioRecorded.mockRejectedValueOnce(new Error('Timeout'))
      .mockImplementationOnce(() => new Promise<string>((_, reject) => { fail = reject; }));
    const view = render(<SpeechInput {...callbacks} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    if (action === 'unmount') view.unmount();
    else view.rerender(<SpeechInput {...callbacks} disabled />);
    expect(callbacks.onAudioRecorded.mock.calls[1][1].aborted).toBe(true);
    await act(async () => { fail(new Error('late failure')); });
    expect(callbacks.onError).toHaveBeenCalledTimes(1);
    expect(callbacks.onTranscriptionChange).not.toHaveBeenCalled();
  });

  it('discards recordings that exceed the upload limit', async () => {
    const callbacks = props();
    render(<SpeechInput {...callbacks} maxAudioBytes={2} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(callbacks.onError).toHaveBeenCalledWith(new Error('recording-too-large')));
    expect(callbacks.onAudioRecorded).not.toHaveBeenCalled();
  });

  it('automatically stops at the duration limit', async () => {
    vi.useFakeTimers();
    const callbacks = props();
    render(<SpeechInput {...callbacks} maxRecordingSeconds={2} />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Start' })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(callbacks.onAudioRecorded).toHaveBeenCalledTimes(1);
    expect(stopTrack).toHaveBeenCalled();
  });
});
