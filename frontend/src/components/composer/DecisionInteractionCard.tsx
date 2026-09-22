import { useId } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  InteractionResponse,
  PendingPlanConfirmationInteraction,
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
  FieldLabel,
  FieldTitle,
} from '@/components/ui/field';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Textarea } from '@/components/ui/textarea';
import { StatusPill } from '@/components/AstraConsole';
import type { SubmitAction } from './types';

// What the engine is asking to do, in its own values — a command line, a
// directory, a host. Those are machine text and keep the mono face and their
// own line breaks (docs/frontend-design.md §6), unlike the plan card's body,
// which is prose the model wrote for a person.
function DecisionBody({ body }: { body: string }) {
  return (
    <pre className="max-h-60 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted p-3 text-xs font-mono text-muted-foreground">
      {body}
    </pre>
  );
}

/**
 * The card for a `decision` interaction whose options are the engine's own.
 *
 * The engine declares the choices and this renders exactly those, under the
 * ids it declared them with — the adapter's copy for an option is model-facing
 * and never reaches the browser. Nothing here knows what any particular option
 * means, which is the point — Codex's four command decisions and its three
 * permission decisions reach a person through the same card, and an engine
 * that adds a fifth needs no change here.
 *
 * The sibling `PlanConfirmationInteractionCard` handles the one decision that
 * is not just a choice among ids: Claude's exit-plan confirmation, where
 * approving also picks a permission mode. That card is chosen when an option
 * declares `permission_mode_choices`; everything else lands here.
 */
export function DecisionInteractionCard({
  decisionInteraction,
  submitting,
  onSubmit,
  variant,
  compactStatusLabel,
  stopControl,
  selectedDecisionId,
  setSelectedDecisionId,
  comment,
  setComment,
  submitAction,
  setSubmitAction,
}: {
  decisionInteraction: PendingPlanConfirmationInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  variant: 'message' | 'composer';
  compactStatusLabel: string;
  stopControl?: ReactNode;
  selectedDecisionId: string;
  setSelectedDecisionId: Dispatch<SetStateAction<string>>;
  comment: string;
  setComment: Dispatch<SetStateAction<string>>;
  submitAction: SubmitAction;
  setSubmitAction: Dispatch<SetStateAction<SubmitAction>>;
}) {
  const { t } = useTranslation();
  const optionIdBase = useId();
  const commentFieldId = useId();

  const options = decisionInteraction.options ?? [];
  // The engine's own words for what it is asking, not a platform sentence
  // about the shape of the question.
  const askedPrompt = String(decisionInteraction.prompt ?? '').trim();
  // Before a reader has chosen, the answer is the first declared option: the
  // adapter declares them in the order it wants them offered, and a card that
  // starts on nothing would let the submit button send an empty decision.
  const chosen = options.find((option) => option.id === selectedDecisionId) ?? options[0];
  const isDenial = !!chosen?.denial;

  const submitDecision = async () => {
    if (submitting || !chosen) return;
    setSubmitAction('decision_submit');
    try {
      await onSubmit({
        interaction_id: decisionInteraction.interaction_id,
        decision: chosen.id,
        comment: comment.trim() || undefined,
      });
    } finally {
      setSubmitAction(null);
    }
  };

  if (variant === 'message') {
    return (
      <section className="space-y-2">
        <div className="flex items-center gap-2 text-xs">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          <span className="text-muted-foreground">
            {askedPrompt || t('misc:composer.decision_message_summary')}
          </span>
        </div>
        {decisionInteraction.body && <DecisionBody body={decisionInteraction.body} />}
      </section>
    );
  }

  return (
    <Card size="sm" className="flex min-h-0 flex-col overflow-hidden">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          {t('misc:composer.title_decision')}
        </CardTitle>
        {askedPrompt && <CardDescription>{askedPrompt}</CardDescription>}
        <CardAction>
          {/* A turn held open for an answer is the waiting state, so it takes
              the waiting tone and its dot rather than the destructive hue
              crimson reserves for failure (docs/frontend-design.md §7). */}
          <StatusPill tone="pending">{t('misc:composer.blocking')}</StatusPill>
        </CardAction>
      </CardHeader>

      <CardContent className="min-h-0 flex-1 space-y-3 overflow-y-auto">
        {decisionInteraction.body && <DecisionBody body={decisionInteraction.body} />}

        {/* One radio group with real radios: arrow keys move between the
            offers and the group is a single tab stop, which is what a reader
            expects of a choice between alternatives. */}
        <RadioGroup
          value={chosen?.id ?? ''}
          disabled={submitting}
          aria-label={t('misc:composer.decision_options_label')}
          onValueChange={(value) => setSelectedDecisionId(String(value))}
        >
          {options.map((option, index) => {
            const optionId = `${optionIdBase}-${option.id}`;
            return (
              <FieldLabel
                key={option.id}
                htmlFor={optionId}
                data-testid={`decision-option-${option.id}`}
              >
                <Field orientation="horizontal">
                  <FieldContent>
                    <FieldTitle>
                      {/* An ordinal counts the options; it is not a machine
                          identity, so it stays on the text face
                          (docs/frontend-design.md §6). */}
                      <span className="tabular-nums text-muted-foreground">{index + 1}.</span>
                      {/* The adapter's copy for an option is model-facing and
                          never reaches the browser, so the row carries the
                          engine's own id — which is also the word the reader
                          is choosing to send back. */}
                      <span className="font-mono">{option.id}</span>
                    </FieldTitle>
                  </FieldContent>
                  <RadioGroupItem value={option.id} id={optionId} />
                </Field>
              </FieldLabel>
            );
          })}
        </RadioGroup>

        {/* The shared field, so this one focus ring is the product's and
            follows it when it moves (docs/frontend-design.md §11). A
            placeholder disappears the moment the reader types, so the box
            carries a label of its own for whoever comes back to it or hears
            it read out. Every option takes a note: the adapter decides how it
            is introduced to the model, and refusing one on an approval would
            drop the only place a reader can say why. */}
        <Field>
          <FieldLabel htmlFor={commentFieldId} className="sr-only">
            {t('misc:composer.decision_comment_label')}
          </FieldLabel>
          <Textarea
            id={commentFieldId}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('misc:composer.decision_comment_placeholder')}
            disabled={submitting}
          />
        </Field>
      </CardContent>

      {/* The card's foot is a control band, so everything in it runs at the
          band's one size (docs/frontend-design.md §9). */}
      <CardFooter className="shrink-0 justify-between gap-2">
        {/* The stop control sits with the actions because this card has
            replaced the composer: while an interaction is pending there is no
            other control on screen that can interrupt the turn. */}
        <div className="shrink-0">{stopControl}</div>
        <Button
          data-testid="decision-submit"
          variant={isDenial ? 'destructive' : 'default'}
          disabled={submitting || !chosen}
          onClick={() => void submitDecision()}
        >
          {submitAction === 'decision_submit'
            ? t('misc:composer.submitting')
            : t('misc:composer.decision_submit')}
        </Button>
      </CardFooter>
    </Card>
  );
}
