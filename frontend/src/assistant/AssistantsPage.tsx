import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles } from 'lucide-react';

import { StatusPill, type PillTone } from '@/components/AstraConsole';
import { Ellipsis, ErrorNote, TruncatingRow } from '@/components/shell';
// One tone map for a workspace state, spelled once: the console's assistant
// pages read this same function, so one state cannot wear two colours across
// the two surfaces (the same reasoning as AstraConsole's toneForState).
import { assistantStateTone } from '@/manage/assistantConfig';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

import {
  getAssistant,
  listAssistants,
  startAssistantConversation,
} from './api';
import type { AssistantRecord } from './types';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

function workspaceStateLabelKey(state: string | undefined): string {
  switch (state) {
    case 'READY': return 'misc:assistant.state_ready';
    case 'MATERIALIZING': return 'misc:assistant.state_materializing';
    case 'HIBERNATING': return 'misc:assistant.state_hibernating';
    case 'RECOVERY_REQUIRED': return 'misc:assistant.state_recovery_required';
    case 'NOT_MATERIALIZED': return 'misc:assistant.state_not_materialized';
    default: return 'misc:assistant.state_unknown';
  }
}

/*
 * Workspace state → console pill tone (docs/frontend-design.md §7).
 *
 * A tone is a pair — a tint ground and the ink chosen to be read on it — which
 * is what carries the label. A ramp hue is a fill: `--citrine` and `--mint` are
 * picked to be seen as a dot or a bar, and set as text over a 10% wash of
 * themselves on this card they measure 1.60:1 and 2.29:1 in the light theme,
 * under the 4.5:1 a label needs; the tones measure 6.07:1 and 5.56:1 on the
 * same card. The dot a tone brings is what §7 asks for besides — a state that
 * survives a greyscale screenshot. The map itself is assistantStateTone
 * (manage/assistantConfig.ts).
 */

export default function AssistantCards({
  onConversationCreated,
}: {
  onConversationCreated: (sessionId: string) => void;
}) {
  const { t } = useTranslation();
  const [assistants, setAssistants] = useState<AssistantRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState<string | null>(null);
  const loadedOnce = useRef(false);

  const refresh = useCallback(async (context?: ReloadContext) => {
    try {
      const basics = await listAssistants();
      const detailed = await Promise.all(
        basics.map((a) => getAssistant(a.assistant_id)),
      );
      setAssistants(detailed);
      loadedOnce.current = true;
      setError('');
    } catch (e: any) {
      if (keepsLastRead(e, context, loadedOnce.current)) return;
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useKeepCurrent(refresh, {
    follow: assistants.some((a) => a.workspace_state === 'MATERIALIZING'),
  });

  const handleStart = async (assistant: AssistantRecord) => {
    setStarting(assistant.assistant_id);
    setError('');
    try {
      const result = await startAssistantConversation(assistant.assistant_id);
      onConversationCreated(result.session_id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setStarting(null);
    }
  };

  // Preserve any unrecognized non-empty wire state so the badge never hides
  // operational information behind the generic unknown label.
  const stateLabelFor = (state: string): string => {
    const key = workspaceStateLabelKey(state);
    if (key === 'misc:assistant.state_unknown' && state) return state;
    return t(key);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col space-y-5">
      {error && <ErrorNote title={t('agents:load_failed')}>{error}</ErrorNote>}
      {/* Keep the page subject visible while the list is loading or failed; the
          status text alone does not identify what the request is loading (§1). */}
      <div className="space-y-1">
        <h1 className="t-h1-tight text-2xl">{t('misc:assistant.pick_title')}</h1>
        <p className="text-sm text-muted-foreground">{t('misc:assistant.pick_subtitle')}</p>
      </div>
      {loading ? (
        <p className="text-sm text-muted-foreground">{t('misc:assistant.loading')}</p>
      ) : (
        <>
      {/*
        An empty list is a state of the page, not a card in a grid of one.
        Inside the grid it keeps the grid's height — the message ends around
        y=200 with ~590px of bare background under it — and inherits a single
        column, so it reads as one lonely card rather than as "there is nothing
        here".
      */}
      {assistants.length === 0 && !error ? (
        <Card className="flex flex-1 items-center justify-center border-dashed">
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            {t('misc:assistant.cards_empty')}
          </CardContent>
        </Card>
      ) : (
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(288px,1fr))]">
        {assistants.map((assistant) => {
          const state = String(assistant.workspace_state || 'NOT_MATERIALIZED');
          return (
            <Card
              key={assistant.assistant_id}
              className="transition-colors hover:bg-muted/30"
              data-testid="assistant-option"
              data-assistant-id={assistant.assistant_id}
              data-assistant-state={state}
            >
              <CardHeader className="border-b bg-muted/30">
                {/*
                  Two rows nested, to hold two alignments at once: the badge
                  sits against the top of the whole header, the icon centres on
                  the text beside it. Without the tracks the text block does not
                  shrink and pushes the badge out of the card — and this card
                  has no `overflow-hidden`, so the badge leaves the frame
                  entirely rather than being cut.
                */}
                <TruncatingRow
                  className="items-start gap-3"
                  trail={
                    <StatusPill tone={assistantStateTone(state)} state={state}>
                      {stateLabelFor(state)}
                    </StatusPill>
                  }
                >
                  <TruncatingRow
                    className="gap-3"
                    lead={
                      <div className="flex size-10 items-center justify-center rounded-xl bg-muted text-muted-foreground">
                        <Sparkles className="size-5" />
                      </div>
                    }
                  >
                    <div className="space-y-1">
                      <CardTitle>
                        <Ellipsis>{assistant.display_name}</Ellipsis>
                      </CardTitle>
                      <CardDescription className="font-mono text-xs">
                        <Ellipsis title={`${assistant.engine_kind} · ${assistant.environment_name}`}>
                          {assistant.engine_kind} · {assistant.environment_name}
                        </Ellipsis>
                      </CardDescription>
                    </div>
                  </TruncatingRow>
                </TruncatingRow>
              </CardHeader>
              <CardContent className="space-y-4 py-4">
                {assistant.description && (
                  <p className="text-sm text-muted-foreground">{assistant.description}</p>
                )}
                <Button
                  className="w-full justify-center"
                  disabled={starting === assistant.assistant_id}
                  onClick={() => void handleStart(assistant)}
                >
                  {starting === assistant.assistant_id ? t('misc:assistant.creating') : t('misc:assistant.start_conversation')}
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>
      )}
        </>
      )}
    </div>
  );
}
