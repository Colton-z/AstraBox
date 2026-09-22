// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook, waitFor } from '@testing-library/react';

import type { PendingInteraction } from '../../types';

const api = vi.hoisted(() => ({
  getHistoryBlocks: vi.fn(),
}));

vi.mock('../../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../api')>(),
  ...api,
}));

import { useFirstPageMessages } from './useFirstPageMessages';

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

describe('useFirstPageMessages pending interaction bootstrap', () => {
  it('applies the first-page authority before exposing the bootstrap as loaded', async () => {
    api.getHistoryBlocks.mockResolvedValue({
      messages: [],
      has_more: false,
      active_turn_overlay: null,
      session_frame_seq: 17,
      pending_interaction: interaction,
      paging_mode: 'blocks',
      next_cursor: null,
      block_count: 0,
    });
    const applyPendingInteraction = vi.fn();
    const { result } = renderHook(() => useFirstPageMessages(
      'session-1',
      applyPendingInteraction,
    ));

    await waitFor(() => expect(result.current.loadedOnce).toBe(true));

    expect(applyPendingInteraction).toHaveBeenCalledOnce();
    expect(applyPendingInteraction).toHaveBeenCalledWith(interaction);
    expect(result.current.sessionFrameSeq).toBe(17);
  });

  it('does not replay a later history refresh as a bootstrap observation', async () => {
    api.getHistoryBlocks
      .mockResolvedValueOnce({
        messages: [],
        has_more: false,
        active_turn_overlay: null,
        session_frame_seq: 17,
        pending_interaction: interaction,
        paging_mode: 'blocks',
        next_cursor: null,
        block_count: 0,
      })
      .mockResolvedValueOnce({
        messages: [],
        has_more: false,
        active_turn_overlay: null,
        session_frame_seq: 18,
        pending_interaction: null,
        paging_mode: 'blocks',
        next_cursor: null,
        block_count: 0,
      });
    const applyPendingInteraction = vi.fn();
    const { result } = renderHook(() => useFirstPageMessages(
      'session-1',
      applyPendingInteraction,
    ));
    await waitFor(() => expect(result.current.loadedOnce).toBe(true));

    await result.current.refetch();

    expect(applyPendingInteraction).toHaveBeenCalledTimes(1);
  });
});
