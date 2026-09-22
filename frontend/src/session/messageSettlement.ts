import type { UIMessage as SDKUIMessage } from 'ai';
import type { PendingInteraction } from '../types';
import { getMessageTurnId, platformTurnMetadata } from './messageIdentity';

const ASSISTANT_STREAM_SLOT_PREFIX = '__astrabox_assistant_stream_slot__:';

type SessionLike = {
  state?: string | null;
  current_turn_id?: string | null;
  last_turn_id?: string | null;
  last_turn_status?: string | null;
  last_turn_failure_phase?: string | null;
  delivery_state?: string | null;
  pending_interaction?: {
    interaction_id?: string | null;
    turn_id?: string | null;
  } | null;
};

const TERMINAL_TURN_STATUSES = new Set(['COMPLETED', 'FAILED', 'INTERRUPTED']);

function findAssistantMessageByTurn(
  messages: SDKUIMessage[],
  turnId: string,
): SDKUIMessage | null {
  const tid = String(turnId ?? '').trim();
  if (!tid) return null;
  return messages.find((m) => m.role === 'assistant' && getMessageTurnId(m) === tid) ?? null;
}

export function isTurnSettledInSessionSnapshot(
  session: SessionLike | null | undefined,
  turnId: string | null | undefined,
): boolean {
  const tid = String(turnId ?? '').trim();
  if (!session || !tid) return false;
  if (String(session.current_turn_id ?? '').trim() === tid) return false;
  if (String(session.pending_interaction?.turn_id ?? '').trim() === tid) return false;
  const lastTid = String(session.last_turn_id ?? '').trim();
  const lastStatus = String(session.last_turn_status ?? '').trim();
  return lastTid === tid && TERMINAL_TURN_STATUSES.has(lastStatus);
}

export function isTransportIdleStatus(
  transportStatus: string | null | undefined,
): boolean {
  const status = String(transportStatus ?? '').trim();
  return status !== 'submitted' && status !== 'streaming';
}

export function getTransportIdleTerminalTurnId(
  session: SessionLike | null | undefined,
  transportStatus: string | null | undefined,
  localTurnId: string | null | undefined,
): string | null {
  const tid = String(localTurnId ?? '').trim();
  if (!tid) return null;
  if (!isTransportIdleStatus(transportStatus)) {
    return null;
  }
  if (String(session?.last_turn_id ?? '').trim() === tid) {
    const deliveryState = String(session?.delivery_state ?? '').trim();
    const failurePhase = String(session?.last_turn_failure_phase ?? '').trim();
    if (deliveryState === 'NOT_RECEIVED' || failurePhase === 'pre_dispatch') {
      return null;
    }
  }
  return isTurnSettledInSessionSnapshot(session, tid) ? tid : null;
}

export function hasAssistantMessageForTurn(
  messages: SDKUIMessage[],
  turnId: string | null | undefined,
): boolean {
  const tid = String(turnId ?? '').trim();
  if (!tid) return false;
  return findAssistantMessageByTurn(messages, tid) !== null;
}

/** Give a known platform turn its own AI SDK assistant message before resume. */
export function ensureAssistantMessageForTurn(
  messages: SDKUIMessage[],
  turnId: string | null | undefined,
): SDKUIMessage[] {
  const tid = String(turnId ?? '').trim();
  if (!tid || hasAssistantMessageForTurn(messages, tid)) {
    return messages;
  }
  return [
    ...messages,
    {
      id: tid,
      role: 'assistant',
      metadata: platformTurnMetadata(tid),
      parts: [],
    },
  ];
}

function assistantStreamSlotId(sessionId: string): string {
  const sid = String(sessionId ?? '').trim();
  if (!sid) {
    throw new Error('assistant stream slot requires a session id');
  }
  return `${ASSISTANT_STREAM_SLOT_PREFIX}${sid}`;
}

/** Remove the transport-only assistant container from reader-facing history. */
export function withoutAssistantStreamSlots(
  messages: SDKUIMessage[],
  sessionId: string,
): SDKUIMessage[] {
  const slotId = assistantStreamSlotId(sessionId);
  if (!messages.some((message) => message.id === slotId)) {
    return messages;
  }
  return messages.filter((message) => message.id !== slotId);
}

/**
 * Give a standing session subscription a fresh AI SDK continuation target.
 * The server's start frame replaces its id with the accepted turn id; the
 * original empty slot stays transport-only and is removed after frames arrive.
 */
export function ensureAssistantStreamSlot(
  messages: SDKUIMessage[],
  sessionId: string,
): SDKUIMessage[] {
  const slotId = assistantStreamSlotId(sessionId);
  const last = messages[messages.length - 1];
  if (
    last?.id === slotId
    && last.role === 'assistant'
    && last.parts.length === 0
    && !getMessageTurnId(last)
  ) {
    return messages;
  }
  return [
    ...withoutAssistantStreamSlots(messages, sessionId),
    { id: slotId, role: 'assistant', parts: [] },
  ];
}

export function shouldAdoptAuthoritativeMessagesForLocalPendingInteraction(
  session: SessionLike | null | undefined,
  localPendingInteraction: PendingInteraction | null,
  authoritativePendingInteraction: PendingInteraction | null,
  hasOverlayTruth: boolean,
): boolean {
  const localInteractionId = String(localPendingInteraction?.interaction_id ?? '').trim();
  if (!localInteractionId) {
    return false;
  }
  if (hasOverlayTruth) {
    return false;
  }
  if (String(session?.state ?? '').trim() !== 'READY') {
    return false;
  }
  if (String(session?.current_turn_id ?? '').trim()) {
    return false;
  }
  if (String(session?.pending_interaction?.interaction_id ?? '').trim()) {
    return false;
  }
  const authoritativeInteractionId = String(
    authoritativePendingInteraction?.interaction_id ?? '',
  ).trim();
  return authoritativeInteractionId !== localInteractionId;
}
