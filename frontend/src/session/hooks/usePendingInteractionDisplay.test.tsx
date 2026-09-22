// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';
import type { UIMessage } from 'ai';

import type { PendingInteraction } from '../../types';
import { usePendingInteractionDisplay } from './usePendingInteractionDisplay';

afterEach(() => {
  cleanup();
});

const interaction = {
  interaction_id: 'interaction-1',
  turn_id: 'turn-1',
  tool_call_id: 'tool-call-1',
  tool_name: 'Write',
  presentation: 'tool_approval',
} as PendingInteraction;

const messages = [{
  id: 'response-1',
  metadata: { turn_id: 'turn-1' },
  role: 'assistant',
  parts: [
    {
      type: 'dynamic-tool',
      toolCallId: 'tool-call-1',
      toolName: 'Write',
      state: 'approval-requested',
      input: { file_path: 'notes.txt' },
    },
    { type: 'data-interaction', data: interaction },
  ],
}] as unknown as UIMessage[];

function renderPendingInteraction(sessionPendingInteraction: PendingInteraction | null) {
  return renderHook(() => usePendingInteractionDisplay({
    messages,
    sessionPendingInteraction,
    isTerminated: false,
    interactionSubmitting: false,
    handleInteractionSubmit: vi.fn().mockResolvedValue(undefined),
  }));
}

describe('usePendingInteractionDisplay', () => {
  it('does not promote historical message metadata into current session state', () => {
    const { result } = renderPendingInteraction(null);

    expect(result.current.visiblePendingInteraction).toBeNull();
    expect(result.current.hasPendingInteraction).toBe(false);
  });

  it('renders authoritative session state using matching message tool details', () => {
    const { result } = renderPendingInteraction(interaction);

    expect(result.current.visiblePendingInteraction).toBe(interaction);
    expect(result.current.pendingTool).toEqual({
      toolCallId: 'tool-call-1',
      toolName: 'Write',
      input: { file_path: 'notes.txt' },
    });
  });
});
