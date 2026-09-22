import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { StickToBottom, useStickToBottomContext } from 'use-stick-to-bottom';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { adminReadLogs } from '@/api';
import type { AdminLogsPage } from '@/types';

import {
  ConsolePageHeader,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleErrorState,
} from './console';

/**
 * Logs — a window onto one log file on the host that answered.
 *
 * Filtering happens on the server, not here: `level`, `keyword` and `lines` go
 * into the request, and the response is already the tail that matched. Doing it
 * in the browser would mean fetching the whole file to hide most of it, and the
 * "last 200 lines" the operator asked for would silently become "the matches
 * within the last 200 lines", which is a different question.
 *
 * A deployment that logs to stderr — the default when `ASTRABOX_LOGGING_PATH` is
 * unset — has no files to read. The server answers with an empty
 * `available_files`, and the page says the host keeps no log files rather than
 * showing an empty log, which would read as "nothing has happened".
 */

const ALL_LEVELS = '__all__';
const LINE_CHOICES = [100, 200, 500, 1000] as const;

/** ERROR and WARNING lines earn colour; everything else stays quiet. */
function lineTone(line: string): string {
  const upper = line.toUpperCase();
  if (upper.includes(' ERROR ') || upper.includes('[ERROR]')) return 'text-crimson-fg';
  if (upper.includes(' WARN') || upper.includes('[WARN')) return 'text-citrine-fg';
  return '';
}

/**
 * Pins the viewport to the newest line whenever a window arrives.
 *
 * `StickToBottom` follows the tail on its own only while it still holds the
 * lock: an operator who scrolled up to read has escaped it, and the window they
 * then ask for with Apply or Refresh is a different tail that must be read from
 * the bottom again. `ignoreEscapes` is what makes that jump hold.
 *
 * Renders nothing; it exists to sit inside `StickToBottom` and read that
 * context, the same shape `SessionConversationView` uses over the transcript.
 */
function LogTailPin({ logWindow }: { logWindow: AdminLogsPage | null }) {
  const { scrollToBottom } = useStickToBottomContext();

  useEffect(() => {
    if (logWindow) void scrollToBottom({ animation: 'instant', ignoreEscapes: true });
  }, [logWindow, scrollToBottom]);

  return null;
}

export default function LogsPage() {
  const { t } = useTranslation();
  const [data, setData] = useState<AdminLogsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lines, setLines] = useState<number>(200);
  const [level, setLevel] = useState(ALL_LEVELS);
  const [keyword, setKeyword] = useState('');
  const [file, setFile] = useState('');

  const load = useCallback(
    async (overrideFile?: string) => {
      setLoading(true);
      try {
        const next = await adminReadLogs({
          lines,
          level: level === ALL_LEVELS ? undefined : level,
          keyword: keyword.trim() || undefined,
          file: (overrideFile ?? file) || undefined,
        });
        setData(next);
        // The server picks a file when none is named; adopt its choice so
        // the selector shows what is actually on screen.
        if (next.current_file && !(overrideFile ?? file)) setFile(next.current_file);
        setError('');
      } catch (e) {
        setError((e as Error)?.message || t('manage:logs.load_failed'));
      }
      setLoading(false);
    },
    [file, keyword, level, lines, t],
  );

  useEffect(() => {
    void load();
    // Deliberately once: the filters are applied on Refresh, not on every
    // keystroke, because each change is a request against a file on disk.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const available = data?.available_files ?? [];
  const hasFiles = available.length > 0 || Boolean(data?.current_file);

  // `SelectValue` prints the selected item's label only when the `Select` root
  // is given the item list; on its own it prints the raw value, and the level
  // filter's trigger would read `__all__`. One array feeds both the root and
  // the options below it so the two spellings cannot drift apart.
  const levelItems = useMemo(
    () => [
      { value: ALL_LEVELS, label: t('manage:logs.level_all') },
      { value: 'ERROR', label: 'ERROR' },
      { value: 'WARNING', label: 'WARNING' },
      { value: 'INFO', label: 'INFO' },
    ],
    [t],
  );
  const lineItems = useMemo(
    () => LINE_CHOICES.map((n) => ({ value: String(n), label: t('manage:logs.lines_option', { count: n }) })),
    [t],
  );

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:logs.title')}
        meta={
          data
            ? t('manage:logs.meta', { machine: data.machine_id || '—', count: data.count ?? data.lines.length })
            : undefined
        }
        description={t('manage:logs.description')}
        actions={
          <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} aria-label={t('common:refresh')}>
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
        }
      />

      {/* `min-h-0 flex-1`: the log tail fills the height PageShell hands the
          page and scrolls inside itself, rather than growing the page to the
          length of the file. */}
      <div className="flex min-h-0 flex-1 flex-col gap-4">
        {/* Nothing to narrow, so nothing to narrow it with. A deployment that logs
            to stderr has no files, so the keyword box, the level and line-count
            selects and Apply would all sit inert above the empty state — the
            failure docs/frontend-design.md §5 names. Gated on `hasFiles` rather
            than on the empty state itself so the gate also holds before the first
            response lands. */}
        {hasFiles && (
          <ConsoleToolbar>
            <ConsoleSearch
              value={keyword}
              onChange={setKeyword}
              // The keyword is a server-side query over the whole log file, not a
              // narrowing of the rows on screen — the collection is unbounded, so
              // the field always renders.
              total={Number.POSITIVE_INFINITY}
              placeholder={t('manage:logs.keyword_placeholder')}
            />
            {/* Every menu here hangs off its trigger's bottom-left corner rather
                than sitting on top of it: `alignItemWithTrigger` is the macOS
                native-select placement, which lines the selected item's text up
                with the trigger's text and leaves the two boxes offset — a menu
                that reads as having missed. */}
            {available.length > 0 ? (
              <Select
                value={file}
                onValueChange={(next) => {
                  // The value is nullable for selects that carry a null option.
                  // None of these do, so a null names no file to read.
                  if (next === null) return;
                  setFile(next);
                  void load(next);
                }}
              >
                <SelectTrigger className="w-64">
                  <SelectValue placeholder={t('manage:logs.file_placeholder')} />
                </SelectTrigger>
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {available.map((f) => (
                    <SelectItem key={f} value={f}>
                      {f}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
            <Select
              value={level}
              items={levelItems}
              onValueChange={(next) => {
                if (next === null) return;
                setLevel(next);
              }}
            >
              <SelectTrigger className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>
                {levelItems.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={String(lines)}
              items={lineItems}
              onValueChange={(next) => {
                if (next === null) return;
                setLines(Number(next));
              }}
            >
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="start" alignItemWithTrigger={false}>
                {lineItems.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="secondary" onClick={() => void load()} disabled={loading}>
              {t('manage:logs.apply')}
            </Button>
          </ConsoleToolbar>
        )}

        {error ? (
          <ConsoleErrorState detail={error} onRetry={() => void load()} />
        ) : !loading && !hasFiles ? (
          <ConsoleEmptyState title={t('manage:logs.no_files')} hint={t('manage:logs.no_files_hint')} />
        ) : (
          // A log reads from the bottom. `instant` on both edges rather than the
          // transcript's spring: the operator asked for this tail, and animating
          // down a thousand lines to reach it is time spent watching the middle
          // of a file nobody asked about.
          <StickToBottom
            className="min-h-0 flex-1 overflow-hidden rounded-lg border bg-card"
            initial="instant"
            resize="instant"
          >
            {/* The scroll element belongs to StickToBottom and only takes a class
                  name, so the keyboard reaches this region by way of its content:
                  focus inside a scroll container is what arrow keys scroll. */}
            <StickToBottom.Content
              scrollClassName="console-scroll overflow-auto"
              className="px-4 py-3"
              tabIndex={0}
            >
              {data?.lines.length ? (
                data.lines.map((line, i) => (
                  <div key={i} className={`t-mono whitespace-pre-wrap break-all text-11 leading-relaxed ${lineTone(line)}`}>
                    {line}
                  </div>
                ))
              ) : (
                <p className="py-10 text-center text-sm text-muted-foreground">
                  {loading ? t('common:loading') : t('manage:logs.no_match')}
                </p>
              )}
            </StickToBottom.Content>
            <LogTailPin logWindow={data} />
          </StickToBottom>
        )}
      </div>
    </PageShell>
  );
}
