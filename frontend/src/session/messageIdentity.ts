import type { UIMessage as SDKUIMessage } from 'ai';

type SessionMessageMetadata = {
  turn_id?: unknown;
};

export function getMessageTurnId(
  message: SDKUIMessage | null | undefined,
): string {
  if (!message || !message.metadata || typeof message.metadata !== 'object') {
    return '';
  }
  return String((message.metadata as SessionMessageMetadata).turn_id ?? '').trim();
}

export function platformTurnMetadata(turnId: string): SessionMessageMetadata {
  const normalized = String(turnId ?? '').trim();
  if (!normalized) {
    throw new Error('message metadata requires a platform turn_id');
  }
  return { turn_id: normalized };
}

/**
 * React keys for the transcript, stable across the live→durable swap.
 *
 * A streaming turn arrives as ONE assistant message whose id is the TURN id,
 * because the platform opens the stream before the engine has produced a
 * message to name. The same turn settles as one or more durable messages
 * carrying their own ids, so keying the list on `message.id` re-keys every
 * assistant message at the moment its answer completes: React discards the
 * element and builds a new one, replaying `astra-enter` even though the
 * transcript still represents the same turn.
 *
 * What survives the swap for an assistant is its position in its turn, so that
 * is the key. A user row keeps `message.id`: the durable one is derived from
 * the same client message id the live projection uses, so it is already stable
 * — and keying it by turn would introduce the very remount this removes, since
 * a queued input carries no turn id until the engine takes it.
 */
export function conversationMessageKey(
  message: SDKUIMessage,
  ordinalInTurn: number,
): string {
  const turnId = getMessageTurnId(message);
  if (message.role !== 'assistant' || !turnId) {
    return String(message.id);
  }
  return `assistant:${turnId}:${ordinalInTurn}`;
}

/** The keys for a whole transcript, in order. */
export function conversationMessageKeys(messages: SDKUIMessage[]): string[] {
  const seen = new Map<string, number>();
  return messages.map((message) => {
    const turnId = getMessageTurnId(message);
    const bucket = `${message.role}:${turnId}`;
    const ordinal = seen.get(bucket) ?? 0;
    seen.set(bucket, ordinal + 1);
    return conversationMessageKey(message, ordinal);
  });
}
