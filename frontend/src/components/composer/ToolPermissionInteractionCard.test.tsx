// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { PendingToolPermissionInteraction } from '../../types';
import { PendingInteractionCard } from '../Composer';

// Rendered through `PendingInteractionCard` rather than the card directly, so
// every case runs against the interaction state machine that actually holds
// the choice — a card driven by props a test invented would pass whether or
// not the two still fit together.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

// A tool-permission record carries the approval request and nothing else — no
// suggested alternatives — so the single allow-once choice is the whole radio
// group the card renders.
const PERMISSION: PendingToolPermissionInteraction = {
  interaction_id: 'interaction-1',
  turn_id: 'turn-1',
  tool_call_id: 'tool-call-1',
  tool_name: 'Write',
  presentation: 'tool_approval',
  raw_input: { file_path: 'notes.txt', content: 'hello' },
};

function renderCard(overrides: { submitting?: boolean } = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <PendingInteractionCard
      interaction={PERMISSION}
      submitting={overrides.submitting ?? false}
      onSubmit={onSubmit}
      variant="composer"
      stopControl={<button type="button">Stop generating</button>}
    />,
  );
  return onSubmit;
}

describe('ToolPermissionInteractionCard', () => {
  it('offers allow-once as one radio group', () => {
    renderCard();

    const group = screen.getByRole('radiogroup', { name: 'Permission options' });
    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(1);
    for (const radio of radios) {
      expect(group.contains(radio)).toBe(true);
    }
    expect(screen.getByRole('radio', { name: /Allow once/ })).toHaveProperty(
      'ariaChecked',
      'true',
    );
  });

  it('approves the one action', () => {
    const onSubmit = renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Allow and continue' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'interaction-1',
      decision: 'approve',
    });
  });

  it('rejects the one action', () => {
    const onSubmit = renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Reject this action' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'interaction-1',
      decision: 'reject',
    });
  });

  it.each([
    ['while the reader is deciding', false],
    ['while a decision is in flight', true],
  ])('keeps the stop control reachable %s', (_case, submitting) => {
    renderCard({ submitting });

    const stop = screen.getByRole('button', { name: 'Stop generating' });
    expect((stop as HTMLButtonElement).disabled).toBe(false);
  });
});
