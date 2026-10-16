import type { UIMessage } from 'ai';
import type { HistoryBlockPage } from '../api';
import type { PendingInteraction } from '../types';
import { isTransientHistoryLoadError } from './sessionPageLoading';
import { latestPageJoins } from './sessionMessages';
import type { TranscriptAccess } from './TranscriptAccess';

/**
 * Reading a session's history (`GET …/history-blocks`) a page at a time.
 *
 * The server owns the position: the first page is read at the output stored so
 * far (`output_seq`) and its `next_cursor` pins every older page to that same
 * position, so the history cannot slide under a reader paging backwards.
 */

export const HISTORY_PAGE_SIZE = 50;
const TRANSIENT_PAGE_RETRY_DELAYS_MS = [250, 1000] as const;

/** The newest history window: its messages, the stream position they reflect, and where older pages start. */
export interface HistoryWindow {
  messages: UIMessage[];
  /** The output position the first page was read through; the stream is followed from here. */
  outputSeq: number | null;
  pendingInteraction: PendingInteraction | null;
  hasMore: boolean;
  nextCursor: string | null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? 'Unknown error');
}

/** One page, retried briefly when the record store was momentarily unreachable. */
export async function readHistoryPage(
  readHistory: TranscriptAccess['readHistory'],
  sessionId: string,
  before: string | undefined,
  signal?: AbortSignal,
): Promise<HistoryBlockPage> {
  let attempt = 0;
  while (true) {
    signal?.throwIfAborted();
    try {
      const page = await readHistory(sessionId, before, HISTORY_PAGE_SIZE, { signal });
      signal?.throwIfAborted();
      if (page.paging_mode !== 'blocks') {
        throw new Error('SESSION_HISTORY_BLOCK_MODE_NOT_AVAILABLE');
      }
      return page;
    } catch (error) {
      signal?.throwIfAborted();
      const delay = TRANSIENT_PAGE_RETRY_DELAYS_MS[attempt++];
      if (delay === undefined || !isTransientHistoryLoadError(errorMessage(error))) throw error;
      await new Promise<void>((resolve) => window.setTimeout(resolve, delay));
    }
  }
}

/**
 * The cursor the next older page is asked for with, or null at the start.
 *
 * A page that says there is more and hands back no way to ask for it — or
 * hands back the cursor just used — has no next position, and paging on would
 * either stop silently or read the same messages for as long as the reader
 * keeps scrolling.
 */
export function nextHistoryCursor(page: HistoryBlockPage, usedCursor: string | null): string | null {
  if (!page.has_more) return null;
  const nextCursor = String(page.next_cursor ?? '').trim();
  if (!nextCursor) {
    throw new Error('SESSION_HISTORY_CURSOR_UNSAFE: has_more=true but the page carried no next_cursor.');
  }
  if (usedCursor && nextCursor === usedCursor) {
    throw new Error(`SESSION_HISTORY_CURSOR_UNSAFE: pagination cursor did not move (${nextCursor}).`);
  }
  return nextCursor;
}

/**
 * The newest page, extended backwards until it reaches the messages already
 * shown.
 *
 * A page that has been open for a while can find the newest page entirely
 * after everything it shows: a busy conversation writes more than a page
 * between two reads. Joining the two windows edge to edge would hide every
 * message in between; walking the newest page's cursor back until the windows
 * share a message rebuilds the gap exactly.
 */
export async function readLatestWindow(
  readHistory: TranscriptAccess['readHistory'],
  sessionId: string,
  shown: readonly UIMessage[],
  signal?: AbortSignal,
): Promise<HistoryWindow> {
  const first = await readHistoryPage(readHistory, sessionId, undefined, signal);
  let messages = first.messages;
  let hasMore = first.has_more;
  let cursor = nextHistoryCursor(first, null);
  while (cursor && !latestPageJoins(shown, messages)) {
    const older = await readHistoryPage(readHistory, sessionId, cursor, signal);
    messages = [...older.messages, ...messages];
    hasMore = older.has_more;
    cursor = nextHistoryCursor(older, cursor);
  }
  return {
    messages,
    outputSeq: typeof first.output_seq === 'number' ? first.output_seq : null,
    pendingInteraction: first.pending_interaction ?? null,
    hasMore,
    nextCursor: cursor,
  };
}
