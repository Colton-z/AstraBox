import { useEffect, useRef } from 'react';

/**
 * How long a return to the tab is ignored after the last re-read. The same
 * value SWR uses for `focusThrottleInterval`, so a page that keeps its read
 * in local state behaves like one that keeps it in SWR.
 */
export const RETURN_THROTTLE_MS = 5_000;

/**
 * Cadence of the re-read while a page is following a record in motion: fast
 * enough that a run the cron just started, or a workspace that just came up,
 * shows within the time it takes the reader to look for it.
 */
export const FOLLOW_INTERVAL_MS = 2_000;

/**
 * Why a page's reload is running.
 *
 * `background` is true for every read this hook starts on its own — a tab
 * return, a follow tick, the browser coming back online. It is absent for the
 * mount read and for the operator's own Refresh, Retry or save, which is what
 * lets a page tell a failure it must keep quiet about from one it must show.
 * A background read also leaves the page's loading state alone: the skeleton
 * is for a table with nothing to show yet, not for a re-read behind rows that
 * are still the latest answer.
 */
export interface ReloadContext {
  readonly background: boolean;
}

/** A page's own read, called by the hook with the reason it is running. */
export type Reload = (context?: ReloadContext) => Promise<unknown> | void;

/**
 * Whether a failed reload must keep what the page last read instead of
 * reporting `error`.
 *
 * A failed background revalidation does not invalidate the last successful
 * read, including an empty list. Retain it whether fetch rejected or an HTTP
 * error arrived. The existing focus, reconnect and follow mechanisms refresh
 * it later; authorization is still enforced by the API. Initial loads and
 * reads the operator explicitly requested report their failures.
 */
export function keepsLastRead(
  error: unknown,
  context: ReloadContext | undefined,
  loaded: boolean,
): boolean {
  return loaded && context?.background === true && error != null;
}

/**
 * Keep a page's locally held read as current as SWR keeps its own.
 *
 * A console page that loads with `useState` + `useEffect` reads once per
 * mount. A tab left open through a deploy, a colleague's change, or an
 * afternoon of the platform's own sweeps then describes a deployment that is
 * gone, and every action it offers is taken against that picture.
 * This hook re-reads when the document becomes visible or the window regains
 * focus (throttled to `RETURN_THROTTLE_MS`), when the browser reports `online`
 * (unthrottled, the way SWR's `revalidateOnReconnect` is: the network coming
 * back is a new fact, not a repeat of the last return), and, while `follow` is
 * true, every `FOLLOW_INTERVAL_MS` as long as the tab is visible — for a
 * record in a transitional state that the reader is sitting on and waiting
 * for. Every read it starts carries `{ background: true }`, see `keepsLastRead`.
 *
 * A background read is not started while `navigator.onLine` is false: the
 * browser knows the request cannot leave the machine, and a follow poll that
 * fails every two seconds is noise in the console and the network log. The
 * flag suppresses requests but cannot prove connectivity: a browser may
 * report online while its gateway is unavailable. Display policy follows
 * the read's purpose.
 *
 * A re-read never overlaps the previous one: the page's own `load` sets its
 * loading state, and two of them racing would let the older answer land last.
 * Use it on pages whose `load` does not reset a draft the reader may be
 * editing; an edit page seeds its draft by record identity for that reason.
 */
export function useKeepCurrent(
  reload: Reload,
  options: { follow?: boolean } = {},
): void {
  const reloadRef = useRef(reload);
  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  const inFlight = useRef(false);
  const lastRunAt = useRef(0);
  const run = useRef(async () => {
    if (inFlight.current || document.visibilityState !== 'visible') return;
    if (navigator.onLine === false) return;
    inFlight.current = true;
    lastRunAt.current = Date.now();
    try {
      await reloadRef.current({ background: true });
    } finally {
      inFlight.current = false;
    }
  });

  useEffect(() => {
    // The mount read counts as the last run: a focus event delivered while
    // the page is still loading would otherwise read the same thing twice.
    lastRunAt.current = Date.now();
    const onReturn = () => {
      if (Date.now() - lastRunAt.current < RETURN_THROTTLE_MS) return;
      void run.current();
    };
    const onReconnect = () => {
      void run.current();
    };
    window.addEventListener('focus', onReturn);
    document.addEventListener('visibilitychange', onReturn);
    window.addEventListener('online', onReconnect);
    return () => {
      window.removeEventListener('focus', onReturn);
      document.removeEventListener('visibilitychange', onReturn);
      window.removeEventListener('online', onReconnect);
    };
  }, []);

  const follow = options.follow === true;
  useEffect(() => {
    if (!follow) return;
    const timer = window.setInterval(() => {
      void run.current();
    }, FOLLOW_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [follow]);
}
