import { useEffect, useState, useCallback, useRef } from 'react';
import { getHistoryBlocks } from '../../api';
import { useTranscriptAccess } from '../TranscriptAccess';
import { keepsLastRead } from '../../hooks/useKeepCurrent';
import type { ActiveTurnOverlay, HistoryBlockPage } from '../../api';
import type { MessageRecord, PendingInteraction } from '../../types';
import { isTransientHistoryLoadError } from '../sessionPageLoading';
import {
  durableWindowsOverlap,
  mergeLatestDurableRecords,
  prependOlderDurableRecords,
} from '../historyWindow';

type AuthoritativeHistoryData = {
  ownerSessionId: string;
  durableRecords: MessageRecord[];
  overlay: ActiveTurnOverlay | null;
  sessionFrameSeq: number | null;
  pendingInteraction: PendingInteraction | null;
  hasMore: boolean;
  nextCursor: string | null;
};

export type AuthoritativeHistoryFetchMode = 'window' | 'acceptance';

export type FetchAuthoritativeHistoryOptions = {
  mode?: AuthoritativeHistoryFetchMode;
  commit?: boolean;
};

const AUTHORITATIVE_HISTORY_PAGE_SIZE = 50;
const DEFAULT_FETCH_MODE: AuthoritativeHistoryFetchMode = 'window';
const TRANSIENT_PAGE_RETRY_DELAYS_MS = [250, 1000] as const;

async function fetchHistoryPage(sessionId: string, before?: string, signal?: AbortSignal, readHistory = getHistoryBlocks) {
  let attempt = 0;
  while (true) {
    signal?.throwIfAborted();
    try {
      const page = await readHistory(sessionId, before, AUTHORITATIVE_HISTORY_PAGE_SIZE, { signal });
      signal?.throwIfAborted();
      return page;
    } catch (error) {
      signal?.throwIfAborted();
      const delay = TRANSIENT_PAGE_RETRY_DELAYS_MS[attempt++];
      if (delay === undefined || !isTransientHistoryLoadError(getErrorMessage(error))) throw error;
      await new Promise<void>((resolve) => window.setTimeout(resolve, delay));
    }
  }
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error ?? 'Unknown error');
}

function requireSessionFrameSeq(value: unknown): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || Number(value) < 0) {
    throw new Error('AUTHORITATIVE_HISTORY_FRAME_CURSOR_INVALID: session_frame_seq must be a non-negative integer or null.');
  }
  return Number(value);
}

/**
 * The cursor the next older page is asked for with, or null at the end.
 *
 * The server owns the position: it issues a token that pins the checkpoint
 * every later page of this read is served against, so the transcript cannot
 * slide under a reader paging backwards. A page that says there is more and
 * hands back no way to ask for it — or hands back the cursor just used — has
 * no next position, and paging on would either stop silently or re-read the
 * same records for as long as the reader keeps scrolling.
 */
function nextAuthoritativeHistoryCursor(
  page: HistoryBlockPage,
  usedCursor: string | null,
): string | null {
  if (page.paging_mode !== 'blocks') {
    throw new Error('SESSION_HISTORY_BLOCK_MODE_NOT_AVAILABLE');
  }
  if (!page.has_more) return null;
  const nextCursor = String(page.next_cursor ?? '').trim();
  if (!nextCursor) {
    throw new Error('AUTHORITATIVE_HISTORY_CURSOR_UNSAFE: has_more=true but the page carried no next_cursor.');
  }
  if (usedCursor && nextCursor === usedCursor) {
    throw new Error(`AUTHORITATIVE_HISTORY_CURSOR_UNSAFE: pagination cursor did not move (${nextCursor}).`);
  }
  return nextCursor;
}

/**
 * The newest page, extended backwards until it reaches the window already loaded.
 *
 * A page that has been open for a while can find the newest page entirely
 * after everything it holds: a busy conversation writes more than one page of
 * records between two reads. Neither window is wrong, and joining them edge to
 * edge would hide every record in between. The cursor the newest page issues
 * pins every older page to the same checkpoint, so walking it back until the
 * two windows share a boundary rebuilds the gap exactly; the merge afterwards
 * then finds the overlap it insists on. A walk that reaches the start of the
 * history without meeting the loaded window is not a gap — the transcript does
 * not contain the records the page holds — and the merge fails loud on it.
 */
async function joinLatestWindowToLoaded(
  sessionId: string,
  loaded: MessageRecord[],
  firstPage: HistoryBlockPage,
  signal?: AbortSignal,
  readHistory = getHistoryBlocks,
): Promise<{ records: MessageRecord[]; hasMore: boolean; nextCursor: string | null }> {
  let records = firstPage.messages;
  let hasMore = firstPage.has_more;
  let cursor = nextAuthoritativeHistoryCursor(firstPage, null);
  if (loaded.length === 0 || records.length === 0 || durableWindowsOverlap(loaded, records)) {
    return { records, hasMore, nextCursor: cursor };
  }
  while (cursor && !durableWindowsOverlap(loaded, records)) {
    const older = await fetchHistoryPage(sessionId, cursor, signal, readHistory);
    signal?.throwIfAborted();
    records = [...older.messages, ...records];
    hasMore = older.has_more;
    cursor = nextAuthoritativeHistoryCursor(older, cursor);
  }
  // Where the walk ended is where this window ends: the pages behind it are
  // what an older-page request asks for next.
  return { records, hasMore, nextCursor: cursor };
}

export async function fetchAuthoritativeHistoryData(
  sessionId: string,
  signal?: AbortSignal,
  loaded: MessageRecord[] = [],
  readHistory = getHistoryBlocks,
): Promise<Omit<AuthoritativeHistoryData, 'ownerSessionId'>> {
  const firstPage = await fetchHistoryPage(sessionId, undefined, signal, readHistory);
  const window = await joinLatestWindowToLoaded(sessionId, loaded, firstPage, signal, readHistory);
  return {
    durableRecords: window.records,
    overlay: firstPage.active_turn_overlay ?? null,
    sessionFrameSeq: requireSessionFrameSeq(firstPage.session_frame_seq),
    pendingInteraction: firstPage.pending_interaction ?? null,
    hasMore: window.hasMore,
    nextCursor: window.nextCursor,
  };
}

export async function settleHistoryFetchForEffect<T>(
  promise: Promise<T>,
): Promise<T | undefined> {
  try {
    return await promise;
  } catch {
    return undefined;
  }
}

/**
 * Hook to fetch the first page of messages for a session.
 *
 * Returns durable records and the active turn overlay as separate fields. The
 * first response also updates lifecycle pending state before the page mounts
 * its stream; it never creates message or tool parts from that state.
 */
export function useFirstPageMessages(
  sessionId: string,
  onBootstrapPendingInteraction?: (interaction: PendingInteraction | null) => void,
) {
  const { readHistory } = useTranscriptAccess();
  const [durableRecords, setDurableRecords] = useState<MessageRecord[]>([]);
  const [overlay, setOverlay] = useState<ActiveTurnOverlay | null>(null);
  const [sessionFrameSeq, setSessionFrameSeq] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [backgroundReadFailure, setBackgroundReadFailure] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const loadedSession = useRef<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const durableRecordsRef = useRef<MessageRecord[]>([]);
  const nextCursorRef = useRef<string | null>(null);
  const hasMoreRef = useRef(false);
  const loadMoreInFlightRef = useRef<Promise<void> | null>(null);
  const currentSessionIdRef = useRef(sessionId);
  const requestScopeRef = useRef<{ sessionId: string; controller: AbortController } | null>(null);
  currentSessionIdRef.current = sessionId;
  const [ownerSessionId, setOwnerSessionId] = useState<string>(sessionId);
  const windowFetchInFlightRef = useRef<Promise<AuthoritativeHistoryData> | null>(null);
  const acceptanceFetchInFlightRef = useRef<Promise<AuthoritativeHistoryData> | null>(null);
  const fetchRequestSeqRef = useRef(0);
  const bootstrapPendingAppliedRef = useRef(false);

  useEffect(() => {
    const scope = { sessionId, controller: new AbortController() };
    requestScopeRef.current = scope;
    fetchRequestSeqRef.current += 1;
    setOwnerSessionId(sessionId);
    setDurableRecords([]);
    setOverlay(null);
    setSessionFrameSeq(null);
    setLoading(true);
    setError(null);
    setBackgroundReadFailure(false);
    setLoadedOnce(false);
    loadedSession.current = null;
    setHasMore(false);
    setLoadingMore(false);
    setLoadMoreError(null);
    durableRecordsRef.current = [];
    nextCursorRef.current = null;
    hasMoreRef.current = false;
    loadMoreInFlightRef.current = null;
    windowFetchInFlightRef.current = null;
    acceptanceFetchInFlightRef.current = null;
    bootstrapPendingAppliedRef.current = false;
    return () => {
      fetchRequestSeqRef.current += 1;
      scope.controller.abort();
    };
  }, [sessionId]);

  const getRequestSignal = useCallback(() => {
    const scope = requestScopeRef.current;
    if (!scope || scope.sessionId !== sessionId) {
      throw new DOMException('Session history owner changed', 'AbortError');
    }
    scope.controller.signal.throwIfAborted();
    return scope.controller.signal;
  }, [sessionId]);

  const fetchFirstPage = useCallback(async (
    options: FetchAuthoritativeHistoryOptions = {},
  ): Promise<AuthoritativeHistoryData> => {
    const signal = getRequestSignal();
    const mode = options.mode ?? DEFAULT_FETCH_MODE;
    const shouldCommit = options.commit ?? mode === 'window';
    const inFlightRef = mode === 'acceptance'
      ? acceptanceFetchInFlightRef
      : windowFetchInFlightRef;

    if (inFlightRef.current) {
      return inFlightRef.current;
    }

    const requestSeq = shouldCommit ? fetchRequestSeqRef.current + 1 : fetchRequestSeqRef.current;
    if (shouldCommit) {
      fetchRequestSeqRef.current = requestSeq;
    }
    let pendingFetch: Promise<AuthoritativeHistoryData> | null = null;
    pendingFetch = (async () => {
      if (shouldCommit) {
        setLoading(true);
        setError(null);
      }
      try {
        const data = await fetchAuthoritativeHistoryData(
          sessionId,
          signal,
          mode === 'acceptance' ? [] : durableRecordsRef.current,
          readHistory,
        );

        const existing = durableRecordsRef.current;
        const records = mode === 'acceptance' ? data.durableRecords : mergeLatestDurableRecords(existing, data.durableRecords);
        const preservedOlderWindow = records.length > data.durableRecords.length;
        const nextData = {
          ownerSessionId: sessionId,
          durableRecords: records,
          overlay: data.overlay,
          sessionFrameSeq: data.sessionFrameSeq,
          pendingInteraction: data.pendingInteraction,
          hasMore: preservedOlderWindow ? hasMoreRef.current : data.hasMore,
          nextCursor: preservedOlderWindow ? nextCursorRef.current : data.nextCursor,
        };

        if (!shouldCommit || requestSeq !== fetchRequestSeqRef.current || currentSessionIdRef.current !== sessionId) {
          return nextData;
        }

        if (!bootstrapPendingAppliedRef.current) {
          onBootstrapPendingInteraction?.(data.pendingInteraction);
          bootstrapPendingAppliedRef.current = true;
        }
        setOwnerSessionId(sessionId);
        durableRecordsRef.current = records;
        nextCursorRef.current = nextData.nextCursor;
        hasMoreRef.current = nextData.hasMore;
        setHasMore(nextData.hasMore);
        setDurableRecords(records);
        setOverlay(data.overlay);
        setSessionFrameSeq(data.sessionFrameSeq);
        setLoadedOnce(true);
        loadedSession.current = sessionId;
        return nextData;
      } catch (err) {
        if (shouldCommit && requestSeq === fetchRequestSeqRef.current && currentSessionIdRef.current === sessionId) {
          setOwnerSessionId(sessionId);
          setBackgroundReadFailure(keepsLastRead(err, { background: true }, loadedSession.current === sessionId));
          setError(getErrorMessage(err));
        }
        throw err;
      } finally {
        if (shouldCommit && requestSeq === fetchRequestSeqRef.current && currentSessionIdRef.current === sessionId) {
          setLoading(false);
        }
        if (pendingFetch && inFlightRef.current === pendingFetch) {
          inFlightRef.current = null;
        }
      }
    })();

    inFlightRef.current = pendingFetch;
    return pendingFetch;
  }, [getRequestSignal, onBootstrapPendingInteraction, sessionId, readHistory]);

  const loadOlder = useCallback(async (): Promise<void> => {
    const signal = getRequestSignal();
    const before = nextCursorRef.current;
    if (!hasMoreRef.current || !before) return;
    if (loadMoreInFlightRef.current) return loadMoreInFlightRef.current;
    let pending: Promise<void> | null = null;
    pending = (async () => {
      setLoadingMore(true);
      setLoadMoreError(null);
      try {
        const page = await fetchHistoryPage(sessionId, before, signal, readHistory);
        if (currentSessionIdRef.current !== sessionId) return;
        const nextCursor = nextAuthoritativeHistoryCursor(page, before);
        const records = prependOlderDurableRecords(durableRecordsRef.current, page.messages);
        durableRecordsRef.current = records;
        nextCursorRef.current = nextCursor;
        hasMoreRef.current = page.has_more;
        setHasMore(page.has_more);
        setDurableRecords(records);
      } catch (reason) {
        if (!signal.aborted && currentSessionIdRef.current === sessionId) setLoadMoreError(getErrorMessage(reason));
      } finally {
        if (!signal.aborted && currentSessionIdRef.current === sessionId) setLoadingMore(false);
        if (loadMoreInFlightRef.current === pending) loadMoreInFlightRef.current = null;
      }
    })();
    loadMoreInFlightRef.current = pending;
    return pending;
  }, [getRequestSignal, sessionId, readHistory]);

  useEffect(() => {
    if (!isTransientHistoryLoadError(loadMoreError)) return;
    const onOnline = () => { void loadOlder(); };
    window.addEventListener('online', onOnline);
    return () => window.removeEventListener('online', onOnline);
  }, [loadMoreError, loadOlder]);

  // Auto-retry on transient errors
  useEffect(() => {
    const loadError = String(error ?? '').trim();
    if (!sessionId || !isTransientHistoryLoadError(loadError)) {
      return;
    }
    let stopped = false;
    let inFlight = false;
    const retry = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        await settleHistoryFetchForEffect(fetchFirstPage());
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => {
      void retry();
    }, 3000);
    void retry();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [error, fetchFirstPage, sessionId]);

  // Initial fetch
  useEffect(() => {
    void settleHistoryFetchForEffect(fetchFirstPage());
  }, [fetchFirstPage]);

  useEffect(() => {
    const reconnect = () => { void settleHistoryFetchForEffect(fetchFirstPage()); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [fetchFirstPage]);

  return {
    ownerSessionId,
    durableRecords,
    overlay,
    sessionFrameSeq,
    loading,
    error: backgroundReadFailure ? null : error,
    loadedOnce,
    hasMore,
    loadingMore,
    loadMoreError,
    loadOlder,
    refetch: fetchFirstPage,
  };
}
