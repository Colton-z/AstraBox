import { useState, useEffect, useCallback, useRef } from 'react';
import type { PendingInteraction, SessionRecord, SessionState } from '../../types';
import {
  getSession,
  endConversation as endConversationApi,
  interruptSession as interruptSessionApi,
  recoverSession as recoverSessionApi,
  terminateSandbox as terminateSandboxApi,
  deleteSession as deleteSessionApi,
} from '../../api';
import { shouldPollSessionDetail } from '../sessionPolling';

export type LifecycleState =
  | 'loading'
  | 'creating'
  | 'ready'
  | 'background'
  | 'busy'
  | 'recovery'
  | 'terminated'
  | 'deleted'
  | 'error';

export type LifecycleRefreshResult = {
  state: LifecycleState;
  detail: SessionRecord | null;
  detailStale: boolean;
};

// Must exceed backend Mongo socketTimeoutMS (30s), otherwise the browser will
// reliably abort detail reads before the server-side request has a chance to
// either succeed or fail definitively under expected Mongo jitter.
const SESSION_DETAIL_TIMEOUT_MS = 40000;
const CREATING_POLL_INTERVAL_MS = 500;
const BUSY_POLL_INTERVAL_MS = 3000;
const DEFAULT_POLL_INTERVAL_MS = 1500;

function mapState(s: SessionState): LifecycleState {
  switch (s) {
    case 'CREATING': return 'creating';
    case 'BACKGROUND_RUNNING': return 'background';
    case 'READY':
    case 'WAITING_INPUT': return 'ready';
    case 'BUSY':
    case 'PROCESSING':
    case 'SENDING':
    case 'INTERRUPTING':
    case 'TERMINATING': return 'busy';
    case 'RECOVERY_REQUIRED': return 'recovery';
    case 'TERMINATED': return 'terminated';
    case 'DELETED': return 'deleted';
    default: return 'ready';
  }
}

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
  const pendingInteractionObservationRef = useRef(0);
  const latestObservedPendingInteractionRef = useRef<PendingInteraction | null>(null);

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
    latestObservedPendingInteractionRef.current = null;
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
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // `sessionRef` keeps this callback stable across refreshed record objects;
  // depending on `session` would restart the polling effect after each read.
  const refresh = useCallback(async (options?: { force?: boolean }) => {
    const force = options?.force === true;
    if (force && refreshControllerRef.current) {
      refreshRequestSeqRef.current += 1;
      refreshControllerRef.current.abort();
      refreshControllerRef.current = null;
      refreshInFlightRef.current = null;
    } else if (refreshInFlightRef.current) {
      return refreshInFlightRef.current;
    }

    const requestSeq = refreshRequestSeqRef.current + 1;
    refreshRequestSeqRef.current = requestSeq;
    const previousState = lifecycleStateRef.current;
    const pendingObservation = pendingInteractionObservationRef.current;
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
        const pendingInteractionChanged = (
          pendingInteractionObservationRef.current !== pendingObservation
        );
        const resolvedDetail = pendingInteractionChanged ? {
          ...detail,
          pending_interaction: latestObservedPendingInteractionRef.current,
        } : detail;
        setSession(resolvedDetail);
        sessionRef.current = resolvedDetail;
        latestObservedPendingInteractionRef.current = (
          resolvedDetail.pending_interaction ?? null
        );
        setError(null);
        setDetailStale(false);
        const newState = mapState(resolvedDetail.state);
        setLifecycleState(newState);
        if (!shouldPollSessionDetail(newState, resolvedDetail, false)) {
          stopPolling();
        }
        return { state: newState, detail: resolvedDetail, detailStale: false };
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
  }, [sessionId, stopPolling]);

  const observePendingInteraction = useCallback((
    pendingInteraction: PendingInteraction | null,
  ) => {
    const current = sessionRef.current;
    if (!current || current.session_id !== sessionId) return;
    pendingInteractionObservationRef.current += 1;
    latestObservedPendingInteractionRef.current = pendingInteraction;
    const next = {
      ...current,
      pending_interaction: pendingInteraction,
    };
    sessionRef.current = next;
    setSession(next);
  }, [sessionId]);

  const bootstrapPendingInteraction = useCallback((
    pendingInteraction: PendingInteraction | null,
  ) => {
    const current = sessionRef.current;
    if (current?.session_id === sessionId) {
      observePendingInteraction(pendingInteraction);
      return;
    }
    pendingInteractionObservationRef.current += 1;
    latestObservedPendingInteractionRef.current = pendingInteraction;
  }, [observePendingInteraction, sessionId]);

  const clearPendingInteraction = useCallback((interactionId: string) => {
    const expectedInteractionId = String(interactionId ?? '').trim();
    if (!expectedInteractionId) return false;
    const current = sessionRef.current;
    if (
      !current
      || current.session_id !== sessionId
      || String(current.pending_interaction?.interaction_id ?? '').trim()
        !== expectedInteractionId
    ) {
      return false;
    }
    pendingInteractionObservationRef.current += 1;
    latestObservedPendingInteractionRef.current = null;
    const next = {
      ...current,
      pending_interaction: null,
    };
    sessionRef.current = next;
    setSession(next);
    return true;
  }, [sessionId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await refresh();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [sessionId, refresh, stopPolling]);

  useEffect(() => {
    const reconnect = () => { void refresh(); };
    window.addEventListener('online', reconnect);
    return () => window.removeEventListener('online', reconnect);
  }, [refresh]);

  useEffect(() => {
    if (!shouldPollSessionDetail(lifecycleState, session, detailStale)) {
      stopPolling();
      return;
    }
    if (pollRef.current) {
      return;
    }
    let cancelled = false;
    const pollOnce = async () => {
      const next = await refresh();
      if (cancelled || !shouldPollSessionDetail(next.state, next.detail, next.detailStale)) {
        stopPolling();
        return;
      }
      const interval =
        next.state === 'creating'
          ? CREATING_POLL_INTERVAL_MS
          : next.state === 'background'
            ? BUSY_POLL_INTERVAL_MS
          : next.state === 'busy'
            ? BUSY_POLL_INTERVAL_MS
            : DEFAULT_POLL_INTERVAL_MS;
      pollRef.current = window.setTimeout(() => {
        void pollOnce();
      }, interval);
    };
    void pollOnce();
    return () => {
      cancelled = true;
      if (pollRef.current) {
        clearTimeout(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [
    detailStale,
    lifecycleState,
    refresh,
    session?.state,
    session?.current_turn_id,
    session?.pending_interaction?.interaction_id,
    stopPolling,
  ]);

  const terminate = useCallback(async () => {
    let terminateError: Error | null = null;
    try {
      setError(null);
      stopPolling();
      if (refreshControllerRef.current) {
        refreshRequestSeqRef.current += 1;
        refreshControllerRef.current.abort();
        refreshControllerRef.current = null;
        refreshInFlightRef.current = null;
      }
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
  }, [sessionId, refresh, stopPolling]);

  const endConversation = useCallback(async () => {
    let endError: Error | null = null;
    try {
      setError(null);
      stopPolling();
      if (refreshControllerRef.current) {
        refreshRequestSeqRef.current += 1;
        refreshControllerRef.current.abort();
        refreshControllerRef.current = null;
        refreshInFlightRef.current = null;
      }
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
  }, [sessionId, refresh, stopPolling]);

  const interrupt = useCallback(async () => {
    try {
      setError(null);
      await interruptSessionApi(sessionId);
    } catch (err) {
      setError((err as Error).message);
      throw err;
    }
  }, [sessionId]);

  const recover = useCallback(async () => {
    try {
      stopPolling();
      setLifecycleState('creating');
      if (refreshControllerRef.current) {
        refreshRequestSeqRef.current += 1;
        refreshControllerRef.current.abort();
        refreshControllerRef.current = null;
        refreshInFlightRef.current = null;
      }
      await recoverSessionApi(sessionId);
      await refresh({ force: true });
    } catch (err) {
      setError((err as Error).message);
      setLifecycleState('error');
    }
  }, [sessionId, refresh, stopPolling]);

  const deleteSession = useCallback(async () => {
    try {
      stopPolling();
      await deleteSessionApi(sessionId);
      setLifecycleState('deleted');
    } catch (err) {
      setError((err as Error).message);
    }
  }, [sessionId, stopPolling]);

  // Both detail reads and live interaction frames update this session-owned field.
  const pendingInteraction = session?.pending_interaction ?? null;

  return {
    session,
    lifecycleState,
    pendingInteraction,
    error,
    detailStale,
    refresh,
    bootstrapPendingInteraction,
    observePendingInteraction,
    clearPendingInteraction,
    interrupt,
    terminate,
    endConversation,
    recover,
    deleteSession,
  };
}
