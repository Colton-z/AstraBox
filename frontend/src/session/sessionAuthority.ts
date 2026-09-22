import type { SessionRecord } from '../types';

export function shouldShowRuntimeUnavailableBanner(
  session: SessionRecord,
  hasPendingInteraction: boolean,
  lifecycleDetailStale: boolean,
  isTerminated: boolean,
  transparentlyRecoverable: boolean,
): boolean {
  // A transparently-recoverable agent_chat (sandbox reclaimed, re-borrows on the next
  // message) is a non-event — no "runtime disconnected" alarm.
  if (lifecycleDetailStale || isTerminated || transparentlyRecoverable) {
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
  if (lifecycleDetailStale || transparentlyRecoverable) {
    return false;
  }
  return (isTerminated || !!session.runtime_unavailable) && !!String(session.last_error ?? '').trim();
}

export function shouldShowRecoverButton(
  lifecycleState: string,
  session: Pick<SessionRecord, 'recovery_policy'>,
  isTerminated: boolean,
): boolean {
  if (isTerminated) {
    return true;
  }
  return lifecycleState === 'recovery' && session.recovery_policy !== 'auto';
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
