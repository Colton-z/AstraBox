import type { SessionRecord } from '../types';

export function hasRuntimeProtocolError(session: SessionRecord): boolean {
  return session.recovery_reason === 'runtime_protocol_incompatible';
}

function isStarting(session: SessionRecord): boolean {
  return String(session.state ?? '').trim().toUpperCase() === 'CREATING';
}

export function shouldShowRuntimeUnavailableBanner(
  session: SessionRecord,
  hasPendingInteraction: boolean,
  lifecycleDetailStale: boolean,
  isTerminated: boolean,
  transparentlyRecoverable: boolean,
): boolean {
  // A transparently-recoverable agent_chat (sandbox reclaimed, re-borrows on the next
  // message) is a non-event — no "runtime disconnected" alarm. Nor is the
  // re-borrow itself: a session that is starting says so, and it stays marked
  // unavailable until the new runtime answers.
  if (lifecycleDetailStale || isTerminated || transparentlyRecoverable || isStarting(session)) {
    return false;
  }
  return !!session.runtime_unavailable && !hasPendingInteraction;
}

export function shouldShowSessionLastError(
  session: SessionRecord,
  isTerminated: boolean,
  lifecycleDetailStale: boolean,
  transparentlyRecoverable: boolean,
): boolean {
  // Same non-event case: suppress the stale "sandbox expired" error line. A terminated
  // session is never transparentlyRecoverable, so its last_error still surfaces.
  // While the session starts again, the error on record is the one the start
  // is recovering from.
  if (lifecycleDetailStale || transparentlyRecoverable || (!isTerminated && isStarting(session))) {
    return false;
  }
  return (isTerminated || !!session.runtime_unavailable || hasRuntimeProtocolError(session))
    && !!String(session.last_error ?? '').trim();
}

export function shouldShowSessionErrorBanner(
  options: {
    showRetryBanner: boolean;
    isTerminated: boolean;
    lifecycleError: string | null;
    historyError: string | null;
    hasPendingInteraction: boolean;
  },
): boolean {
  const {
    showRetryBanner,
    isTerminated,
    lifecycleError,
    historyError,
    hasPendingInteraction,
  } = options;
  if (showRetryBanner) {
    return true;
  }
  if (isTerminated) {
    return false;
  }
  if (String(lifecycleError ?? '').trim()) {
    return true;
  }
  return Boolean(String(historyError ?? '').trim() && !hasPendingInteraction);
}
