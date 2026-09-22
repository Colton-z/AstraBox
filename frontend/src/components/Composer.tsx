import { useState, useRef, useEffect } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  PendingInteraction,
  PendingQuestionnaireInteraction,
  InteractionResponse,
} from '../types';

import { QuestionnaireInteractionCard } from './composer/QuestionnaireInteractionCard';
import { ToolPermissionInteractionCard } from './composer/ToolPermissionInteractionCard';
import { PlanConfirmationInteractionCard } from './composer/PlanConfirmationInteractionCard';
import { DecisionInteractionCard } from './composer/DecisionInteractionCard';
import type { SubmitAction } from './composer/types';

export { ComposerQueue } from './composer/ComposerQueue';

export function PendingInteractionCard({
  interaction,
  submitting,
  onSubmit,
  variant = 'composer',
  stopControl,
}: {
  interaction: PendingInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  variant?: 'message' | 'composer';
  stopControl?: ReactNode;
}) {
  const { t } = useTranslation();
  const [comment, setComment] = useState('');
  const [activeQuestionId, setActiveQuestionId] = useState('');
  const [selectedOptions, setSelectedOptions] = useState<Record<string, string[]>>({});
  const [freeTextAnswers, setFreeTextAnswers] = useState<Record<string, string>>({});
  const [customModeByQuestion, setCustomModeByQuestion] = useState<Record<string, boolean>>({});
  const [selectedPlanOption, setSelectedPlanOption] = useState<string>('default');
  const [selectedDecisionId, setSelectedDecisionId] = useState<string>('');
  const [selectedPermissionSuggestionId, setSelectedPermissionSuggestionId] = useState<string>('allow_once');
  const [showPlanCommentInput, setShowPlanCommentInput] = useState(false);
  const [submitAction, setSubmitAction] = useState<SubmitAction>(null);

  // Clear submitAction when submitting finishes (success → component unmounts;
  // error → machine returns to pendingInteraction with submitting=false).
  const prevSubmittingRef = useRef(submitting);
  useEffect(() => {
    if (prevSubmittingRef.current && !submitting) {
      setSubmitAction(null);
    }
    prevSubmittingRef.current = submitting;
  }, [submitting]);

  const pendingTitle =
    interaction.presentation === 'form'
      ? t('misc:composer.title_questionnaire')
      : interaction.presentation === 'decision'
        ? t('misc:composer.title_plan_confirmation')
        : t('misc:composer.title_tool_permission');
  const localizedPrompt =
    interaction.presentation === 'tool_approval'
      ? t('chat:interaction.prompt_tool_permission', { tool: interaction.tool_name })
      : interaction.presentation === 'form'
        ? t('chat:interaction.prompt_questionnaire')
        : t('chat:interaction.prompt_plan_confirmation');
  const pendingPrompt =
    localizedPrompt && localizedPrompt !== pendingTitle ? localizedPrompt : '';
  const compactStatusLabel = interaction.presentation === 'form' ? t('misc:composer.compact_questionnaire') : t('misc:composer.compact_confirm');

  useEffect(() => {
    setComment('');
    setSelectedOptions({});
    setSelectedPlanOption('default');
    setSelectedPermissionSuggestionId('allow_once');
    setShowPlanCommentInput(false);
    setSubmitAction(null);
    if (interaction.presentation === 'form') {
      const questionnaire = interaction as PendingQuestionnaireInteraction;
      const presetAnswers = questionnaire.preset_answers ?? {};
      const initialFreeText: Record<string, string> = {};
      const initialCustomMode: Record<string, boolean> = {};
      questionnaire.questions.forEach((question) => {
        const preset = presetAnswers[question.id];
        if (question.allow_free_text && typeof preset === 'string') {
          initialFreeText[question.id] = preset;
          initialCustomMode[question.id] = true;
        } else if (question.allow_free_text && question.options.length === 0) {
          initialCustomMode[question.id] = true;
        }
      });
      setActiveQuestionId(questionnaire.questions[0]?.id ?? '');
      setFreeTextAnswers(initialFreeText);
      setCustomModeByQuestion(initialCustomMode);
      return;
    }
    setActiveQuestionId('');
    setFreeTextAnswers({});
    setCustomModeByQuestion({});
    setSelectedDecisionId('');
  }, [interaction.interaction_id, interaction.presentation]);

  if (interaction.presentation === 'form') {
    return (
      <QuestionnaireInteractionCard
        questionnaire={interaction}
        stopControl={stopControl}
        submitting={submitting}
        onSubmit={onSubmit}
        variant={variant}
        compactStatusLabel={compactStatusLabel}
        pendingTitle={pendingTitle}
        pendingPrompt={pendingPrompt}
        activeQuestionId={activeQuestionId}
        setActiveQuestionId={setActiveQuestionId}
        selectedOptions={selectedOptions}
        setSelectedOptions={setSelectedOptions}
        freeTextAnswers={freeTextAnswers}
        setFreeTextAnswers={setFreeTextAnswers}
        customModeByQuestion={customModeByQuestion}
        setCustomModeByQuestion={setCustomModeByQuestion}
        submitAction={submitAction}
        setSubmitAction={setSubmitAction}
      />
    );
  }

  if (interaction.presentation === 'tool_approval') {
    return (
      <ToolPermissionInteractionCard
        toolInteraction={interaction}
        submitting={submitting}
        onSubmit={onSubmit}
        variant={variant}
        localizedPrompt={localizedPrompt}
        compactStatusLabel={compactStatusLabel}
        stopControl={stopControl}
        selectedPermissionSuggestionId={selectedPermissionSuggestionId}
        setSelectedPermissionSuggestionId={setSelectedPermissionSuggestionId}
        submitAction={submitAction}
        setSubmitAction={setSubmitAction}
      />
    );
  }

  if (interaction.presentation === 'decision') {
    // Which card answers a decision is decided by what the engine declared,
    // not by the presentation alone. Claude's exit-plan confirmation is the
    // one decision whose approval also picks a permission mode, and it says
    // so by declaring `permission_mode_choices`; every other engine's
    // decision is a choice among ids this console must not know by name.
    // Reading the ids instead — matching `approve` and `reject` — offers an
    // empty choice to an engine that spells them differently, and the turn
    // stays held open with no way to answer it.
    const decides = interaction.options ?? [];
    const picksPermissionMode = decides.some(
      (option) => (option.permission_mode_choices ?? []).length > 0,
    );
    if (!picksPermissionMode) {
      return (
        <DecisionInteractionCard
          decisionInteraction={interaction}
          stopControl={stopControl}
          submitting={submitting}
          onSubmit={onSubmit}
          variant={variant}
          compactStatusLabel={compactStatusLabel}
          selectedDecisionId={selectedDecisionId}
          setSelectedDecisionId={setSelectedDecisionId}
          comment={comment}
          setComment={setComment}
          submitAction={submitAction}
          setSubmitAction={setSubmitAction}
        />
      );
    }
    return (
      <PlanConfirmationInteractionCard
        planInteraction={interaction}
        stopControl={stopControl}
        submitting={submitting}
        onSubmit={onSubmit}
        variant={variant}
        pendingTitle={pendingTitle}
        pendingPrompt={pendingPrompt}
        compactStatusLabel={compactStatusLabel}
        selectedPlanOption={selectedPlanOption}
        setSelectedPlanOption={setSelectedPlanOption}
        showPlanCommentInput={showPlanCommentInput}
        setShowPlanCommentInput={setShowPlanCommentInput}
        comment={comment}
        setComment={setComment}
        submitAction={submitAction}
        setSubmitAction={setSubmitAction}
      />
    );
  }

  // Every presentation the seam declares has a card; an unknown one is a
  // browser that cannot answer what the backend is blocked on, so it fails
  // loudly instead of rendering some other card's controls over it. The
  // annotation makes the omission a compile error the day a presentation is
  // added, not a runtime surprise.
  const unsupported: never = interaction;
  throw new Error(
    'unsupported pending interaction presentation: '
    + String((unsupported as PendingInteraction).presentation),
  );
}
