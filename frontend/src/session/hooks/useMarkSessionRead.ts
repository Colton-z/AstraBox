import { useEffect } from 'react';

import { markSessionRead } from '@/api';

/**
 * Record that the owner has read this conversation while it is on screen.
 *
 * The list marks a conversation unread when it gained a reply after its
 * ``read_at`` (a scheduled run, a heartbeat, a long turn finishing while the
 * owner was elsewhere). Reading happens when the conversation is open in a
 * visible tab: on arrival, on each new reply, when the tab comes back into
 * view, and on leaving, which covers replies that streamed in while it was
 * open.
 */
export function useMarkSessionRead(sessionId: string, lastOutputAt: string | null | undefined): void {
  useEffect(() => {
    if (!sessionId) return undefined;
    const markIfVisible = () => {
      if (document.visibilityState !== 'visible') return;
      markSessionRead(sessionId).catch((failure: unknown) => {
        // A missed read mark only leaves the conversation shown as unread.
        console.warn('marking the conversation read failed', failure);
      });
    };
    markIfVisible();
    document.addEventListener('visibilitychange', markIfVisible);
    return () => {
      document.removeEventListener('visibilitychange', markIfVisible);
      markIfVisible();
    };
  }, [sessionId, lastOutputAt]);
}
