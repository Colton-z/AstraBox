import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR, { useSWRConfig } from 'swr';

import { AppTopbar, ErrorNote, PageShell } from '@/components/shell';
import { assistantOpensPanel } from '@/manage/assistantConfig';

import { getAssistant, openMainConversation } from './api';
import { AssistantPanelView } from './AssistantPanelView';
import { usePrimaryAssistant } from './AssistantRail';

/**
 * `/assistants/:assistantId`: the Assistant's main conversation.
 *
 * Opening an Assistant always lands in the same conversation, the one its
 * scheduled and proactive work reports into; the server starts it when there
 * is none. This route asks for it and replaces itself with the session view,
 * so the address an Assistant row links to stays stable while the
 * conversation behind it can be replaced. `/assistants` opens the primary
 * Assistant, which the rail has already asked for.
 *
 * An Assistant whose runtime hosts no conversations (`runtime` on its read)
 * has none to land in: this route shows its runtime's own page instead
 * (`AssistantPanelView`), and `/assistants` moves to the Assistant's address,
 * since no session view replaces it. Which kind an Assistant is decides what
 * the route asks the server for, so the Assistant is read first — the primary
 * from the read the rail already holds, any other from the server.
 */
export function AssistantMainRoute({ onOpened }: { onOpened: () => void }) {
  const { t } = useTranslation();
  const { assistantId: routeId = '' } = useParams();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();
  const primary = usePrimaryAssistant();
  const assistantId = routeId || primary.data?.assistant_id || '';
  const held = primary.data?.assistant_id === assistantId ? primary.data : undefined;
  const read = useSWR(
    assistantId && !held ? ['assistant', assistantId] : null,
    () => getAssistant(assistantId),
  );
  const record = held ?? read.data;
  const opensPanel = record ? assistantOpensPanel(record) : undefined;
  const [error, setError] = useState('');

  useEffect(() => {
    if (!routeId && primary.error) navigate('/agents', { replace: true });
  }, [navigate, primary.error, routeId]);

  useEffect(() => {
    if (!routeId && opensPanel && assistantId) {
      navigate(`/assistants/${encodeURIComponent(assistantId)}`, { replace: true });
    }
  }, [assistantId, navigate, opensPanel, routeId]);

  useEffect(() => {
    if (!assistantId || opensPanel !== false) return undefined;
    let cancelled = false;
    setError('');
    openMainConversation(assistantId)
      .then((conversation) => {
        if (cancelled) return;
        void mutate('assistants:rail');
        onOpened();
        navigate(`/sessions/${encodeURIComponent(conversation.session_id)}`, { replace: true });
      })
      .catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      });
    return () => { cancelled = true; };
  }, [assistantId, mutate, navigate, onOpened, opensPanel]);

  if (routeId && opensPanel && record) {
    // Keyed by the Assistant: moving from one to another through the rail
    // keeps this route mounted, and the view holds its Assistant's state.
    return <AssistantPanelView key={record.assistant_id} initial={record} />;
  }

  const failure = error || (read.error as Error | undefined)?.message || '';
  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar crumbs={[{ label: t('shell:tab_agents') }]} trailLabel={t('shell:breadcrumb_navigation')} />
      <PageShell measure="narrow">
        {failure ? (
          <ErrorNote title={t('shell:assistant_open_failed')}>{failure}</ErrorNote>
        ) : (
          <p className="text-sm text-muted-foreground" role="status">{t('common:loading')}</p>
        )}
      </PageShell>
    </div>
  );
}
