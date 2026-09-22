// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';

import i18n from '../../i18n';
import type {
  PendingPlanConfirmationInteraction,
  PendingQuestionnaireInteraction,
  PendingToolPermissionInteraction,
} from '../../types';
import { SessionPendingInteractionFooter } from './SessionPendingInteractionFooter';

afterEach(() => {
  cleanup();
});

const interaction: PendingToolPermissionInteraction = {
  interaction_id: 'interaction-1',
  turn_id: 'turn-1',
  tool_call_id: 'tool-call-1',
  tool_name: 'Write',
  presentation: 'tool_approval',
};

describe('SessionPendingInteractionFooter', () => {
  it.each([
    ['en', 'Stop generating'],
    ['zh', '停止生成'],
  ])('keeps a native permission stoppable with an accessible %s control', async (language, stopLabel) => {
    await i18n.changeLanguage(language);
    const onStop = vi.fn().mockResolvedValue(undefined);
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    render(
      <SessionPendingInteractionFooter
        displayedQueue={[]}
        removeQueueItem={vi.fn()}
        retryQueueItem={vi.fn()}
        canRetryQueued={false}
        interaction={interaction}
        submitting={false}
        onSubmit={onSubmit}
        isInterruptSettling={false}
        onStop={onStop}
        permissionMode="default"
        permissionModes={['default', 'acceptEdits', 'plan', 'bypassPermissions']}
        showPermissionMode
        canChangePermissionMode
        modeSwitching={false}
        cyclePermissionMode={vi.fn().mockResolvedValue(undefined)}
        selectPermissionMode={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    const panel = screen.getByTestId('pending-interaction-panel');
    const stop = within(panel).getByRole('button', { name: stopLabel });
    expect((stop as HTMLButtonElement).disabled).toBe(false);

    stop.click();

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('keeps option descriptions on the type scale and renders plans as prose', async () => {
    await i18n.changeLanguage('en');
    const shared = {
      displayedQueue: [],
      removeQueueItem: vi.fn(),
      retryQueueItem: vi.fn(),
      canRetryQueued: false,
      submitting: false,
      onSubmit: vi.fn().mockResolvedValue(undefined),
      isInterruptSettling: false,
      onStop: vi.fn().mockResolvedValue(undefined),
      permissionMode: 'default' as const,
      permissionModes: ['default', 'acceptEdits', 'plan', 'bypassPermissions'],
      showPermissionMode: true,
      canChangePermissionMode: true,
      modeSwitching: false,
      cyclePermissionMode: vi.fn().mockResolvedValue(undefined),
      selectPermissionMode: vi.fn().mockResolvedValue(undefined),
    };

    const { rerender } = render(
      <SessionPendingInteractionFooter interaction={interaction} {...shared} />,
    );
    expect(screen.getByText('Allow just this action without saving any permission rule.').className)
      .toContain('text-xs');

    const plan: PendingPlanConfirmationInteraction = {
      interaction_id: 'plan-interaction-1',
      turn_id: 'turn-1',
      tool_call_id: 'tool-call-plan-1',
      tool_name: 'ExitPlanMode',
      presentation: 'decision',
      body: '# Plan\nCreate one file, then verify it.',
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
    };
    rerender(<SessionPendingInteractionFooter interaction={plan} {...shared} />);

    const renderedPlan = screen.getByText(/Create one file, then verify it/);
    expect(renderedPlan.tagName).toBe('DIV');
    expect(renderedPlan.className).not.toContain('font-mono');
    expect(screen.getByText(/later actions still get confirmed one by one/).className)
      .toContain('text-xs');
  });

  it('scrolls long questionnaire content without scrolling away its actions', async () => {
    await i18n.changeLanguage('en');
    const questionnaire: PendingQuestionnaireInteraction = {
      interaction_id: 'questionnaire-1',
      turn_id: 'turn-1',
      tool_call_id: 'tool-call-questionnaire-1',
      tool_name: 'AskUserQuestion',
      presentation: 'form',
      questions: [
        {
          id: 'question-1',
          header: 'Question 1',
          question: 'Choose the first answer.',
          allow_free_text: true,
          allow_empty_text: false,
          options: Array.from({ length: 8 }, (_, index) => ({
            label: `Option ${index + 1}`,
            description: `Description ${index + 1}`,
          })),
        },
        {
          id: 'question-2',
          header: 'Question 2',
          question: 'Choose the second answer.',
          allow_free_text: true,
          allow_empty_text: false,
          options: [{ label: 'Continue' }],
        },
      ],
    };

    render(
      <SessionPendingInteractionFooter
        displayedQueue={[]}
        removeQueueItem={vi.fn()}
        retryQueueItem={vi.fn()}
        canRetryQueued={false}
        interaction={questionnaire}
        submitting={false}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        isInterruptSettling={false}
        onStop={vi.fn().mockResolvedValue(undefined)}
        permissionMode="default"
        permissionModes={['default', 'acceptEdits', 'plan', 'bypassPermissions']}
        showPermissionMode
        canChangePermissionMode
        modeSwitching={false}
        cyclePermissionMode={vi.fn().mockResolvedValue(undefined)}
        selectPermissionMode={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    const panel = screen.getByTestId('pending-interaction-panel');
    const readingColumn = panel.querySelector('[data-slot="reading-column"]');
    const navigation = panel.querySelector('[data-slot="pending-interaction-question-navigation"]');
    const scrollBody = panel.querySelector('[data-slot="pending-interaction-scroll-body"]');
    // The action band is the card's own foot.
    const actions = panel.querySelector('[data-slot="card-footer"]');
    // The card navigates questions with a step-switcher button group rather
    // than a tab strip, so the row is a button by role.
    const secondQuestion = within(panel).getByRole('button', { name: /Question 2/ });
    const submit = within(panel).getByRole('button', { name: 'Submit answer' });

    expect(readingColumn?.className).toContain('overflow-hidden');
    expect(navigation?.className).toContain('shrink-0');
    expect(scrollBody?.className).toContain('overflow-y-auto');
    expect(actions?.className).toContain('shrink-0');
    expect(scrollBody?.contains(secondQuestion)).toBe(false);
    expect(navigation?.contains(secondQuestion)).toBe(true);
    expect(scrollBody?.contains(submit)).toBe(false);
    expect(actions?.contains(submit)).toBe(true);
  });
});
