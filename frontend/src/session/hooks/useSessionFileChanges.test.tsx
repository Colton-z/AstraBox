// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { SessionFileChanges } from '../../api';
import { changedFilePaths } from '../fileChanges';
import { useSessionFileChanges } from './useSessionFileChanges';

const { read } = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock('../../api', () => ({ getSessionFileChanges: read }));

function turn(turnId: string, path: string, messageId = turnId) {
  return {
    turn_id: turnId,
    messages: [{
      message_id: messageId,
      parts: [{
        type: 'data-file-changes' as const,
        id: `file-changes:${messageId}`,
        data: { toolCallId: messageId, toolName: 'Write', files: [{ path, diff: null }] },
      }],
    }],
  };
}

function live(turnId: string, path: string): SDKUIMessage {
  const message = turn(turnId, path, `${turnId}-live`).messages[0];
  return { id: message.message_id, role: 'assistant', metadata: { turn_id: turnId }, parts: message.parts };
}

const base = {
  sessionId: 'session-a', enabled: true, lifecycleState: 'ready', lastTurnId: 'turn-2',
  messages: [] as SDKUIMessage[],
};

beforeEach(() => { read.mockReset(); });
afterEach(cleanup);

describe('durable file changes', () => {
  it('retains early changes when the mounted history window contains only the last turn', async () => {
    read.mockResolvedValue({ through_seq: 200, turns: [turn('turn-1', 'early.txt'), turn('turn-2', 'late.txt')] });
    const { result, rerender } = renderHook((props) => useSessionFileChanges(props), {
      initialProps: { ...base, messages: [live('turn-2', 'late.txt')] },
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(changedFilePaths(result.current.messages, true)).toEqual(['early.txt', 'late.txt']);
    expect(result.current.messages).toHaveLength(2);
    rerender({ ...base, messages: [] });
    expect(changedFilePaths(result.current.messages, true)).toEqual(['early.txt', 'late.txt']);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('loads only new terminal events and replaces an already-known turn', async () => {
    read.mockResolvedValueOnce({ through_seq: 200, turns: [turn('turn-1', 'early.txt')] });
    const { result } = renderHook(() => useSessionFileChanges(base));
    await waitFor(() => expect(result.current.loading).toBe(false));
    read.mockResolvedValueOnce({ through_seq: 250, turns: [{ turn_id: 'turn-1', messages: [] }, turn('turn-2', 'late.txt')] });
    await act(async () => { await result.current.refresh(); });
    expect(read.mock.calls[1][1]).toBe(200);
    expect(changedFilePaths(result.current.messages, true)).toEqual(['late.txt']);
  });

  it('combines unfinished live work with durable turns without resurrecting stale window results', async () => {
    read.mockResolvedValue({ through_seq: 200, turns: [turn('turn-1', 'correct.txt', 'native-id')] });
    const { result } = renderHook(() => useSessionFileChanges({
      ...base, messages: [live('turn-1', 'superseded.txt'), live('turn-3', 'active.txt')],
    }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(changedFilePaths(result.current.messages, true)).toEqual(['correct.txt', 'active.txt']);
  });

  it('does not publish a response from the conversation that was just left', async () => {
    let finishOld!: (value: SessionFileChanges) => void;
    read.mockImplementationOnce(() => new Promise<SessionFileChanges>((resolve) => { finishOld = resolve; }));
    const { result, rerender } = renderHook((props) => useSessionFileChanges(props), { initialProps: base });
    read.mockResolvedValueOnce({ through_seq: 10, turns: [turn('other-turn', 'other.txt')] });
    rerender({ ...base, sessionId: 'session-b' });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { finishOld({ through_seq: 900, turns: [turn('old-turn', 'private-old.txt')] }); });
    expect(changedFilePaths(result.current.messages, true)).toEqual(['other.txt']);
    expect(read.mock.calls[1][1]).toBe(0);
  });

  it('keeps failure visible and retries from the last successful checkpoint', async () => {
    read.mockResolvedValueOnce({ through_seq: 200, turns: [turn('turn-1', 'kept.txt')] });
    const { result } = renderHook(() => useSessionFileChanges(base));
    await waitFor(() => expect(result.current.loading).toBe(false));
    read.mockRejectedValueOnce(new Error('read unavailable'));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBe('read unavailable');
    read.mockResolvedValueOnce({ through_seq: 205, turns: [] });
    await act(async () => { await result.current.refresh(); });
    expect(read.mock.calls[2][1]).toBe(200);
    expect(result.current.error).toBeNull();
    expect(changedFilePaths(result.current.messages, true)).toEqual(['kept.txt']);
  });

  it('distinguishes a failed initial read from an empty history', async () => {
    read.mockRejectedValue(new Error('history unavailable'));
    const { result } = renderHook(() => useSessionFileChanges(base));
    await waitFor(() => expect(result.current.error).toBe('history unavailable'));
    expect(result.current.loading).toBe(false);
  });

  it('can read a terminated conversation without live compute', async () => {
    read.mockResolvedValue({ through_seq: 200, turns: [turn('turn-1', 'kept.txt')] });
    const { result } = renderHook(() => useSessionFileChanges({ ...base, lifecycleState: 'terminated' }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(changedFilePaths(result.current.messages, true)).toEqual(['kept.txt']);
  });

  it('does not fetch when this engine has no Diff capability', () => {
    const { result } = renderHook(() => useSessionFileChanges({ ...base, enabled: false }));
    expect(read).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
  });
});
