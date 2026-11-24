import { useCallback, useState } from 'react';
import { getSessionUserAction, performSessionUserAction } from '../../api';
import { useUserActionGate } from '../../hooks/useUserActionGate';

/**
 * What the conversation's owner must do before it can run, kept current.
 *
 * `runtime` is the session as the page last read it: `starting` while its
 * runtime starts, `serving` while a runtime serves it, and the box it is bound
 * to. When the page watches a start bind the session to a different box that
 * then serves it, the start has met the owner's prerequisites: binding a new
 * box takes a startup, which reads them before it takes the box, and the
 * deployment's `prepare_session` refuses an engine activation the owner must
 * act on first (docs/providers/runtime-preparation.md). The gate takes that as
 * its answer for the binding the start produced instead of reading it again.
 * A start that ends without a serving runtime or on the same box (a
 * reattachment), and any later change to `invalidation`, is read as before.
 */
export function useSessionUserAction(
  sessionId: string,
  principalId: string,
  invalidation: string,
  enabled: boolean,
  runtime: { starting: boolean; serving: boolean; sandboxId: string | null },
) {
  const owner = `${principalId}:${sessionId}`;
  const [watched, setWatched] = useState({
    starting: runtime.starting, sandboxId: runtime.sandboxId, established: null as string | null,
  });
  if (watched.starting !== runtime.starting) {
    // Set during render so the gate never reads for the binding a start established.
    const bound = !runtime.starting && runtime.serving && runtime.sandboxId !== watched.sandboxId;
    setWatched({
      starting: runtime.starting,
      sandboxId: runtime.sandboxId,
      established: bound ? JSON.stringify([owner, invalidation]) : null,
    });
  }
  const read = useCallback(() => getSessionUserAction(sessionId), [sessionId]);
  const perform = useCallback((actionId: string) => performSessionUserAction(sessionId, actionId), [sessionId]);
  return useUserActionGate(
    owner, invalidation, enabled, read, perform, watched.established === JSON.stringify([owner, invalidation]),
  );
}
