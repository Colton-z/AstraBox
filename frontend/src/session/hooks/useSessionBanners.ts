import type { SessionRecord } from '../../types';
import { localizeDisplayText } from '../../utils/format';
import {
  shouldShowRuntimeUnavailableBanner,
  shouldShowSessionLastError,
  shouldShowRecoverButton,
  shouldShowSessionErrorBanner,
} from '../sessionAuthority';

// Derives header and banner visibility from session, lifecycle, and history
// state, and selects the error text displayed by the retry banner.
export function useSessionBanners({
  session,
  hasPendingInteraction,
  lifecycleDetailStale,
  isTerminated,
  transparentlyRecoverable,
  isAssistantConversation,
  isAgentRuntimeDeleted,
  lifecycleState,
  showRetryBanner,
  lifecycleError,
  historyError,
  chatErrorMessage,
  transientBannerVisible,
}: {
  session: SessionRecord;
  hasPendingInteraction: boolean;
  lifecycleDetailStale: boolean;
  isTerminated: boolean;
  transparentlyRecoverable: boolean;
  isAssistantConversation: boolean;
  isAgentRuntimeDeleted: boolean;
  lifecycleState: string;
  showRetryBanner: boolean;
  lifecycleError: string | null;
  historyError: string | null;
  chatErrorMessage: string | undefined;
  transientBannerVisible: boolean;
}) {
  const showRuntimeUnavailableBanner = shouldShowRuntimeUnavailableBanner(session, hasPendingInteraction, lifecycleDetailStale, isTerminated, transparentlyRecoverable);
  const showSessionLastError = shouldShowSessionLastError(session, isTerminated, lifecycleDetailStale, transparentlyRecoverable);
  const showRecoverButton = !isAssistantConversation && !isAgentRuntimeDeleted && !transparentlyRecoverable && shouldShowRecoverButton(lifecycleState, session, isTerminated);
  const combinedShowRetryBanner = shouldShowSessionErrorBanner({
    showRetryBanner,
    isTerminated,
    lifecycleError,
    historyError,
    hasPendingInteraction,
  });
  const bannerErrorMessage = localizeDisplayText(
    String(chatErrorMessage || lifecycleError || historyError || '').trim(),
  );
  const showTransientBackendMessage = transientBannerVisible && !lifecycleError && !historyError;

  return {
    showRuntimeUnavailableBanner,
    showSessionLastError,
    showRecoverButton,
    combinedShowRetryBanner,
    bannerErrorMessage,
    showTransientBackendMessage,
  };
}
