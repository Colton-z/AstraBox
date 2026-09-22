import { useRef } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import type { useFirstPageMessages } from './useFirstPageMessages';
import type { useSessionLifecycle } from './useSessionLifecycle';
import { prepareInitialMessages } from '../prepareInitialMessages';
import { shouldBootstrapSessionMessages } from '../sessionBootstrapOwnership';

// Caches the initial SDK message list across renders, keyed by session id. The
// cache is populated during render as soon as bootstrap data is ready, ahead of
// the outer component's early-return loading/error states. The final read can
// fall back to `prepareInitialMessages` at the call site because it is not a
// hook call.
export function useInitialMessagesCache({
  sessionId,
  effectiveLifecycleState,
  effectiveSession,
  effectiveHistory,
}: {
  sessionId: string;
  effectiveLifecycleState: string;
  effectiveSession: ReturnType<typeof useSessionLifecycle>['session'];
  effectiveHistory: ReturnType<typeof useFirstPageMessages>;
}) {
  const initialMessagesRef = useRef<{
    sessionId: string;
    messages: SDKUIMessage[];
  } | null>(null);
  if (initialMessagesRef.current?.sessionId !== sessionId) {
    initialMessagesRef.current = null;
  }

  const dataReady = shouldBootstrapSessionMessages({
    sessionId,
    lifecycleState: effectiveLifecycleState,
    session: effectiveSession,
    historyOwnerSessionId: effectiveHistory.ownerSessionId,
    historyLoadedOnce: effectiveHistory.loadedOnce,
  });

  if (dataReady && initialMessagesRef.current === null && effectiveSession) {
    initialMessagesRef.current = {
      sessionId,
      messages: prepareInitialMessages(effectiveHistory.durableRecords, effectiveHistory.overlay, effectiveSession),
    };
  }

  return initialMessagesRef;
}
