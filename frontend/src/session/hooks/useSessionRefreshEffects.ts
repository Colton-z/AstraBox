import { useEffect, useRef, useState } from 'react';
import { MANUAL_REFRESH_EVENT } from '../../utils/format';

// Maintains two independent refresh triggers: a nonce that makes the files
// panel refetch after work ends, and a rehydrate call when the app-wide
// manual-refresh event fires.
export function useSessionRefreshEffects({
  working,
  handleRehydrate,
}: {
  /** A response runs, or a message is on its way. */
  working: boolean;
  handleRehydrate: () => Promise<void>;
}) {
  // The agent writes files into the workspace while it works; the files panel
  // otherwise only fetches on mount + manual refresh. Bump a nonce on the
  // working true→false transition; the panel refetches its open directories
  // once. A sub-agent that finishes on its own is answered by a response the
  // runtime starts itself, so its writes land before that edge too.
  const [filesRefreshNonce, setFilesRefreshNonce] = useState(0);
  const prevWorkingRef = useRef(false);
  useEffect(() => {
    const wasWorking = prevWorkingRef.current;
    prevWorkingRef.current = working;
    if (wasWorking && !working) {
      setFilesRefreshNonce((n) => n + 1);
    }
  }, [working]);

  useEffect(() => {
    const handler = () => { void handleRehydrate(); };
    window.addEventListener(MANUAL_REFRESH_EVENT, handler);
    return () => { window.removeEventListener(MANUAL_REFRESH_EVENT, handler); };
  }, [handleRehydrate]);

  return { filesRefreshNonce };
}
