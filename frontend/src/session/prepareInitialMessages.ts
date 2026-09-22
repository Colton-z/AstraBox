import type { UIMessage as SDKUIMessage, UIMessagePart } from 'ai';
import type { ActiveTurnOverlay } from '../api';
import type { MessageRecord, PendingInteraction, SessionRecord } from '../types';
import {
  attachPendingInteractionToExistingTool,
  convertRecordToSDKMessage,
  blocksToSDKParts,
} from './hooks/useInitialMessages';
import { getMessageTurnId, platformTurnMetadata } from './messageIdentity';
import { getPendingToolCallId, shouldBindToolPermissionBlock } from './pendingToolMatch';

type AnyPart = UIMessagePart<any, any>;
/**
 * Determine if a durable user record should be suppressed because it belongs
 * to a delivery-failed turn. The delivery_failure field from session detail is
 * the authoritative source for this decision.
 */
function shouldSuppressForDeliveryFailure(
  record: MessageRecord,
  session: Pick<SessionRecord, 'delivery_state' | 'delivery_failure'>,
): boolean {
  if (record.role !== 'user') return false;
  if (String(session.delivery_state ?? '').trim() !== 'NOT_RECEIVED') return false;
  if (!session.delivery_failure) return false;
  const failureTurnId = String(session.delivery_failure.turn_id ?? '').trim();
  const failureClientMessageId = String(session.delivery_failure.client_message_id ?? '').trim();
  const recordTurnId = String(record.turn_id ?? '').trim();
  const recordClientMessageId = String(record.client_message_id ?? '').trim();
  return (
    (!!failureTurnId && recordTurnId === failureTurnId) ||
    (!!failureClientMessageId && recordClientMessageId === failureClientMessageId)
  );
}

/**
 * Convert an ActiveTurnOverlay to an SDK message. Uses blocksToSDKParts for
 * block conversion, consistent with how durable records are converted.
 */
function convertOverlayToSDKMessage(
  record: MessageRecord,
  pi?: PendingInteraction | null,
): SDKUIMessage {
  const matchingPi =
    pi && record.role === 'assistant' && record.turn_id === pi.turn_id
      ? pi
      : null;
  const parts: AnyPart[] =
    record.role === 'user'
      ? [{ type: 'text' as const, text: record.content || '' }]
      : blocksToSDKParts(record.blocks || [], matchingPi);

  return {
    id: record.message_id,
    metadata: platformTurnMetadata(record.turn_id),
    client_message_id: record.client_message_id ?? null,
    role: record.role,
    parts,
  } as SDKUIMessage;
}

/**
 * Core pure function: takes durable records + overlay + session and produces
 * the initial messages for useChat.
 *
 * Design doc reference: section 5.2
 *
 * Steps:
 * 1. Filter out delivery-failed user messages from durable records
 * 2. Convert durable records to SDK messages
 * 3. Merge overlay by message_id while preserving its platform turn_id
 * 4. Attach pending interaction metadata to existing tool blocks if possible
 * 5. Assert that assistant message ids are unique.
 */
export function prepareInitialMessages(
  durableRecords: MessageRecord[],
  overlay: ActiveTurnOverlay | null,
  session: Pick<SessionRecord, 'delivery_state' | 'delivery_failure' | 'pending_interaction'>,
): SDKUIMessage[] {
  // Step 1+2: Filter delivery-failed user messages and convert durable records
  let messages = durableRecords
    .filter((record) => !shouldSuppressForDeliveryFailure(record, session))
    .map((record) => convertRecordToSDKMessage(record, session.pending_interaction ?? null));

  // Step 3: Merge the volatile overlay into the same native message identity.
  if (overlay) {
    const overlayRecords = overlay.messages?.length
      ? overlay.messages
      : [overlay.message];
    for (const record of overlayRecords) {
      const overlaySDK = convertOverlayToSDKMessage(
        record,
        session.pending_interaction ?? null,
      );
      const existingIndex = messages.findIndex(
        (message) => (
          message.role === record.role
          && message.id === record.message_id
        ),
      );
      if (existingIndex >= 0) {
        messages[existingIndex] = overlaySDK;
      } else {
        messages.push(overlaySDK);
      }
    }
  }

  // Step 4: Attach pending interaction metadata to existing tool blocks if possible
  if (session.pending_interaction) {
    messages = attachPendingInteractionToExistingTool(messages, session.pending_interaction);
  }

  // A duplicated assistant message id would render the same engine response twice.
  if (import.meta.env.DEV) {
    const assistantMessageIds = new Set<string>();
    for (const msg of messages) {
      if (msg.role !== 'assistant') continue;
      const messageId = String(msg.id ?? '').trim();
      if (!messageId) continue;
      if (assistantMessageIds.has(messageId)) {
        console.error(
          `[prepareInitialMessages] INV-1 violation: duplicate assistant message_id "${messageId}"`,
        );
      }
      assistantMessageIds.add(messageId);
    }

    // A delivery-failed user message belongs in either the timeline or outbox,
    // never both.
    if (session.delivery_failure) {
      const failedTurnId = String(session.delivery_failure.turn_id ?? '').trim();
      const failedCMId = String(session.delivery_failure.client_message_id ?? '').trim();
      const stillInTimeline = messages.some((msg) => {
        if (msg.role !== 'user') return false;
        const turnId = getMessageTurnId(msg);
        const msgId = String(msg.id ?? '').trim();
        return (failedTurnId && turnId === failedTurnId) || (failedCMId && msgId === failedCMId);
      });
      if (stillInTimeline) {
        console.error(
          `[prepareInitialMessages] INV-2 violation: delivery-failed user message (turn=${failedTurnId}) still in timeline — should be suppressed to outbox`,
        );
      }
    }
  }

  return messages;
}
