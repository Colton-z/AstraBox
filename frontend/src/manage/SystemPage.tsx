import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell, Ellipsis } from '@/components/shell';
import { adminProcessHealth, adminSystemOverview } from '@/api';
import { stateLabel } from '@/utils/format';
import type { AdminProcessHealth, AdminProcessRuntimeRow, AdminSystemOverview } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  ConsoleEmptyState,
  StatusPill,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import { keepsLastRead, type ReloadContext } from '@/hooks/useKeepCurrent';
import { shortId } from './sandboxConfig';

/**
 * System — the server process answering this request, right now.
 *
 * Every other console page lists stored deployment records. This page reads the
 * live process — its threads, file descriptors, and open agent runtimes.
 *
 * First, it polls. Every reading is stale the moment it lands, and an operator
 * watching a wedged runtime needs it to move. The ten-second interval keeps the
 * thread, task, and file-descriptor readings useful without requiring manual
 * refreshes.
 *
 * Second, it is one replica's answer. `machine_id` is in the header for exactly
 * that reason: behind more than one replica, a refresh can land somewhere else,
 * and a thread count that jumps is two machines rather than a leak.
 *
 * MCP connection totals are omitted because the current HTTP forwarder creates
 * request-scoped clients and does not maintain a persistent connection count.
 */

const POLL_MS = 10_000;

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: 'ready' | 'busy' | 'danger';
}) {
  const toneClass =
    tone === 'danger' ? 'text-crimson-fg' : tone === 'busy' ? 'text-citrine-fg' : tone === 'ready' ? 'text-mint-fg' : '';
  return (
    <div className="rounded-lg border bg-card px-4 py-3">
      <div className="console-label">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${toneClass}`}>{value}</div>
      {sub ? <div className="mt-0.5 text-11 text-muted-foreground">{sub}</div> : null}
    </div>
  );
}

/** A titled panel for a long list — thread names, pending tasks — capped in
 *  height and scrolled rather than growing the page. */
function ListPanel({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border bg-card">
      <div className="border-b px-4 py-3">
        {/* This title introduces a page section, so it uses the shared section
            heading size and the semantic heading role required by
            docs/frontend-design.md §§6 and 9. */}
        <h2 className="t-h2-tight text-15">{title}</h2>
        {subtitle ? <div className="text-11 text-muted-foreground">{subtitle}</div> : null}
      </div>
      <div tabIndex={0} className="console-scroll max-h-80 overflow-auto px-4 py-2">{children}</div>
    </div>
  );
}

export default function SystemPage() {
  const { t } = useTranslation();
  const [overview, setOverview] = useState<AdminSystemOverview | null>(null);
  const [health, setHealth] = useState<AdminProcessHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const inFlight = useRef<Promise<void> | null>(null);
  // True after any successful combined read (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback((context?: ReloadContext) => {
    if (inFlight.current) return inFlight.current;
    // Existing readings stay visible while a poll is in flight, but `loading`
    // still disables Refresh so a manual read cannot overlap the scheduled one.
    const request = (async () => {
      setLoading(true);
      // Both or neither: a page showing fresh health beside a stale overview
      // invites comparing two moments as if they were one.
      const [o, h] = await Promise.allSettled([adminSystemOverview(), adminProcessHealth()]);
      if (o.status === 'fulfilled' && h.status === 'fulfilled') {
        setOverview(o.value);
        setHealth(h.value);
        setError('');
        loaded.current = true;
      } else {
        // Every failed half must be a lost network for the readings to stay.
        // Otherwise the first half that is not one is reported, so an HTTP
        // refusal is what the page shows even when the other half lost the
        // network in the same read.
        const reported = [o, h].find(
          (result): result is PromiseRejectedResult =>
            result.status === 'rejected'
            && !keepsLastRead(result.reason, context, loaded.current),
        );
        if (reported) {
          setError((reported.reason as Error)?.message || t('manage:system.load_failed'));
        }
      }
      setLoading(false);
    })();
    inFlight.current = request;
    void request.finally(() => {
      if (inFlight.current === request) inFlight.current = null;
    });
    return request;
  }, [t]);

  // Start the next interval only after both reads settle. A fixed interval can
  // launch a second overview while the first is still using the persistence
  // pool; under load it also leaves no quiet time between adjacent reads.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    // The first read of each cycle is the mount's or the operator's (Refresh
    // and Retry restart the cycle through `reloadKey`); every scheduled read
    // after it is background.
    const poll = async (context?: ReloadContext) => {
      await load(context);
      if (!cancelled) {
        timer = window.setTimeout(() => void poll({ background: true }), POLL_MS);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [load, reloadKey]);

  const runtimes = health?.runtimes?.items ?? [];
  const fds = health?.file_descriptors;
  const severity = health?.severity ?? '';

  const columns = useMemo<ConsoleColumn<AdminProcessRuntimeRow>[]>(
    () => [
      {
        key: 'session',
        intent: 'identifier',
        header: t('manage:system.col_runtime_session'),
        cell: (r) => <Ellipsis title={r.session_id}>{shortId(r.session_id)}</Ellipsis>,
      },
      {
        key: 'sandbox',
        intent: 'identifier',
        header: t('common:sandbox'),
        cell: (r) => <Ellipsis title={r.sandbox_id ?? ''}>{r.sandbox_id ? shortId(r.sandbox_id) : '—'}</Ellipsis>,
      },
      {
        key: 'turn',
        intent: 'status',
        header: t('manage:system.col_current_turn'),
        cell: (r) =>
          r.current_task_done === false ? (
            <StatusPill tone="running" live>
              {t('manage:system.turn_running')}
            </StatusPill>
          ) : r.current_task_done === true ? (
            <StatusPill tone="idle">{t('manage:system.turn_idle')}</StatusPill>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
      {
        key: 'watchdog',
        intent: 'compact',
        header: t('manage:system.col_watchdog'),
        cell: (r) => (
          <span className={r.watchdog_done === true ? 'text-muted-foreground' : ''}>
            {r.watchdog_done === false
              ? t('manage:system.watchdog_running')
              : r.watchdog_done === true
                ? t('manage:system.watchdog_stopped')
                : '—'}
          </span>
        ),
      },
      {
        key: 'lock',
        intent: 'compact',
        header: t('manage:system.col_lock'),
        // A held lock and a set disconnect are the two readings that say a
        // runtime is stuck rather than busy, so they are called out rather than
        // printed as yes/no among the rest.
        cell: (r) =>
          r.lock_locked === true ? (
            <span className="text-citrine-fg">{t('manage:system.lock_held')}</span>
          ) : (
            <span className="text-muted-foreground">{t('manage:system.lock_free')}</span>
          ),
      },
      {
        key: 'disconnect',
        intent: 'compact',
        header: t('manage:system.col_disconnect'),
        cell: (r) =>
          r.disconnect_set === true ? (
            <span className="text-crimson-fg">{t('common:yes')}</span>
          ) : (
            <span className="text-muted-foreground">{t('common:no')}</span>
          ),
      },
      {
        key: 'loop',
        intent: 'compact',
        header: t('manage:system.col_owner_loop'),
        cell: (r) => (
          <span className={r.owner_loop_running === false ? 'text-crimson-fg' : 'text-muted-foreground'}>
            {r.owner_loop_running === true
              ? t('manage:system.loop_running')
              : r.owner_loop_running === false
                ? t('manage:system.loop_stopped')
                : '—'}
          </span>
        ),
      },
    ],
    [t],
  );

  const stateCounts = Object.entries(overview?.session_state_counts ?? {});

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:system.title')}
        meta={
          health?.machine_id
            ? t('manage:system.meta', { machine: health.machine_id, pid: health.pid ?? '—' })
            : undefined
        }
        description={t('manage:system.description')}
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

      {/* `flex-1` so the failure card fills the page it stands in for. */}
      <div className="flex flex-1 flex-col gap-4">
        {error ? (
          <ConsoleErrorState detail={error} onRetry={() => setReloadKey((k) => k + 1)} />
        ) : loading && !health ? (
          <ConsoleTableSkeleton columns={columns} rows={4} />
        ) : (
          <div className="space-y-6">
            {/* Columns follow the space rather than a breakpoint ladder — the same
                reason the agent grid does. */}
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]">
              <Stat
                label={t('manage:system.stat_health')}
                value={severity ? t(`manage:system.severity_${severity}`, { defaultValue: severity }) : '—'}
                tone={severity === 'error' ? 'danger' : severity === 'warning' ? 'busy' : 'ready'}
              />
              <Stat label={t('manage:system.stat_env')} value={overview?.server_env || '—'} />
              <Stat
                label={t('manage:system.stat_runtimes')}
                value={health?.runtimes?.count ?? '—'}
                sub={t('manage:system.stat_runtimes_sub', {
                  turns: health?.runtimes?.current_task_count ?? 0,
                  locked: health?.runtimes?.locked_count ?? 0,
                })}
              />
              <Stat label={t('manage:system.stat_threads')} value={health?.threads?.count ?? '—'} sub={t('manage:system.stat_threads_sub', { count: health?.threads?.non_daemon_count ?? 0 })} />
              <Stat
                label={t('manage:system.stat_fds')}
                value={`${fds?.count ?? '—'} / ${fds?.soft_limit ?? '—'}`}
                sub={fds?.usage_ratio != null ? `${Math.round(fds.usage_ratio * 100)}%` : undefined}
                tone={fds?.usage_ratio != null && fds.usage_ratio > 0.8 ? 'danger' : undefined}
              />
              <Stat label={t('manage:system.stat_tasks')} value={health?.asyncio?.pending_task_count ?? '—'} sub={t('manage:system.stat_tasks_sub', { count: health?.asyncio?.task_count ?? 0 })} />
              <Stat
                label={t('manage:system.stat_memory')}
                value={health?.memory?.rss_mb != null ? `${health.memory.rss_mb} MB` : '—'}
                sub={health?.memory?.max_rss_mb != null ? t('manage:system.stat_memory_sub', { peak: health.memory.max_rss_mb }) : undefined}
              />
              {/* Absent on a platform without getloadavg, so the card is too
                  rather than showing three dashes. */}
              {health?.load_avg && health.load_avg.length === 3 ? (
                <Stat
                  label={t('manage:system.stat_load')}
                  value={health.load_avg.map((v) => v.toFixed(2)).join('  ')}
                  sub={t('manage:system.stat_load_sub')}
                />
              ) : null}
              <Stat label={t('manage:system.stat_sessions')} value={overview?.total_sessions ?? '—'} sub={t('manage:system.stat_sessions_sub')} />
            </div>

            {stateCounts.length > 0 ? (
              <div className="rounded-lg border bg-card px-4 py-3">
                <div className="console-label">{t('manage:system.state_distribution')}</div>
                <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2">
                  {/* The wire spelling (RECOVERY_REQUIRED) is not what a reader is
                      shown (§6): stateLabel owns that translation, the same one
                      the session surface renders. */}
                  {stateCounts.map(([state, count]) => (
                    <div key={state} className="flex items-center gap-2">
                      <span className="text-13 text-muted-foreground">{stateLabel(state)}</span>
                      <span className="text-sm font-medium tabular-nums">{count}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <div>
              {/* Same band as every other section heading in the console (§9). */}
              <h2 className="t-h2-tight mb-2 text-15">{t('manage:system.runtimes_title')}</h2>
              <ConsoleTable
                columns={columns}
                rows={runtimes}
                rowKey={(r) => r.session_id}
                empty={
                  <ConsoleEmptyState
                    title={t('manage:system.no_runtimes')}
                    hint={t('manage:system.no_runtimes_hint')}
                  />
                }
              />
            </div>

            <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(320px,1fr))]">
              <ListPanel
                title={t('manage:system.threads_title')}
                subtitle={t('manage:system.threads_subtitle', { count: health?.threads?.non_daemon_count ?? 0 })}
              >
                {(health?.threads?.items ?? []).map((thread, i) => (
                  <div key={`${thread.ident ?? i}-${thread.name ?? i}`} className="flex items-baseline justify-between gap-3 border-b border-border/40 py-1 last:border-0">
                    <span data-slot="verbatim" className="t-mono min-w-0 text-11">
                      <Ellipsis>{thread.name || '—'}</Ellipsis>
                    </span>
                    <span className="shrink-0 text-10 text-muted-foreground">
                      {thread.daemon ? t('manage:system.thread_daemon') : t('manage:system.thread_non_daemon')}
                    </span>
                  </div>
                ))}
              </ListPanel>

              <ListPanel
                title={t('manage:system.tasks_title')}
                subtitle={t('manage:system.tasks_subtitle', { count: health?.asyncio?.task_count ?? 0 })}
              >
                {(health?.asyncio?.sample_pending_tasks ?? []).map((task, i) => (
                  <div key={`${task.name ?? 'task'}-${i}`} className="min-w-0 border-b border-border/40 py-1 last:border-0">
                    <div className="t-mono text-11">
                      <Ellipsis>{task.name || '—'}</Ellipsis>
                    </div>
                    <div className="text-10 text-muted-foreground">
                      <Ellipsis title={task.coro || ''}>{task.coro || '—'}</Ellipsis>
                    </div>
                  </div>
                ))}
              </ListPanel>
            </div>

            {health?.captured_at ? (
              <p className="text-11 text-muted-foreground">
                {t('manage:system.captured_at', { at: formatDateTime(health.captured_at) })}
              </p>
            ) : null}
          </div>
        )}
      </div>
    </PageShell>
  );
}
