import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight, Download, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useSWRConfig } from 'swr';

import { Button, buttonVariants } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import { Pagination, PaginationContent, PaginationItem } from '@/components/ui/pagination';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { adminListSessions, buildAdminBatchTranscriptsUrl, listAgents } from '@/api';
import type { AdminSessionFilters, AdminSessionPage, AdminSessionSummary, AgentConfig } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  DateRangeFilter,
  StatusPill,
  NameCell,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import {
  formatDuration,
  sessionStateLabel,
  sessionStateTone,
  sessionStateIsLive,
  sessionUserDisplay,
  shortId,
} from './sessionConfig';
import { MANAGE_NAV_COUNT_KEYS } from './navCounts';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

const ALL = '__all__';
const PAGE_SIZE = 50;

/**
 * Sessions — a page of them, narrowed by the server.
 *
 * Agent and start-time filters are applied by the server before pagination, and
 * `total_items` counts the full narrowed collection.
 *
 * Status and free-text search apply only to the loaded page. They help scan the
 * visible rows and do not describe the full deployment.
 *
 * The export beside them carries the same filters, because the operator's
 * question is "give me these", and an Export that ignored the narrowing above
 * it would answer a different one.
 */
export default function SessionsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { mutate } = useSWRConfig();

  const [data, setData] = useState<AdminSessionPage | null>(null);
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [state, setState] = useState<string>(ALL);
  const [agentId, setAgentId] = useState<string>(ALL);
  const [window, setWindow] = useState<{ since?: string; until?: string }>({});
  const [reloadKey, setReloadKey] = useState(0);

  /** What the server is being asked for — and what Export must be asked for. */
  const filters = useMemo<AdminSessionFilters>(
    () => ({
      agent_id: agentId === ALL ? undefined : agentId,
      // A date is a day, and a window that excluded the last day's own
      // conversations would quietly drop them. `until` is pushed to the end of
      // the day it names.
      since: window.since ? `${window.since}T00:00:00+00:00` : undefined,
      until: window.until ? `${window.until}T23:59:59+00:00` : undefined,
    }),
    [agentId, window.since, window.until],
  );

  // True after any successful read, an empty page included (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const result = await adminListSessions({ ...filters, page, pageSize: PAGE_SIZE });
      setData(result);
      if (page === 1 && !filters.agent_id && !filters.since && !filters.until) {
        await mutate(MANAGE_NAV_COUNT_KEYS.sessions, result.pagination.total_items, {
          revalidate: false,
        });
      }
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      if (page === 1 && !filters.agent_id && !filters.since && !filters.until) {
        await mutate(MANAGE_NAV_COUNT_KEYS.sessions, null, { revalidate: false });
      }
      setError((e as Error).message);
    }
    if (!background) setLoading(false);
  }, [filters, mutate, page]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);
  useKeepCurrent(load);

  // The agent list is the filter's vocabulary, and it is the deployment's, not
  // this page's: picking from the agents that happen to appear on page one
  // would hide every agent whose conversations are further down.
  useEffect(() => {
    void listAgents().then(setAgents).catch(() => {});
  }, []);

  // A narrowing that leaves the reader on page 7 of 2 shows an empty table and
  // no reason for it.
  useEffect(() => {
    setPage(1);
  }, [filters]);

  const docs = data?.items ?? [];
  const pagination = data?.pagination;

  const states = useMemo(
    () => [...new Set(docs.map((s) => s.state).filter(Boolean))].sort(),
    [docs],
  );

  // A filter's vocabulary, written once and read twice: the options in the
  // popup, and the label the closed trigger shows. `<SelectValue>` renders the
  // raw value unless the root carries the same list as its `items`
  // (@base-ui/react/select), so without these the triggers would read
  // `__all__`, an agent's id, and a wire spelling like `PROVISIONING`.
  const agentOptions = useMemo(
    () => [
      { value: ALL, label: t('manage:filter.all_agents') },
      ...agents.map((a) => ({ value: a.agent_id, label: a.name })),
    ],
    [agents, t],
  );

  // `sessionStateLabel` is the same translation the status column reads.
  const stateOptions = useMemo(
    () => [
      { value: ALL, label: t('manage:filter.all_status') },
      ...states.map((s) => ({ value: s, label: sessionStateLabel(s) })),
    ],
    [states, t],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return docs.filter((s) => {
      if (state !== ALL && s.state !== state) return false;
      if (!q) return true;
      return (
        String(s.session_id || '').toLowerCase().includes(q) ||
        String(s.sandbox_id || '').toLowerCase().includes(q) ||
        sessionUserDisplay(s).toLowerCase().includes(q)
      );
    });
  }, [docs, search, state]);

  // Whether the narrowing controls have been earned (docs/frontend-design.md
  // §5): a deployment with no conversations must not carry an agent picker and
  // a date window above its empty state. The search box withdraws itself below
  // a scannable count of its own; these two selects have no such threshold, so
  // they take this one.
  //
  // A narrowing that is in force keeps the whole row, whatever it left behind:
  // an agent filter that matched nothing empties the list, and hiding the
  // control that emptied it leaves the reader nothing to undo. `window` here is
  // this page's date state, not the global.
  const narrowed =
    agentId !== ALL || !!window.since || !!window.until || state !== ALL || search.trim() !== '';
  const narrowingEarned = narrowed || (pagination?.total_items ?? 0) > 0;

  const columns: ConsoleColumn<AdminSessionSummary>[] = [
    {
      key: 'user',
      intent: 'name',
      header: t('manage:common_fields.user'),
      cell: (s) => <NameCell name={sessionUserDisplay(s)} sub={shortId(s.session_id)} />,
    },
    {
      key: 'agent',
      intent: 'text',
      header: t('manage:common_fields.agent'),
      // `template_name` is the agent's name as the session recorded it
      // (`session_service.py` writes it from the agent's own `name`). That is
      // what a reader picks a row by; the agent's id truncates to nothing in
      // this track (docs/frontend-design.md §2), and a name does not belong in
      // the identifier face (§6).
      cell: (s) => <span className="text-foreground">{s.template_name || '—'}</span>,
    },
    {
      key: 'state',
      intent: 'status',
      header: t('common:status'),
      cell: (s) => (
        <StatusPill tone={sessionStateTone(s.state)} live={sessionStateIsLive(s.state)}>
          {sessionStateLabel(s.state)}
        </StatusPill>
      ),
    },
    {
      key: 'sandbox',
      intent: 'identifier',
      header: t('common:sandbox'),
      cell: (s) => <span className="text-muted-foreground">{shortId(s.sandbox_id)}</span>,
    },
    {
      key: 'created',
      intent: 'timestamp',
      header: t('common:created_at'),
      cell: (s) => <span className="text-muted-foreground">{formatDateTime(s.created_at)}</span>,
    },
    {
      key: 'duration',
      intent: 'compact',
      header: t('manage:common_fields.duration'),
      cell: (s) => <span className="text-muted-foreground">{formatDuration(s.duration_seconds)}</span>,
    },
    {
      key: 'runtime',
      intent: 'compact',
      header: t('manage:common_fields.local_runtime'),
      cell: (s) =>
        s.has_local_runtime ? (
          <span className="text-mint-fg">{t('common:yes')}</span>
        ) : (
          <span className="text-muted-foreground">{t('common:no')}</span>
        ),
    },
  ];

  // `Pagination` hard-codes `aria-label="pagination"` in English and centres
  // its row. This page is translated and sets the count opposite the buttons,
  // so both are overridden here.
  //
  // The controls are `<Button>`s inside the items rather than
  // `PaginationLink`: turning a page is a state change with no URL, and
  // `PaginationLink` renders an `<a>`, which has no `disabled` — the first and
  // last page would stay activatable.
  const pager =
    pagination && pagination.total_pages > 1 ? (
      <Pagination
        aria-label={t('manage:sessions.page_of', {
          page: pagination.page,
          pages: pagination.total_pages,
          total: pagination.total_items,
        })}
        className="flex items-center justify-between gap-3"
      >
        <span className="text-11 text-muted-foreground">
          {t('manage:sessions.page_of', {
            page: pagination.page,
            pages: pagination.total_pages,
            total: pagination.total_items,
          })}
        </span>
        <PaginationContent className="gap-2">
          <PaginationItem>
            <Button
              variant="outline"
              size="sm"
              disabled={loading || pagination.page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft className="size-4" />
              {t('manage:sessions.prev_page')}
            </Button>
          </PaginationItem>
          <PaginationItem>
            <Button
              variant="outline"
              size="sm"
              disabled={loading || pagination.page >= pagination.total_pages}
              onClick={() => setPage((p) => p + 1)}
            >
              {t('manage:sessions.next_page')}
              <ChevronRight className="size-4" />
            </Button>
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    ) : null;

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:sessions.title')}
        // The collection's count, never the page's. A "running" tally taken
        // from fifty loaded rows, printed beside a total for a hundred
        // thousand, invents a number about the ones not loaded.
        meta={
          pagination
            ? t('manage:sessions.meta_total', { count: pagination.total_items })
            : undefined
        }
        description={t('manage:sessions.description')}
        actions={
          <>
            {/* Carries the same filters as the list. The operator's question is
                "give me these", so an Export that ignored the narrowing above it
                would answer a different one. Only once the filter selects
                something (§5). */}
            {/* A download is a destination, so Export borrows the button's
                shape through `buttonVariants` instead of being a `Button`:
                `@base-ui/react/button` imposes button semantics on whatever
                it renders, which its own documentation rules out for an
                `<a>`. */}
            {(pagination?.total_items ?? 0) > 0 && (
              <a
                className={buttonVariants({ variant: 'outline' })}
                href={buildAdminBatchTranscriptsUrl(filters)}
                download
              >
                <Download className="size-4" />
                {t('manage:sessions.export_filtered', {
                  count: pagination?.total_items ?? 0,
                })}
              </a>
            )}
            <Button variant="outline" size="icon" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} aria-label={t('common:refresh')}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            </Button>
          </>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        {narrowingEarned && (
          <ConsoleToolbar>
            {/* Server-side, so it narrows the deployment. */}
            <Select
              items={agentOptions}
              value={agentId}
              // Base UI reports a cleared select as `null`, which this filter has
              // no way to reach: every item carries a value, `__all__` included.
              onValueChange={(value) => { if (value !== null) setAgentId(value); }}
            >
              {/* Named independently of its value. `SelectValue` renders the
                  chosen agent, so a screen reader hears "Investment Research"
                  and never learns which filter it is — and before the agent
                  list resolves it renders nothing at all, which is the state
                  axe reported as a button with no discernible text. */}
              <SelectTrigger className="w-56" aria-label={t('manage:filter.agent_aria')}>
                <SelectValue />
              </SelectTrigger>
              {/* The menu hangs off the trigger's box; it does not sit on top of
                  it. Base UI's default `alignItemWithTrigger` is the macOS
                  native-select behaviour, which places the popup so the SELECTED
                  ITEM's text lands on the trigger's text: the text lines up and
                  the boxes do not, which reads as a menu that missed rather than
                  as one that opened. `align="start"` keeps the left edges
                  together. */}
              <SelectContent align="start" alignItemWithTrigger={false}>
                {agentOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <DateRangeFilter value={window} onChange={setWindow} />

            {/* Client-side, over the loaded page only — see the note under the
                table, which says so rather than letting an empty result read as
                "no such session in the deployment". */}
            <ConsoleSearch value={search} onChange={setSearch} total={docs.length} placeholder={t('manage:sessions.search_placeholder')} />
            {(states.length > 1 || state !== ALL) && (
              <Select
                items={stateOptions}
                value={state}
                onValueChange={(value) => { if (value !== null) setState(value); }}
              >
                <SelectTrigger className="w-40" aria-label={t('manage:filter.status_aria')}>
                  <SelectValue />
                </SelectTrigger>
                {/* Anchored like the agent filter above. The options render in a
                    portal, where the check for a wire spelling on screen cannot
                    see them (§6, §8) — `stateOptions` carries the translation. */}
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {stateOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
          rowKey={(s) => s.session_id}
          onRowClick={(s) => navigate(`/manage/sessions/${s.session_id}`)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState
                title={t('manage:sessions.error_list_title')}
                detail={error}
                onRetry={() => setReloadKey((k) => k + 1)}
              />
            ) : (pagination?.total_items ?? 0) === 0 ? (
              <ConsoleEmptyState
                title={t('manage:sessions.empty_title')}
                hint={t('manage:sessions.empty_hint')}
              />
            ) : (
              // Not "no such session": the status filter and the search ran over
              // this page, and there are more pages.
              <ConsoleTableNote>{t('manage:sessions.no_match_on_page')}</ConsoleTableNote>
            )
          }
        />
        {pager}
      </div>
    </PageShell>
  );
}
