import { useCallback } from 'react';
import { getSessionUserAction, performSessionUserAction } from '../../api';
import { useUserActionGate } from '../../hooks/useUserActionGate';

/** What the conversation's owner must do before it can run, kept current. */
export function useSessionUserAction(sessionId: string, principalId: string, invalidation: string, enabled: boolean) {
  const read = useCallback(() => getSessionUserAction(sessionId), [sessionId]);
  const perform = useCallback((actionId: string) => performSessionUserAction(sessionId, actionId), [sessionId]);
  return useUserActionGate(`${principalId}:${sessionId}`, invalidation, enabled, read, perform);
}
