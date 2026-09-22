import type { SessionRecord } from '../types';

export function doesSessionDetailBelongToRoute(
  sessionId: string,
  session: SessionRecord | null | undefined,
): boolean {
  return String(session?.session_id ?? '').trim() === String(sessionId).trim();
}

export function doesFirstPageBelongToRoute(
  sessionId: string,
  ownerSessionId: string | null | undefined,
): boolean {
  return String(ownerSessionId ?? '').trim() === String(sessionId).trim();
}

export function shouldBootstrapSessionMessages({
  sessionId,
  lifecycleState,
  session,
  historyOwnerSessionId,
  historyLoadedOnce,
}: {
  sessionId: string;
  lifecycleState: string;
  session: SessionRecord | null | undefined;
  historyOwnerSessionId: string | null | undefined;
  historyLoadedOnce: boolean;
}): boolean {
  if (!doesSessionDetailBelongToRoute(sessionId, session)) {
    return false;
  }
  if (lifecycleState === 'loading' || lifecycleState === 'error') {
    return false;
  }
  if (lifecycleState === 'creating') {
    return true;
  }
  return doesFirstPageBelongToRoute(sessionId, historyOwnerSessionId) && historyLoadedOnce;
}
