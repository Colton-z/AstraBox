import { useMemo } from 'react';
import type { UIMessage } from 'ai';
import type { PendingInteraction } from '../../types';
import { pendingToolCallId } from '../pendingToolMatch';

/**
 * The interaction the page shows in place of its composer, and the tool call
 * in the transcript it waits on. A terminated session waits on nobody.
 */
export function usePendingInteractionDisplay({
  messages,
  pendingInteraction,
  isTerminated,
}: {
  messages: UIMessage[];
  pendingInteraction: PendingInteraction | null;
  isTerminated: boolean;
}) {
  const visiblePendingInteraction = isTerminated ? null : pendingInteraction;
  const toolCallId = useMemo(
    () => pendingToolCallId(visiblePendingInteraction, messages),
    [messages, visiblePendingInteraction],
  );
  const pendingConversationScrollKey = [
    String(visiblePendingInteraction?.interaction_id ?? '').trim(),
    messages.length,
    toolCallId,
  ].join(':');
  return {
    visiblePendingInteraction,
    hasPendingInteraction: visiblePendingInteraction !== null,
    pendingToolCallId: toolCallId,
    pendingConversationScrollKey,
  };
}
