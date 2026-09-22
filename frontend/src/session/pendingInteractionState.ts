import type { PendingInteraction } from '../types';

export function getVisiblePendingInteraction(
  pendingInteraction: PendingInteraction | null,
  ...hiddenInteractionIds: Array<string | null | undefined>
): PendingInteraction | null {
  const currentInteractionId = String(pendingInteraction?.interaction_id ?? '').trim();
  const shouldHideCurrentInteraction = currentInteractionId
    && hiddenInteractionIds.some((value) => String(value ?? '').trim() === currentInteractionId);
  if (shouldHideCurrentInteraction) {
    return null;
  }
  return pendingInteraction;
}

export function shouldReleaseSuppressedPendingInteraction(
  suppressedInteractionId: string | null | undefined,
  authoritativeInteractionId: string | null | undefined,
  interactionSubmitting: boolean,
): boolean {
  const suppressedId = String(suppressedInteractionId ?? '').trim();
  if (!suppressedId) return false;

  const authoritativeId = String(authoritativeInteractionId ?? '').trim();
  // Submission failed or never advanced: the same authoritative pending
  // interaction is still active after the submit attempt finished.
  if (!interactionSubmitting && authoritativeId === suppressedId) {
    return true;
  }

  // A different authoritative interaction has replaced it.
  if (authoritativeId && authoritativeId !== suppressedId) {
    return true;
  }

  return false;
}
