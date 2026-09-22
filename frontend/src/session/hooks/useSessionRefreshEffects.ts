import { useEffect, useRef, useState } from 'react';
import { MANUAL_REFRESH_EVENT } from '../../utils/format';

// Maintains two independent refresh triggers: a nonce that makes the files
// panel refetch after a turn becomes inactive, and a rehydrate call when the
// app-wide manual-refresh event fires.
export function useSessionRefreshEffects({
  isStreaming,
  isSubmitted,
  lifecycleState,
  handleRehydrate,
}: {
  isStreaming: boolean;
  isSubmitted: boolean;
  lifecycleState: string;
  handleRehydrate: () => Promise<void>;
}) {
  // The agent writes files into the workspace while it works; the files panel
  // otherwise only fetches on mount + manual refresh. Bump a nonce on the
  // work-active true→false transition; the panel refetches its open
  // directories once. Work is a turn (isStreaming||isSubmitted) or a
  // background child ('background'): a child that finishes while nobody
  // types is the same edge, and one whose completion the engine answers with
  // a turn (background→busy→ready) stays active until that answer ends, so
  // the edge lands after every write either of them made.
  const [filesRefreshNonce, setFilesRefreshNonce] = useState(0);
  const prevWorkActiveRef = useRef(false);
  useEffect(() => {
    const workActive = isStreaming || isSubmitted || lifecycleState === 'background';
    const wasActive = prevWorkActiveRef.current;
    prevWorkActiveRef.current = workActive;
    if (wasActive && !workActive) {
      setFilesRefreshNonce((n) => n + 1);
    }
  }, [isStreaming, isSubmitted, lifecycleState]);

  useEffect(() => {
    const handler = () => { void handleRehydrate(); };
    window.addEventListener(MANUAL_REFRESH_EVENT, handler);
    return () => { window.removeEventListener(MANUAL_REFRESH_EVENT, handler); };
  }, [handleRehydrate]);

  return { filesRefreshNonce };
}
