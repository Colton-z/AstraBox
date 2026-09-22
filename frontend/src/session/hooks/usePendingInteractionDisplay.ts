import { useCallback, useEffect, useMemo, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { InteractionResponse, PendingInteraction } from '../../types';
import {
  getVisiblePendingInteraction,
  shouldReleaseSuppressedPendingInteraction,
} from '../pendingInteractionState';
import { buildPendingToolInfo } from '../pendingToolMatch';

// Session state owns whether an interaction is current. Message metadata is
// retained only to locate the matching tool.
export function usePendingInteractionDisplay({
  messages,
  sessionPendingInteraction,
  isTerminated,
  interactionSubmitting,
  handleInteractionSubmit,
}: {
  messages: SDKUIMessage[];
  sessionPendingInteraction: PendingInteraction | null;
  isTerminated: boolean;
  interactionSubmitting: boolean;
  handleInteractionSubmit: (response: InteractionResponse) => Promise<void>;
}) {
  const [suppressedPIId, setSuppressedPIId] = useState<string | null>(null);
  const authoritativePendingInteraction = useMemo(
    () => getVisiblePendingInteraction(sessionPendingInteraction, suppressedPIId),
    [sessionPendingInteraction, suppressedPIId],
  );
  const visiblePendingInteraction = useMemo(
    () => isTerminated ? null : authoritativePendingInteraction,
    [authoritativePendingInteraction, isTerminated],
  );
  const hasPendingInteraction = !!visiblePendingInteraction;
  const pendingTool = buildPendingToolInfo(visiblePendingInteraction, messages);
  const pendingToolCallId = String(visiblePendingInteraction?.tool_call_id ?? '').trim()
    || String(pendingTool?.toolCallId ?? '').trim();
  const pendingConversationScrollKey = [
    String(visiblePendingInteraction?.interaction_id ?? '').trim(),
    messages.length,
    pendingToolCallId,
  ].join(':');
  const handleVisibleInteractionSubmit = useCallback(async (response: InteractionResponse) => {
    const interactionId = String(visiblePendingInteraction?.interaction_id ?? '').trim();
    if (interactionId) {
      setSuppressedPIId(interactionId);
    }
    await handleInteractionSubmit(response);
  }, [handleInteractionSubmit, visiblePendingInteraction?.interaction_id]);

  useEffect(() => {
    if (!suppressedPIId) return;
    if (shouldReleaseSuppressedPendingInteraction(
      suppressedPIId,
      sessionPendingInteraction?.interaction_id,
      interactionSubmitting,
    )) {
      setSuppressedPIId(null);
    }
  }, [
    suppressedPIId,
    sessionPendingInteraction?.interaction_id,
    interactionSubmitting,
  ]);
  return {
    visiblePendingInteraction,
    hasPendingInteraction,
    pendingTool,
    pendingToolCallId,
    pendingConversationScrollKey,
    handleVisibleInteractionSubmit,
  };
}
