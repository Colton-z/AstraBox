/**
 * Come back to a tab the way a returning operator does.
 *
 * A console page holds its read in local state and re-issues it from
 * `useKeepCurrent` (frontend/src/hooks/useKeepCurrent.ts), which listens for
 * `visibilitychange` on the document and `focus` on the window. Those two
 * events, in that order, are the whole stimulus: the pair the hook binds, the
 * pair the release guard binds (`frontendRelease.ts:117-118`), and the pair SWR's
 * `initFocus` binds. Headless Chromium does not reliably background a
 * Playwright page, so a synthetic dispatch is the honest stimulus rather than a
 * shortcut; the upgrade path, if a real tab switch is ever wanted, is
 * `context.newPage()` + `bringToFront()` gated on `document.visibilityState`
 * actually reaching 'hidden'.
 *
 * This lives in fixtures rather than in one spec because the throttle below is
 * a copy of a product constant, and a second private copy in a second spec is
 * how a number gets six of them.
 */
import type { Page } from '@playwright/test';

/**
 * `RETURN_THROTTLE_MS` from frontend/src/hooks/useKeepCurrent.ts — the hook
 * drops a return that arrives within this long of the page's own last read, so
 * a return sent earlier proves nothing about what a returning operator sees.
 *
 * It is duplicated here because a spec cannot import from the frontend bundle.
 * When the product's value changes, this one is re-derived from that file; the
 * two are not independent choices.
 */
export const RETURN_THROTTLE_MS = 5_000;

/**
 * Margin for the skew between the hook stamping its clock and the request it
 * caused reaching the test process.
 */
export const THROTTLE_MARGIN_MS = 1_000;

/**
 * Return to *page*, no sooner than its re-read throttle allows.
 *
 * *readAt* is when the page under test last read its own data — the mount, or
 * the settling of the read the spec last watched. The wait is spent here rather
 * than by the caller so that a return is never dispatched inside the window the
 * product drops it in, which would be indistinguishable from a page that
 * ignores returns entirely.
 */
export async function returnToTab(page: Page, readAt: number): Promise<void> {
  const due = readAt + RETURN_THROTTLE_MS + THROTTLE_MARGIN_MS - Date.now();
  if (due > 0) await page.waitForTimeout(due);
  await page.evaluate(() => {
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });
}
