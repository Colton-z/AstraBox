import type { SessionRecord } from '../types';
import {
  agentRuntimeWakePhaseLabel,
  isAgentRuntimeDeletedForSession,
  isTransparentlyRecoverableAgentSession,
  sessionOperationalStatusLabel,
} from '../utils/format';
import { toneForState, type PillTone } from '../components/AstraConsole';

type TranslateFn = (key: string, opts?: Record<string, unknown>) => string;

export interface SessionRunStatus {
  isAgentRuntimeDeleted: boolean;
  transparentlyRecoverable: boolean;
  canSendNow: boolean;
  canQueueMessage: boolean;
  canSend: boolean;
  headerStatusLabel: string;
  headerIsLive: boolean;
  headerTone: PillTone;
  headerRunState: string;
}

/**
 * Derive send eligibility and every header run indicator from one effective
 * lifecycle state so the controls, label, pulse, tone, and data state agree.
 */
export function computeSessionRunStatus({
  session,
  lifecycleState,
  isTerminated,
  hasPendingInteraction,
  isSubmitted,
  isStreaming,
  isInterruptSettling,
  t,
}: {
  session: SessionRecord;
  lifecycleState: string;
  isTerminated: boolean;
  hasPendingInteraction: boolean;
  isSubmitted: boolean;
  isStreaming: boolean;
  isInterruptSettling: boolean;
  t: TranslateFn;
}): SessionRunStatus {
  const effectiveLifecycleState = isTerminated
    ? 'terminated'
    : hasPendingInteraction
    ? 'interaction'
    : isSubmitted || isStreaming
      ? 'busy'
      : lifecycleState;
  const isAgentRuntimeDeleted = isAgentRuntimeDeletedForSession(session);
  // Per-session: a reclaimed/expired sandbox re-borrows transparently on the next
  // message, so it reads ready — never runtime disconnected / sandbox expired / recovery required.
  const transparentlyRecoverable = isTransparentlyRecoverableAgentSession(session);
  const canSendNow = (effectiveLifecycleState === 'ready' || effectiveLifecycleState === 'background') && !isAgentRuntimeDeleted && !isStreaming && !isSubmitted && !hasPendingInteraction && !isInterruptSettling;
  const canQueueMessage = !isAgentRuntimeDeleted && !hasPendingInteraction && !isInterruptSettling && (effectiveLifecycleState === 'busy' || effectiveLifecycleState === 'creating' || lifecycleState === 'creating');
  const canSend = canSendNow || canQueueMessage;
  // `isSubmitted` covers the delivery window while the durable lifecycle
  // projection can still read `ready`. `isStreaming` begins after that gap, so
  // it cannot identify the locally bridged busy state.
  const isLocallyBridgedBusy = isSubmitted && lifecycleState === 'ready';
  const sendingWakePhaseLabel = isSubmitted ? agentRuntimeWakePhaseLabel(session) : '';
  // "Sending" names the pre-delivery wake and dispatch window. After the sandbox
  // receives the turn, the Agent is processing it even if the local chat status
  // remains `submitted` until the first streamable chunk. The server's
  // `delivery_state` distinguishes those phases for the header label.
  const turnDeliveredToSandbox = String(session.delivery_state ?? '').trim().toUpperCase() === 'RECEIVED';
  const headerStatusLabel = isTerminated
    ? t('chat:status.terminated')
    : isAgentRuntimeDeleted
    ? t('chat:status.terminated')
    : hasPendingInteraction
    ? t('chat:status.awaiting_answer')
    : isInterruptSettling
      ? t('chat:status.stopping')
      : isSubmitted
        ? turnDeliveredToSandbox
          ? t('chat:status.processing')
          : sendingWakePhaseLabel
            ? t('chat:status.sending_with_phase', { phase: sendingWakePhaseLabel })
            : t('chat:status.sending')
        : isStreaming
          // Live-stream and post-stream durable settlement both read "processing".
          // Committing the terminal frame and projecting READY is an internal,
          // sub-second boundary; separate labels would expose that boundary as a
          // generating → processing flicker before the user-visible ready state.
          ? t('chat:status.processing')
          : effectiveLifecycleState === 'recovery'
            ? transparentlyRecoverable ? t('chat:status.ready') : t('chat:status.pending_recovery')
            : effectiveLifecycleState === 'background'
              ? t('chat:status.background_running')
            : isLocallyBridgedBusy
              ? t('chat:status.processing')
              // A transparently-recoverable agent_chat (sandbox gone / agent hibernating,
              // re-borrows on next message) reads ready — not hibernating / runtime disconnected / sandbox expired.
              : transparentlyRecoverable
                ? t('chat:status.ready')
                : sessionOperationalStatusLabel(session);
  // Header live/tone derivation — drives the astra pulse + the status pill color.
  const headerIsLive = isStreaming || isSubmitted || effectiveLifecycleState === 'busy' || effectiveLifecycleState === 'background';
  const headerTone: PillTone = isTerminated || isAgentRuntimeDeleted
    ? 'idle'
    : hasPendingInteraction
      ? 'pending'
      : headerIsLive
        ? 'running'
        // Derive tone from state because the label is translated. Matching
        // English label text would classify localized live states as idle.
        : transparentlyRecoverable
          ? 'done'
          : toneForState(session.state);
  // Raw run-state string for the pill's `data-state`, kept consistent with the
  // tone above so a live turn reads PROCESSING+astra and a settled one READY+mint.
  const headerRunState: string = isTerminated || isAgentRuntimeDeleted
    ? 'TERMINATED'
    : hasPendingInteraction
      ? 'WAITING_INPUT'
      : headerIsLive
        ? 'PROCESSING'
        : effectiveLifecycleState === 'creating'
          ? 'CREATING'
          : effectiveLifecycleState === 'recovery'
            ? 'RECOVERY_REQUIRED'
            : String(session.state || 'READY').toUpperCase();

  return {
    isAgentRuntimeDeleted,
    transparentlyRecoverable,
    canSendNow,
    canQueueMessage,
    canSend,
    headerStatusLabel,
    headerIsLive,
    headerTone,
    headerRunState,
  };
}
