import { useState, useEffect, useCallback, useRef } from 'react';
import type { SessionRecord, SessionState } from '../../types';
import {
  getSession,
  endConversation as endConversationApi,
  recoverSession as recoverSessionApi,
  terminateSandbox as terminateSandboxApi,
  deleteSession as deleteSessionApi,
} from '../../api';
import { sessionDetailPollInterval } from '../sessionPolling';

export type LifecycleState =
  | 'loading'
  | 'creating'
  | 'ready'
  | 'busy'
  | 'terminated'
  | 'deleted'
  | 'error';

export type LifecycleRefreshResult = {
  state: LifecycleState;
  detail: SessionRecord | null;
  detailStale: boolean;
};

// Must exceed the backend's database statement timeout, otherwise the browser
// aborts detail reads before the server has a chance to either succeed or fail
// definitively.
const SESSION_DETAIL_TIMEOUT_MS = 40000;

function mapState(state: SessionState): LifecycleState {
  switch (state) {
    case 'CREATING': return 'creating';
    case 'PROCESSING': return 'busy';
    case 'TERMINATED': return 'terminated';
    case 'DELETED': return 'deleted';
    case 'READY':
    case 'WAITING_INPUT':
    default:
      return 'ready';
  }
}

/**
 * One session's detail (`GET /sessions/{id}`) and the actions on its lifecycle.
 *
 * The detail is read on mount, again whenever a caller asks (the
 * conversation's stream asks when a response starts or ends), and on a timer
 * while the session is not at rest (`sessionPolling.ts`).
 */
export function useSessionLifecycle(sessionId: string) {
  const [session, setSession] = useState<SessionRecord | null>(null);
  const [lifecycleState, setLifecycleState] = useState<LifecycleState>('loading');
  const [error, setError] = useState<string | null>(null);
  const [detailStale, setDetailStale] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRequestSeqRef = useRef(0);
  const lifecycleStateRef = useRef<LifecycleState>('loading');
  const sessionRef = useRef<SessionRecord | null>(null);
  const detailStaleRef = useRef(false);
  const refreshControllerRef = useRef<AbortController | null>(null);
  const refreshInFlightRef = useRef<Promise<LifecycleRefreshResult> | null>(null);

  useEffect(() => {
    lifecycleStateRef.current = lifecycleState;
  }, [lifecycleState]);

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    detailStaleRef.current = detailStale;
  }, [detailStale]);

  // Clear per-session state before details for the new route can arrive.
  useEffect(() => {
    setLifecycleState('loading');
    lifecycleStateRef.current = 'loading';
    setSession(null);
    sessionRef.current = null;
    setError(null);
    setDetailStale(false);
    // Invalidate in-flight detail reads from the previous route. This must stay
    // monotonic; resetting to 0 lets an old aborted request reuse the new seq.
    refreshRequestSeqRef.current += 1;
    if (refreshControllerRef.current) {
      refreshControllerRef.current.abort();
      refreshControllerRef.current = null;
    }
    refreshInFlightRef.current = null;
  }, [sessionId]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const cancelRefresh = useCallback(() => {
    if (refreshControllerRef.current) {
      refreshRequestSeqRef.current += 1;
      refreshControllerRef.current.abort();
      refreshControllerRef.current = null;
      refreshInFlightRef.current = null;
    }
  }, []);

  // `sessionRef` keeps this callback stable across refreshed record objects;
  // depending on `session` would restart the polling effect after each read.
  const refresh = useCallback(async (options?: { force?: boolean }) => {
    const force = options?.force === true;
    if (force) {
      cancelRefresh();
    } else if (refreshInFlightRef.current) {
      return refreshInFlightRef.current;
    }

    const requestSeq = refreshRequestSeqRef.current + 1;
    refreshRequestSeqRef.current = requestSeq;
    const previousState = lifecycleStateRef.current;
    const controller = new AbortController();
    refreshControllerRef.current = controller;

    let pendingRefresh: Promise<LifecycleRefreshResult> | null = null;
    pendingRefresh = (async () => {
      const timer = window.setTimeout(() => {
        controller.abort();
      }, SESSION_DETAIL_TIMEOUT_MS);
      try {
        const detail = await getSession(sessionId, { signal: controller.signal });
        if (requestSeq !== refreshRequestSeqRef.current) {
          return { state: lifecycleStateRef.current, detail: sessionRef.current, detailStale: detailStaleRef.current };
        }
        setSession(detail);
        sessionRef.current = detail;
        setError(null);
        setDetailStale(false);
        const newState = mapState(detail.state);
        setLifecycleState(newState);
        return { state: newState, detail, detailStale: false };
      } catch (err) {
        const normalizedError =
          err instanceof DOMException && err.name === 'AbortError'
            ? new Error(`SESSION_DETAIL_TIMEOUT: session detail request timed out after ${SESSION_DETAIL_TIMEOUT_MS}ms`)
            : err;
        if (requestSeq !== refreshRequestSeqRef.current) {
          return { state: lifecycleStateRef.current, detail: sessionRef.current, detailStale: detailStaleRef.current };
        }
        if (sessionRef.current && previousState !== 'loading') {
          console.warn('[useSessionLifecycle] background refresh failed:', normalizedError);
          setDetailStale(true);
          return { state: previousState, detail: sessionRef.current, detailStale: true };
        }
        setError((normalizedError as Error).message);
        setDetailStale(false);
        setLifecycleState('error');
        return { state: 'error' as LifecycleState, detail: null, detailStale: false };
      } finally {
        if (refreshControllerRef.current === controller) {
          refreshControllerRef.current = null;
        }
        if (pendingRefresh && refreshInFlightRef.current === pendingRefresh) {
          refreshInFlightRef.current = null;
        }
        window.clearTimeout(timer);
      }
    })();

    refreshInFlightRef.current = pendingRefresh;
    return pendingRefresh;
  }, [cancelRefresh, sessionId]);

  useEffect(() => {
    void refresh();
    return () => stopPolling();
  }, [sessionId, refresh, stopPolling]);

  useEffect(() => {
    const reconnect = () => { void refresh(); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [refresh]);

  const pollInterval = sessionDetailPollInterval(lifecycleState, session, detailStale);
  useEffect(() => {
    stopPolling();
    if (pollInterval === null) return;
    let cancelled = false;
    const pollOnce = async () => {
      await refresh();
      if (!cancelled) pollRef.current = setTimeout(() => { void pollOnce(); }, pollInterval);
    };
    pollRef.current = setTimeout(() => { void pollOnce(); }, pollInterval);
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [pollInterval, refresh, stopPolling]);

  const terminate = useCallback(async () => {
    let terminateError: Error | null = null;
    try {
      setError(null);
      stopPolling();
      cancelRefresh();
      await terminateSandboxApi(sessionId);
    } catch (err) {
      terminateError = err as Error;
    }

    const refreshResult = await refresh({ force: true });
    if (
      refreshResult.state === 'terminated'
      || refreshResult.detail?.state === 'TERMINATED'
    ) {
      return;
    }

    if (terminateError) {
      setError(terminateError.message);
    }
  }, [cancelRefresh, sessionId, refresh, stopPolling]);

  const endConversation = useCallback(async () => {
    let endError: Error | null = null;
    try {
      setError(null);
      stopPolling();
      cancelRefresh();
      await endConversationApi(sessionId);
    } catch (err) {
      endError = err as Error;
    }

    const refreshResult = await refresh({ force: true });
    if (
      refreshResult.state === 'terminated'
      || refreshResult.detail?.state === 'TERMINATED'
    ) {
      return;
    }

    if (endError) {
      setError(endError.message);
    }
  }, [cancelRefresh, sessionId, refresh, stopPolling]);

  const recover = useCallback(async () => {
    try {
      stopPolling();
      setLifecycleState('creating');
      cancelRefresh();
      await recoverSessionApi(sessionId);
      await refresh({ force: true });
    } catch (err) {
      setError((err as Error).message);
      setLifecycleState('error');
    }
  }, [cancelRefresh, sessionId, refresh, stopPolling]);

  const deleteSession = useCallback(async () => {
    try {
      stopPolling();
      await deleteSessionApi(sessionId);
      setLifecycleState('deleted');
    } catch (err) {
      setError((err as Error).message);
    }
  }, [sessionId, stopPolling]);

  return {
    session,
    lifecycleState,
    error,
    detailStale,
    refresh,
    terminate,
    endConversation,
    recover,
    deleteSession,
  };
}
