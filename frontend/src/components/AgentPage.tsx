import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Bot, Link as LinkIcon } from 'lucide-react';
import { toast } from 'sonner';
import useSWR from 'swr';

import { ApiError, MODULE_BASE, getAgent, startAgentConversation } from '@/api';
import { AppTopbar, EmptyState, ErrorNote, PageShell } from '@/components/shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { agentDisplayName, agentModel } from '@/manage/agentConfig';
import { agentTags } from './agentCatalog';

/** A stable destination for an Agent, using the reader's existing access. */
export function AgentPage({ onConversationCreated }: {
  onConversationCreated: (sessionId: string) => void;
}) {
  const { agentId = '' } = useParams();
  const { t } = useTranslation();
  const { data: agent, error, mutate } = useSWR(['agent:detail', agentId],
    ([, id]) => getAgent(id));
  const [starting, setStarting] = useState(false);
  const [actionError, setActionError] = useState('');
  const unavailable = error instanceof ApiError && error.status === 404;
  const available = !error && agent?.can_start_conversation === true;
  const name = available ? agentDisplayName(agent) : t('agents:detail_title');

  const start = async () => {
    setStarting(true);
    setActionError('');
    try {
      const created = await startAgentConversation(agentId);
      onConversationCreated(created.session_id);
    } catch (e) {
      setActionError((e as Error).message);
      void mutate();
    } finally {
      setStarting(false);
    }
  };

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}${MODULE_BASE}/agents/${encodeURIComponent(agentId)}`,
      );
      toast.success(t('agents:link_copied'));
    } catch (e) {
      toast.error(t('agents:link_copy_failed'), { description: (e as Error).message });
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar
        crumbs={[{ label: t('shell:tab_agents'), to: '/agents' }, { label: name }]}
        trailLabel={t('shell:breadcrumb_navigation')}
      />
      <PageShell measure="narrow">
        {!agent && !error && <p role="status" className="text-sm text-muted-foreground">{t('agents:home_loading')}</p>}
        {error && !unavailable && (
          <ErrorNote title={t('agents:action_failed')}>
            {error.message}
            <Button variant="outline" onClick={() => void mutate()}>{t('common:retry')}</Button>
          </ErrorNote>
        )}
        {(unavailable || (!error && agent && !available)) && (
          <EmptyState icon={<Bot className="size-5" />} title={t('agents:unavailable')} hint={t('agents:unavailable_hint')} />
        )}
        {available && (
          <div className="flex flex-col gap-6">
            <header className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <h1 className="t-h1-tight break-words text-2xl">{name}</h1>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => void copyLink()}>
                  <LinkIcon className="size-4" />{t('agents:copy_link')}
                </Button>
                <Button disabled={starting} onClick={() => void start()}>
                  {starting ? t('agents:home_starting') : t('agents:home_start')}
                </Button>
              </div>
            </header>
            {actionError && <ErrorNote title={t('agents:action_failed')}>{actionError}</ErrorNote>}
            {agent.description?.trim() && (
              <p className="t-copy whitespace-pre-wrap break-words text-muted-foreground">{agent.description}</p>
            )}
            {agentTags(agent).length > 0 && (
              <ul className="flex flex-wrap gap-1.5" aria-label={t('agents:tags_label')}>
                {agentTags(agent).map((tag) => <li key={tag}><Badge variant="outline">#{tag}</Badge></li>)}
              </ul>
            )}
            <dl className="flex flex-col gap-1 border-t pt-4 text-sm">
              <dt className="text-muted-foreground">{t('agents:model')}</dt>
              <dd className="break-words">{agentModel(agent)}</dd>
            </dl>
          </div>
        )}
      </PageShell>
    </div>
  );
}
