import { useEffect, useRef, useState } from 'react';

import type { SessionRecord } from '@/types';

/** A conversation gained a reply after its owner last read it. */
export function isUnread(session: SessionRecord): boolean {
  const output = session.last_output_at;
  if (!output) return false;
  return !session.read_at || output > session.read_at;
}

function notificationsSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

/** The browser's notification permission, and a way to ask for it. */
export function useNotificationPermission(): {
  permission: NotificationPermission | 'unsupported';
  request: () => void;
} {
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(
    notificationsSupported() ? Notification.permission : 'unsupported',
  );
  return {
    permission,
    request: () => {
      if (!notificationsSupported()) return;
      void Notification.requestPermission().then(setPermission);
    },
  };
}

/**
 * Show a browser notification for each new unread reply the list reveals.
 *
 * A reply the owner did not wait for — a scheduled run, a heartbeat, a long
 * turn that finished while they were elsewhere — is announced once, when the
 * list first shows it unread, unless that conversation is open in a visible
 * tab. The first list read only records what is already there.
 */
export function useReplyNotifications(
  sessions: SessionRecord[],
  activeSessionId: string | null,
  options: { title: (session: SessionRecord) => string; body: string; open: (sessionId: string) => void },
): void {
  const announced = useRef<Map<string, string> | null>(null);
  const { title, body, open } = options;
  useEffect(() => {
    if (sessions.length === 0 && announced.current === null) return;
    const first = announced.current === null;
    const seen = announced.current ?? new Map<string, string>();
    announced.current = seen;
    for (const session of sessions) {
      const output = session.last_output_at;
      if (!output || seen.get(session.session_id) === output) continue;
      seen.set(session.session_id, output);
      if (first || !isUnread(session)) continue;
      const watching = session.session_id === activeSessionId && document.visibilityState === 'visible';
      if (watching || !notificationsSupported() || Notification.permission !== 'granted') continue;
      const notification = new Notification(title(session), { body, tag: session.session_id });
      notification.onclick = () => {
        window.focus();
        open(session.session_id);
        notification.close();
      };
    }
  }, [sessions, activeSessionId, title, body, open]);
}
