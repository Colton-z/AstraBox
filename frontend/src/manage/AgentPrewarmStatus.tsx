import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { getAgentPreparedRuntime, refreshAgentPreparedRuntime } from '@/api';
import type { AgentPreparedRuntimeStatus } from '@/types';
import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell';
import { useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import { ConsoleCard, ConsoleFieldRow, formatDateTimeSeconds } from './console';

export function AgentPrewarmStatus({ agentId, savedAt, enabled, dirty }: {
  agentId: string;
  savedAt: string;
  enabled: boolean;
  dirty: boolean;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<AgentPreparedRuntimeStatus | null>(null);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (context?: ReloadContext) => {
    try {
      setStatus(await getAgentPreparedRuntime(agentId));
      setError('');
    } catch (e) {
      // A failed background read leaves the last observation on screen;
      // initial and administrator-requested reads report their own failure.
      if (!context?.background) setError((e as Error).message);
    }
  }, [agentId]);

  useEffect(() => { void load(); }, [load, savedAt]);
  useKeepCurrent(load, { follow: enabled });

  const reprepare = async () => {
    setRefreshing(true);
    setError('');
    try {
      setStatus(await refreshAgentPreparedRuntime(agentId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  };
  const state = !status ? 'loading' : !status.enabled ? 'disabled'
    : status.ready ? 'ready' : status.last_error ? 'failed'
      : status.state === 'expired' ? 'expired' : 'preparing';

  return (
    <ConsoleCard title={t('manage:prewarm.label')} intro={t('manage:prewarm.help')}>
      <div className="flex flex-col gap-2" data-testid="agent-prewarm-status">
        <div className="flex flex-wrap items-center gap-3">
          <span data-testid="prewarm-state">{t(`manage:prewarm.${state}`)}</span>
          <span className="tabular-nums" data-testid="prewarm-count">
            {t('manage:prewarm.available', { count: status?.prepared_count ?? '—' })}
          </span>
          <Button type="button" variant="outline" size="sm"
            disabled={!enabled || dirty || refreshing || !status?.enabled}
            onClick={() => void reprepare()}>
            {t(refreshing ? 'manage:prewarm.requesting' : 'manage:prewarm.reprepare')}
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => void load()}>
            {t('common:refresh')}
          </Button>
        </div>
        {status?.sandbox_id && (
          <ConsoleFieldRow label={t('common:sandbox')}>
            <span className="font-mono select-text">{status.sandbox_id}</span>
          </ConsoleFieldRow>
        )}
        {status?.prepared_at && Number.isFinite(Date.parse(status.prepared_at)) && (
          <ConsoleFieldRow label={t('manage:prewarm.prepared_at')}>
            <time dateTime={status.prepared_at} className="tabular-nums">
              {formatDateTimeSeconds(status.prepared_at)}
            </time>
          </ConsoleFieldRow>
        )}
        {dirty && <p className="text-sm text-muted-foreground">{t('manage:prewarm.save_first')}</p>}
        {status?.last_error && <ErrorNote>{status.last_error}</ErrorNote>}
        {error && <ErrorNote>{error}</ErrorNote>}
      </div>
    </ConsoleCard>
  );
}
