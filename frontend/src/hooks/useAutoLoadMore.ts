import { useEffect, useRef } from 'react';

/**
 * Read the next page when the returned sentinel scrolls into view.
 *
 * Attach the ref to an element at the end of the list. While `enabled` is true
 * the observer asks for the next page each time the sentinel enters the
 * viewport. Callers pass `enabled: false` while a page is in flight and after a
 * page has failed, so a failure waits for the reader's retry instead of
 * repeating on every scroll.
 *
 * The observer is rebuilt whenever `enabled` turns true again. A fresh
 * observer reports the sentinel's current state, so a page that leaves the
 * list shorter than its scroller reads the next one without waiting for a
 * scroll that cannot happen.
 */
export function useAutoLoadMore<T extends Element>(enabled: boolean, onLoadMore: () => void) {
  const ref = useRef<T | null>(null);
  const load = useRef(onLoadMore);
  load.current = onLoadMore;

  useEffect(() => {
    const node = ref.current;
    if (!enabled || !node) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) load.current();
    }, { rootMargin: '200px 0px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [enabled]);

  return ref;
}
