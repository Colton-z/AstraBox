import { useCallback, useEffect, useRef, useState } from 'react';
import { getSessionUserAction, performSessionUserAction } from '../../api';
import type { SessionUserActionState } from '../../types';
import { readUserAction } from '../userAction';

export function useSessionUserAction(sessionId: string, principalId: string, invalidation: string) {
  const owner = `${principalId}:${sessionId}`;
  const current = useRef(owner);
  current.current = owner;
  const generation = useRef(0);
  const operation = useRef<{ owner: string; generation: number } | null>(null);
  const [view, setView] = useState<{
    owner: string; invalidation: string; state: SessionUserActionState | null; busy: boolean; error: string | null;
  }>({ owner, invalidation, state: null, busy: true, error: null });

  const load = useCallback(async (actionId?: string) => {
    if (operation.current?.owner === owner) return;
    const token = ++generation.current;
    operation.current = { owner, generation: token };
    setView((previous) => ({
      owner, invalidation, state: previous.owner === owner ? previous.state : null, busy: true, error: null,
    }));
    try {
      const state = actionId === undefined
        ? await getSessionUserAction(sessionId)
        : await performSessionUserAction(sessionId, actionId);
      if (typeof state?.ready !== 'boolean' || !Array.isArray(state.actions)
          || state.actions.some((action) => !action || typeof action.id !== 'string' || !action.id
            || typeof action.label !== 'string' || !action.label.trim())
          || (!state.ready && !readUserAction(state.user_action))
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
  }, [owner, sessionId, invalidation]);

  useEffect(() => {
    operation.current = null;
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  const owned = view.owner === owner && view.invalidation === invalidation;
  return {
    state: owned ? view.state : null,
    busy: !owned || view.busy,
    error: owned ? view.error : null,
    ready: owned && !view.busy && !view.error && view.state?.ready === true,
    refresh: () => { void load(); },
    perform: (actionId: string) => { void load(actionId); },
  };
}
