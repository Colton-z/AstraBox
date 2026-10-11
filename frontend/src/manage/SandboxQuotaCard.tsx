import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';

import { ErrorNote } from '@/components/shell';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import type { AdminSandboxQuota, AdminSandboxQuotaBackend } from '@/types';

import { ConsoleCard, ConsoleFact } from './console';
import { formatDateTime } from './agentConfig';
import { sandboxStateIsEnding } from './sandboxConfig';

const GIB = 2 ** 30;

/**
 * The session id a pool member carries until a conversation claims it
 * (`_client_pool_session_id` in `astrabox/providers/open_sandbox/agent_pool.py`).
 */
const POOL_MEMBER_SESSION_PREFIX = 'client-pool-';

/**
 * Where a ceiling came from, for the source strings the quota writes itself
 * (`configure_sandbox_quotas` in
 * `astrabox/core/service/orchestrator/runtime/sandbox_quota.py`). Any other
 * source, such as the account of a ceiling derived from host memory, is quoted
 * as given.
 */
const SOURCE_LABEL = new Map<string, string>([
  ['configured', 'manage:sandboxes.quota_source_configured'],
  ['unlimited', 'manage:sandboxes.quota_source_unlimited'],
  ['unlimited (unset)', 'manage:sandboxes.quota_source_unset'],
]);

interface Figure {
  label: string;
  used: number;
  /** `null` is no ceiling. */
  limit: number | null;
  format: (value: number) => string;
  source: string | undefined;
}

function formatMemory(bytes: number): string {
  return `${(bytes / GIB).toFixed(1)} Gi`;
}

/** Cores, without the float residue a sum of fractional charges carries. */
function formatCpu(cores: number): string {
  return String(Number(cores.toFixed(2)));
}

function figures(entry: AdminSandboxQuotaBackend): Figure[] {
  const { quota, usage } = entry;
  return [
    {
      label: 'manage:sandboxes.quota_sandboxes',
      used: usage.boxes,
      limit: quota.boxes,
      format: String,
      source: quota.sources.boxes,
    },
    {
      label: 'manage:sandboxes.quota_memory',
      used: usage.memory_bytes,
      limit: quota.memory_bytes,
      format: formatMemory,
      source: quota.sources.memory,
    },
    {
      label: 'manage:sandboxes.quota_cpu',
      used: usage.cpu,
      limit: quota.cpu,
      format: formatCpu,
      source: quota.sources.cpu,
    },
    {
      label: 'manage:sandboxes.quota_concurrent_creates',
      used: usage.creating,
      limit: quota.concurrent_creates,
      format: String,
      source: quota.sources.concurrent_creates,
    },
  ];
}

/**
 * Whether a figure is at its ceiling. The quota refuses a sandbox that would
 * take usage past a ceiling, and every sandbox is charged something, so usage
 * at the ceiling already refuses the next one. Usage that reads the same as the
 * ceiling at the precision shown counts as at it, so the card never prints two
 * equal numbers without the warning; that also covers a sum of fractional CPU
 * charges landing a rounding error short of the ceiling it has reached.
 */
function atCeiling({ used, limit, format }: Figure): boolean {
  return limit !== null && (used >= limit || format(used) === format(limit));
}

/**
 * The backend's sandboxes by what they are doing. The counts need not add up to
 * the sandboxes held against the quota: a reservation whose create has answered
 * but that no inventory shows yet is held without being any of them, and so is
 * a sandbox that names no session. A sandbox being removed counts as cleaning up
 * whatever session it names.
 */
function sandboxCounts(entry: AdminSandboxQuotaBackend) {
  let inUse = 0;
  let prepared = 0;
  let cleaningUp = 0;
  for (const box of entry.boxes) {
    if (sandboxStateIsEnding(box.state)) {
      cleaningUp += 1;
      continue;
    }
    if (!box.session_id) continue;
    if (box.session_id.startsWith(POOL_MEMBER_SESSION_PREFIX)) prepared += 1;
    else inUse += 1;
  }
  const starting = entry.reservations.filter((r) => r.state === 'creating').length;
  return { inUse, prepared, starting, cleaningUp };
}

function sourceText(source: string | undefined, t: TFunction): ReactNode {
  if (!source) return undefined;
  const key = SOURCE_LABEL.get(source);
  return key ? t(key) : <span data-slot="verbatim">{source}</span>;
}

function FigureValue({ figure }: { figure: Figure }) {
  const { t } = useTranslation();
  const full = atCeiling(figure);
  const used = figure.format(figure.used);
  return (
    // The glyph goes with the colour so a full ceiling still reads in a
    // greyscale screenshot (docs/frontend-design.md §7), and the hidden text
    // says what both mean to a screen reader.
    <span className={cn('inline-flex items-center gap-1.5 tabular-nums', full && 'text-crimson-fg')}>
      {full && <AlertTriangle aria-hidden className="size-3.5 shrink-0" />}
      {figure.limit === null
        ? t('manage:sandboxes.quota_used_no_limit', { used })
        : t('manage:sandboxes.quota_used_of_limit', { used, limit: figure.format(figure.limit) })}
      {full && <span className="sr-only">{' '}{t('manage:sandboxes.quota_full')}</span>}
    </span>
  );
}

/**
 * One count in the same grammar as the system page's state distribution: the
 * word, then the number. The space between them takes no room in the flex row;
 * it keeps the pair's text one phrase ("In use 2", not "In use2") wherever it
 * is read as text.
 */
function Count({ label, value }: { label: string; value: number | string }) {
  return (
    <span className="flex items-baseline gap-2">
      <span className="text-13 text-muted-foreground">{label}</span>{' '}
      <span className="text-sm font-medium tabular-nums">{value}</span>
    </span>
  );
}

function BackendQuota({ entry, named }: { entry: AdminSandboxQuotaBackend; named: boolean }) {
  const { t } = useTranslation();
  const counts = sandboxCounts(entry);
  // Until the quota has taken the backend's inventory, the figures hold only
  // the reservations, and a count read off the inventory would be a zero that
  // nobody counted.
  const counted = entry.inventory_at !== null;
  return (
    <div className="flex flex-col gap-4">
      {named && <p className="console-val t-label">{entry.backend}</p>}
      {/* Columns follow the space rather than a breakpoint ladder, as the
          system page's readings do. */}
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(180px,1fr))]">
        {figures(entry).map((figure) => (
          <ConsoleFact
            key={figure.label}
            label={t(figure.label)}
            value={<FigureValue figure={figure} />}
            detail={sourceText(figure.source, t)}
          />
        ))}
      </div>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
        <Count label={t('manage:sandboxes.quota_in_use')} value={counted ? counts.inUse : '—'} />
        <Count label={t('manage:sandboxes.quota_prepared')} value={counted ? counts.prepared : '—'} />
        <Count label={t('manage:sandboxes.quota_starting')} value={counts.starting} />
        <Count label={t('manage:sandboxes.quota_cleaning_up')} value={counted ? counts.cleaningUp : '—'} />
      </div>
      <p className="text-11 text-muted-foreground">
        {counted
          ? t('manage:sandboxes.quota_as_of', { at: formatDateTime(entry.inventory_at) })
          : t('manage:sandboxes.quota_not_counted')}
      </p>
    </div>
  );
}

/**
 * The sandbox quota above the inventory: each ceiling with what is held
 * against it and where the ceiling came from, then the sandboxes by what they
 * are doing — enough for an operator to see why a conversation was refused.
 *
 * The page owns the read and passes its outcome in. A failed read is shown in
 * place of the figures, never as zeros.
 */
export function SandboxQuotaCard({
  quota,
  error,
}: {
  quota: AdminSandboxQuota | null;
  error: string;
}) {
  const { t } = useTranslation();
  // One backend is the deployment's only one, and the inventory below names it
  // on every row; with more, each section says whose figures it holds.
  const named = (quota?.backends.length ?? 0) > 1;
  return (
    <ConsoleCard title={t('manage:sandboxes.quota_title')} intro={t('manage:sandboxes.quota_intro')}>
      {error ? (
        <ErrorNote title={t('manage:sandboxes.quota_error_title')}>{error}</ErrorNote>
      ) : quota ? (
        quota.backends.map((entry) => (
          <BackendQuota key={entry.backend} entry={entry} named={named} />
        ))
      ) : (
        <Skeleton className="h-16" />
      )}
    </ConsoleCard>
  );
}
