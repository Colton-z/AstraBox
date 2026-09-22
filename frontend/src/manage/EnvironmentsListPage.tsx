import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import { listAdminEnvironments } from '@/api';
import type { EnvironmentConfig } from '@/types';

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
  envDisplayName,
  envEngineLabel,
  envSandbox,
  envUpdatedAt,
  formatDateTime,
} from './environmentConfig';
import { MANAGE_NAV_COUNT_KEYS } from './navCounts';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

type StatusFilter = 'all' | 'enabled' | 'disabled';

export default function EnvironmentsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();
  const [docs, setDocs] = useState<EnvironmentConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');


  // True after any successful read, an empty list included (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const list = await listAdminEnvironments();
      setDocs([...list].sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''))));
      await mutate(MANAGE_NAV_COUNT_KEYS.environments, list.length, { revalidate: false });
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      await mutate(MANAGE_NAV_COUNT_KEYS.environments, null, { revalidate: false });
      setError((e as Error).message);
    } finally {
      if (!background) setLoading(false);
    }
  }, [mutate]);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load);


  const enabledCount = useMemo(() => docs.filter((e) => e.enabled !== false).length, [docs]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return docs.filter((e) => {
      const enabled = e.enabled !== false;
      if (status === 'enabled' && !enabled) return false;
      if (status === 'disabled' && enabled) return false;
      if (!q) return true;
      return (
        String(e.name || '').toLowerCase().includes(q) ||
        envDisplayName(e).toLowerCase().includes(q) ||
        envEngineLabel(e).toLowerCase().includes(q)
      );
    });
  }, [docs, search, status]);


  // A row opens the record's own page (docs/frontend-design.md §3) rather than
  // a panel beside the list: the form needs the width, and the URL of a record
  // should be the record.
  const openRecord = (name: string) => navigate(`/manage/environments/${encodeURIComponent(name)}`);
  const openCreate = () => navigate('/manage/environments/new');

  const columns: ConsoleColumn<EnvironmentConfig>[] = [
    {
      key: 'name',
      intent: 'name',
      header: t('common:name'),
      // Enabled is what nearly every environment is, so it earns no column —
      // only the departure is marked, next to the name where the reader is
      // already looking (docs/frontend-design.md §2).
      cell: (e) => (
        <NameCell
          name={
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate">{envDisplayName(e)}</span>
              {e.enabled === false && (
                <StatusPill tone="idle">{t('common:disabled')}</StatusPill>
              )}
              {e.engine_available === false && (
                <StatusPill tone="failed">{t('manage:environments.engine_missing')}</StatusPill>
              )}
            </span>
          }
          sub={e.name}
        />
      ),
    },
    {
      key: 'engine',
      intent: 'text',
      header: t('common:engine'),
      cell: (e) => <span className="text-foreground">{envEngineLabel(e)}</span>,
    },
    {
      key: 'sandbox',
      intent: 'identifier',
      header: t('common:sandbox'),
      cell: (e) => <span className="text-muted-foreground">{envSandbox(e)}</span>,
    },
    {
      key: 'updated',
      intent: 'timestamp',
      header: t('common:updated_at'),
      cell: (e) => <span className="text-muted-foreground">{formatDateTime(envUpdatedAt(e))}</span>,
    },
  ];

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:environments.title')}
        meta={t('manage:environments.meta', { count: docs.length, enabled: enabledCount })}
        description={t('manage:environments.description')}
        actions={
          <>
            <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} aria-label={t('common:refresh')}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            </Button>
            <Button onClick={openCreate}>
            <Plus className="size-4" />
            {t('manage:environments.create')}
          </Button>
          </>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        <ConsoleToolbar>
          <ConsoleSearch value={search} onChange={setSearch} total={docs.length} placeholder={t('manage:environments.search_placeholder')} />
          <FilterChips
            aria-label={t('manage:filter.status_aria')}
            value={status}
            onChange={setStatus}
            options={[
              { value: 'all', label: t('common:all'), count: docs.length },
              { value: 'enabled', label: t('common:enabled'), count: enabledCount },
              { value: 'disabled', label: t('common:disabled'), count: docs.length - enabledCount },
            ]}
          />
        </ConsoleToolbar>

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
          rowKey={(e) => e.name}
          onRowClick={(e) => openRecord(e.name)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState
                title={t('manage:environments.error_title')}
                detail={error}
                onRetry={() => void load()}
              />
            ) : docs.length === 0 ? (
              <ConsoleEmptyState
                title={t('manage:environments.empty_title')}
                hint={t('manage:environments.empty_hint')}
                action={
                  <Button size="sm" onClick={openCreate}>
                    <Plus className="size-4" />
                    {t('manage:environments.create')}
                  </Button>
                }
              />
            ) : (
              <ConsoleTableNote>{t('manage:environments.no_match', { query: search })}</ConsoleTableNote>
            )
          }
        />
      </div>
    </PageShell>
  );
}
