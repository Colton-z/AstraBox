/**
 * Delete the sessions a spec created unless the result carries failure evidence.
 *
 * Failed, timed-out, and interrupted tests keep the session row, journal, and
 * sandbox Pod for diagnosis and report their identifiers in the test tail.
 * Other results delete their tracked sessions.
 *
 * Playwright records the final outcome after the test body unwinds, so the
 * keep-or-delete decision belongs in `afterEach`. Other state-changing teardown
 * must use `onPassOnly`; interrupting a turn, evicting a runtime, or deleting
 * the Agent or Assistant would otherwise empty the retained evidence.
 */
import { test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from './astraApi';

/** Statuses that mean "keep the scene". A timeout is a failure with evidence. */
const KEEP = new Set(['failed', 'timedOut', 'interrupted']);

/**
 * Register per-test session cleanup and return the list to push ids onto.
 *
 * Call once at the top of a spec file; push every session id the test creates.
 * Failed, timed-out, and interrupted results keep the sessions and name them in
 * the report tail. Other results delete them.
 *
 * ```ts
 * const sessions = trackSessions();
 * // ...
 * sessions.push(sessionId);
 * ```
 */
export function trackSessions(): string[] {
  const sessions: string[] = [];

  test.afterEach(async ({ request }, testInfo) => {
    const ids = sessions.splice(0, sessions.length).filter(Boolean);
    if (ids.length === 0) return;

    if (!KEEP.has(String(testInfo.status || ''))) {
      const api = new AstraApi(request);
      for (const id of ids) {
        await api.deleteSession(id).catch(() => {});
      }
      return;
    }

    await reportKeptSessions(request, ids);
  });

  return sessions;
}

/**
 * Run *fn* after a test result that carries no failure evidence.
 *
 * The general form of the session tracker's rule: state-changing teardown skips
 * failed, timed-out, and interrupted results. Deleting the Agent or Assistant,
 * interrupting a stuck turn, or evicting a runtime would empty the retained
 * evidence.
 *
 * ```ts
 * onPassOnly(async ({ request }) => {
 *   await new AstraApi(request).deleteAgent(agentId);
 * });
 * ```
 */
export function onPassOnly(
  fn: (args: { request: APIRequestContext }) => Promise<void>,
): void {
  test.afterEach(async ({ request }, testInfo) => {
    if (KEEP.has(String(testInfo.status || ''))) return;
    await fn({ request }).catch(() => {});
  });
}

/**
 * Print the kept sessions the way the Python suite prints them.
 *
 * The sandbox id is resolved here rather than left to the reader: a session id
 * alone does not identify the Pod, and the session still answers now — after
 * the run it may not. A lookup that fails is reported as `<unresolved>` rather
 * than swallowed, because "we could not ask" and "there is no sandbox" send an
 * operator to different places.
 */
async function reportKeptSessions(request: APIRequestContext, ids: string[]): Promise<void> {
  const api = new AstraApi(request);
  const lines = [`KEPT for diagnosis (${ids.length} session(s)) — these were NOT deleted:`];
  for (const id of ids) {
    let sandboxId = '';
    try {
      const detail = await api.getSession(id);
      sandboxId = String((detail.sandbox_id as string | null | undefined) || '');
    } catch {
      sandboxId = '';
    }
    lines.push(`  session=${id} sandbox=${sandboxId || '<unresolved>'}`);
  }
  // eslint-disable-next-line no-console -- the report tail is where an operator looks
  console.log(lines.join('\n'));
}
