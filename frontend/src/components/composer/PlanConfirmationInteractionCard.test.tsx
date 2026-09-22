// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { PendingPlanConfirmationInteraction } from '../../types';
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

const PLAN: PendingPlanConfirmationInteraction = {
  interaction_id: 'plan-interaction-1',
  turn_id: 'turn-1',
  tool_call_id: 'tool-call-plan-1',
  tool_name: 'ExitPlanMode',
  presentation: 'decision',
  body: '# Plan\nCreate one file, then verify it.',
  // The adapter declares the options; the card derives its radio rows from
  // them instead of a hardcoded vendor set.
  options: [
    {
      id: 'approve',
      denial: false,
      permission_mode_choices: ['bypassPermissions', 'acceptEdits', 'default'],
      default_permission_mode: 'default',
    },
    { id: 'revise', denial: true, applies_permission_mode: 'plan' },
    { id: 'reject', denial: true, applies_permission_mode: 'plan' },
  ],
  // Allowed directions ride the verbatim native input.
  raw_input: { allowedPrompts: [{ tool: 'Bash', prompt: 'Run the test suite' }] },
};

function renderCard(overrides: { submitting?: boolean } = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <PendingInteractionCard
      interaction={PLAN}
      submitting={overrides.submitting ?? false}
      onSubmit={onSubmit}
      variant="composer"
      stopControl={<button type="button">Stop generating</button>}
    />,
  );
  return onSubmit;
}

describe('PlanConfirmationInteractionCard', () => {
  it('offers the four exits as one radio group, not four separate tab stops', () => {
    renderCard();

    const group = screen.getByRole('radiogroup', { name: 'Exit plan mode options' });
    expect(screen.getAllByRole('radio')).toHaveLength(4);
    for (const radio of screen.getAllByRole('radio')) {
      expect(group.contains(radio)).toBe(true);
    }
    // `Approve once` is the choice the machine starts on.
    expect(screen.getByRole('radio', { name: /Approve once/ })).toHaveProperty(
      'ariaChecked',
      'true',
    );
  });

  it('selects an exit when the reader presses its row and approves with that mode', () => {
    const onSubmit = renderCard();

    // The whole row is the control: the reader aims at the label, not at the
    // 16px dot beside it.
    fireEvent.click(screen.getByText('Approve, auto-accept edits'));

    expect(screen.getByRole('radio', { name: /Approve, auto-accept edits/ })).toHaveProperty(
      'ariaChecked',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Approve and continue' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'plan-interaction-1',
      decision: 'approve',
      permission_mode: 'acceptEdits',
    });
  });

  it('opens the feedback box on reject and sends what the reader wrote', () => {
    const onSubmit = renderCard();

    expect(screen.queryByRole('textbox')).toBeNull();

    fireEvent.click(screen.getByText('Reject'));
    const box = screen.getByRole('textbox');
    // The shared field, so the product's one focus ring follows it
    // (docs/frontend-design.md §11).
    expect(box.getAttribute('data-slot')).toBe('textarea');

    fireEvent.change(box, { target: { value: 'Split it in two.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reject this plan' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'plan-interaction-1',
      decision: 'reject',
      comment: 'Split it in two.',
    });
  });

  it('lists the allowed next steps as a list a reader can be told the length of', () => {
    renderCard();

    const list = screen.getByRole('list');
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(list.textContent).toContain('Bash');
    expect(list.textContent).toContain('Run the test suite');
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
