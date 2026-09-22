import type { PendingInteraction } from '../types';

export function getPendingInteractionRefreshKey(
  pendingInteraction: PendingInteraction | null | undefined,
): string {
  if (!pendingInteraction) {
    return '';
  }
  return [
    String(pendingInteraction.turn_id ?? '').trim(),
    String(pendingInteraction.presentation ?? '').trim(),
    String(pendingInteraction.interaction_id ?? '').trim(),
    String(pendingInteraction.tool_call_id ?? '').trim(),
  ].join('|');
}

export function shouldRefetchMessagesForPendingInteractionChange({
  hasLoadedRecords,
  previousKey,
  nextKey,
}: {
  hasLoadedRecords: boolean;
  previousKey: string | null;
  nextKey: string;
}): boolean {
  return hasLoadedRecords && previousKey !== null && previousKey !== nextKey;
}
