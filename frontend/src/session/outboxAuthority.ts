import type { UIMessage as SDKUIMessage } from 'ai';

import type { MessageRecord, OutboxItem, SessionRecord } from '../types';
import { messageMatchesNativeInput } from './nativeInputProjection';

function getNormalizedCurrentFailureClientId(
  session: Pick<SessionRecord, 'delivery_state' | 'delivery_failure'>,
): string {
  if (String(session.delivery_state ?? '').trim() !== 'NOT_RECEIVED') {
    return '';
  }
  return String(session.delivery_failure?.client_message_id ?? '').trim();
}

function getDurableUserClientMessageIds(
  durableMessages: Array<Pick<MessageRecord, 'role' | 'client_message_id'>>,
): Set<string> {
  const ids = new Set<string>();
  durableMessages.forEach((message) => {
    if (message.role !== 'user') {
      return;
    }
    const clientMessageId = String(message.client_message_id ?? '').trim();
    if (clientMessageId) {
      ids.add(clientMessageId);
    }
  });
  return ids;
}

export function getDurableUserTurnIdForClientMessage(
  durableMessages: Array<Pick<MessageRecord, 'role' | 'client_message_id' | 'turn_id'>>,
  clientMessageId: string,
): string {
  const normalizedClientMessageId = String(clientMessageId ?? '').trim();
  if (!normalizedClientMessageId) {
    return '';
  }
  const match = durableMessages.find((message) => (
    message.role === 'user'
    && String(message.client_message_id ?? '').trim() === normalizedClientMessageId
  ));
  return String(match?.turn_id ?? '').trim();
}

export function markOutboxItemFailed(
  outbox: OutboxItem[],
  clientMessageId: string,
  failureReason: string,
): OutboxItem[] {
  const normalizedClientMessageId = String(clientMessageId ?? '').trim();
  if (!normalizedClientMessageId) {
    return outbox;
  }
  let changed = false;
  const next = outbox.map((item) => {
    if (
      String(item.client_message_id ?? '').trim() !== normalizedClientMessageId
      || item.status !== 'sending'
    ) {
      return item;
    }
    changed = true;
    return {
      ...item,
      status: 'failed' as const,
      failure_reason: failureReason,
    };
  });
  return changed ? next : outbox;
}

/**
 * `authoritative` is the server's accepted-but-unconsumed FIFO, so a row that
 * has left it was consumed — and consumption is the handover, which also emits
 * the frame that puts the message in the transcript. The two reach this client
 * by different routes, and a session read can win the race against the frame.
 * Dropping the row on the read alone would take the message off the queue while
 * nothing shows it yet, so `visible` holds the removal until the transcript
 * has it. Nothing lingers: only consumption shortens that FIFO.
 */
export function reconcileNativeInputOutbox(
  outbox: OutboxItem[],
  authoritative: OutboxItem[],
  visible: SDKUIMessage[] = [],
): OutboxItem[] {
  const matches = (left: OutboxItem, right: OutboxItem) => (
    (!!left.command_id && left.command_id === right.command_id)
    || (!!left.input_id && left.input_id === right.input_id)
    || left.client_message_id === right.client_message_id
  );
  const kept = outbox.filter((item) => (
    item.status !== 'accepted'
    || (!item.command_id && !item.input_id)
    || authoritative.some((candidate) => matches(item, candidate))
    || retainQueuedOutbox([item], visible).length > 0
  ));
  let next = kept;
  let changed = kept.length !== outbox.length;
  authoritative.forEach((item) => {
    const index = next.findIndex((candidate) => matches(candidate, item));
    if (index < 0) {
      next = [...next, item];
      changed = true;
      return;
    }
    const current = next[index];
    if (current.status !== 'sending' && current.status !== 'accepted') {
      return;
    }
    const merged = { ...current, ...item };
    if (Object.keys(merged).every((key) => (
      merged[key as keyof OutboxItem] === current[key as keyof OutboxItem]
    ))) {
      return;
    }
    next = next.map((candidate, candidateIndex) => (
      candidateIndex === index ? merged : candidate
    ));
    changed = true;
  });
  // Same reason as `reconcileOutboxWithSession`: an effect stores this, so
  // "nothing changed" has to be the same array or the effect never settles.
  return changed ? next : outbox;
}

/**
 * The queue holds what the transcript does not.
 *
 * This is the only rule that removes a queued row on its own. Every other
 * candidate — a turn ending, a receipt arriving — is a guess about when the
 * message became visible somewhere else, and a wrong guess drops it off both
 * surfaces: the queue row goes while the engine has not taken the message, and
 * nothing shows it until the next turn's history lands. Stating the invariant
 * instead also repairs a row whose handover frame never arrived.
 *
 * `messages` is what the user can see, so a row leaves exactly when a user
 * message carrying its identity is on screen. A failed row is not waiting on a
 * handover and stays until the user dismisses or retries it.
 */
export function retainQueuedOutbox(
  outbox: OutboxItem[],
  messages: SDKUIMessage[],
): OutboxItem[] {
  if (outbox.length === 0) {
    return outbox;
  }
  const remaining = outbox.filter((item) => {
    if (item.status === 'failed') {
      return true;
    }
    const clientMessageId = String(item.client_message_id ?? '').trim();
    if (!clientMessageId) {
      return true;
    }
    // Reuse the projection's identity test so "this row is that message" means
    // one thing across the session view.
    const identity = {
      clientMessageId,
      inputId: String(item.input_id ?? '').trim() || clientMessageId,
      platformTurnId: '',
      responseMessageId: '',
      content: '',
    };
    return !messages.some((message) => messageMatchesNativeInput(message, identity));
  });
  return remaining.length === outbox.length ? outbox : remaining;
}

export function reconcileOutboxWithSession(
  outbox: OutboxItem[],
  session: Pick<SessionRecord, 'delivery_state' | 'delivery_failure'>,
  durableMessages: Array<Pick<MessageRecord, 'role' | 'client_message_id'>> = [],
): OutboxItem[] {
  if (outbox.length === 0) {
    return outbox;
  }

  const currentFailureClientId = getNormalizedCurrentFailureClientId(session);
  const durableUserClientIds = getDurableUserClientMessageIds(durableMessages);

  const remaining = outbox.filter((item) => {
    const clientMessageId = String(item.client_message_id ?? '').trim();
    if (!clientMessageId) {
      return true;
    }
    if (currentFailureClientId && clientMessageId === currentFailureClientId) {
      return true;
    }
    if (durableUserClientIds.has(clientMessageId)) {
      return false;
    }
    return true;
  });
  // Removing nothing must be expressible: this runs in an effect, and its
  // caller stores the result. A fresh array for an unchanged outbox is a state
  // change, so an effect that re-runs on an unstable dependency would write a
  // new value on every render and never settle — React unwinds that as
  // "Maximum update depth exceeded" and the session subtree dies mid-turn.
  return remaining.length === outbox.length ? outbox : remaining;
}
