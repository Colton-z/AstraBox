import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import { Ellipsis, EmptyState, ErrorNote, TruncatingRow } from '@/components/shell';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import { listAgents, startAgentConversation } from '../api';
import type { AgentConfig } from '../types';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

function agentStateLabel(state: string, t: TFunction): string {
  return ({
    ACTIVE: t('agents:state_active'),
    PROVISIONING: t('agents:state_provisioning'),
    HIBERNATING: t('agents:state_hibernating'),
  } as Record<string, string>)[state] ?? state;
}

/**
 * The user-facing agent picker: the configured agents, and one click to open a
 * conversation on one.
 *
 * There is no "run a bare harness" entry here on purpose — every conversation
 * belongs to an agent (docs/domain-model.md), so an empty list is a genuine
 * "nothing has been configured yet" and points at the management console rather
 * than offering a nameless session.
 */
export function AgentHome({
  onConversationCreated,
}: {
  onConversationCreated: (sessionId: string) => void;
}) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<AgentConfig[] | null>(null);
  const [error, setError] = useState('');
  const [startingId, setStartingId] = useState<string | null>(null);
  const loadedOnce = useRef(false);

  const refresh = useCallback(async (context?: ReloadContext) => {
    try {
      setAgents(await listAgents());
      loadedOnce.current = true;
      setError('');
    } catch (e) {
      if (keepsLastRead(e, context, loadedOnce.current)) return;
      setError((e as Error).message);
      setAgents([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useKeepCurrent(refresh);

  const startConversationOn = async (agentId: string) => {
    setStartingId(agentId);
    setError('');
    try {
      const created = await startAgentConversation(agentId);
      onConversationCreated(created.session_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStartingId(null);
    }
  };

  if (agents === null) {
    return <p className="text-sm text-muted-foreground">{t('agents:home_loading')}</p>;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col space-y-6">
      <div className="space-y-1.5">
        <h1 className="t-h1-tight text-2xl">{t('agents:home_title')}</h1>
        <p className="text-sm text-muted-foreground">{t('agents:home_subtitle')}</p>
      </div>
      {error && <ErrorNote title={t('agents:action_failed')}>{error}</ErrorNote>}
      {/*
        An empty list is a state of the page, not a card in a grid of one.
        Inside the grid it keeps the grid's height — the message ends around
        y=200 with ~590px of bare background under it — and takes a single
        column, so it reads as one lonely card rather than as "there is nothing
        here".
      */}
      {agents.length === 0 ? (
        // The frame and the centring stay on the wrapper: the shared empty
        // state carries the parts, not the surface they stand on, and the
        // dashed border it does carry has no width to draw with.
        <div className="flex flex-1 items-center justify-center rounded-xl bg-card ring-1 ring-foreground/10">
          <EmptyState
            title={t('agents:home_empty')}
            hint={t('agents:home_empty_hint')}
            action={
              /* The console is a destination, so this borrows the button's
                 shape through `buttonVariants` instead of being a `Button`:
                 `@base-ui/react/button` imposes button semantics on whatever it
                 renders — `role="button"` on a non-native element — which its
                 own documentation rules out for an `<a>`. */
              <a className={buttonVariants({ variant: 'outline' })} href="/manage/agents">
                {t('agents:home_empty_cta')}
              </a>
            }
          />
        </div>
      ) : (
      // Columns follow the space rather than a breakpoint ladder that stops at
      // three: `auto-fill` adds a column whenever one fits. Under
      // `xl:grid-cols-3` a 2560px window shows the same three cards as a
      // 1440px one.
      //
      // The 288px floor is measured, not picked. At 1280 the page column is
      // 976px inside its gutter, and a 320px floor fits only two columns there
      // — narrower than the three a breakpoint ladder gives. 288 keeps those
      // three and still yields four at 1920.
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(288px,1fr))]">
        {agents.map((agent) => (
          <Card
            key={agent.agent_id}
            data-testid="agent-option"
            data-agent-name={agent.name}
            className="group flex h-full flex-col gap-0 overflow-hidden py-0 shadow-none transition-colors duration-150 hover:border-primary/40"
          >
            <CardHeader className="border-b bg-muted/30 py-4">
              {/*
                The badge keeps its size and the text gives way. Written as a
                plain flex row the text block does not shrink below its own
                content — it pushes the row 366px past the card's edge, where
                the card's `overflow-hidden` cuts the name mid-letter and takes
                the badge with it.
              */}
              <TruncatingRow
                className="items-start gap-3"
                trail={
                  <Badge
                    variant={agent.state === 'ACTIVE' ? 'secondary' : 'outline'}
                    className="t-mono text-10 font-normal"
                  >
                    {agentStateLabel(String(agent.state || ''), t)}
                  </Badge>
                }
              >
                <div className="space-y-1">
                  <CardTitle className="tracking-[-0.01em]">
                    <Ellipsis>{agent.name}</Ellipsis>
                  </CardTitle>
                  {/* The model is the agent's defining property, so it is what the
                      card names underneath — not an internal id. An agent that
                      names none runs on the deployment's configured model
                      (resolve_model_config fills it from ASTRABOX_MODEL_NAME),
                      so the card says that in the text face. */}
                  {agent.model?.trim() ? (
                    <CardDescription className="t-mono text-11">
                      <Ellipsis>{agent.model}</Ellipsis>
                    </CardDescription>
                  ) : (
                    <CardDescription className="text-11">
                      {t('common:model_deployment_default')}
                    </CardDescription>
                  )}
                </div>
              </TruncatingRow>
            </CardHeader>
            {/* The content column grows and the action sits at its foot, so
                every card in a row ends on the same line. Descriptions run
                one to three lines, so with the button following the text
                directly no two buttons in a row line up. */}
            <CardContent className="flex flex-1 flex-col gap-4 py-4">
              {/* Descriptions distinguish agents in the picker. An empty
                  description stays empty because repeating the adjacent action
                  label would not help readers choose between cards. The clamp
                  prevents one long description from setting the row height;
                  flex space keeps the action buttons aligned. */}
              <p className="t-copy line-clamp-3 flex-1 text-muted-foreground">
                {agent.description?.trim() || ''}
              </p>
              {/* Quiet, not accent. A picker has as many of these as it has
                  agents, and five accent buttons on one screen say that five
                  things are the primary action — which is the same as saying
                  none is (docs/frontend-design.md §7). The card's own hover
                  border is what marks the one under the pointer. */}
              <Button
                variant="outline"
                disabled={startingId === agent.agent_id}
                onClick={() => void startConversationOn(agent.agent_id)}
                className="w-full justify-center"
              >
                {startingId === agent.agent_id ? t('agents:home_starting') : t('agents:home_start')}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
      )}
    </div>
  );
}
