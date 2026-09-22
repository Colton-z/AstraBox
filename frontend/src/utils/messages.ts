import type {
  ContentBlock,
  MessageRecord,
  PendingInteraction,
} from '../types';

export type UIMessage = MessageRecord & {
  id: string;
  streaming?: boolean;
  blocks?: ContentBlock[];
  _turnBlockOffset?: number;
};

export type QueuedMessageStatus = 'queued' | 'sending' | 'failed';
export type QueuedMessageSource =
  | 'local'
  | 'authoritative-delivery-failed'
  | 'native'
  | 'native-submit';

export type QueuedMessageItem = {
  id: string;
  content: string;
  status: QueuedMessageStatus;
  error?: string;
  source?: QueuedMessageSource;
};

export function toUIMessage(record: MessageRecord): UIMessage {
  return {
    ...record,
    id: record.message_id,
    streaming: false,
  };
}

export function pickMergedMessageContent(existing: UIMessage, incoming: UIMessage): string {
  const incomingContent = String(incoming.content || '');
  const existingContent = String(existing.content || '');

  if (!existingContent) return incomingContent;
  if (!incomingContent) return existingContent;
  if (incomingContent === existingContent) return incomingContent;
  if (incomingContent.includes(existingContent)) return incomingContent;
  if (existingContent.includes(incomingContent)) return existingContent;
  if (incoming.streaming && existingContent.length > incomingContent.length) {
    return existingContent;
  }
  return incomingContent;
}

export function pickMergedMessageBlocks(
  existing: UIMessage,
  incoming: UIMessage,
): ContentBlock[] | undefined {
  const existingBlocks = Array.isArray(existing.blocks) ? existing.blocks : undefined;
  const incomingBlocks = Array.isArray(incoming.blocks) ? incoming.blocks : undefined;

  // DB blocks are authoritative when turn is complete (not streaming)
  if (!incoming.streaming && incomingBlocks && incomingBlocks.length > 0) {
    return incomingBlocks;
  }
  // During streaming, keep existing (live) blocks
  return existingBlocks ?? incomingBlocks;
}

export function mergeMessages(base: UIMessage[], incoming: UIMessage[]): UIMessage[] {
  const ordered = [...base];
  const index = new Map<string, number>();
  ordered.forEach((item, i) => index.set(item.id, i));

  for (const item of incoming) {
    const existingIndex = index.get(item.id);
    if (existingIndex === undefined) {
      index.set(item.id, ordered.length);
      ordered.push(item);
      continue;
    }

    const existing = ordered[existingIndex];

    ordered[existingIndex] = {
      ...existing,
      ...item,
      content: pickMergedMessageContent(existing, item),
      blocks: pickMergedMessageBlocks(existing, item),
      streaming: item.streaming ?? existing.streaming ?? false,
    };
  }

  return ordered;
}

export function reconcilePersistedMessages(
  base: UIMessage[],
  incoming: UIMessage[],
  options: {
    dropTransientStreaming: boolean;
  },
): UIMessage[] {
  const merged = mergeMessages(base, incoming);
  if (!options.dropTransientStreaming) {
    return merged;
  }

  const persistedIds = new Set(incoming.map((item) => item.id));
  return merged.filter((item) => {
    if (!item.streaming) return true;
    if (persistedIds.has(item.id)) return true;
    if (item.content || (item.blocks && item.blocks.length > 0)) return true;
    return false;
  });
}

export function hasMatchingPendingToolCard(
  messages: UIMessage[],
  interaction: PendingInteraction | null,
): boolean {
  if (!interaction) {
    return false;
  }

  return messages.some((message) => {
    if (message.turn_id !== interaction.turn_id || !Array.isArray(message.blocks)) {
      return false;
    }

    return message.blocks.some(
      (block) => block.type === 'tool_use' && block.name === interaction.tool_name,
    );
  });
}

export type RefreshSessionOptions = {
  suppressError?: boolean;
};

export function mergeIncrementalText(existing: string, incoming: string): string {
  if (!existing) return incoming;
  if (!incoming) return existing;
  if (existing === incoming) return existing;
  if (existing.includes(incoming)) return existing;
  if (incoming.includes(existing)) return incoming;

  let overlap = 0;
  const maxOverlap = Math.min(existing.length, incoming.length);
  for (let size = maxOverlap; size > 0; size -= 1) {
    if (existing.slice(existing.length - size) === incoming.slice(0, size)) {
      overlap = size;
      break;
    }
  }

  return `${existing}${incoming.slice(overlap)}`;
}

export const MAX_RECOVERY_PAGES_PER_TICK = 8;
