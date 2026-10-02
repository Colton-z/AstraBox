/** Read native DSH mainline events and preserve independent prose blocks. */
import type { MessageRecord } from './astraApi';

export interface NativeEvent {
  type: string;
  seq: number;
  data: Record<string, unknown>;
}

export function events(value: unknown): NativeEvent[] {
  if (Array.isArray(value)) return value.flatMap(events);
  if (!value || typeof value !== 'object') return [];
  const row = value as Record<string, unknown>;
  if (typeof row.type === 'string' && typeof row.seq === 'number' && row.data) {
    return [row as unknown as NativeEvent];
  }
  return Object.values(row).flatMap(events);
}

export function textBlockValues(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((block) => block?.type === 'text').map((block) => String(block.text));
}

export function textBlocks(value: unknown): string {
  return textBlockValues(value).join('\n');
}

export function messageProseBlocks(message: MessageRecord): string[] {
  return textBlockValues(message.blocks);
}
