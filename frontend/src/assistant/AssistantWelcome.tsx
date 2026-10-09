import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';

import { EmptyState } from '@/components/shell';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Badge } from '@/components/ui/badge';
import { listAssistantConnectors, listConnectors } from '../api';

import { getAssistant } from './api';

function Names({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-col items-center gap-1.5">
      <span className="t-eyebrow text-muted-foreground">{label}</span>
      <ul className="flex flex-wrap justify-center gap-1.5">
        {names.map((name) => (
          <li key={name}><Badge variant="secondary" className="font-normal">{name}</Badge></li>
        ))}
      </ul>
    </div>
  );
}

/**
 * An Assistant's main conversation before its first message.
 *
 * What it can work with is whatever this deployment and its owner set up, so
 * that is what the page lists, read from the Assistant and the connector
 * catalog: the apps connected for it, the ones the owner could connect, and
 * the Skills and tools it has, its own and inherited. Nothing here suggests
 * an app the deployment does not offer.
 */
export function AssistantWelcome({ assistantId }: { assistantId: string }) {
  const { t } = useTranslation();
  const assistant = useSWR(['assistant', assistantId], () => getAssistant(assistantId));
  const connectors = useSWR('connectors', listConnectors);
  const enabled = useSWR(['assistant-connectors', assistantId], () => listAssistantConnectors(assistantId));

  const record = assistant.data;
  const on = new Set(enabled.data ?? []);
  const catalog = connectors.data ?? [];
  const connected = catalog
    .filter((c) => c.status === 'connected' && on.has(c.connector_id))
    .map((c) => c.name);
  const connectable = catalog
    .filter((c) => c.available && c.status !== 'connected')
    .map((c) => c.name);
  const unique = (names: string[]) => Array.from(new Set(names));
  const skills = unique([...(record?.inherited?.skills ?? []), ...(record?.skills ?? [])]);
  const tools = unique([
    ...(record?.inherited?.mcp_servers ?? []),
    ...Object.keys(record?.mcp_servers ?? {}),
  ]);
  const error = (assistant.error ?? connectors.error ?? enabled.error) as Error | undefined;
  const name = record?.display_name ?? '';

  return (
    <div className="relative flex-1 min-h-0" role="log" data-testid="assistant-welcome">
      <EmptyState
        icon={(
          <Avatar className="size-8">
            <AvatarFallback className="font-semibold">
              {Array.from(name.trim())[0]?.toUpperCase() ?? ''}
            </AvatarFallback>
          </Avatar>
        )}
        title={name || t('common:loading')}
        hint={error ? error.message : t('chat:assistant_welcome.hint')}
        action={(
          <div className="flex flex-col gap-4">
            <Names label={t('chat:assistant_welcome.connected')} names={connected} />
            <Names label={t('chat:assistant_welcome.skills')} names={skills} />
            <Names label={t('chat:assistant_welcome.tools')} names={tools} />
            {connectable.length > 0 && (
              <div className="flex flex-col items-center gap-1.5">
                <Names label={t('chat:assistant_welcome.connectable')} names={connectable} />
                <Link to="/connections" className="text-13 text-primary underline-offset-4 hover:underline">
                  {t('chat:assistant_welcome.open_connections')}
                </Link>
              </div>
            )}
          </div>
        )}
      >
        <div className="bg-starfield pointer-events-none absolute inset-0 opacity-50" aria-hidden />
      </EmptyState>
    </div>
  );
}
