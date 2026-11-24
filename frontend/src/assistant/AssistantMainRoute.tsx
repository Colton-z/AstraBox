import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR, { useSWRConfig } from 'swr';

import { AppTopbar, ErrorNote, PageShell } from '@/components/shell';
import { assistantOpensPanel } from '@/manage/assistantConfig';

import { getAssistant, openMainConversation } from './api';
import { useShowAssistantPanel } from './AssistantPanelHost';
import { usePrimaryAssistant } from './AssistantRail';

/**
 * Open the Assistant's native page when its runtime declares one, shown by
 * `AssistantPanelHost`. A runtime without a native page opens the platform main
 * conversation, where scheduled and proactive work reports. `/assistants`
 * opens the primary Assistant.
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
  const showPanel = useShowAssistantPanel();
  const panelRecord = routeId && opensPanel && record ? record : null;

  useEffect(() => {
    if (panelRecord) showPanel(panelRecord);
  }, [panelRecord, showPanel]);

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

  if (panelRecord) {
    // The host shows the page, and keeps it loaded when the route moves on.
    return null;
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
