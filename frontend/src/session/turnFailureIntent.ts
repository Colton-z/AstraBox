import type { SessionRecord } from '../types';

export const USER_INTERRUPTED_TURN_ERROR = 'Request interrupted by user';

export function isUserInterruptedTurnFailure(
  session: Pick<SessionRecord, 'last_turn_status' | 'last_turn_error'> | null | undefined,
): boolean {
  if (!session) return false;
  return String(session.last_turn_status ?? '').trim() === 'FAILED'
    && String(session.last_turn_error ?? '').trim() === USER_INTERRUPTED_TURN_ERROR;
}
