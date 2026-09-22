import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import {
  listAgents,
  listDeployments,
} from '@/api';
import type { AgentConfig, AgentDeployment } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  FilterChips,
  StatusPill,
  NameCell,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

// A Deployment is an internal schedule or authenticated external/channel
// trigger binding on an Agent (docs/domain-model.md).
type StatusFilter = 'all' | 'enabled' | 'disabled';

// One row per binding, with the agent it triggers resolved for display.
interface BindingRow {
  deployment: AgentDeployment;
  agentName: string;
}

export default function DeploymentsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [rows, setRows] = useState<BindingRow[]>([]);
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');

  // True after any successful read, an empty list included (see keepsLastRead).
  const loaded = useRef(false);
  const refresh = useCallback(async (context?: ReloadContext) => {
    try {
      // The deployments endpoint resolves management scope and Agent names
      // across the collection, avoiding a request for every Agent.
      const [agentList, deployments] = await Promise.all([
        listAgents(),
        listDeployments(),
      ]);
      const agentNames = new Map(agentList.map((agent) => [agent.agent_id, agent.name]));
      setAgents(agentList);
      setRows(deployments.map((deployment) => ({
        deployment,
        agentName:
          deployment.agent_name
          || agentNames.get(deployment.agent_id)
          || deployment.agent_id,
      })));
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useKeepCurrent(refresh);

  const enabledCount = useMemo(
    () => rows.filter((r) => r.deployment.enabled !== false).length,
    [rows],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      const enabled = r.deployment.enabled !== false;
      if (status === 'enabled' && !enabled) return false;
      if (status === 'disabled' && enabled) return false;
      if (!q) return true;
      return (
        r.agentName.toLowerCase().includes(q) ||
        String(r.deployment.name || '').toLowerCase().includes(q) ||
        String(r.deployment.scene || '').toLowerCase().includes(q) ||
        r.deployment.deployment_id.toLowerCase().includes(q)
      );
    });
  }, [rows, search, status]);

  const openCreate = () => navigate('/manage/deployments/new');

  const columns: ConsoleColumn<BindingRow>[] = [
    {
      key: 'agent',
      intent: 'name',
      header: t('manage:deployments.field_agent'),
      cell: (r) => <NameCell name={r.agentName} sub={r.deployment.deployment_id} />,
    },
    {
      key: 'scene',
      intent: 'identifier',
      header: t('manage:deployments.field_scene'),
      cell: (r) => (
        <NameCell
          name={r.deployment.name || r.deployment.scene}
          sub={r.deployment.name ? r.deployment.scene : undefined}
          subKind="text"
        />
      ),
    },
    {
      key: 'status',
      intent: 'status',
      header: t('common:status'),
      cell: (r) =>
        r.deployment.enabled !== false ? (
          <StatusPill tone="done">{t('common:enabled')}</StatusPill>
        ) : (
          <StatusPill tone="idle">{t('common:disabled')}</StatusPill>
        ),
    },
    {
      key: 'created',
      intent: 'timestamp',
      header: t('common:created_at'),
      cell: (r) => (
        <span className="text-muted-foreground">
          {r.deployment.created_at ? formatDateTime(r.deployment.created_at) : '—'}
        </span>
      ),
    },
  ];

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:deployments.title')}
        meta={t('manage:deployments.meta', { count: rows.length, enabled: enabledCount })}
        description={t('manage:deployments.description')}
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="icon" onClick={() => void refresh()} disabled={loading} aria-label={t('common:refresh')}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            </Button>
            <Button onClick={openCreate} disabled={agents.length === 0}>
              <Plus className="size-4" />
              {t('manage:deployments.new_binding')}
            </Button>
          </div>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        {/* Not over the error card. A failed refresh keeps the bindings it last
            read and empties the table, so an always-mounted toolbar would count
            triggers nobody can see and narrow a list that is not on screen — a
            control answering nothing (§5). The search field owns its own
            threshold and the chips their own counts, so this condition says only
            the thing neither of them can know. */}
        {!error && (
          <ConsoleToolbar>
            <ConsoleSearch
              value={search}
              onChange={setSearch}
              total={rows.length}
              placeholder={t('manage:deployments.search_placeholder')}
            />
            <FilterChips
              aria-label={t('manage:filter.status_aria')}
              value={status}
              onChange={setStatus}
              options={[
                { value: 'all', label: t('common:all'), count: rows.length },
                { value: 'enabled', label: t('common:enabled'), count: enabledCount },
                { value: 'disabled', label: t('common:disabled'), count: rows.length - enabledCount },
              ]}
            />
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
          rowKey={(r) => r.deployment.deployment_id}
          onRowClick={(r) => navigate(`/manage/deployments/${r.deployment.deployment_id}`)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState
                title={t('manage:deployments.error_list_title')}
                detail={error}
                onRetry={() => void refresh()}
              />
            ) : rows.length === 0 ? (
              <ConsoleEmptyState
                title={t('manage:deployments.empty_title')}
                hint={
                  agents.length === 0
                    ? t('manage:deployments.empty_hint_no_agents')
                    : t('manage:deployments.empty_hint')
                }
                action={
                  agents.length > 0 ? (
                    <Button size="sm" onClick={openCreate}>
                      <Plus className="size-4" />
                      {t('manage:deployments.new_binding')}
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <ConsoleTableNote>{t('manage:deployments.no_match', { query: search })}</ConsoleTableNote>
            )
          }
        />
      </div>
    </PageShell>
  );
}
