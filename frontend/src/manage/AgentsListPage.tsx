import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import { listAgentsPage } from '@/api';
import type { AgentConfig, AgentListPage } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  ConsoleLoadMore,
  FilterChips,
  usePagedList,
  useSettled,
  StatusPill,
  NameCell,
  type ConsoleColumn,
} from './console';
import {
  agentDisplayName,
  agentModel,
  agentUpdatedAt,
  formatDateTime,
} from './agentConfig';
import { MANAGE_NAV_COUNT_KEYS } from './navCounts';
import { useKeepCurrent } from '@/hooks/useKeepCurrent';

type StatusFilter = 'all' | 'enabled' | 'disabled';
type AgentCounts = { total: number; enabled: number };

const rowsOf = (page: AgentListPage) => page.agents;
const countsOf = (page: AgentListPage): AgentCounts | null =>
  page.total == null ? null : { total: page.total, enabled: page.enabled ?? 0 };

/**
 * Agents — a page at a time, by name regardless of case.
 *
 * The server pages the list, and search and the status chips narrow it there,
 * so they reach every Agent the reader may see rather than the rows loaded so
 * far. The header, the chips and the rail count the whole list: the first
 * page of every read carries those totals.
 */
export default function AgentsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');
  const query = useSettled(search);

  const fetchPage = useCallback(
    (cursor: string | null) => listAgentsPage({ cursor, q: query, status }),
    [query, status],
  );
  const publishCount = useCallback(
    (counts: AgentCounts | null) => {
      void mutate(MANAGE_NAV_COUNT_KEYS.agents, counts?.total ?? null, { revalidate: false });
    },
    [mutate],
  );
  const list = usePagedList<AgentListPage, AgentConfig, AgentCounts>({
    fetchPage,
    rowsOf,
    countsOf,
    onCounts: publishCount,
  });
  const { rows, loading, error, reload: load } = list;
  useKeepCurrent(load);

  const total = list.counts?.total ?? 0;
  const enabledCount = list.counts?.enabled ?? 0;

  // A row opens the Agent's own page (docs/frontend-design.md §3). The Agent
  // editor is a form, and a form needs the full width.
  const openRecord = (id: string) => navigate(`/manage/agents/${encodeURIComponent(id)}`);
  const openCreate = () => navigate('/manage/agents/new');

  const columns: ConsoleColumn<AgentConfig>[] = [
    {
      key: 'name',
      intent: 'name',
      header: t('common:name'),
      // Use the description as the sub-line. `name` usually duplicates
      // `display_name`, while the description helps distinguish two Agents
      // (docs/frontend-design.md §2).
      //
      // Enabled and public describe nearly every row, so they earn no column of
      // their own; only the states worth noticing are marked, and they are
      // marked here, where the reader is already looking.
      cell: (row) => (
        <NameCell
          name={
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate">{agentDisplayName(row)}</span>
              {row.enabled === false && (
                <StatusPill tone="idle">{t('manage:agents.badge_disabled')}</StatusPill>
              )}
              {row.visibility === 'private' && (
                <StatusPill tone="idle">{t('manage:agents.badge_private')}</StatusPill>
              )}
              {row.visibility === 'allowlist' && (
                <StatusPill tone="idle">{t('manage:agents.badge_allowlist')}</StatusPill>
              )}
            </span>
          }
          sub={row.description?.trim() || undefined}
          subKind="text"
        />
      ),
    },
    {
      key: 'model',
      intent: 'identifier',
      header: t('common:model'),
      // An agent that names no model runs on the one the deployment
      // configured: resolve_model_config() fills model_name from
      // ASTRABOX_MODEL_NAME at turn time. A dash would read as a data hole, so
      // the cell names that behaviour instead. It is a word, not an identity,
      // so it leaves the column's mono face.
      cell: (row) =>
        agentModel(row) ? (
          <span className="text-muted-foreground">{agentModel(row)}</span>
        ) : (
          <span className="font-sans text-muted-foreground">{t('common:model_deployment_default')}</span>
        ),
    },
    {
      // Which environment an agent runs on decides its engine, its sandbox
      // backend and its image, so the list carries it rather than making the
      // reader open each agent to find out.
      key: 'environment',
      intent: 'identifier',
      header: t('manage:agents.col_environment'),
      cell: (row) => (
        <span className="text-muted-foreground">{row.environment_name || '—'}</span>
      ),
    },
    {
      key: 'updated',
      intent: 'timestamp',
      header: t('common:updated_at'),
      cell: (row) => <span className="text-muted-foreground">{formatDateTime(agentUpdatedAt(row))}</span>,
    },
  ];

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:agents.title')}
        meta={t('manage:agents.meta', { count: total, enabled: enabledCount })}
        description={t('manage:agents.description')}
        actions={
          <>
            <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} aria-label={t('common:refresh')}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            </Button>
            <Button onClick={openCreate}>
            <Plus className="size-4" />
            {t('manage:agents.create')}
          </Button>
          </>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        {/* Not over the error card. A failed refresh keeps the rows it last read
            but empties the table, and chips left standing there would count
            agents that are not on screen and narrow a list the reader cannot see
            (docs/frontend-design.md §5). The search field owns its own threshold
            and the chips their own counts, so this condition says only the thing
            neither of them can know. */}
        {!error && (
          <ConsoleToolbar>
            <ConsoleSearch
              value={search}
              onChange={setSearch}
              total={total}
              placeholder={t('manage:agents.search_placeholder')}
            />
            <FilterChips
              aria-label={t('manage:filter.status_aria')}
              value={status}
              onChange={setStatus}
              options={[
                { value: 'all', label: t('common:all'), count: total },
                { value: 'enabled', label: t('common:enabled'), count: enabledCount },
                { value: 'disabled', label: t('common:disabled'), count: total - enabledCount },
              ]}
            />
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : rows}
          rowKey={(a) => a.agent_id}
          onRowClick={(a) => openRecord(a.agent_id)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState
                title={t('manage:agents.error_title')}
                detail={error}
                onRetry={() => void load()}
              />
            ) : total === 0 ? (
              <ConsoleEmptyState
                title={t('manage:agents.empty_title')}
                hint={t('manage:agents.empty_hint')}
                action={
                  <Button size="sm" onClick={openCreate}>
                    <Plus className="size-4" />
                    {t('manage:agents.create')}
                  </Button>
                }
              />
            ) : (
              <ConsoleTableNote>{t('manage:agents.no_match', { query: search })}</ConsoleTableNote>
            )
          }
        />
        {!loading && !error && (
          <ConsoleLoadMore
            hasMore={list.hasMore}
            loading={list.loadingMore}
            error={list.moreError}
            onLoadMore={() => void list.loadMore()}
          />
        )}
      </div>
    </PageShell>
  );
}
