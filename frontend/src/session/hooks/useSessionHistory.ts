import { useCallback, useEffect, useRef, useState } from 'react';
import type { UIMessage } from 'ai';
import { useTranscriptAccess } from '../TranscriptAccess';
import { isTransientHistoryLoadError } from '../sessionPageLoading';
import { prependOlderMessages } from '../sessionMessages';
import {
  nextHistoryCursor,
  readHistoryPage,
  readLatestWindow,
  type HistoryWindow,
} from '../historyPages';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? 'Unknown error');
}

interface HistoryState {
  ownerSessionId: string;
  window: HistoryWindow | null;
  loading: boolean;
  error: string | null;
  loadingMore: boolean;
  loadMoreError: string | null;
}

function initialState(sessionId: string): HistoryState {
  return {
    ownerSessionId: sessionId,
    window: null,
    loading: true,
    error: null,
    loadingMore: false,
    loadMoreError: null,
  };
}

const EMPTY: UIMessage[] = [];

/**
 * A session's history, read through the page's transcript access.
 *
 * The session page reads the newest window here once, to open the
 * conversation from it; from then on the live transcript owns its messages. A
 * share link pages through the whole history with `loadOlder`.
 */
export function useSessionHistory(sessionId: string) {
  const { readHistory } = useTranscriptAccess();
  const [state, setState] = useState<HistoryState>(() => initialState(sessionId));
  const stateRef = useRef(state);
  stateRef.current = state;
  const scopeRef = useRef<{ sessionId: string; controller: AbortController } | null>(null);

  useEffect(() => {
    const scope = { sessionId, controller: new AbortController() };
    scopeRef.current = scope;
    setState(initialState(sessionId));
    return () => scope.controller.abort();
  }, [sessionId]);

  const refetch = useCallback(async (): Promise<HistoryWindow | null> => {
    const scope = scopeRef.current;
    if (!scope || scope.sessionId !== sessionId) return null;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const window = await readLatestWindow(readHistory, sessionId, [], scope.controller.signal);
      if (scope.controller.signal.aborted) return null;
      setState((current) => ({ ...current, ownerSessionId: sessionId, window, loading: false, error: null }));
      return window;
    } catch (error) {
      if (scope.controller.signal.aborted) return null;
      setState((current) => ({ ...current, ownerSessionId: sessionId, loading: false, error: errorMessage(error) }));
      return null;
    }
  }, [readHistory, sessionId]);

  const loadOlder = useCallback(async () => {
    const scope = scopeRef.current;
    const current = stateRef.current;
    const before = current.window?.nextCursor;
    if (!scope || !current.window?.hasMore || !before || current.loadingMore) return;
    setState((state) => ({ ...state, loadingMore: true, loadMoreError: null }));
    try {
      const page = await readHistoryPage(readHistory, sessionId, before, scope.controller.signal);
      const nextCursor = nextHistoryCursor(page, before);
      setState((state) => (state.window ? {
        ...state,
        loadingMore: false,
        window: {
          ...state.window,
          messages: prependOlderMessages(state.window.messages, page.messages),
          hasMore: page.has_more,
          nextCursor,
        },
      } : state));
    } catch (error) {
      if (scope.controller.signal.aborted) return;
      setState((state) => ({ ...state, loadingMore: false, loadMoreError: errorMessage(error) }));
    }
  }, [readHistory, sessionId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  // A read that failed because the record store was briefly unreachable is
  // asked again until it answers; any other failure waits for the reader.
  useEffect(() => {
    if (!isTransientHistoryLoadError(state.error)) return;
    const timer = window.setInterval(() => { void refetch(); }, 3000);
    const onOnline = () => { void refetch(); };
    window.addEventListener('online', onOnline);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('online', onOnline);
    };
  }, [refetch, state.error]);

  const owned = state.ownerSessionId === sessionId;
  return {
    ownerSessionId: state.ownerSessionId,
    window: owned ? state.window : null,
    messages: owned ? state.window?.messages ?? EMPTY : EMPTY,
    loading: !owned || state.loading,
    loadedOnce: owned && state.window !== null,
    error: owned ? state.error : null,
    hasMore: owned && Boolean(state.window?.hasMore),
    loadingMore: owned && state.loadingMore,
    loadMoreError: owned ? state.loadMoreError : null,
    loadOlder,
    refetch,
  };
}
