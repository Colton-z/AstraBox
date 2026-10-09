import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import { ErrorNote, PageShell } from '@/components/shell';

import { openMainConversation } from './api';
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
 */
export function AssistantMainRoute({ onOpened }: { onOpened: () => void }) {
  const { t } = useTranslation();
  const { assistantId: routeId = '' } = useParams();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();
  const primary = usePrimaryAssistant();
  const assistantId = routeId || primary.data?.assistant_id || '';
  const [error, setError] = useState('');

  useEffect(() => {
    if (!routeId && primary.error) navigate('/agents', { replace: true });
  }, [navigate, primary.error, routeId]);

  useEffect(() => {
    if (!assistantId) return undefined;
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
  }, [assistantId, mutate, navigate, onOpened]);

  return (
    <PageShell measure="narrow">
      {error ? (
        <ErrorNote title={t('shell:assistant_open_failed')}>{error}</ErrorNote>
      ) : (
        <p className="text-sm text-muted-foreground" role="status">{t('common:loading')}</p>
      )}
    </PageShell>
  );
}
