import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell, Ellipsis } from '@/components/shell';
import { adminListErrors } from '@/api';
import type { AdminErrorItem, AdminErrorsPage } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  FilterChips,
  StatusPill,
  type ConsoleColumn,
  type FilterChipOption,
  type PillTone,
} from './console';
import { formatDateTime } from './agentConfig';
import { shortId } from './sandboxConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

/**
 * Errors — what has gone wrong, across sessions and turns.
 *
 * The rows come from two places the server joins: sessions carrying a
 * `last_error`, and turn checkpoints that failed. `source` says which, and it is
 * a column rather than two tables because the operator's question ("what is
 * broken right now") does not care, while the follow-up ("broken where") does.
 *
 * A row is not the thing itself — it is a summary pointing at a session, so the
 * row click hands off to that session's own page rather than growing a second
 * detail view over the same record. The address is the record's, not a filter
 * on the list: `/manage/sessions` reads no query parameter, so a click carrying
 * one lands on an unfiltered page of five hundred rows.
 *
 * The list is a window, not an archive: the server clamps to 500 and orders by
 * recency, and there is no paging behind it. The header says how many it is
 * showing so the count is never read as "this is every error there has been".
 */

const ALL = '__all__';

/** A row plus the key it is rendered under — see the `filtered` memo. */
type ErrorRow = AdminErrorItem & { rowKey: string };

/** The server's severity vocabulary, mapped onto the console's pill tones. */
function severityTone(severity: string | undefined): PillTone {
  switch ((severity || '').toLowerCase()) {
    case 'error':
      return 'failed';
    case 'warning':
      return 'pending';
    default:
      return 'idle';
  }
}

function errorTime(item: AdminErrorItem): string | undefined {
  return item.updated_at || item.failed_at || item.blocked_at;
}

export default function ErrorsListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [page, setPage] = useState<AdminErrorsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [search, setSearch] = useState('');
  const [source, setSource] = useState(ALL);

  // True after any successful read, an empty page included (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      setPage(await adminListErrors(500));
      setError('');
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      setError((e as Error)?.message || t('manage:errors.load_failed'));
    }
    if (!background) setLoading(false);
  }, [t]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);
  useKeepCurrent(load);

  const rows = page?.errors ?? [];

  const sources = useMemo(() => {
    const seen = new Set(rows.map((r) => String(r.source || '')).filter(Boolean));
    return Array.from(seen).sort();
  }, [rows]);

  // Keyed here rather than at render: an error row carries no id of its own, and
  // two failed checkpoints for one session with no turn id would otherwise share
  // a key. Position in the server's ordering is the only thing that separates
  // them, so it goes into the key while that ordering is still in hand.
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return rows
      .map((r, i) => ({ ...r, rowKey: `${r.source ?? 'x'}-${r.session_id ?? 'none'}-${r.turn_id ?? ''}-${i}` }))
      .filter((r) => {
        if (source !== ALL && String(r.source || '') !== source) return false;
        if (!needle) return true;
        return [r.session_id, r.sandbox_id, r.turn_id, r.message, r.last_error, r.display_name, r.user_id]
          .some((v) => String(v || '').toLowerCase().includes(needle));
      });
  }, [rows, search, source]);

  const sourceOptions = useMemo<FilterChipOption[]>(
    () => [
      { value: ALL, label: t('manage:errors.source_all') },
      ...sources.map((s) => ({
        value: s,
        label: t(`manage:errors.source_${s}`, { defaultValue: s }),
      })),
    ],
    [sources, t],
  );

  const columns = useMemo<ConsoleColumn<ErrorRow>[]>(
    () => [
      {
        key: 'severity',
        intent: 'status',
        header: t('manage:errors.col_severity'),
        cell: (r) => (
          <StatusPill tone={severityTone(r.severity)} truncate>
            {r.severity
              ? t(`manage:errors.severity_${String(r.severity).toLowerCase()}`, { defaultValue: r.severity })
              : '—'}
          </StatusPill>
        ),
      },
      {
        key: 'message',
        intent: 'name',
        header: t('manage:errors.col_message'),
        // The message is why the row exists, so it takes the widest track and
        // the whole text stays reachable through the title on hover.
        cell: (r) => {
          const text = r.message || r.last_error || '';
          // Marked as the deployment's own words. It is quoted, not written
          // here, so the console's rules about how it writes do not reach it:
          // one error message in the deployment is the single word READY, and
          // the uppercase-chrome check (docs/frontend-design.md §8) reads
          // unmarked text like that as a shouting label.
          return (
            <span data-slot="verbatim">
              <Ellipsis title={text}>{text || '—'}</Ellipsis>
            </span>
          );
        },
      },
      {
        key: 'source',
        intent: 'text',
        header: t('manage:errors.col_source'),
        cell: (r) => (
          <span className="text-muted-foreground">
            {r.source ? t(`manage:errors.source_${r.source}`, { defaultValue: r.source }) : '—'}
          </span>
        ),
      },
      {
        key: 'session',
        intent: 'identifier',
        header: t('common:session'),
        cell: (r) => <Ellipsis title={r.session_id ?? ''}>{r.session_id ? shortId(r.session_id) : '—'}</Ellipsis>,
      },
      {
        key: 'turn',
        intent: 'identifier',
        header: t('manage:errors.col_turn'),
        cell: (r) => <Ellipsis title={r.turn_id ?? ''}>{r.turn_id ? shortId(r.turn_id) : '—'}</Ellipsis>,
      },
      {
        key: 'when',
        intent: 'timestamp',
        header: t('manage:errors.col_when'),
        cell: (r) => {
          const at = errorTime(r);
          return <span className="text-muted-foreground">{at ? formatDateTime(at) : '—'}</span>;
        },
      },
    ],
    [t],
  );

  const counts = page?.counts;

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:errors.title')}
        meta={
          counts
            ? t('manage:errors.meta', {
                total: counts.total ?? rows.length,
                sessions: counts.session ?? 0,
                turns: counts.turn_checkpoint ?? 0,
              })
            : undefined
        }
        description={t('manage:errors.description')}
        actions={
          <Button
            variant="outline"
            size="icon"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={loading}
            aria-label={t('common:refresh')}
          >
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        <ConsoleToolbar>
          <ConsoleSearch value={search} onChange={setSearch} total={rows.length} placeholder={t('manage:errors.search_placeholder')} />
          {sources.length > 1 ? <FilterChips value={source} onChange={setSource} options={sourceOptions} /> : null}
        </ConsoleToolbar>

        {error ? (
          <ConsoleErrorState detail={error} onRetry={() => setReloadKey((k) => k + 1)} />
        ) : loading && !page ? (
          <ConsoleTableSkeleton columns={columns} rows={6} />
        ) : (
          <ConsoleTable
            columns={columns}
            rows={filtered}
            rowKey={(r) => r.rowKey}
            // Only rows that name a session can hand off; a checkpoint error
            // without one has nowhere to go, so its row stays inert rather than
            // navigating to a session route with no id in it.
            onRowClick={(r) =>
              r.session_id ? navigate(`/manage/sessions/${encodeURIComponent(r.session_id)}`) : undefined
            }
            empty={
              <ConsoleEmptyState
                title={rows.length ? t('manage:errors.none_match') : t('manage:errors.none')}
                hint={rows.length ? t('manage:errors.none_match_hint') : t('manage:errors.none_hint')}
              />
            }
          />
        )}
      </div>
    </PageShell>
  );
}
