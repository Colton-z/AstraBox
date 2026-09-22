import { useId } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  InteractionResponse,
  PendingPlanConfirmationInteraction,
  PendingPlanPrompt,
  PermissionMode,
} from '../../types';
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
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
  FieldTitle,
} from '@/components/ui/field';
import { Item, ItemContent, ItemDescription, ItemGroup, ItemTitle } from '@/components/ui/item';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import { StatusPill } from '@/components/AstraConsole';
import type { SubmitAction } from './types';

// The plan is what the agent wrote for a person to read, so it keeps the text
// face and its own line breaks; setting it in mono would put prose in the
// terminal voice (docs/frontend-design.md §6).
function PlanBody({ plan }: { plan: string }) {
  return (
    <div className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
      {plan}
    </div>
  );
}


// The allowed directions ride the verbatim native input rather than a
// platform-authored field. The record carries the native rows untouched, so
// rows without a prompt are dropped here, at render time.
function readAllowedPrompts(rawInput: Record<string, unknown> | undefined): PendingPlanPrompt[] {
  const rows = rawInput?.allowedPrompts;
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows.flatMap((row) => {
    if (!row || typeof row !== 'object') {
      return [];
    }
    const entry = row as Record<string, unknown>;
    const prompt = String(entry.prompt ?? '').trim();
    if (!prompt) {
      return [];
    }
    return [{ tool: String(entry.tool ?? '').trim() || null, prompt }];
  });
}

export function PlanConfirmationInteractionCard({
  planInteraction,
  submitting,
  onSubmit,
  variant,
  pendingTitle,
  pendingPrompt,
  compactStatusLabel,
  stopControl,
  selectedPlanOption,
  setSelectedPlanOption,
  showPlanCommentInput,
  setShowPlanCommentInput,
  comment,
  setComment,
  submitAction,
  setSubmitAction,
}: {
  planInteraction: PendingPlanConfirmationInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  variant: 'message' | 'composer';
  pendingTitle: string;
  pendingPrompt: string;
  compactStatusLabel: string;
  stopControl?: ReactNode;
  selectedPlanOption: string;
  setSelectedPlanOption: Dispatch<SetStateAction<string>>;
  showPlanCommentInput: boolean;
  setShowPlanCommentInput: Dispatch<SetStateAction<boolean>>;
  comment: string;
  setComment: Dispatch<SetStateAction<string>>;
  submitAction: SubmitAction;
  setSubmitAction: Dispatch<SetStateAction<SubmitAction>>;
}) {
  const { t } = useTranslation();
  const optionIdBase = useId();
  const commentFieldId = useId();

  const allowedPrompts = readAllowedPrompts(planInteraction.raw_input);
  const options = planInteraction.options ?? [];
  // The card offers the one approving option with the permission modes its
  // adapter declared, plus the declared 'reject' denial. The mode strings are
  // the engine's own vocabulary and are carried back verbatim; only the copy
  // beside them is platform-authored, keyed per mode below.
  const approveOption = options.find((option) => option.id === 'approve' && !option.denial);
  const rejectOption = options.find((option) => option.id === 'reject' && option.denial);
  const modeCopy: Record<string, { label: string; description: string }> = {
    bypassPermissions: {
      label: t('misc:composer.plan_bypass_label'),
      description: t('misc:composer.plan_bypass_desc'),
    },
    acceptEdits: {
      label: t('misc:composer.plan_accept_edits_label'),
      description: t('misc:composer.plan_accept_edits_desc'),
    },
    default: {
      label: t('misc:composer.plan_once_label'),
      description: t('misc:composer.plan_once_desc'),
    },
  };

  const EXIT_PLAN_CHOICES: Array<{
    id: string;
    label: string;
    description: string;
    permissionMode: PermissionMode | null;
  }> = [
    ...(approveOption?.permission_mode_choices ?? []).map((mode) => ({
      id: mode,
      // A mode this build has no copy for still gets a button under its exact
      // engine name rather than a key or a blank row.
      label: modeCopy[mode]?.label ?? mode,
      description: modeCopy[mode]?.description ?? '',
      permissionMode: mode as PermissionMode,
    })),
    ...(rejectOption
      ? [{
        id: rejectOption.id,
        label: t('misc:composer.plan_reject_label'),
        description: t('misc:composer.plan_reject_desc'),
        permissionMode: null,
      }]
      : []),
  ];

  const isRejectOption = !!rejectOption && selectedPlanOption === rejectOption.id;
  const showFeedback = isRejectOption && showPlanCommentInput;

  const submitPlanChoice = async () => {
    if (submitting) return;
    const choice = EXIT_PLAN_CHOICES.find((c) => c.id === selectedPlanOption);
    try {
      if (isRejectOption) {
        setSubmitAction('plan_reject');
        await onSubmit({
          interaction_id: planInteraction.interaction_id,
          decision: 'reject',
          comment: comment.trim() || undefined,
        });
      } else {
        setSubmitAction('plan_approve');
        await onSubmit({
          interaction_id: planInteraction.interaction_id,
          decision: 'approve',
          // Nothing selected leaves the mode to the option's declared default
          // rather than to a guess made here.
          permission_mode: choice?.permissionMode ?? undefined,
        });
      }
    } finally {
      setSubmitAction(null);
    }
  };

  if (variant === 'message') {
    const summaryText = pendingPrompt || t('misc:composer.plan_message_summary');
    return (
      <section className="space-y-2">
        <div className="flex items-center gap-2 text-xs">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          <span className="text-muted-foreground">{summaryText}</span>
        </div>
        {planInteraction.body && <PlanBody plan={planInteraction.body} />}
      </section>
    );
  }

  return (
    <Card size="sm" className="flex min-h-0 flex-col overflow-hidden">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          {pendingTitle}
        </CardTitle>
        {pendingPrompt && <CardDescription>{pendingPrompt}</CardDescription>}
        <CardAction>
          {/* A turn held open for an answer is the waiting state, so it takes
              the waiting tone and its dot rather than the destructive hue
              crimson reserves for failure (docs/frontend-design.md §7) — the
              same citrine the tool row awaiting this confirmation paints. */}
          <StatusPill tone="pending">{t('misc:composer.blocking')}</StatusPill>
        </CardAction>
      </CardHeader>

      <CardContent className="min-h-0 flex-1 space-y-3 overflow-y-auto">
        {planInteraction.body && <PlanBody plan={planInteraction.body} />}
        {allowedPrompts.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">{t('misc:composer.allowed_directions')}</div>
            {/* `ItemGroup` declares a list, so each row has to claim to be one
                of its items — a list whose children carry no role reports as
                empty to a reader who cannot see the rows
                (docs/frontend-design.md §10). */}
            <ItemGroup>
              {allowedPrompts.map((item, index) => (
                <Item key={`${item.tool ?? 'prompt'}:${index}`} role="listitem" variant="muted" size="xs">
                  <ItemContent>
                    <ItemTitle>{item.tool || t('misc:composer.suggestion')}</ItemTitle>
                    {/* The whole suggested direction is the point of the row,
                        so it is not clamped to two lines. */}
                    <ItemDescription className="line-clamp-none">{item.prompt}</ItemDescription>
                  </ItemContent>
                </Item>
              ))}
            </ItemGroup>
          </div>
        )}

        {/* One radio group with real radios: arrow keys move between the four
            exits and the group is a single tab stop, which is what a reader
            expects of a choice between alternatives. */}
        <RadioGroup
          value={selectedPlanOption}
          disabled={submitting}
          aria-label={t('misc:composer.exit_plan_options_label')}
          onValueChange={(value) => {
            const nextId = String(value);
            setSelectedPlanOption(nextId);
            if (nextId !== 'reject') {
              setShowPlanCommentInput(false);
            } else {
              setShowPlanCommentInput(true);
            }
          }}
        >
          {EXIT_PLAN_CHOICES.map((choice, index) => {
            const optionId = `${optionIdBase}-${choice.id}`;
            return (
              <FieldLabel key={choice.id} htmlFor={optionId}>
                <Field orientation="horizontal">
                  <FieldContent>
                    <FieldTitle>
                      {/* An ordinal counts the options; it is not a machine
                          identity, so it stays on the text face
                          (docs/frontend-design.md §6). */}
                      <span className="tabular-nums text-muted-foreground">{index + 1}.</span>
                      {choice.label}
                    </FieldTitle>
                    <FieldDescription className="text-xs">{choice.description}</FieldDescription>
                  </FieldContent>
                  <RadioGroupItem value={choice.id} id={optionId} />
                </Field>
              </FieldLabel>
            );
          })}
        </RadioGroup>

        {showFeedback && (
          // The shared field, so this one focus ring is the product's and
          // follows it when it moves (docs/frontend-design.md §11). A
          // placeholder disappears the moment the reader types, so the box
          // carries a label of its own for whoever comes back to it or hears
          // it read out.
          <Field>
            <FieldLabel htmlFor={commentFieldId} className="sr-only">
              {t('misc:composer.reject_feedback_label')}
            </FieldLabel>
            <Textarea
              id={commentFieldId}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t('misc:composer.reject_feedback_placeholder')}
              disabled={submitting}
            />
          </Field>
        )}
      </CardContent>

      {/* The card's foot is a control band, so everything in it runs at the
          band's one size — the same 32px the stop control beside it and every
          record page's actions run at (docs/frontend-design.md §9). */}
      <CardFooter className="shrink-0 justify-between gap-2">
        {/* The stop control sits with the actions because this card has
            replaced the composer: while an interaction is pending there is no
            other control on screen that can interrupt the turn, and a reader
            who does not want to decide has nothing else to press. */}
        <div className="shrink-0">{stopControl}</div>
        <Button
          variant={isRejectOption ? 'destructive' : 'default'}
          disabled={submitting}
          onClick={() => void submitPlanChoice()}
        >
          {submitAction === 'plan_approve'
            ? t('misc:composer.submitting')
            : submitAction === 'plan_reject'
              ? t('misc:composer.submitting')
              : isRejectOption
                ? t('misc:composer.reject_plan')
                : t('misc:composer.approve_continue')}
        </Button>
      </CardFooter>
    </Card>
  );
}
