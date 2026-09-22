// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';

import type { PendingInteraction, SessionRecord } from '../../types';

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  endConversation: vi.fn(),
  interruptSession: vi.fn(),
  recoverSession: vi.fn(),
  terminateSandbox: vi.fn(),
  deleteSession: vi.fn(),
}));

vi.mock('../../api', () => api);

import { useSessionLifecycle } from './useSessionLifecycle';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const interaction = {
  interaction_id: 'interaction-1',
  turn_id: 'turn-1',
  tool_call_id: 'tool-call-1',
  tool_name: 'Write',
  presentation: 'tool_approval',
} as PendingInteraction;

describe('useSessionLifecycle pending interaction authority', () => {
  it('keeps first-page authority when it arrives before session detail', async () => {
    let resolveDetail: ((session: SessionRecord) => void) | undefined;
    api.getSession.mockImplementation(() => new Promise<SessionRecord>((resolve) => {
      resolveDetail = resolve;
    }));
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(resolveDetail).toBeTypeOf('function'));

    act(() => {
      result.current.bootstrapPendingInteraction(interaction);
    });
    await act(async () => {
      resolveDetail?.({
        session_id: 'session-1',
        state: 'READY',
        pending_interaction: null,
      } as SessionRecord);
    });

    expect(result.current.pendingInteraction).toBe(interaction);
  });

  it('hydrates a live interaction into the current session snapshot', async () => {
    api.getSession.mockResolvedValue({
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: null,
    } as SessionRecord);
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.session?.session_id).toBe('session-1'));

    act(() => {
      result.current.observePendingInteraction(interaction);
    });

    expect(result.current.session?.pending_interaction).toBe(interaction);
    expect(result.current.pendingInteraction).toBe(interaction);
  });

  it('does not let an older in-flight detail read erase a live interaction', async () => {
    const ready = {
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: null,
    } as SessionRecord;
    let resolveStaleDetail: ((session: SessionRecord) => void) | undefined;
    api.getSession
      .mockResolvedValueOnce(ready)
      .mockImplementationOnce(() => new Promise<SessionRecord>((resolve) => {
        resolveStaleDetail = resolve;
      }));
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.session?.session_id).toBe('session-1'));

    let refreshPromise: Promise<unknown> | undefined;
    act(() => {
      refreshPromise = result.current.refresh({ force: true });
    });
    await waitFor(() => expect(resolveStaleDetail).toBeTypeOf('function'));
    act(() => {
      result.current.observePendingInteraction(interaction);
    });
    await act(async () => {
      resolveStaleDetail?.(ready);
      await refreshPromise;
    });

    expect(result.current.pendingInteraction).toBe(interaction);
  });

  it('does not let an older interaction replace a newer live interaction', async () => {
    const nextInteraction = {
      ...interaction,
      interaction_id: 'interaction-2',
      tool_call_id: 'tool-call-2',
    } as PendingInteraction;
    const ready = {
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: null,
    } as SessionRecord;
    let resolveStaleDetail: ((session: SessionRecord) => void) | undefined;
    api.getSession
      .mockResolvedValueOnce(ready)
      .mockImplementationOnce(() => new Promise<SessionRecord>((resolve) => {
        resolveStaleDetail = resolve;
      }));
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.session?.session_id).toBe('session-1'));

    let refreshPromise: Promise<unknown> | undefined;
    act(() => {
      refreshPromise = result.current.refresh({ force: true });
    });
    await waitFor(() => expect(resolveStaleDetail).toBeTypeOf('function'));
    act(() => {
      result.current.observePendingInteraction(nextInteraction);
    });
    await act(async () => {
      resolveStaleDetail?.({ ...ready, pending_interaction: interaction });
      await refreshPromise;
    });

    expect(result.current.pendingInteraction).toBe(nextInteraction);
  });

  it('accepts a later authoritative detail that clears the interaction', async () => {
    const ready = {
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: null,
    } as SessionRecord;
    api.getSession.mockResolvedValue(ready);
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.session?.session_id).toBe('session-1'));
    act(() => {
      result.current.observePendingInteraction(interaction);
    });

    await act(async () => {
      await result.current.refresh({ force: true });
    });

    expect(result.current.pendingInteraction).toBeNull();
  });

  it('does not let an older detail resurrect an interaction cleared locally', async () => {
    const pending = {
      session_id: 'session-1',
      state: 'WAITING_INPUT',
      pending_interaction: interaction,
    } as SessionRecord;
    let resolveStaleDetail: ((session: SessionRecord) => void) | undefined;
    api.getSession
      .mockResolvedValueOnce(pending)
      .mockImplementationOnce(() => new Promise<SessionRecord>((resolve) => {
        resolveStaleDetail = resolve;
      }));
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.pendingInteraction).toBe(interaction));

    let refreshPromise: Promise<unknown> | undefined;
    act(() => {
      refreshPromise = result.current.refresh({ force: true });
    });
    await waitFor(() => expect(resolveStaleDetail).toBeTypeOf('function'));
    act(() => {
      result.current.clearPendingInteraction(interaction.interaction_id);
    });
    await act(async () => {
      resolveStaleDetail?.(pending);
      await refreshPromise;
    });

    expect(result.current.pendingInteraction).toBeNull();
  });

  it('does not let an older answer clear a newer live interaction', async () => {
    const nextInteraction = {
      ...interaction,
      interaction_id: 'interaction-2',
      tool_call_id: 'tool-call-2',
    } as PendingInteraction;
    api.getSession.mockResolvedValue({
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: interaction,
    } as SessionRecord);
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.pendingInteraction).toBe(interaction));

    act(() => {
      result.current.observePendingInteraction(nextInteraction);
    });
    let cleared = true;
    act(() => {
      cleared = result.current.clearPendingInteraction(interaction.interaction_id);
    });

    expect(cleared).toBe(false);
    expect(result.current.pendingInteraction).toBe(nextInteraction);
  });

  it('clears the interaction that the successful answer actually targeted', async () => {
    api.getSession.mockResolvedValue({
      session_id: 'session-1',
      state: 'READY',
      pending_interaction: interaction,
    } as SessionRecord);
    const { result } = renderHook(() => useSessionLifecycle('session-1'));
    await waitFor(() => expect(result.current.pendingInteraction).toBe(interaction));

    let cleared = false;
    act(() => {
      cleared = result.current.clearPendingInteraction(interaction.interaction_id);
    });

    expect(cleared).toBe(true);
    expect(result.current.pendingInteraction).toBeNull();
  });
});
