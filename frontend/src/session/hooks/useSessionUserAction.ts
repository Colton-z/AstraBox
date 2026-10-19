import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, getSessionUserAction, performSessionUserAction } from '../../api';
import { useKeepCurrent } from '../../hooks/useKeepCurrent';
import type { SessionUserActionState } from '../../types';
import { readUserAction } from '../userAction';

export function useSessionUserAction(sessionId: string, principalId: string, invalidation: string, enabled: boolean) {
  const owner = `${principalId}:${sessionId}`;
  const current = useRef<string | null>(enabled ? owner : null);
  current.current = enabled ? owner : null;
  const generation = useRef(0);
  const operation = useRef<{ owner: string; generation: number } | null>(null);
  const [view, setView] = useState<{
    owner: string; invalidation: string; state: SessionUserActionState | null; busy: boolean; error: string | null;
  }>({ owner, invalidation, state: null, busy: true, error: null });

  const load = useCallback(async (actionId?: string) => {
    if (!enabled || current.current !== owner || operation.current?.owner === owner) return;
    const token = ++generation.current;
    operation.current = { owner, generation: token };
    setView((previous) => ({
      owner, invalidation, state: previous.owner === owner ? previous.state : null, busy: true, error: null,
    }));
    try {
      const state = actionId === undefined
        ? await getSessionUserAction(sessionId)
        : await performSessionUserAction(sessionId, actionId).catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 409 && error.code === 'INVALID_REQUEST'
            && current.current === owner && generation.current === token) {
            return getSessionUserAction(sessionId);
          }
          throw error;
        });
      if (typeof state?.applicable !== 'boolean' || typeof state.ready !== 'boolean' || !Array.isArray(state.actions)
          || state.actions.some((action) => !action || typeof action.id !== 'string' || !action.id
            || typeof action.label !== 'string' || !action.label.trim())
          || (!state.applicable && (state.ready || state.user_action != null || state.actions.length > 0))
          || (state.applicable && !state.ready && !readUserAction(state.user_action))
          || (state.ready && (state.user_action != null || state.actions.length > 0))) {
        throw new Error('Invalid session user-action response');
      }
      if (current.current === owner && generation.current === token) {
        setView({ owner, invalidation, state, busy: false, error: null });
      }
    } catch (error) {
      if (current.current === owner && generation.current === token) {
        setView((previous) => ({ ...previous, owner, invalidation, busy: false, error: String(error) }));
      }
    } finally {
      if (operation.current?.generation === token) operation.current = null;
    }
  }, [owner, sessionId, invalidation, enabled]);

  useEffect(() => {
    operation.current = null;
    void load();
    return () => { generation.current += 1; };
  }, [load]);
  useKeepCurrent(() => load());

  const owned = enabled && view.owner === owner && view.invalidation === invalidation;
  return {
    applicable: enabled && (!owned || view.state?.applicable !== false),
    state: owned ? view.state : null,
    busy: enabled && (!owned || view.busy),
    error: owned ? view.error : null,
    ready: owned && !view.busy && !view.error && view.state?.ready === true,
    refresh: () => { void load(); },
    perform: (actionId: string) => { void load(actionId); },
  };
}
