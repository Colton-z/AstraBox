// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { PendingPlanConfirmationInteraction } from '../../types';
import { PendingInteractionCard } from '../Composer';

// Render through PendingInteractionCard to exercise its routing: sending
// decisions with engine-specific ids to the exit-plan card would leave them
// without matching `approve` or `reject` options.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

// Codex's command approval, as its adapter declares it.
const APPROVAL: PendingPlanConfirmationInteraction = {
  interaction_id: 'codex-14',
  turn_id: 'turn-1',
  tool_call_id: 'item-9',
  tool_name: 'commandExecution',
  presentation: 'decision',
  prompt: 'This command writes outside the workspace.',
  body: 'Command: rm -rf /tmp/cache\nWorking directory: /home/agent/workspace',
  options: [
    { id: 'accept', denial: false },
    { id: 'acceptForSession', denial: false },
    { id: 'decline', denial: true },
    { id: 'cancel', denial: true },
  ],
  raw_input: { itemId: 'item-9', command: 'rm -rf /tmp/cache' },
};

function renderCard(
  interaction: PendingPlanConfirmationInteraction = APPROVAL,
  overrides: { submitting?: boolean } = {},
) {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  render(
    <PendingInteractionCard
      interaction={interaction}
      submitting={overrides.submitting ?? false}
      onSubmit={onSubmit}
      variant="composer"
      stopControl={<button type="button">Stop generating</button>}
    />,
  );
  return onSubmit;
}

describe('DecisionInteractionCard', () => {
  it('offers every option the adapter declared, as one radio group', () => {
    renderCard();

    const group = screen.getByRole('radiogroup', { name: 'Decision options' });
    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(4);
    for (const radio of radios) {
      expect(group.contains(radio)).toBe(true);
    }
    expect(screen.getByRole('radio', { name: /acceptForSession/ }))
      .toBeTruthy();
    expect(screen.getByRole('radio', { name: /cancel/ })).toBeTruthy();
  });

  it('says which command it is asking about', () => {
    renderCard();

    expect(screen.getByText(/rm -rf \/tmp\/cache/)).toBeTruthy();
    expect(screen.getByText(/\/home\/agent\/workspace/)).toBeTruthy();
    // The engine's own reason, not a platform sentence about plan mode.
    expect(screen.getByText('This command writes outside the workspace.')).toBeTruthy();
  });

  it('answers with the id the engine will be answered with', async () => {
    const onSubmit = renderCard();

    fireEvent.click(screen.getByRole('radio', { name: /acceptForSession/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Send decision' }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        interaction_id: 'codex-14',
        decision: 'acceptForSession',
      }),
    );
  });

  it('sends the first declared option when the reader changes nothing', async () => {
    const onSubmit = renderCard();

    fireEvent.click(screen.getByRole('button', { name: 'Send decision' }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ decision: 'accept' }),
    );
  });

  it('carries a note back with the decision', async () => {
    const onSubmit = renderCard();

    fireEvent.click(screen.getByRole('radio', { name: /^\d+\.\s*decline$/ }));
    fireEvent.change(screen.getByLabelText('Add a note for the agent'), {
      target: { value: 'Use the workspace copy instead.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send decision' }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        decision: 'decline',
        comment: 'Use the workspace copy instead.',
      }),
    );
  });

  it('shows an option with no adapter copy under its exact engine id', () => {
    renderCard({
      ...APPROVAL,
      options: [{ id: 'applyNetworkPolicyAmendment', denial: false }],
    });

    expect(screen.getByRole('radio', { name: /applyNetworkPolicyAmendment/ })).toBeTruthy();
  });

  it('leaves an exit-plan confirmation to the card that can pick a mode', () => {
    renderCard({
      ...APPROVAL,
      tool_name: 'ExitPlanMode',
      options: [
        {
          id: 'approve',
          denial: false,
          permission_mode_choices: ['default'],
          default_permission_mode: 'default',
        },
        { id: 'reject', denial: true },
      ],
    });

    expect(screen.getByRole('radiogroup', { name: 'Exit plan mode options' })).toBeTruthy();
  });
});
