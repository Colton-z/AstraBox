import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import {
  Pagination,
  PaginationContent,
  PaginationItem,
} from '@/components/ui/pagination';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { adminListSandboxes } from '@/api';
import type { AdminSandboxPage, AdminSandboxSummary } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  StatusPill,
  NameCell,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import {
  sandboxImage,
  sandboxMatches,
  sandboxStateIsLive,
  sandboxStateLabel,
  sandboxStateTone,
  shortId,
} from './sandboxConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

const ALL = '__all__';

//: One page as asked for from the backend. The backend pages at the source, so
//: this is a real request size, not a client-side slice.
const PAGE_SIZE = 50;

/**
 * Sandboxes — what the sandbox backend is actually running.
 *
 * Read-only by construction: a sandbox is created and destroyed by a session's
 * lifecycle, and this page never offers a second authority over that. It asks
 * the backend three questions (list, detail, diagnostics) and shows the answers
 * as given.
 *
 * Three behaviours are deliberate and worth keeping:
 *
 * * a backend that cannot enumerate its sandboxes produces an error here, with
 *   its reason — never an empty table, which would read as "nothing is
 *   running";
 * * paging is the backend's. The counters on the response drive the pager, so
 *   the page never implies it is showing the whole inventory when it is not.
 *   The only count in the header is the backend's own `total_items`: anything
 *   derived from the loaded page would be a number about a window, printed
 *   where a number about the inventory is expected;
 * * search and the status filter run over the loaded page, because the listing
 *   seam takes no filter (`list_sandboxes(page, page_size)`) and there is
 *   nothing to push down to. So whenever the inventory spans more than one
 *   page, the page says so — an empty result is "not on this page", which is a
 *   different claim from "no such sandbox", and the copy never makes the
 *   stronger one.
 */
export default function SandboxesListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [data, setData] = useState<AdminSandboxPage | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [state, setState] = useState<string>(ALL);
  const [reloadKey, setReloadKey] = useState(0);
  // Set only by the hook's own re-read and consumed by the effect below: a
  // page change or a Refresh bumps the key without it and reads in the open.
  const backgroundRead = useRef(false);
  // True after any successful read, an empty page included (see keepsLastRead).
  const loaded = useRef(false);

  useEffect(() => {
    let alive = true;
    const context: ReloadContext = { background: backgroundRead.current };
    backgroundRead.current = false;
    void (async () => {
      if (!context.background) setLoading(true);
      try {
        const result = await adminListSandboxes({ page, pageSize: PAGE_SIZE });
        if (!alive) return;
        setData(result);
        setError('');
        loaded.current = true;
      } catch (e) {
        if (!alive || keepsLastRead(e, context, loaded.current)) return;
        setError((e as Error).message);
        setData(null);
      } finally {
        // Unconditional: a background read that replaced a cancelled open one
        // (the key moved while the mount read was in flight) is the read whose
        // end clears the skeleton.
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [page, reloadKey]);
  useKeepCurrent(useCallback((context?: ReloadContext) => {
    backgroundRead.current = context?.background === true;
    setReloadKey((k) => k + 1);
  }, []));

  const rows = useMemo(() => data?.items ?? [], [data]);

  // The states offered by the filter are the ones on this page — the backend
  // publishes no vocabulary of states, and inventing one would offer a filter
  // for a value nothing here has.
  const states = useMemo(
    () => [...new Set(rows.map((s) => s.state).filter(Boolean))].sort(),
    [rows],
  );

  // The filter's vocabulary, written once and read twice: the options in the
  // popup, and the label the closed trigger shows. `<SelectValue>` renders the
  // raw value unless the root carries the same list as its `items`
  // (@base-ui/react/select), so without this the trigger would read `__all__`
  // — the sentinel, on screen (§6). A state keeps the backend's own spelling
  // here because that is what the status column shows too; `sandboxStateLabel`
  // exists to say so.
  const stateOptions = useMemo(
    () => [
      { value: ALL, label: t('manage:filter.all_status') },
      ...states.map((s) => ({ value: s, label: s })),
    ],
    [states, t],
  );

  const filtered = useMemo(
    () =>
      rows.filter(
        (s) => (state === ALL || s.state === state) && sandboxMatches(s, search),
      ),
    [rows, search, state],
  );

  const columns: ConsoleColumn<AdminSandboxSummary>[] = [
    {
      key: 'id',
      intent: 'name',
      header: t('manage:sandboxes.col_sandbox'),
      cell: (s) => <NameCell name={shortId(s.sandbox_id)} sub={s.backend} />,
    },
    {
      key: 'state',
      intent: 'status',
      header: t('common:status'),
      cell: (s) => (
        <StatusPill tone={sandboxStateTone(s.state)} live={sandboxStateIsLive(s.state)}>
          {sandboxStateLabel(s.state)}
        </StatusPill>
      ),
    },
    {
      key: 'session',
      intent: 'identifier',
      header: t('manage:sandboxes.col_session'),
      cell: (s) => (
        <span className="text-muted-foreground">
          {s.session_id ? shortId(s.session_id) : '—'}
        </span>
      ),
    },
    {
      key: 'image',
      intent: 'identifier',
      header: t('manage:sandboxes.col_image'),
      cell: (s) => <span className="text-muted-foreground">{sandboxImage(s)}</span>,
    },
    {
      key: 'created',
      intent: 'timestamp',
      header: t('common:created_at'),
      cell: (s) => <span className="text-muted-foreground">{formatDateTime(s.created_at)}</span>,
    },
    {
      key: 'expires',
      intent: 'timestamp',
      header: t('manage:sandboxes.col_expires'),
      cell: (s) => <span className="text-muted-foreground">{formatDateTime(s.expires_at)}</span>,
    },
  ];

  const pagination = data?.pagination;
  const totalPages = pagination?.total_pages ?? 1;
  // More than this page exists, so a client-side filter is looking at a window.
  const partialView = !!pagination && (pagination.total_pages > 1 || pagination.has_next_page);
  const filtering = search.trim() !== '' || state !== ALL;
  const pager =
    pagination && (pagination.has_next_page || pagination.page > 1) ? (
      <div className="flex items-center justify-end gap-2 border-t px-4 py-2.5">
        {/* A sentence about two counts, so it is not mono: `tabular-nums` is
            what keeps the digits from twitching as the pages turn, and mono
            would claim the reader will paste this (docs/frontend-design.md
            §6, §8). */}
        <span className="mr-auto text-11 text-muted-foreground tabular-nums">
          {t('manage:sandboxes.page_of', {
            page: pagination.page,
            pages: totalPages,
            total: pagination.total_items,
          })}
        </span>
        {/* A navigation landmark holding the two controls, and buttons rather
            than `PaginationLink`: turning a page here sets state instead of
            visiting an address, and a control that must be able to say
            `disabled` cannot be an anchor.

            `mx-0` because an auto margin on a flex item claims the free space
            the count's `mr-auto` claims, which parks the pager mid-row instead
            of against the right edge. */}
        <Pagination
          aria-label={t('manage:sandboxes.page_of', {
            page: pagination.page,
            pages: totalPages,
            total: pagination.total_items,
          })}
          className="mx-0 w-auto justify-end"
        >
          <PaginationContent className="gap-2">
            <PaginationItem>
              <Button
                variant="outline"
                size="sm"
                disabled={loading || pagination.page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                <ChevronLeft className="size-4" />
                {t('manage:sandboxes.prev_page')}
              </Button>
            </PaginationItem>
            <PaginationItem>
              <Button
                variant="outline"
                size="sm"
                disabled={loading || !pagination.has_next_page}
                onClick={() => setPage((p) => p + 1)}
              >
                {t('manage:sandboxes.next_page')}
                <ChevronRight className="size-4" />
              </Button>
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    ) : undefined;

  return (
    <PageShell
    >
      <ConsolePageHeader
        title={t('manage:sandboxes.title')}
        // The backend's own inventory count, and nothing derived from the page
        // in hand: a "running" tally taken from 50 loaded rows, printed next to
        // a total for thousands, invents a number about how many are not.
        meta={pagination ? t('manage:sandboxes.meta', { count: pagination.total_items }) : undefined}
        description={t('manage:sandboxes.description')}
        actions={
          <Button variant="outline" size="icon" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} aria-label={t('common:refresh')}>
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        <ConsoleToolbar>
          <ConsoleSearch
            value={search}
            onChange={setSearch}
            // Whether search renders follows the whole inventory — that is what
            // the reader is hunting through — even though its reach is only the
            // loaded page (the scope note below owns that honesty).
            total={pagination?.total_items ?? rows.length}
            placeholder={t('manage:sandboxes.search_placeholder')}
          />
          {/* One distinct state on this page = a choice that changes nothing, so
              the select waits for a second one — unless it is actively narrowing,
              which must stay visible to be undone (docs/frontend-design.md §5). */}
          {(states.length > 1 || state !== ALL) && (
            <Select
              items={stateOptions}
              value={state}
              // Base UI reports a cleared select as `null`, which this filter has
              // no way to reach: every option carries a value, `__all__`
              // included. Narrowing to nothing is not a state it can hold.
              onValueChange={(value) => { if (value !== null) setState(value); }}
            >
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              {/* The menu hangs off the trigger's box; it does not sit on top of
                  it. Base UI's default `alignItemWithTrigger` is the macOS
                  native-select behaviour, which places the popup so the selected
                  item's text lands on the trigger's text: the text lines up and
                  the boxes do not, which reads as a menu that missed rather than
                  as one that opened. `align="start"` keeps the left edges
                  together. */}
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

        {/* Said where the filter is, while it is on: the controls above reach one
            page, and an operator who types an id that is not on it must not read
            the empty table as an answer about the inventory. */}
        {!loading && !error && filtering && partialView && pagination && (
          <p className="text-11 leading-snug text-muted-foreground">
            {t('manage:sandboxes.filter_scope_note', {
              page: pagination.page,
              pages: totalPages,
            })}
          </p>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
          rowKey={(s) => s.sandbox_id}
          onRowClick={(s) => navigate(`/manage/sandboxes/${s.sandbox_id}`)}
          footer={error || loading ? undefined : pager}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              // A backend that cannot answer says so, with its reason. Rendering
              // an empty table here would claim the backend runs nothing.
              <ConsoleErrorState
                title={t('manage:sandboxes.error_title')}
                detail={error}
                onRetry={() => setReloadKey((k) => k + 1)}
              />
            ) : rows.length === 0 ? (
              <ConsoleEmptyState
                title={t('manage:sandboxes.empty_title')}
                hint={t('manage:sandboxes.empty_hint')}
              />
            ) : partialView && pagination ? (
              // Nothing matched in this window. Saying "no results" here would
              // answer a question about the inventory that was never asked of it.
              <ConsoleTableNote>
                {t('manage:sandboxes.no_match_on_page', {
                  page: pagination.page,
                  pages: totalPages,
                })}
              </ConsoleTableNote>
            ) : (
              // One page is the whole inventory, so this really is "no results".
              <ConsoleTableNote>{t('manage:sandboxes.no_match')}</ConsoleTableNote>
            )
          }
        />
      </div>
    </PageShell>
  );
}
