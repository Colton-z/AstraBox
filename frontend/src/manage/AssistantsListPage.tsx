import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import {
  listAssistants,
} from '@/assistant/api';
import type { AssistantRecord } from '@/assistant/types';

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
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

type StateFilter = 'all' | 'ready' | 'dormant';

export default function AssistantsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [assistants, setAssistants] = useState<AssistantRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StateFilter>('all');

  // True after any successful read, an empty list included (see keepsLastRead).
  const loaded = useRef(false);

  const refresh = useCallback(async (context?: ReloadContext) => {
    try {
      setAssistants(await listAssistants());
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

  // Follow while any workspace is materializing — it's the one transient state.
  useKeepCurrent(refresh, {
    follow: assistants.some((a) => assistantMaterializing(a.workspace_state)),
  });

  const readyCount = useMemo(
    () => assistants.filter((a) => String(a.workspace_state) === 'READY').length,
    [assistants],
  );
  const dormantCount = assistants.length - readyCount;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return assistants.filter((a) => {
      const isReady = String(a.workspace_state) === 'READY';
      if (status === 'ready' && !isReady) return false;
      if (status === 'dormant' && isReady) return false;
      if (!q) return true;
      return (
        assistantDisplayName(a).toLowerCase().includes(q) ||
        String(a.environment_name || '').toLowerCase().includes(q) ||
        String(a.engine_kind || '').toLowerCase().includes(q)
      );
    });
  }, [assistants, search, status]);

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
        meta={t('manage:assistants.meta', { count: assistants.length, ready: readyCount })}
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
              total={assistants.length}
              placeholder={t('manage:assistants.search_placeholder')}
            />
            <FilterChips
              aria-label={t('manage:filter.status_aria')}
              value={status}
              onChange={setStatus}
              options={[
                { value: 'all', label: t('common:all'), count: assistants.length },
                { value: 'ready', label: t('manage:assistants.filter_ready'), count: readyCount },
                { value: 'dormant', label: t('manage:assistants.filter_dormant'), count: dormantCount },
              ]}
            />
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
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
            ) : assistants.length === 0 ? (
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
      </div>
    </PageShell>
  );
}
