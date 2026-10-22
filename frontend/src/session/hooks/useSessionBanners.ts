import type { SessionRecord } from '../../types';
import { localizeDisplayText } from '../../utils/format';
import {
  hasRuntimeProtocolError,
  shouldShowRuntimeUnavailableBanner,
  shouldShowSessionLastError,
  shouldShowSessionErrorBanner,
} from '../sessionAuthority';

// Derives header and banner visibility from session and lifecycle state, and
// selects the error text the banner under the header shows. What went wrong
// with one message or one response is shown on that message, not here.
export function useSessionBanners({
  session,
  hasPendingInteraction,
  lifecycleDetailStale,
  isTerminated,
  transparentlyRecoverable,
  isAssistantConversation,
  lifecycleError,
  streamTrouble,
  isWorking,
  isSending,
}: {
  session: SessionRecord;
  hasPendingInteraction: boolean;
  lifecycleDetailStale: boolean;
  isTerminated: boolean;
  transparentlyRecoverable: boolean;
  isAssistantConversation: boolean;
  lifecycleError: string | null;
  /** The session's stream has failed to connect for longer than a blip. */
  streamTrouble: boolean;
  isWorking: boolean;
  isSending: boolean;
}) {
  const showRuntimeUnavailableBanner = shouldShowRuntimeUnavailableBanner(session, hasPendingInteraction, lifecycleDetailStale, isTerminated, transparentlyRecoverable);
  const showSessionLastError = shouldShowSessionLastError(session, isTerminated, lifecycleDetailStale, transparentlyRecoverable);
  // Recovery requires a surviving Agent; deleted histories remain read-only.
  const showRecoverButton = ((isTerminated && !isAssistantConversation) || hasRuntimeProtocolError(session))
    && session.state !== 'DELETED'
    && session.agent_state !== 'DELETING' && session.agent_state !== 'DELETED';
  // An idle subscription follows future activity; losing it did not fail a user operation.
  const showTransientBackendMessage = streamTrouble && (isWorking || isSending) && !isTerminated && !lifecycleError;
  const showBanner = shouldShowSessionErrorBanner({
    showRetryBanner: showTransientBackendMessage,
    isTerminated,
    lifecycleError,
    historyError: null,
    hasPendingInteraction,
  });
  const bannerErrorMessage = localizeDisplayText(String(lifecycleError || '').trim());

  return {
    showRuntimeUnavailableBanner,
    showSessionLastError,
    showRecoverButton,
    showBanner,
    bannerErrorMessage,
    showTransientBackendMessage,
  };
}
