import type { SessionRecord } from '../types';
import { hasRuntimeProtocolError } from './sessionAuthority';
import {
  isTransparentlyRecoverableAgentSession,
  sessionOperationalStatusLabel,
} from '../utils/format';
import { toneForState, type PillTone } from '../components/AstraConsole';

type TranslateFn = (key: string, opts?: Record<string, unknown>) => string;

export interface SessionRunStatus {
  transparentlyRecoverable: boolean;
  /** A message sent now goes straight to an idle runtime. */
  canSendNow: boolean;
  /** A message sent now waits behind the response that runs, or for the session to start. */
  canQueueMessage: boolean;
  canSend: boolean;
  headerStatusLabel: string;
  headerIsLive: boolean;
  headerTone: PillTone;
  headerRunState: string;
}

/**
 * Derive send eligibility and every header run indicator from one view of the
 * session so the controls, label, pulse, tone, and data state agree.
 *
 * The session accepts a message in every state but TERMINATED and DELETED: a
 * message sent while a response runs, or while the session starts, waits its
 * turn on the server.
 */
export function computeSessionRunStatus({
  session,
  lifecycleState,
  isTerminated,
  hasPendingInteraction,
  isSending,
  isWorking,
  isStopping,
  t,
}: {
  session: SessionRecord;
  lifecycleState: string;
  isTerminated: boolean;
  hasPendingInteraction: boolean;
  isSending: boolean;
  isWorking: boolean;
  isStopping: boolean;
  t: TranslateFn;
}): SessionRunStatus {
  const transparentlyRecoverable = isTransparentlyRecoverableAgentSession(session);
  const protocolError = hasRuntimeProtocolError(session);
  const accepting = !isTerminated
    && !protocolError
    && (lifecycleState === 'ready' || lifecycleState === 'busy' || lifecycleState === 'creating')
    && !hasPendingInteraction
    && !isStopping;
  const waits = isWorking || lifecycleState === 'creating';
  const canSendNow = accepting && !waits;
  const canQueueMessage = accepting && waits;
  const headerIsLive = !isTerminated && !protocolError && (isWorking || isSending);
  const headerStatusLabel = isTerminated
    ? t('chat:status.terminated')
    : protocolError
      ? sessionOperationalStatusLabel(session)
      : hasPendingInteraction
        ? t('chat:status.awaiting_answer')
        : isStopping
          ? t('chat:status.stopping')
          : isWorking
            ? t('chat:status.processing')
            : isSending
              ? t('chat:status.sending')
              : transparentlyRecoverable
                // A conversation whose sandbox is gone borrows a new one with the
                // next message, so it reads ready.
                ? t('chat:status.ready')
                : sessionOperationalStatusLabel(session);
  // Derive tone from state because the label is translated. Matching English
  // label text would classify localized live states as idle.
  const headerTone: PillTone = isTerminated
    ? 'idle'
    : protocolError
      ? toneForState('RECOVERY_REQUIRED')
      : hasPendingInteraction
        ? 'pending'
        : headerIsLive
          ? 'running'
          : transparentlyRecoverable
            ? 'done'
            : toneForState(session.state);
  // Raw run-state string for the pill's `data-state`, kept consistent with the
  // tone above so a live response reads PROCESSING and a settled one READY.
  const headerRunState: string = isTerminated
    ? 'TERMINATED'
    : protocolError
      ? 'RECOVERY_REQUIRED'
      : hasPendingInteraction
        ? 'WAITING_INPUT'
        : headerIsLive
          ? 'PROCESSING'
          : lifecycleState === 'creating'
            ? 'CREATING'
            : String(session.state || 'READY').toUpperCase();

  return {
    transparentlyRecoverable,
    canSendNow,
    canQueueMessage,
    canSend: canSendNow || canQueueMessage,
    headerStatusLabel,
    headerIsLive,
    headerTone,
    headerRunState,
  };
}
