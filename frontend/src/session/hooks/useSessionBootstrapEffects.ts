import { useEffect, useRef } from 'react';
import { MANUAL_REFRESH_EVENT } from '../../utils/format';
import { isTransientSessionLoadError } from '../utils/chatHelpers';
import type { useSessionLifecycle } from './useSessionLifecycle';

// Bootstrap-phase side effects for SessionPage's outer component: (A) notify
// the parent app when the session's list-visible overview changes, (B) retry
// loading via the manual-refresh event while no session is resolved, and (C)
// poll on a transient session-detail load error.
export function useSessionBootstrapEffects({
  sessionId,
  effectiveSession,
  lifecycle,
  onSessionChanged,
}: {
  sessionId: string;
  effectiveSession: ReturnType<typeof useSessionLifecycle>['session'];
  lifecycle: ReturnType<typeof useSessionLifecycle>;
  onSessionChanged: () => Promise<void>;
}): void {
  const overviewSignatureRef = useRef<string | null>(null);

  useEffect(() => {
    if (!effectiveSession) {
      overviewSignatureRef.current = null;
      return;
    }
    const nextSignature = [
      effectiveSession.state,
      effectiveSession.title || '',
      effectiveSession.startup_progress || '',
      effectiveSession.runtime_unavailable ? 'runtime-unavailable' : 'runtime-available',
    ].join('|');
    if (overviewSignatureRef.current === nextSignature) return;
    overviewSignatureRef.current = nextSignature;
    void onSessionChanged();
  }, [effectiveSession, onSessionChanged]);

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
}
