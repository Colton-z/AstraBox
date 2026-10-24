import type { SessionRecord } from '../types';

type PollingSession = Pick<SessionRecord, 'state' | 'current_responses' | 'pending_interaction' | 'held_inputs'>;

const WORKING_POLL_INTERVAL_MS = 3000;
const SETTLING_POLL_INTERVAL_MS = 1500;

/**
 * Whether the session is at rest: ready, with no response running, no input
 * the platform still holds, and no interaction waiting on its owner. A
 * terminated or deleted session is at rest too; nothing will change it.
 */
export function isSessionAtRest(session: PollingSession | null | undefined): boolean {
  const state = String(session?.state ?? '');
  if (state === 'TERMINATED' || state === 'DELETED') return true;
  return state === 'READY'
    && !session?.current_responses?.length
    && !session?.held_inputs?.length
    && !session?.pending_interaction;
}

/**
 * How long until the session's detail is read again, or `null` while nothing
 * about it is expected to change. The conversation's stream asks for a read
 * itself when startup changes or a response starts or ends; the timer covers
 * inputs held and runtime activity while a conversation is not at rest.
 */
export function sessionDetailPollInterval(
  lifecycleState: string,
  session: PollingSession | null | undefined,
  detailStale: boolean,
): number | null {
  if (detailStale) return SETTLING_POLL_INTERVAL_MS;
  if (lifecycleState === 'busy') return WORKING_POLL_INTERVAL_MS;
  if (lifecycleState !== 'ready') return null;
  if (isSessionAtRest(session)) return null;
  return session?.pending_interaction ? WORKING_POLL_INTERVAL_MS : SETTLING_POLL_INTERVAL_MS;
}

/**
 * Whether the child-run list is read on a timer: a sub-agent is still working
 * while no response of the conversation's own is, so nothing on the stream
 * marks its progress.
 */
export function shouldPollChildRuns(options: {
  liveSubagentCount: number;
  working: boolean;
  hasPendingInteraction: boolean;
}): boolean {
  return options.liveSubagentCount > 0 && !options.working && !options.hasPendingInteraction;
}
