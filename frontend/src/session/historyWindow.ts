import type { UIMessage as SDKUIMessage } from 'ai';
import type { MessageRecord } from '../types';

// Windows overlap by native message identity, not by turn: one turn can
// contain several assistant messages, each of which must remain readable.
function suffixPrefixOverlap<T>(left: T[], right: T[], key: (value: T) => string): number {
  const maxOverlap = Math.min(left.length, right.length);
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    if (left.slice(left.length - overlap).every((item, index) => key(item) === key(right[index]))) {
      return overlap;
    }
  }
  return 0;
}

function recordKey(record: MessageRecord): string {
  return `${record.role}:${record.message_id}`;
}

export function prependOlderDurableRecords(existing: MessageRecord[], olderPage: MessageRecord[]): MessageRecord[] {
  if (olderPage.length === 0) return existing;
  const overlap = suffixPrefixOverlap(olderPage, existing, recordKey);
  return [...olderPage.slice(0, olderPage.length - overlap), ...existing];
}

/** Whether `latest` continues `existing`: the two windows share a boundary. */
export function durableWindowsOverlap(existing: MessageRecord[], latest: MessageRecord[]): boolean {
  return suffixPrefixOverlap(existing, latest, recordKey) > 0;
}

export function mergeLatestDurableRecords(existing: MessageRecord[], latestPage: MessageRecord[]): MessageRecord[] {
  if (existing.length === 0) return latestPage;
  if (latestPage.length === 0) return existing;
  const overlap = suffixPrefixOverlap(existing, latestPage, recordKey);
  if (overlap === 0) throw new Error('SESSION_HISTORY_WINDOWS_DO_NOT_OVERLAP');
  return [...existing.slice(0, existing.length - overlap), ...latestPage];
}

export function didPrependDurableRecords(previous: MessageRecord[], next: MessageRecord[]): boolean {
  if (previous.length === 0 || next.length <= previous.length) return false;
  const offset = next.length - previous.length;
  return previous.every((record, index) => recordKey(record) === recordKey(next[offset + index]));
}

// Prepending durable history must neither discard live suffix messages nor
// replace their in-progress parts with an older database projection.
export function prependAuthoritativeMessages(existing: SDKUIMessage[], authoritative: SDKUIMessage[]): SDKUIMessage[] {
  if (existing.length === 0) return authoritative;
  const firstExisting = authoritative.findIndex((message) => message.id === existing[0].id && message.role === existing[0].role);
  if (firstExisting < 0) throw new Error('SESSION_HISTORY_MESSAGE_WINDOWS_DO_NOT_OVERLAP');
  return [...authoritative.slice(0, firstExisting), ...existing];
}
