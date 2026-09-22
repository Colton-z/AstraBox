// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UIMessageStreamError, type ChatOnFinishCallback, type UIMessage } from 'ai';

import type { SessionRecord } from '../../types';
import { appendTurnInput } from '../../api';

const chat = vi.hoisted(() => ({
  options: null as Record<string, unknown> | null,
  resumeStream: vi.fn(),
  setMessages: vi.fn(),
  stop: vi.fn(),
  clearError: vi.fn(),
}));

vi.mock('@ai-sdk/react', () => ({
  useChat: (options: Record<string, unknown>) => {
    chat.options = options;
    return {
      messages: [],
      resumeStream: chat.resumeStream,
      setMessages: chat.setMessages,
      status: 'ready',
      stop: chat.stop,
      error: undefined,
      clearError: chat.clearError,
      addToolApprovalResponse: vi.fn(),
      addToolOutput: vi.fn(),
    };
  },
}));

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: vi.fn() },
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../../api', () => ({
  MODULE_BASE: '/api/v1',
  answerPendingInteraction: vi.fn(),
  appendTurnInput: vi.fn(),
}));

import { useSessionChat } from './useSessionChat';

afterEach(() => {
  cleanup();
  chat.options = null;
  vi.clearAllMocks();
});

describe('useSessionChat under an unstable durableRecords identity', () => {
  // SessionPage hands this hook a new `durableRecords: []` array on every render
  // whenever the loaded first page does not belong to the current route — the
  // window around a history refetch. That is an identity change, so every
  // effect keyed on it re-runs each render. An effect that also writes state
  // unconditionally then never settles: React unwinds it as "Maximum update
  // depth exceeded", the session subtree dies, and the live view stops
  // rendering the turn that is running.
  //
  // The queue is what makes it unconditional: the outbox reconciler returns
  // its input unchanged only for an empty outbox, so the loop opens exactly
  // when a message is waiting — which is every send.
  it('settles instead of looping while a message sits in the queue', async () => {
    vi.mocked(appendTurnInput).mockResolvedValue({
      session_id: 'session-1',
      command_id: 'command-loop',
      client_message_id: 'client-loop',
      input_id: 'input-loop',
      status: 'delivered',
    });
    const session = {
      session_id: 'session-1',
      user_id: 'user-1',
      template_name: 'claude-code',
      state: 'READY',
      current_turn_id: null,
      engine_kind: 'claude_code',
      pending_interaction: null,
    } as SessionRecord;
    const refresh = vi.fn().mockResolvedValue({ detail: session });

    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useSessionChat({
        sessionId: 'session-1',
        session,
        // The identity that changes on every render.
        durableRecords: [],
        initialMessages: [],
        overlay: null,
        sessionFrameSeq: 0,
        needsResume: false,
        lifecycleState: 'ready',
        liveSubscriptionEnabled: false,
        pendingInteraction: null,
        permissionModeRef: { current: 'default' },
        refresh,
        refreshAuthoritativeHistory: vi.fn(),
        observePendingInteraction: vi.fn(),
        clearPendingInteraction: vi.fn(() => true),
        interrupt: vi.fn(),
        recover: vi.fn(),
      });
    });

    await act(async () => {
      result.current.sendClientMessageNow('client-loop', 'queued while history refetches');
    });
    await waitFor(() => expect(result.current.outbox).toHaveLength(1));

    const settled = renders;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    // A render count that keeps climbing with no input is the loop.
    expect(renders - settled).toBeLessThan(5);
    expect(result.current.outbox).toHaveLength(1);
  });
});

describe('useSessionChat structural stream recovery', () => {
  it('treats child-run frames as projection invalidations, not message content', () => {
    const session = {
      session_id: 'session-1',
      user_id: 'user-1',
      template_name: 'claude-code',
      state: 'READY',
      current_turn_id: null,
      engine_kind: 'claude_code',
      pending_interaction: null,
    } as SessionRecord;
    const { result } = renderHook(() => useSessionChat({
      sessionId: 'session-1',
      session,
      durableRecords: [],
      initialMessages: [],
      overlay: null,
      sessionFrameSeq: 0,
      needsResume: false,
      lifecycleState: 'ready',
      liveSubscriptionEnabled: false,
      pendingInteraction: null,
      permissionModeRef: { current: 'default' },
      refresh: vi.fn().mockResolvedValue({ detail: session }),
      refreshAuthoritativeHistory: vi.fn(),
      observePendingInteraction: vi.fn(),
      clearPendingInteraction: vi.fn(() => true),
      interrupt: vi.fn(),
      recover: vi.fn(),
    }));

    expect(result.current.childRunRevision).toBe(0);
    act(() => {
      (chat.options?.onData as (part: unknown) => void)({
        type: 'data-child-runs-changed',
        transient: true,
        data: { frameSeq: 17 },
      });
    });
    expect(result.current.childRunRevision).toBe(18);
    expect(result.current.messages).toEqual([]);
    expect(() => {
      (chat.options?.onData as (part: unknown) => void)({
        type: 'data-child-runs-changed',
        transient: true,
        data: { frameSeq: '17' },
      });
    }).toThrow('malformed child-run invalidation cursor');
  });

  // A submitted input remains platform-owned until `data-input-consumed`.
  // Before that handover it belongs only in the queue; projecting it into the
  // agent-owned transcript would show the same message on both surfaces.
  it('keeps a submitted input in the queue until the engine consumes it', async () => {
    vi.mocked(appendTurnInput).mockResolvedValue({
      session_id: 'session-1',
      command_id: 'command-plan',
      client_message_id: 'client-plan',
      input_id: 'input-plan',
      status: 'delivered',
    });
    const session = {
      session_id: 'session-1',
      user_id: 'user-1',
      template_name: 'claude-code',
      state: 'READY',
      current_turn_id: null,
      engine_kind: 'claude_code',
      pending_interaction: null,
    } as SessionRecord;
    const permissionModeRef = { current: 'plan' };
    const refresh = vi.fn().mockResolvedValue({ detail: session });
    const refreshAuthoritativeHistory = vi.fn();
    const observePendingInteraction = vi.fn();
    const clearPendingInteraction = vi.fn(() => true);
    const interrupt = vi.fn();
    const recover = vi.fn();
    const durableRecords: [] = [];
    const initialMessages: [] = [];

    const { result } = renderHook(() => useSessionChat({
      sessionId: 'session-1',
      session,
      durableRecords,
      initialMessages,
      overlay: null,
      sessionFrameSeq: 0,
      needsResume: false,
      lifecycleState: 'ready',
      liveSubscriptionEnabled: false,
      pendingInteraction: null,
      permissionModeRef,
      refresh,
      refreshAuthoritativeHistory,
      observePendingInteraction,
      clearPendingInteraction,
      interrupt,
      recover,
    }));

    act(() => {
      result.current.sendClientMessageNow('client-plan', 'plan prompt');
    });

    // Held by the platform: the queue owns it, the transcript does not show it.
    await waitFor(() => expect(result.current.outbox).toEqual([
      expect.objectContaining({ client_message_id: 'client-plan', text: 'plan prompt' }),
    ]));
    expect(result.current.messages).toEqual([]);
    expect(appendTurnInput).toHaveBeenCalledWith('session-1', {
      client_message_id: 'client-plan',
      content: 'plan prompt',
      permission_mode: 'plan',
    });

    // Accepted is still the platform holding it — the engine has not read it.
    act(() => {
      (chat.options?.onData as (part: unknown) => void)({
        type: 'data-turn-accepted',
        data: { clientMessageId: 'client-plan', turnId: 'turn-1' },
      });
    });
    expect(result.current.messages).toEqual([]);
    expect(result.current.outbox).toHaveLength(1);

    // Consumption is the handover: one surface to the other, never both.
    act(() => {
      (chat.options?.onData as (part: unknown) => void)({
        type: 'data-input-consumed',
        data: {
          inputId: 'client-plan',
          responseMessageId: 'assistant-1',
          clientMessageId: 'client-plan',
          content: 'plan prompt',
        },
      });
    });
    await waitFor(() => expect(result.current.messages).toEqual([
      expect.objectContaining({
        id: 'client-plan:user',
        client_message_id: 'client-plan',
        role: 'user',
        parts: [{ type: 'text', text: 'plan prompt' }],
      }),
    ]));
    expect(result.current.outbox).toEqual([]);
  });

  it('rehydrates the authoritative tool tree before reopening the session stream', async () => {
    let finishFirstSubscription: (() => void) | undefined;
    chat.resumeStream
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        finishFirstSubscription = resolve;
      }))
      .mockImplementation(() => new Promise<void>(() => {}));

    const session = {
      session_id: 'session-1',
      user_id: 'user-1',
      template_name: 'claude-code',
      state: 'PROCESSING',
      current_turn_id: 'turn-1',
      engine_kind: 'claude_code',
      pending_interaction: null,
    } as SessionRecord;
    const refresh = vi.fn().mockResolvedValue({ detail: session });
    const refreshAuthoritativeHistory = vi.fn().mockResolvedValue({
      ownerSessionId: 'session-1',
      durableRecords: [],
      overlay: {
        turn_id: 'turn-1',
        message: {
          session_id: 'session-1',
          message_id: 'turn-1',
          turn_id: 'turn-1',
          role: 'assistant',
          content: '',
          blocks: [{
            type: 'tool_use',
            id: 'call-1',
            name: 'Write',
            input: { file_path: 'proof.txt' },
          }],
        },
        resume_cursor: { turn_id: 'turn-1', frame_seq: 52 },
      },
      sessionFrameSeq: 55,
      pendingInteraction: null,
    });

    renderHook(() => useSessionChat({
      sessionId: 'session-1',
      session,
      durableRecords: [],
      initialMessages: [],
      overlay: null,
      sessionFrameSeq: 46,
      needsResume: true,
      lifecycleState: 'busy',
      liveSubscriptionEnabled: true,
      pendingInteraction: null,
      permissionModeRef: { current: 'default' },
      refresh,
      refreshAuthoritativeHistory,
      observePendingInteraction: vi.fn(),
      clearPendingInteraction: vi.fn(() => true),
      interrupt: vi.fn(),
      recover: vi.fn(),
    }));

    await waitFor(() => expect(chat.resumeStream).toHaveBeenCalledTimes(1));
    const onError = chat.options?.onError as ((error: Error) => void) | undefined;
    expect(onError).toBeTypeOf('function');

    act(() => {
      onError?.(new UIMessageStreamError({
        chunkType: 'tool-invocation',
        chunkId: 'call-1',
        message: 'No tool invocation found for tool call ID "call-1".',
      }));
    });

    await waitFor(() => expect(refreshAuthoritativeHistory).toHaveBeenCalledOnce());
    await waitFor(() => expect(chat.setMessages).toHaveBeenCalledWith([
      expect.objectContaining({
        id: 'turn-1',
        role: 'assistant',
        parts: [expect.objectContaining({
          type: 'dynamic-tool',
          toolCallId: 'call-1',
          state: 'input-available',
        })],
      }),
    ]));

    await act(async () => {
      finishFirstSubscription?.();
      await Promise.resolve();
    });
    await waitFor(() => expect(chat.resumeStream).toHaveBeenCalledTimes(2));
    expect(chat.clearError).toHaveBeenCalled();
  });

  it('reopens only after the previous terminal response has settled', async () => {
    let settleFirstSubscription: (() => void) | undefined;
    chat.resumeStream
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        settleFirstSubscription = resolve;
      }))
      .mockImplementation(() => new Promise<void>(() => {}));

    const session = {
      session_id: 'session-1',
      user_id: 'user-1',
      template_name: 'claude-code',
      state: 'READY',
      current_turn_id: null,
      engine_kind: 'claude_code',
      pending_interaction: null,
    } as SessionRecord;

    renderHook(() => useSessionChat({
      sessionId: 'session-1',
      session,
      durableRecords: [],
      initialMessages: [],
      overlay: null,
      sessionFrameSeq: 41,
      needsResume: false,
      lifecycleState: 'ready',
      liveSubscriptionEnabled: true,
      pendingInteraction: null,
      permissionModeRef: { current: 'default' },
      refresh: vi.fn().mockResolvedValue({ detail: session }),
      refreshAuthoritativeHistory: vi.fn(),
      observePendingInteraction: vi.fn(),
      clearPendingInteraction: vi.fn(() => true),
      interrupt: vi.fn(),
      recover: vi.fn(),
    }));

    await waitFor(() => expect(chat.resumeStream).toHaveBeenCalledTimes(1));
    const finishedMessage: UIMessage = {
      id: 'response-1',
      role: 'assistant',
      parts: [{ type: 'text', text: 'The response is complete.' }],
    };
    act(() => {
      (chat.options?.onFinish as ChatOnFinishCallback<UIMessage>)({
        message: finishedMessage,
        messages: [finishedMessage],
        isAbort: false,
        isDisconnect: false,
        isError: false,
      });
    });
    expect(chat.resumeStream).toHaveBeenCalledTimes(1);

    await act(async () => {
      settleFirstSubscription?.();
      await Promise.resolve();
    });
    await waitFor(() => expect(chat.resumeStream).toHaveBeenCalledTimes(2));
  });

  it('reopens a dropped subscription on the backoff without waiting for a status change', async () => {
    vi.useFakeTimers();
    try {
      chat.resumeStream
        .mockImplementationOnce(async () => {
          (chat.options?.onError as (error: Error) => void)?.(new TypeError('Failed to fetch'));
        })
        .mockImplementation(() => new Promise<void>(() => {}));

      const session = {
        session_id: 'session-1',
        user_id: 'user-1',
        template_name: 'claude-code',
        state: 'BUSY',
        current_turn_id: 'turn-1',
        engine_kind: 'claude_code',
        pending_interaction: null,
      } as SessionRecord;

      renderHook(() => useSessionChat({
        sessionId: 'session-1',
        session,
        durableRecords: [],
        initialMessages: [],
        overlay: null,
        sessionFrameSeq: 41,
        needsResume: true,
        lifecycleState: 'busy',
        liveSubscriptionEnabled: true,
        pendingInteraction: null,
        permissionModeRef: { current: 'default' },
        refresh: vi.fn().mockResolvedValue({ detail: session }),
        refreshAuthoritativeHistory: vi.fn(),
        observePendingInteraction: vi.fn(),
        clearPendingInteraction: vi.fn(() => true),
        interrupt: vi.fn(),
        recover: vi.fn(),
      }));

      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(chat.resumeStream).toHaveBeenCalledTimes(1);

      // The transport's status is unchanged ('ready' throughout in this
      // harness): the only thing that can bring the second attempt is the
      // page's own backoff, which for a dropped turn is 1s per attempt made.
      await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
      expect(chat.resumeStream).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      expect(chat.resumeStream).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

});

describe('useSessionChat interrupt settling', () => {
  const baseSession = (turnId: string | null) => ({
    session_id: 'session-1',
    user_id: 'user-1',
    template_name: 'claude-code',
    state: 'PROCESSING',
    current_turn_id: turnId,
    engine_kind: 'claude_code',
    pending_interaction: null,
  } as SessionRecord);

  const baseProps = (turnId: string | null) => ({
    sessionId: 'session-1',
    session: baseSession(turnId),
    durableRecords: [] as never[],
    initialMessages: [] as never[],
    overlay: null,
    sessionFrameSeq: 0,
    needsResume: false,
    lifecycleState: 'busy',
    liveSubscriptionEnabled: false,
    pendingInteraction: null,
    permissionModeRef: { current: 'default' },
    refresh: vi.fn().mockResolvedValue({ detail: baseSession(turnId) }),
    refreshAuthoritativeHistory: vi.fn(),
    observePendingInteraction: vi.fn(),
    clearPendingInteraction: vi.fn(() => true),
    interrupt: vi.fn().mockResolvedValue(undefined),
    recover: vi.fn(),
  });

  it('clears the stopping state when the projection moves to a successor turn', async () => {
    // A stop that hands the session to a queued next input settles into a new
    // busy turn, never into idle — the consumed boundary frame can fall into
    // the stream-reader gap, so the projection's turn change must clear the
    // stopping paint on its own.
    const { result, rerender } = renderHook(
      (props: ReturnType<typeof baseProps>) => useSessionChat(props),
      { initialProps: baseProps('turn-stopped') },
    );

    await act(async () => {
      await result.current.handleStopGeneration();
    });
    expect(result.current.isInterruptSettling).toBe(true);

    rerender(baseProps('turn-handoff'));
    await waitFor(() => expect(result.current.isInterruptSettling).toBe(false));
  });

  it('keeps the stopping state while the stopped turn is still the active one', async () => {
    const { result, rerender } = renderHook(
      (props: ReturnType<typeof baseProps>) => useSessionChat(props),
      { initialProps: baseProps('turn-stopped') },
    );

    await act(async () => {
      await result.current.handleStopGeneration();
    });
    expect(result.current.isInterruptSettling).toBe(true);

    rerender(baseProps('turn-stopped'));
    expect(result.current.isInterruptSettling).toBe(true);
  });
});
