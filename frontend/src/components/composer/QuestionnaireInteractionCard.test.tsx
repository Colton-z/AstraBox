// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { PendingQuestionnaireInteraction } from '../../types';
import { PendingInteractionCard } from '../Composer';

// Rendered through `PendingInteractionCard` rather than the card directly, so
// every case runs against the interaction state machine that actually holds
// the answers — a card driven by props a test invented would pass whether or
// not the two still fit together.

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

function questionnaire(
  questions: PendingQuestionnaireInteraction['questions'],
): PendingQuestionnaireInteraction {
  return {
    interaction_id: 'interaction-1',
    turn_id: 'turn-1',
    tool_name: 'AskUserQuestion',
    presentation: 'form',
    questions,
  };
}

const ONE_ANSWER = questionnaire([
  {
    id: 'q1',
    header: 'Colour',
    question: 'Which colour should the button be?',
    multi_select: false,
    allow_free_text: true,
    allow_empty_text: false,
    options: [{ label: 'Red', description: 'The warm one' }, { label: 'Blue' }],
  },
]);

const MANY_ANSWERS = questionnaire([
  {
    id: 'q1',
    header: 'Colours',
    question: 'Which colours should the palette hold?',
    multi_select: true,
    allow_free_text: true,
    allow_empty_text: false,
    options: [{ label: 'Red' }, { label: 'Blue' }],
  },
]);

function renderCard(
  interaction: PendingQuestionnaireInteraction,
  overrides: {
    onSubmit?: (response: unknown) => Promise<void>;
    submitting?: boolean;
  } = {},
) {
  const onSubmit = overrides.onSubmit ?? vi.fn().mockResolvedValue(undefined);
  render(
    <PendingInteractionCard
      interaction={interaction}
      submitting={overrides.submitting ?? false}
      onSubmit={onSubmit as never}
      variant="composer"
      stopControl={<button type="button">Stop generating</button>}
    />,
  );
  return onSubmit;
}

describe('QuestionnaireInteractionCard', () => {
  it('announces a one-answer question as radios and a many-answer question as checkboxes', () => {
    renderCard(ONE_ANSWER);
    // Two options plus the custom-answer row, and nothing offering the reader
    // a second tick on a question that takes one answer.
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);

    cleanup();

    renderCard(MANY_ANSWERS);
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
  });

  it('offers the custom answer as one of the question options', () => {
    renderCard(ONE_ANSWER);

    const choices = screen.getAllByRole('radio');
    const custom = screen.getByRole('radio', { name: /Write your own/ });

    // Last of the same group, not a control of its own beneath it.
    expect(choices).toContain(custom);
    expect(choices[choices.length - 1]).toBe(custom);
    // The reader is told which question it answers: the group is the
    // question's fieldset, named by its legend.
    expect(custom.closest('fieldset')).toBe(
      screen.getByRole('group', { name: 'Colour' }),
    );
  });

  it('takes the written answer in the product shared textarea', () => {
    renderCard(ONE_ANSWER);
    fireEvent.click(screen.getByRole('radio', { name: /Write your own/ }));

    const box = screen.getByRole('textbox');
    expect(box.tagName).toBe('TEXTAREA');
    expect(box.getAttribute('data-slot')).toBe('textarea');
  });

  it('keeps option ordinals on the text face', () => {
    renderCard(ONE_ANSWER);

    // An ordinal counts the options on screen; it is not a machine identity,
    // so it does not take the mono face (docs/frontend-design.md §6).
    for (const ordinal of ['1.', '2.', '3.']) {
      expect(screen.getByText(ordinal).className).not.toContain('font-mono');
    }
  });

  it.each([
    ['while the reader is answering', false],
    ['while an answer is in flight', true],
  ])('keeps the stop control reachable %s', (_case, submitting) => {
    renderCard(ONE_ANSWER, { submitting });

    const stop = screen.getByRole('button', { name: 'Stop generating' });
    expect((stop as HTMLButtonElement).disabled).toBe(false);
  });

  it('submits the option the reader picked', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderCard(ONE_ANSWER, { onSubmit });

    fireEvent.click(screen.getByRole('radio', { name: /Blue/ }));

    // The pick has to come back to the reader as well as reach the payload:
    // the choices are controlled, so a `checked` that is never bound leaves
    // the card showing nothing selected while it submits the answer anyway.
    expect((screen.getByRole('radio', { name: /Blue/ }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('radio', { name: /Red/ }) as HTMLInputElement).checked).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'interaction-1',
      answers: [
        {
          question_id: 'q1',
          option_label: 'Blue',
          option_labels: undefined,
          free_text: undefined,
        },
      ],
    });
  });

  it('submits every option the reader picked on a many-answer question', () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderCard(MANY_ANSWERS, { onSubmit });

    fireEvent.click(screen.getByRole('checkbox', { name: /Red/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Blue/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }));

    expect(onSubmit).toHaveBeenCalledWith({
      interaction_id: 'interaction-1',
      answers: [
        {
          question_id: 'q1',
          option_label: undefined,
          option_labels: ['Red', 'Blue'],
          free_text: undefined,
        },
      ],
    });
  });

  it('shows one question at a time and moves between them without claiming to be tabs', () => {
    renderCard(
      questionnaire([
        {
          id: 'q1',
          header: 'Colour',
          question: 'Which colour?',
          multi_select: false,
          allow_free_text: true,
          allow_empty_text: false,
          options: [{ label: 'Red' }],
        },
        {
          id: 'q2',
          header: 'Size',
          question: 'Which size?',
          multi_select: false,
          allow_free_text: true,
          allow_empty_text: false,
          options: [{ label: 'Large' }],
        },
      ]),
    );

    // A step switcher, not a tab strip: a `role="tab"` here would advertise a
    // tabpanel that resolves to nothing (docs/frontend-design.md §10).
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
    expect(screen.queryAllByRole('tabpanel')).toHaveLength(0);

    const switcher = screen.getByRole('group', { name: 'Questions to answer' });
    const second = screen.getByRole('button', { name: /Size/ });
    expect(screen.getByRole('button', { name: /Colour/ })).toHaveProperty(
      'ariaCurrent',
      'step',
    );

    // Only the question on screen is reachable, so a reader cannot answer one
    // they cannot read.
    expect(screen.queryByRole('radio', { name: /Large/ })).toBeNull();

    fireEvent.click(second);

    expect(switcher).toBeTruthy();
    expect(screen.getByRole('radio', { name: /Large/ })).toBeTruthy();
    expect(screen.queryByRole('radio', { name: /Red/ })).toBeNull();
  });
});
