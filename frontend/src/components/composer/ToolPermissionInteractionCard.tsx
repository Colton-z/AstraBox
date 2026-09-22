import { useId } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  InteractionResponse,
  PendingToolPermissionInteraction,
} from '../../types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
  FieldTitle,
} from '@/components/ui/field';
import { Item, ItemContent, ItemDescription, ItemTitle } from '@/components/ui/item';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { CodeBlock, CodeBlockCopyButton } from '@/components/ai-elements/code-block';
import { StatusPill } from '@/components/AstraConsole';
import { ChevronDownIcon } from 'lucide-react';
import type { SubmitAction } from './types';

export function ToolPermissionInteractionCard({
  toolInteraction,
  submitting,
  onSubmit,
  variant,
  localizedPrompt,
  compactStatusLabel,
  stopControl,
  selectedPermissionSuggestionId,
  setSelectedPermissionSuggestionId,
  submitAction,
  setSubmitAction,
}: {
  toolInteraction: PendingToolPermissionInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  variant: 'message' | 'composer';
  localizedPrompt: string;
  compactStatusLabel: string;
  stopControl?: ReactNode;
  selectedPermissionSuggestionId: string;
  setSelectedPermissionSuggestionId: Dispatch<SetStateAction<string>>;
  submitAction: SubmitAction;
  setSubmitAction: Dispatch<SetStateAction<SubmitAction>>;
}) {
  const { t } = useTranslation();
  const optionIdBase = useId();
  const summaryText = localizedPrompt || toolInteraction.tool_name;
  const permissionChoices: Array<{ id: string; label: string; description: string | null }> = [
    {
      id: 'allow_once',
      label: t('misc:composer.allow_once_label'),
      description: t('misc:composer.allow_once_desc'),
    },
  ];
  const selectedChoice =
    permissionChoices.find((item) => item.id === selectedPermissionSuggestionId) ??
    permissionChoices[0];

  const submitDecision = async (decision: 'approve' | 'reject') => {
    if (submitting) return;
    setSubmitAction(decision === 'approve' ? 'tool_approve' : 'tool_reject');
    try {
      await onSubmit({
        interaction_id: toolInteraction.interaction_id,
        decision,
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
          <span className="text-muted-foreground">{summaryText}</span>
        </div>
      </section>
    );
  }

  return (
    <Card size="sm" className="flex min-h-0 flex-col overflow-hidden">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Badge variant="secondary">{compactStatusLabel}</Badge>
          {summaryText}
        </CardTitle>
        <CardAction>
          {/* A turn held open for an answer is the waiting state, so it takes
              the waiting tone and its dot rather than the destructive hue
              crimson reserves for failure (docs/frontend-design.md §7) — the
              same citrine the tool row awaiting this approval paints. */}
          <StatusPill tone="pending">{t('misc:composer.blocking')}</StatusPill>
        </CardAction>
      </CardHeader>

      {/* `min-w-0` because the arguments below are a code block that scrolls
          sideways rather than wrapping: without it the block's own width sets
          the card's and a long single-line payload pushes past its edge. */}
      <CardContent className="min-h-0 min-w-0 flex-1 space-y-3 overflow-y-auto">
        <Item variant="muted" size="sm">
          <ItemContent>
            <ItemTitle>
              {toolInteraction.tool_name}
              <span className="text-xs font-normal text-muted-foreground">{t('misc:composer.tool_approval')}</span>
            </ItemTitle>
          </ItemContent>
        </Item>

        {/* One radio group with real radios: arrow keys move between the
            offers and the group is a single tab stop, which is what a reader
            expects of a choice between alternatives. The group follows
            `selectedChoice` rather than the raw id so the option that is
            painted is the one a decision would send. */}
        <RadioGroup
          value={selectedChoice?.id ?? ''}
          disabled={submitting}
          aria-label={t('misc:composer.permission_options_label')}
          onValueChange={(value) => setSelectedPermissionSuggestionId(String(value))}
        >
          {permissionChoices.map((choice, index) => {
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
                    {choice.description && (
                      <FieldDescription className="text-xs">{choice.description}</FieldDescription>
                    )}
                  </FieldContent>
                  <RadioGroupItem value={choice.id} id={optionId} />
                </Field>
              </FieldLabel>
            );
          })}
        </RadioGroup>

        {toolInteraction.raw_input && (
          // The kit's collapsible rather than `<details>`: every other
          // disclosure on this surface is one, and the chevron that says which
          // way it opens comes with it. `<summary>` also matches no rule in the
          // shared focus-ring selector, so it takes focus and paints nothing.
          <Collapsible className="text-sm">
            <CollapsibleTrigger className="group flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
              {t('misc:composer.view_tool_input')}
              <ChevronDownIcon className="size-4 shrink-0 transition-transform group-data-panel-open:rotate-180" />
            </CollapsibleTrigger>
            <CollapsibleContent>
              {/* The tool's arguments are what the agent sent verbatim, so they
                  keep the mono face and their own line breaks
                  (docs/frontend-design.md §6). The height bound rides an outer
                  div because `CodeBlock`'s own container is `overflow-hidden`:
                  a `max-h` on it would cut the payload with nothing to scroll. */}
              <div className="mt-1 max-h-60 overflow-y-auto">
                <CodeBlock code={JSON.stringify(toolInteraction.raw_input, null, 2)} language="json">
                  <CodeBlockCopyButton aria-label={t('common:copy')} />
                </CodeBlock>
              </div>
            </CollapsibleContent>
          </Collapsible>
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
        {/* Two opposed decisions, so they stay two controls with a gap between
            them. `ButtonGroup` welds its children onto a shared edge, which
            reads as one segmented control with a selected half — the wrong
            thing to say about reject and allow. */}
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="outline"
            disabled={submitting}
            onClick={() => void submitDecision('reject')}
          >
            {submitAction === 'tool_reject' ? t('misc:composer.rejecting') : t('misc:composer.reject_operation')}
          </Button>
          <Button
            disabled={submitting}
            onClick={() => void submitDecision('approve')}
          >
            {submitAction === 'tool_approve'
              ? t('misc:composer.submitting')
              : t('misc:composer.allow_continue')}
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}
