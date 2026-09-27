import { useCallback, useEffect, useRef, useState } from 'react';

import { keepsLastRead, type ReloadContext } from '@/hooks/useKeepCurrent';

/** The paging fields every cursor-paged list response carries. */
export interface CursorPage {
  has_more: boolean;
  next_cursor?: string | null;
}

/** How long typing must pause before a search goes to the server. */
export const SEARCH_SETTLE_MS = 250;

interface Loaded<Row, Counts> {
  rows: Row[];
  counts: Counts | null;
  hasMore: boolean;
  cursor: string | null;
  pages: number;
}

const NOTHING_LOADED = { rows: [], counts: null, hasMore: false, cursor: null, pages: 0 };

/**
 * A console list the server pages, narrows and counts.
 *
 * `fetchPage(cursor)` reads one page for the current narrowing; when the
 * function changes (a new search or status), the list starts again from its
 * first page. `reload` re-reads as many pages as are on screen, so a return to
 * the tab or a Refresh keeps what the reader has loaded instead of folding it
 * back to one page. `loadMore` appends the next page. Only the latest read
 * lands: an answer for a narrowing the reader has already left is dropped.
 *
 * `loading` is true for the first read and the reader's own Refresh (the
 * skeleton is for a table with nothing to show yet); a narrowing change keeps
 * the rows on screen until the new ones arrive.
 */
export function usePagedList<Page extends CursorPage, Row, Counts>({
  fetchPage,
  rowsOf,
  countsOf,
  onCounts,
}: {
  fetchPage: (cursor: string | null) => Promise<Page>;
  rowsOf: (page: Page) => Row[];
  countsOf: (page: Page) => Counts | null;
  /** Called with the counts of every successful first page, and with `null` when a shown read fails. */
  onCounts?: (counts: Counts | null) => void;
}) {
  const [state, setState] = useState<Loaded<Row, Counts>>(NOTHING_LOADED);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [moreError, setMoreError] = useState('');
  // True after any successful read, an empty list included (see keepsLastRead).
  const loaded = useRef(false);
  const pages = useRef(1);
  const latest = useRef(0);
  const cursor = useRef<string | null>(null);

  const read = useCallback(
    async (count: number): Promise<Loaded<Row, Counts>> => {
      const next: Loaded<Row, Counts> = { rows: [], counts: null, hasMore: false, cursor: null, pages: 0 };
      do {
        const page = await fetchPage(next.cursor);
        if (next.pages === 0) next.counts = countsOf(page);
        next.rows.push(...rowsOf(page));
        next.hasMore = page.has_more;
        next.cursor = page.next_cursor ?? null;
        next.pages += 1;
      } while (next.hasMore && next.cursor && next.pages < count);
      return next;
    },
    [countsOf, fetchPage, rowsOf],
  );

  const apply = useCallback(
    (next: Loaded<Row, Counts>) => {
      pages.current = Math.max(1, next.pages);
      cursor.current = next.cursor;
      setState(next);
      setError('');
      setMoreError('');
      loaded.current = true;
      onCounts?.(next.counts);
    },
    [onCounts],
  );

  const reload = useCallback(
    async (context?: ReloadContext) => {
      const token = ++latest.current;
      const background = context?.background === true;
      if (!background) setLoading(true);
      try {
        const next = await read(pages.current);
        if (token === latest.current) apply(next);
      } catch (e) {
        if (token !== latest.current || keepsLastRead(e, context, loaded.current)) return;
        onCounts?.(null);
        setError((e as Error).message);
      } finally {
        if (!background && token === latest.current) setLoading(false);
      }
    },
    [apply, onCounts, read],
  );

  // A new narrowing starts from its first page, without the skeleton.
  const narrowed = useRef(false);
  useEffect(() => {
    pages.current = 1;
    if (!narrowed.current) {
      narrowed.current = true;
      void reload();
      return;
    }
    const token = ++latest.current;
    void read(1).then(
      (next) => {
        if (token === latest.current) apply(next);
      },
      (e: unknown) => {
        if (token !== latest.current) return;
        onCounts?.(null);
        setError((e as Error).message);
      },
    );
  }, [apply, onCounts, read, reload]);

  const loadMore = useCallback(async () => {
    const from = cursor.current;
    if (!from) return;
    const token = latest.current;
    setLoadingMore(true);
    try {
      const page = await fetchPage(from);
      if (token !== latest.current) return;
      pages.current += 1;
      cursor.current = page.next_cursor ?? null;
      setState((current) => ({
        ...current,
        rows: [...current.rows, ...rowsOf(page)],
        hasMore: page.has_more,
        cursor: cursor.current,
        pages: pages.current,
      }));
      setMoreError('');
    } catch (e) {
      if (token === latest.current) setMoreError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, rowsOf]);

  return {
    rows: state.rows,
    counts: state.counts,
    hasMore: state.hasMore,
    loading,
    loadingMore,
    error,
    moreError,
    reload,
    loadMore,
  };
}

/** `value`, once it has stopped changing for {@link SEARCH_SETTLE_MS}. */
export function useSettled(value: string, delay = SEARCH_SETTLE_MS): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return settled;
}
