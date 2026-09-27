import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import {
  listAssistantsPage,
} from '@/assistant/api';
import type { AssistantListPage, AssistantRecord } from '@/assistant/types';

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
  assistantDisplayName,
  assistantEngineLabel,
  assistantStateLabel,
  assistantStateTone,
  assistantStateIsLive,
  assistantMaterializing,
  assistantUpdatedAt,
  formatDateTime,
} from './assistantConfig';
import { useKeepCurrent } from '@/hooks/useKeepCurrent';

type StateFilter = 'all' | 'ready' | 'dormant';
type AssistantCounts = { total: number; ready: number };

const rowsOf = (page: AssistantListPage) => page.assistants;
const countsOf = (page: AssistantListPage): AssistantCounts | null =>
  page.total == null ? null : { total: page.total, ready: page.ready ?? 0 };

/**
 * Assistants — a page at a time, the most recently edited first.
 *
 * The server pages the list, and search and the state chips narrow it there,
 * so they reach every Assistant the reader owns rather than the rows loaded so
 * far. The header and the chips count the whole list: the first page of every
 * read carries those totals.
 */
export default function AssistantsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StateFilter>('all');
  const query = useSettled(search);

  const fetchPage = useCallback(
    (cursor: string | null) => listAssistantsPage({ cursor, q: query, status }),
    [query, status],
  );
  const list = usePagedList<AssistantListPage, AssistantRecord, AssistantCounts>({
    fetchPage,
    rowsOf,
    countsOf,
  });
  const { rows: assistants, loading, error, reload: refresh } = list;

  // Follow while any workspace is materializing — it's the one transient state.
  useKeepCurrent(refresh, {
    follow: assistants.some((a) => assistantMaterializing(a.workspace_state)),
  });

  const total = list.counts?.total ?? 0;
  const readyCount = list.counts?.ready ?? 0;
  const dormantCount = total - readyCount;

  // ---- URL helpers ----------------------------------------------------------
  const openCreate = () => navigate('/manage/assistants/new');

  const columns: ConsoleColumn<AssistantRecord>[] = [
    {
      key: 'name',
      intent: 'name',
      header: t('common:name'),
      cell: (a) => <NameCell name={assistantDisplayName(a)} sub={a.environment_name} />,
    },
    {
      key: 'engine',
      intent: 'text',
      header: t('common:engine'),
      cell: (a) => <span className="text-foreground">{assistantEngineLabel(a)}</span>,
    },
    {
      key: 'state',
      intent: 'status',
      header: t('manage:assistants.workspace_header'),
      cell: (a) => (
        <StatusPill
          tone={assistantStateTone(a.workspace_state)}
          live={assistantStateIsLive(a.workspace_state)}
        >
          {assistantStateLabel(a.workspace_state)}
        </StatusPill>
      ),
    },
    {
      key: 'updated',
      intent: 'timestamp',
      header: t('common:updated_at'),
      cell: (a) => (
        <span className="text-muted-foreground">{formatDateTime(assistantUpdatedAt(a))}</span>
      ),
    },
  ];

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:assistants.title')}
        meta={t('manage:assistants.meta', { count: total, ready: readyCount })}
        description={t('manage:assistants.description')}
        actions={
          <>
            <Button variant="outline" size="icon" onClick={() => void refresh()} disabled={loading} aria-label={t('common:refresh')}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            </Button>
            <Button onClick={() => void openCreate()}>
            <Plus className="size-4" />
            {t('manage:assistants.create')}
          </Button>
          </>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        {/* Not over the error card. A failed refresh keeps the counts it last
            read and empties the table, so an always-mounted toolbar would count
            assistants nobody can see and narrow a list that is not on screen — a
            control answering nothing (§5). The search field owns its own
            threshold and the chips their own counts, so this condition says only
            the thing neither of them can know. */}
        {!error && (
          <ConsoleToolbar>
            <ConsoleSearch
              value={search}
              onChange={setSearch}
              total={total}
              placeholder={t('manage:assistants.search_placeholder')}
            />
            <FilterChips
              aria-label={t('manage:filter.status_aria')}
              value={status}
              onChange={setStatus}
              options={[
                { value: 'all', label: t('common:all'), count: total },
                { value: 'ready', label: t('manage:assistants.filter_ready'), count: readyCount },
                { value: 'dormant', label: t('manage:assistants.filter_dormant'), count: dormantCount },
              ]}
            />
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : assistants}
          rowKey={(a) => a.assistant_id}
          onRowClick={(a) => navigate(`/manage/assistants/${a.assistant_id}`)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState
                title={t('manage:assistants.error_title')}
                detail={error}
                onRetry={() => void refresh()}
              />
            ) : total === 0 ? (
              <ConsoleEmptyState
                title={t('manage:assistants.empty_title')}
                hint={t('manage:assistants.empty_hint')}
                action={
                  <Button size="sm" onClick={() => void openCreate()}>
                    <Plus className="size-4" />
                    {t('manage:assistants.create')}
                  </Button>
                }
              />
            ) : (
              <ConsoleTableNote>{t('manage:assistants.no_match', { query: search })}</ConsoleTableNote>
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
