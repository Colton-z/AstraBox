/** Select API requests for the console audit's bounded settlement heuristic. */
export function blocksConsoleSettlement(url: string, method = 'GET'): boolean {
  const pathname = new URL(url).pathname;
  if (!pathname.startsWith('/api/')) return false;
  // The active-turn stream deliberately remains open while a session page is
  // usable. Its DOM readiness is asserted separately by the session view.
  if (pathname.endsWith('/ai-stream')) return false;
  // File listing depends on sandbox connectivity. The files panel has its own
  // loading and error states, so its request does not block the console audit.
  if (/^\/api\/v1\/sessions\/[^/]+\/files\/list$/.test(pathname)) return false;
  // SystemPage schedules another read ten seconds after its reads settle.
  // Background refreshes must not hold the audit open; gotoSettled checks the
  // page's loading state separately after waiting for other API requests.
  if (
    pathname === '/api/v1/admin/system/overview'
    || pathname === '/api/v1/admin/process/health'
  ) {
    return false;
  }
  // Session-list refreshes are excluded from the console-wide quiet window.
  if (pathname === '/api/v1/admin/sessions/all') return false;
  if (method.toUpperCase() === 'GET' && pathname === '/api/v1/sessions') return false;
  // Session detail is polled while the view is open. Navigating to another
  // session cancels an in-flight poll, and Playwright can retain that canceled
  // Request without a terminal event. The page's loading state remains the
  // authority for the initial detail read.
  return !(
    method.toUpperCase() === 'GET'
    && /^\/api\/v1\/sessions\/[^/]+$/.test(pathname)
  );
}
