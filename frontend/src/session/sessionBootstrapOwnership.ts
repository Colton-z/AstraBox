import type { SessionRecord } from '../types';

/** Whether a session detail read is the current route's: a slow read for the previous route can land after navigation. */
export function doesSessionDetailBelongToRoute(
  sessionId: string,
  session: SessionRecord | null | undefined,
): boolean {
  return String(session?.session_id ?? '').trim() === String(sessionId).trim();
}
