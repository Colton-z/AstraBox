import type { UIMessage } from 'ai';
import { inputMetadata } from './sessionMessages';

export interface ConversationItem {
  key: string;
  message: UIMessage;
  /** A display slice, retaining the logical response's identity and metadata. */
  responseSegment?: 'preceding' | 'last';
  acceptedBy?: string;
}

/** Place loaded input bubbles at the runtime's recorded consumption boundaries. */
export function conversationTimeline(messages: readonly UIMessage[]): ConversationItem[] {
  const inputs = new Map(messages.filter((message) => message.role === 'user')
    .map((message) => [inputMetadata(message).input_id, message]));
  const moved = new Set<string>();
  const items: ConversationItem[] = [];
  for (const message of messages) {
    if (message.role !== 'assistant') {
      items.push({ key: message.id, message });
      continue;
    }
    let start = 0;
    let key = message.id;
    for (let index = 0; index < message.parts.length; index += 1) {
      const part = message.parts[index];
      if (part.type !== 'data-input-consumed') continue;
      const data = part.data as { inputId: string };
      const input = inputs.get(data.inputId);
      if (!input) continue;
      items.push({ key, message: { ...message, parts: message.parts.slice(start, index) },
        responseSegment: 'preceding' });
      items.push({ key: input.id, message: input, acceptedBy: message.id });
      moved.add(input.id);
      start = index + 1;
      key = `${message.id}:after:${data.inputId}`;
    }
    items.push({ key, message: start ? { ...message, parts: message.parts.slice(start) } : message,
      responseSegment: start ? 'last' : undefined });
  }
  return items.filter((item) => item.message.role !== 'user' || item.acceptedBy || !moved.has(item.message.id));
}
