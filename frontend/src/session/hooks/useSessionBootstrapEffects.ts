import { useEffect, useRef } from 'react';
import { MANUAL_REFRESH_EVENT } from '../../utils/format';
import { isTransientSessionLoadError } from '../utils/chatHelpers';
import { shouldPollOverlayTruthGap } from '../sessionPolling';
import type { useSessionLifecycle } from './useSessionLifecycle';
import type { useFirstPageMessages } from './useFirstPageMessages';

// Bootstrap-phase side effects for SessionPage's outer component: (A) notify
// the parent app when the session's list-visible overview signature changes,
// (B) retry loading via the manual-refresh event while no session is resolved,
// (C) poll on a transient session-detail load error, and (D) poll until the
// durable snapshot catches up after a turn appears settled.
export function useSessionBootstrapEffects({
  sessionId,
  effectiveSession,
  effectiveLifecycleState,
  effectiveHistory,
  lifecycle,
  firstPage,
  onSessionChanged,
}: {
  sessionId: string;
  effectiveSession: ReturnType<typeof useSessionLifecycle>['session'];
  effectiveLifecycleState: string;
  effectiveHistory: ReturnType<typeof useFirstPageMessages>;
  lifecycle: ReturnType<typeof useSessionLifecycle>;
  firstPage: ReturnType<typeof useFirstPageMessages>;
  onSessionChanged: () => Promise<void>;
}): void {
  const overviewSignatureRef = useRef<string | null>(null);

  useEffect(() => {
    if (!effectiveSession) {
      overviewSignatureRef.current = null;
      return;
    }
    const runtime = effectiveSession.agent_runtime;
    const nextSignature = [
      effectiveSession.state,
      effectiveSession.title || '',
      effectiveSession.deleted ? 'deleted' : 'active',
      effectiveSession.startup_progress || '',
      runtime?.state || '',
      runtime?.startup_progress || '',
      runtime?.sandbox_id || '',
      runtime?.runtime_unavailable ? 'runtime-unavailable' : 'runtime-available',
      runtime?.last_error || '',
    ].join('|');
    if (overviewSignatureRef.current === nextSignature) return;
    overviewSignatureRef.current = nextSignature;
    void onSessionChanged();
  }, [
    effectiveSession?.agent_runtime?.last_error,
    effectiveSession?.agent_runtime?.runtime_unavailable,
    effectiveSession?.agent_runtime?.sandbox_id,
    effectiveSession?.agent_runtime?.startup_progress,
    effectiveSession?.agent_runtime?.state,
    effectiveSession?.deleted,
    effectiveSession?.startup_progress,
    effectiveSession?.state,
    effectiveSession?.title,
    effectiveSession,
    onSessionChanged,
  ]);

  useEffect(() => {
    if (!sessionId || effectiveSession) return;
    const handler = () => { void lifecycle.refresh({ force: true }); };
    window.addEventListener(MANUAL_REFRESH_EVENT, handler);
    return () => { window.removeEventListener(MANUAL_REFRESH_EVENT, handler); };
  }, [effectiveSession, lifecycle.refresh, sessionId]);

  useEffect(() => {
    const loadError = String(lifecycle.error ?? '').trim();
    if (!sessionId || effectiveSession || !isTransientSessionLoadError(loadError)) return;
    let stopped = false;
    let inFlight = false;
    const retry = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try { await lifecycle.refresh(); } finally { inFlight = false; }
    };
    const timer = window.setInterval(() => { void retry(); }, 3000);
    void retry();
    return () => { stopped = true; window.clearInterval(timer); };
  }, [effectiveSession, lifecycle.error, lifecycle.refresh, sessionId]);

  useEffect(() => {
    if (!sessionId || !shouldPollOverlayTruthGap(
      effectiveLifecycleState,
      effectiveSession,
      effectiveHistory.overlay,
    )) {
      return;
    }
    let stopped = false;
    let inFlight = false;
    const refreshTruth = async () => {
      if (stopped || inFlight) {
        return;
      }
      inFlight = true;
      try {
        await Promise.allSettled([
          lifecycle.refresh({ force: true }),
          firstPage.refetch(),
        ]);
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => {
      void refreshTruth();
    }, 1500);
    void refreshTruth();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [
    effectiveHistory.overlay?.turn_id,
    effectiveLifecycleState,
    effectiveSession?.current_turn_id,
    effectiveSession?.pending_interaction?.interaction_id,
    firstPage.refetch,
    lifecycle.refresh,
    sessionId,
  ]);
}
