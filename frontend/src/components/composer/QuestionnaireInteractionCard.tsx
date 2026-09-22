import { useId } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import type { PendingQuestionnaireInteraction, InteractionResponse } from '../../types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Field, FieldLabel } from '@/components/ui/field';
import {
  Questionnaire,
  QuestionnaireActions,
  QuestionnaireChoice,
  QuestionnaireChoiceDescription,
  QuestionnaireChoices,
  QuestionnaireDescription,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireTitle,
} from '@/components/ui/questionnaire';
import { StatusPill } from '@/components/AstraConsole';
import { Textarea } from '@/components/ui/textarea';
import type { SubmitAction } from './types';

// The value the custom-answer choice carries inside its question's group. It is
// a choice like any other, so it needs a value that no option label can hold;
// `Questionnaire.Item` keys its choices by value, and two choices sharing one
// value would read as the same answer.
const CUSTOM_ANSWER_VALUE = '__custom_answer__';

export function QuestionnaireInteractionCard({
  questionnaire,
  submitting,
  onSubmit,
  variant,
  compactStatusLabel,
  stopControl,
  pendingTitle,
  pendingPrompt,
  activeQuestionId,
  setActiveQuestionId,
  selectedOptions,
  setSelectedOptions,
  freeTextAnswers,
  setFreeTextAnswers,
  customModeByQuestion,
  setCustomModeByQuestion,
  submitAction,
  setSubmitAction,
}: {
  questionnaire: PendingQuestionnaireInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  variant: 'message' | 'composer';
  compactStatusLabel: string;
  stopControl?: ReactNode;
  pendingTitle: string;
  pendingPrompt: string;
  activeQuestionId: string;
  setActiveQuestionId: Dispatch<SetStateAction<string>>;
  selectedOptions: Record<string, string[]>;
  setSelectedOptions: Dispatch<SetStateAction<Record<string, string[]>>>;
  freeTextAnswers: Record<string, string>;
  setFreeTextAnswers: Dispatch<SetStateAction<Record<string, string>>>;
  customModeByQuestion: Record<string, boolean>;
  setCustomModeByQuestion: Dispatch<SetStateAction<Record<string, boolean>>>;
  submitAction: SubmitAction;
  setSubmitAction: Dispatch<SetStateAction<SubmitAction>>;
}) {
  const { t } = useTranslation();
  const answerFieldId = useId();

  const usesCustomAnswer = (question: PendingQuestionnaireInteraction['questions'][number]) =>
    question.allow_free_text && (question.options.length === 0 || customModeByQuestion[question.id] === true);

  const activateCustomAnswer = (questionId: string) => {
    setCustomModeByQuestion((prev) => ({ ...prev, [questionId]: true }));
    setSelectedOptions((prev) => ({ ...prev, [questionId]: [] }));
  };

  const toggleOption = (questionId: string, label: string, multiSelect: boolean) => {
    setSelectedOptions((prev) => {
      const current = prev[questionId] ?? [];
      const next = multiSelect
        ? current.includes(label)
          ? current.filter((value) => value !== label)
          : [...current, label]
        : [label];
      return { ...prev, [questionId]: next };
    });
    setCustomModeByQuestion((prev) => ({ ...prev, [questionId]: false }));
  };

  const answers = questionnaire.questions.map((question) => {
    const selected = selectedOptions[question.id] ?? [];
    const freeText = freeTextAnswers[question.id] ?? '';
    const useFreeText = usesCustomAnswer(question);
    return {
      question_id: question.id,
      option_label: useFreeText || question.multi_select ? undefined : selected[0],
      option_labels: useFreeText || !question.multi_select ? undefined : selected,
      free_text: useFreeText ? freeText : undefined,
    };
  });

  const isQuestionAnswered = (
    question: PendingQuestionnaireInteraction['questions'][number],
  ) => {
    const selected = selectedOptions[question.id] ?? [];
    const freeText = freeTextAnswers[question.id] ?? '';
    if (usesCustomAnswer(question)) {
      return question.allow_empty_text || freeText.length > 0;
    }
    return question.multi_select ? selected.length > 0 : Boolean(selected[0]);
  };

  const activeQuestion =
    questionnaire.questions.find((question) => question.id === activeQuestionId) ??
    questionnaire.questions[0];
  const hasQuestionTabs = questionnaire.questions.length > 1;
  const canSubmitAnswers = questionnaire.questions.every((question) => {
    return isQuestionAnswered(question);
  });
  const answeredQuestionCount = questionnaire.questions.filter(isQuestionAnswered).length;
  const remainingQuestionCount = questionnaire.questions.length - answeredQuestionCount;
  const hasAnyAnswer = questionnaire.questions.some((question) => isQuestionAnswered(question));
  const hasPendingCustomBlank = questionnaire.questions.some((question) => {
    const freeText = freeTextAnswers[question.id] ?? '';
    return usesCustomAnswer(question) && !question.allow_empty_text && freeText.length === 0;
  });
  const shouldOfferDecline = !hasAnyAnswer && hasPendingCustomBlank;

  const submitAnswers = async () => {
    if (submitting) {
      return;
    }
    if (shouldOfferDecline) {
      setSubmitAction('questionnaire_decline');
      try {
        await onSubmit({
          interaction_id: questionnaire.interaction_id,
          decline: true,
        });
      } finally {
        setSubmitAction(null);
      }
      return;
    }
    if (!canSubmitAnswers) {
      return;
    }
    setSubmitAction('questionnaire_submit');
    try {
      await onSubmit({
        interaction_id: questionnaire.interaction_id,
        answers,
      });
    } finally {
      setSubmitAction(null);
    }
  };

  if (variant === 'message') {
    return null;
  }

  return (
    <Card size="sm" className="flex min-h-0 flex-col overflow-hidden">
      <CardHeader className="shrink-0">
        <CardTitle className="flex items-center gap-2">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          {pendingTitle}
        </CardTitle>
        {pendingPrompt && <CardDescription>{pendingPrompt}</CardDescription>}
        {hasQuestionTabs && (
          <span data-testid="questionnaire-progress" className="text-xs tabular-nums text-muted-foreground" aria-live="polite">
            {t('misc:composer.question_progress', {
              answered: answeredQuestionCount,
              total: questionnaire.questions.length,
            })}
          </span>
        )}
        <CardAction>
          {/* Waiting for an answer is not a failure, and crimson is what this
              product says failure with (docs/frontend-design.md §7). The
              permission card next to it says this in the same pending tone. */}
          <StatusPill tone="pending">{t('misc:composer.blocking')}</StatusPill>
        </CardAction>
      </CardHeader>

      <CardContent className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* `Questionnaire` owns which question is on screen and hides the rest,
            and `item`/`onItemChange` bind that to the `activeQuestionId` the
            interaction state machine already holds, so the step the reader is
            on survives a re-render from anywhere else in the machine. */}
        <Questionnaire
          className="clip-content min-h-0 flex-1"
          item={activeQuestion?.id ?? ''}
          onItemChange={setActiveQuestionId}
        >
          {hasQuestionTabs && (
            // A step switcher, not a tab strip: `Questionnaire.Item` renders a
            // fieldset, so a `role="tab"` here would advertise a tabpanel
            // relationship that resolves to nothing (docs/frontend-design.md
            // §10). `aria-current` names the step the reader is on.
            //
            // Separate buttons that wrap, not a `ButtonGroup` on a horizontal
            // scroller. A group is a segmented control: it strips the inner
            // rounding and collapses the shared borders, so several question
            // headers would read as one control holding one value. A scroller
            // makes the row a clip box — `overflow-x: auto` forces the other
            // axis to `auto` too — which shaves the focus ring off the first
            // and last button (§11). Wrapping needs no clip box, which §11
            // prefers to compensating for one.
            <div
              role="group"
              data-slot="pending-interaction-question-navigation"
              className="flex max-w-full shrink-0 flex-wrap gap-1.5"
              aria-label={t('misc:composer.tab_questions_label')}
            >
              {questionnaire.questions.map((question, index) => {
                const label = (question.header || question.question || t('misc:summary.question_n', { index: index + 1 })).trim();
                const answered = isQuestionAnswered(question);
                const active = question.id === activeQuestion?.id;
                return (
                  <Button
                    key={question.id}
                    variant={active ? 'secondary' : 'outline'}
                    aria-current={active ? 'step' : undefined}
                    disabled={submitting}
                    onClick={() => setActiveQuestionId(question.id)}
                  >
                    <span
                      className={`inline-flex size-5 items-center justify-center rounded-full text-10 font-medium ${answered ? 'bg-mint/20 text-mint-fg' : 'bg-muted text-muted-foreground'}`}
                      aria-hidden="true"
                    >
                      {answered ? '✓' : index + 1}
                    </span>
                    <span className="sr-only">
                      {answered ? t('misc:composer.question_answered') : t('misc:composer.question_unanswered')}
                    </span>
                    <span>{label}</span>
                  </Button>
                );
              })}
            </div>
          )}

          {/* Long question content scrolls here, between the pinned step
              switcher above and the pinned action band below, so the controls
              that answer or stop the turn never leave the screen.

              Scrolling makes this a clip box on both axes, and a choice row
              flush against it loses the outer edge of its focus ring and runs
              under the scrollbar. Containment is not the mistake (§11) — the
              flush geometry is, so the rows are inset by the ring's reach and
              the inset is given back as negative margin, which keeps their
              edge on the card's own column. */}
          <div data-slot="pending-interaction-scroll-body" className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1">
          {questionnaire.questions.map((question, questionIndex) => {
            const title = (question.header || question.question || t('misc:summary.question_n', { index: questionIndex + 1 })).trim();
            const description = question.question && question.question !== title ? question.question : '';
            const usesCustom = usesCustomAnswer(question);
            const selected = selectedOptions[question.id] ?? [];
            const questionFieldId = `${answerFieldId}-${question.id}`;
            return (
              // `multiple` is what decides whether each choice below is a
              // checkbox or a radio, and it decides it for both what the
              // choice renders and what it announces. A checkbox announced as
              // a radio tells a reader they must choose exactly one when they
              // may choose three (docs/frontend-design.md §10).
              <QuestionnaireItem
                key={question.id}
                name={question.id}
                multiple={question.multi_select === true}
                disabled={submitting}
              >
                <QuestionnaireTitle>{title}</QuestionnaireTitle>
                {description && <QuestionnaireDescription>{description}</QuestionnaireDescription>}
                <span className="text-xs text-muted-foreground">
                  {question.multi_select ? t('misc:tool.multi_select') : t('misc:tool.single_select')}
                </span>

                {/* One group, and the answer the reader writes is one of its
                    choices — so the custom row lives inside it rather than in
                    a block of its own. The group takes its name from the
                    question above it: `QuestionnaireItem` is a fieldset and
                    `QuestionnaireTitle` its legend, so naming this div as well
                    would be an `aria-label` on an element that carries no
                    role, which no reader is told (docs/frontend-design.md
                    §10). */}
                <QuestionnaireChoices>
                  {question.options.map((option, optionIndex) => (
                    <QuestionnaireChoice
                      key={option.label}
                      value={option.label}
                      checked={selected.includes(option.label)}
                      disabled={submitting}
                      onChange={() =>
                        toggleOption(question.id, option.label, question.multi_select === true)
                      }
                    >
                      <span className="flex min-w-0 gap-1.5">
                        {/* An ordinal is a count of the options, not a machine
                            identity, so it stays on the text face and only
                            takes tabular figures to keep the column straight
                            (docs/frontend-design.md §6). */}
                        <span className="shrink-0 tabular-nums text-muted-foreground">{optionIndex + 1}.</span>
                        <span className="min-w-0 flex-1 font-medium">{option.label}</span>
                      </span>
                      {option.description && (
                        <QuestionnaireChoiceDescription className="text-xs">
                          {option.description}
                        </QuestionnaireChoiceDescription>
                      )}
                    </QuestionnaireChoice>
                  ))}
                  {question.allow_free_text && <QuestionnaireChoice
                    value={CUSTOM_ANSWER_VALUE}
                    checked={usesCustom}
                    disabled={submitting}
                    onChange={() => activateCustomAnswer(question.id)}
                  >
                    <span className="flex min-w-0 gap-1.5">
                      <span className="shrink-0 tabular-nums text-muted-foreground">{`${question.options.length + 1}.`}</span>
                      <span className="min-w-0 flex-1 font-medium">{t('misc:composer.custom_answer')}</span>
                    </span>
                    <QuestionnaireChoiceDescription className="text-xs">
                      {t('misc:composer.custom_answer_hint')}
                    </QuestionnaireChoiceDescription>
                  </QuestionnaireChoice>}
                </QuestionnaireChoices>

                {usesCustom && (
                  // The shared field, so this one focus ring is the product's
                  // and follows it when it moves (docs/frontend-design.md §11).
                  // A placeholder disappears the moment the reader types, so
                  // the box carries a label of its own for whoever comes back
                  // to it or hears it read out.
                  <Field>
                    <FieldLabel htmlFor={questionFieldId} className="sr-only">
                      {t('misc:composer.custom_answer')}
                    </FieldLabel>
                    <Textarea
                      id={questionFieldId}
                      value={freeTextAnswers[question.id] ?? ''}
                      onFocus={() => activateCustomAnswer(question.id)}
                      onChange={(e) => {
                        activateCustomAnswer(question.id);
                        setFreeTextAnswers((prev) => ({ ...prev, [question.id]: e.target.value }));
                      }}
                      placeholder={question.options.length > 0 ? t('misc:composer.custom_answer_placeholder') : t('misc:composer.answer_placeholder')}
                      disabled={submitting}
                    />
                  </Field>
                )}
              </QuestionnaireItem>
            );
          })}
          </div>
          {hasQuestionTabs && activeQuestion && (
            <QuestionnaireActions data-testid="questionnaire-navigation" className="shrink-0 border-t border-border pt-3">
              <QuestionnairePrevious disabled={submitting}>
                {t('misc:composer.previous_question')}
              </QuestionnairePrevious>
              <QuestionnaireNext variant="outline" disabled={submitting || !isQuestionAnswered(activeQuestion)}>
                {t('misc:composer.next_question')}
              </QuestionnaireNext>
            </QuestionnaireActions>
          )}
        </Questionnaire>
      </CardContent>

      {/* The card's foot is a control band, so everything in it runs at the
          band's one size — the same 32px the stop control beside it and every
          record page's actions run at (docs/frontend-design.md §9). */}
      <CardFooter className="shrink-0 justify-between gap-2">
        {/* The stop control sits with the actions because this card has
            replaced the composer: while an interaction is pending there is no
            other control on screen that can interrupt the turn, and a reader
            who does not want to answer the question has nothing else to
            press. */}
        <div className="shrink-0">{stopControl}</div>
        {hasQuestionTabs && !shouldOfferDecline && (
          <span data-testid="questionnaire-submit-hint" className="min-w-0 flex-1 text-xs text-muted-foreground">
            {remainingQuestionCount > 0
              ? t('misc:composer.questions_remaining', { remaining: remainingQuestionCount })
              : t('misc:composer.questions_complete')}
          </span>
        )}
        <Button
          variant={shouldOfferDecline ? 'outline' : 'default'}
          disabled={submitting || (!shouldOfferDecline && !canSubmitAnswers)}
          onClick={() => void submitAnswers()}
        >
          {submitAction === 'questionnaire_decline'
            ? t('misc:composer.declining')
            : submitAction === 'questionnaire_submit'
              ? t('misc:composer.submitting')
              : shouldOfferDecline
                ? t('misc:composer.decline_answer')
                : t('misc:composer.submit_answer')}
        </Button>
      </CardFooter>
    </Card>
  );
}
