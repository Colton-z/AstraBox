import { describe, expect, it } from 'vitest';

import { shouldReleaseSuppressedPendingInteraction } from './pendingInteractionState';

describe('shouldReleaseSuppressedPendingInteraction', () => {
  it('keeps a submitted interaction hidden while both projections refresh', () => {
    expect(
      shouldReleaseSuppressedPendingInteraction('answered', null, false),
    ).toBe(false);
  });

  it('releases suppression when the same authoritative interaction survives a failed submit', () => {
    expect(
      shouldReleaseSuppressedPendingInteraction('answered', 'answered', false),
    ).toBe(true);
  });

  it('releases suppression when a different interaction becomes current', () => {
    expect(
      shouldReleaseSuppressedPendingInteraction('answered', 'next', true),
    ).toBe(true);
  });
});
