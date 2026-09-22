/**
 * E2E: the conversation rail follows work nobody did in this tab.
 *
 * The journey is the one the product is for — a reader leaves the console on
 * `/agents` while a conversation is created, run and archived somewhere else (a
 * trigger, a channel, the CLI, the API, another device) — and the property is
 * that the rail in the sidebar, which is the product's only index of what is
 * happening, agrees with the product without the reader touching the window.
 *
 * WHAT CARRIES THE FRESHNESS, AND WHY A UNIT TEST CANNOT SEE IT. The rail is
 * `useSWRInfinite(['sessions', cursor], …, { refreshInterval:
 * RAIL_REFRESH_INTERVAL_MS })` — ten seconds, App.tsx:103-122 — and SWR
 * suspends that cadence while the tab is hidden. It is the only producer this
 * route has: `refreshOverview` (App.tsx:154) reaches the rail from the in-tab
 * conversation create (App.tsx:158) and from the SessionPage route
 * (App.tsx:401), this tab opens neither, `App` mounts once under `/*` so a
 * route change never remounts the hook, and there is no EventSource,
 * WebSocket, BroadcastChannel or `storage` listener in `frontend/src`. A jsdom
 * test renders that tree and sees one fetch per key; what this spec measures is
 * the passage of time on a mounted page against a product that moved
 * underneath it.
 *
 * WHY THE WINDOW MUST STAY UNTOUCHED — READ BEFORE EDITING. `revalidateOnFocus`
 * (main.tsx:45) is a second, unrelated cure, and SWR arms it with exactly two
 * listeners (`initFocus` in the swr package: a non-capturing `window` 'focus'
 * and a `document` 'visibilitychange'). A spec that clicked, pressed a key,
 * resized, reloaded or called `bringToFront` during the measurement would hand
 * the page that signal and then report focus revalidation while claiming to
 * measure the rail's own cadence. So from `railWatchFrom` to the end this spec
 * issues no page interaction of any kind; `expect.poll`, `toHaveAttribute` and
 * `page.evaluate` are queries and dispatch nothing. The witness below mirrors
 * SWR's two listeners, and the measurement is thrown out by name if either of
 * them fires. Element focus is recorded and does not void the run: `focus` does
 * not bubble, so a non-capturing window listener — SWR's included — never sees
 * a focus event whose target is not the window. Do not run this spec headed: a
 * window manager giving the tab focus invalidates it.
 *
 * WHAT EACH ASSERTION CAN AND CANNOT CATCH, stated rather than implied:
 *   · ASSERT A (a conversation created elsewhere appears) is the one binary,
 *     permanent fact with no vacuity hole — a row that does not exist cannot be
 *     "already correct".
 *   · ASSERT B (that row's pill settles at READY) catches exactly one half-fix:
 *     a rail pushed once on a creation event and then frozen, whose row keeps
 *     the state it was born in. It does NOT catch a rail frozen at READY,
 *     because the API waits for READY before the row can appear. The rail's
 *     `data-state` really does move — the list rows go through
 *     `derive_ui_state` (session_read.py:995-1043 via
 *     `_render_session_rows`:201-240), which reports PROCESSING for the
 *     duration of a turn — but PROCESSING→READY is not monotonic, so a spec
 *     that waited for PROCESSING would fail a correct product whenever the turn
 *     ended between two refreshes. ASSERT C is the assertion that a frozen rail
 *     cannot survive.
 *   · ASSERT C (a conversation archived elsewhere disappears) is the removal
 *     direction of the same mechanism: archive sets `hidden: true`
 *     (session_repository.py:923) and the rail's own page query filters on it
 *     (session_repository.py:579, :602).
 *   · Deliberately NOT asserted: the Sessions count badge (App.tsx:222 renders
 *     `sessions.length`, a whole-user number other parallel workers change
 *     underneath this test); the generated first-turn title, which depends on
 *     the title model and is owned by
 *     command-metadata.exclusive.spec.ts:157-161 — and note what that spec
 *     proves, because it is easy to mistake for a counter-example: it watches
 *     the same sidebar row from a tab sitting ON the conversation, so
 *     SessionPage is mounted and `refreshOverview` is wired. It exercises a
 *     different producer from the one under test here. Also not asserted: the
 *     relative time (`formatShortTime`, App.tsx:77), which needs more than 60s
 *     of wall clock to move off "just now".
 *
 * WHY THE FETCH COUNT RIDES IN THE EVIDENCE AND NOT IN AN ASSERTION. A red must
 * be able to say "the page never asked", which is why the meter exists. But
 * asserting on it would pin the mechanism: a rail healed by a push over an
 * existing channel is a correct rail that refetches nothing, and it passes here.
 *
 * ENGINE-INDEPENDENT. The out-of-band turn goes through `POST
 * /sessions/{id}/turn-inputs`, which is engine-agnostic, and asks for a one-line
 * reply with no tools, so no engine vocabulary, tool card, permission mode or
 * background path is touched and the reply text is never read. Every oracle is a
 * DOM data-attribute rather than a localized label, so no locale pin is needed.
 * `parallel` is honest: nothing is held to itself, and every oracle is scoped to
 * `[data-session-id]` of this spec's own conversations, so another worker's
 * sessions in the same user's rail can neither satisfy nor break it.
 *
 * ONE SHARED-SUBSTRATE NOTE. Archiving an Agent conversation releases its
 * runtime allocation — for Agent tenancy that is the isolated placement inside
 * the shared box, not the box (commands.py:267-274). A sibling worker's
 * conversation in the same box keeps its own placement; the precedent for a
 * reclaim in this lane is session-list-history.parallel.spec.ts:79.
 */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

declare global {
  interface Window {
    /**
     * Focus-family signals seen by the watched tab, appended by the init
     * script. `revalidates` is true for the two SWR itself listens to.
     */
    __railWatchEvents?: { type: string; at: number; revalidates: boolean }[];
    /** When the untouched window opened. `null` until the spec opens it. */
    __railWatchFrom?: number | null;
  }
}

/**
 * How long the rail may take to agree with the product, in each direction.
 *
 * The rail refreshes every RAIL_REFRESH_INTERVAL_MS (10s, App.tsx:52), so the
 * budget has to clear a whole cadence plus the request it issues, and be too
 * short to sit through a change of cadence unnoticed. Thirty seconds is three
 * of them, and leaves the create, the turn and the archive well inside the
 * lane's 180s wall. A deployment whose rail is deliberately slower sets
 * ASTRABOX_E2E_RAIL_FRESHNESS_MS and re-checks the total rather than this file.
 */
const RAIL_FRESHNESS_MS = parseTimeoutEnv('ASTRABOX_E2E_RAIL_FRESHNESS_MS', 30_000);

// Sessions created here are deleted only when the test passes. A failed,
// timed-out or interrupted result keeps them and their boxes and names them in
// the report tail — see fixtures/sessionCleanup.ts. Archive-then-delete is safe:
// the tracker's deleteSession is `.catch(() => {})`.
const sessions = trackSessions();

test('the conversation rail follows work done elsewhere while the reader stays on /agents', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);

  // ── setup: a row that exists BEFORE the page loads ────────────────────────
  // The fence. Without a conversation already listed, a red at ASSERT A could
  // equally mean "the rail never rendered at all" (the empty hero, App.tsx:354)
  // and the finding would not be readable. It is also what ASSERT D re-checks at
  // the end, so a rail that "converged" by emptying and re-rendering is not read
  // as a rail that followed.
  const agent = await api.defaultAgent();
  const watched = await api.startConversation(String(agent.agent_id));
  sessions.push(watched.session_id);
  await api.waitForSessionReady(watched.session_id);

  const railRow = (sessionId: string) =>
    page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`);

  // The control read, taken through the APIRequestContext rather than the page:
  // the very request the rail would issue if it asked (`page=1`, the first page
  // of `listSessionsPage`, api.ts:1440). It is what separates "the product
  // never published this" from "the open page never asked again", and it does
  // not disturb the meter, which counts only what the tab itself sends. Raw
  // route rather than a new fixture method, the idiom of
  // session-list-history.parallel.spec.ts:88.
  const listedByPlatform = async (): Promise<string[]> => {
    const listPage = await api.data<{ sessions?: { session_id?: string }[] }>(
      'GET',
      '/sessions?page=1&limit=50',
    );
    return (listPage.sessions ?? []).map((session) => String(session.session_id || ''));
  };

  // ── instrumentation, all of it attached before the first navigation ───────
  // A crashed React tree also stops rendering rows, so a red must be able to
  // tell "never learned" from "died", and a listener added later misses the
  // render that mattered.
  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));

  // The meter: first-page session-list reads this TAB issued. Requests, not
  // responses — the diagnosis this spec exists to print is "the page never
  // asked", which is a statement about what left the tab. `page=1` with no
  // cursor is exactly the rail's own SWR key `['sessions', null]`
  // (App.tsx:103-122 → listSessionsPage, api.ts:1440); a `load more`
  // page carries a cursor and is excluded. Reads the AstraApi fixture makes go
  // through the APIRequestContext, not the page, and are deliberately not
  // counted — which is what lets the control assertions below re-ask the
  // platform mid-measurement without disturbing it.
  const listPath = apiPath('/sessions');
  const railFetches: number[] = [];
  const railFailures: string[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET') return;
    const url = new URL(req.url());
    if (url.pathname !== listPath) return;
    if (url.searchParams.get('page') !== '1') return;
    if (url.searchParams.get('cursor')) return;
    railFetches.push(Date.now());
  });
  // Asked-and-refused is a different bug from never-asked, and a red that cannot
  // tell them apart sends the reader to the wrong file. It rides in the polled
  // value and the attached evidence rather than in an assertion of its own: SWR
  // retries a transient Mongo error by design (main.tsx:47-52), so a recovered
  // one is not a defect and must not turn a correct rail red.
  page.on('response', (res) => {
    if (res.request().method() !== 'GET') return;
    if (new URL(res.url()).pathname !== listPath) return;
    if (!res.ok()) railFailures.push(`${res.status()} ${res.url()}`);
  });

  // Main-frame document loads. `performance.timeOrigin` below already proves the
  // document was never replaced; this names the same thing in the other
  // direction, so a red says "it reloaded" rather than only "the origin moved".
  const appUrl = appPath('/agents');
  const documentLoads: string[] = [];
  page.on('request', (req) => {
    if (req.resourceType() !== 'document') return;
    documentLoads.push(req.url());
  });

  // The two listeners SWR registers for `revalidateOnFocus`, mirrored exactly
  // (`initFocus` in the swr package: a non-capturing `window` 'focus' and a
  // `document` 'visibilitychange'), plus a capture-phase reader for element
  // focus — which SWR structurally cannot see, because `focus` does not bubble
  // and a non-capturing window listener therefore only fires for the window
  // itself. Installed at document start, so nothing that reaches SWR reaches it
  // unseen.
  await page.addInitScript(() => {
    window.__railWatchEvents = [];
    window.__railWatchFrom = null;
    const record = (type: string, revalidates: boolean) => {
      window.__railWatchEvents?.push({ type, at: Date.now(), revalidates });
    };
    window.addEventListener('focus', () => record('window-focus', true));
    document.addEventListener('visibilitychange', () => record('visibilitychange', true));
    window.addEventListener(
      'focus',
      (event) => {
        if (event.target === window) return;
        record('element-focus', false);
      },
      true,
    );
  });

  // ── the reader opens the list page, and stops ─────────────────────────────
  await page.goto(appUrl, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('sessions-page')).toBeVisible();
  await expect(
    railRow(watched.session_id),
    'the rail must have rendered its list before anything happens elsewhere, or nothing '
      + 'below is a fence',
  ).toBeVisible();

  // Prove the meter works before trusting a low reading from it. Mounting the
  // shell always reads the first page once, so a zero here means this spec is
  // counting the wrong path — and an unarmed counter reports "never asked"
  // forever. The likely cause is the console's API base having diverged from
  // ASTRABOX_E2E_APP_PREFIX, which no assertion below would notice.
  expect(
    railFetches.length,
    `no GET ${listPath}?page=1 was seen while the shell mounted, so this spec is not `
      + 'measuring anything. Check the app prefix before reading a zero as evidence.',
  ).toBeGreaterThanOrEqual(1);

  // No session view is ever opened. `openSessionView` is not imported on
  // purpose: mounting SessionPage is what hands the rail its only refresh
  // callback (App.tsx:401 `onSessionChanged={refreshOverview}`), so a spec that
  // opened one would be measuring that callback.
  const mountedAt = await page.evaluate(() => performance.timeOrigin);
  const loadedDocuments = documentLoads.length;
  expect(loadedDocuments, 'the app document must have been requested by this tab').toBeGreaterThanOrEqual(1);

  // ── the untouched window opens here ───────────────────────────────────────
  await page.evaluate(() => {
    window.__railWatchFrom = Date.now();
  });

  /**
   * Refuse to believe the measurement if the tab regained focus.
   *
   * `revalidateOnFocus: true` (main.tsx:45) revalidates the rail on a stray
   * window focus or visibilitychange, for a reason that has nothing to do with
   * its own cadence. A spec that let one stand would report green on a rail
   * with no cadence at all, which is the one direction of wrongness that
   * matters here. Element focus is reported in the evidence and does not void
   * the run, because SWR's non-capturing window listener cannot see it.
   */
  const expectNoFocusSignals = async (where: string) => {
    const seen = await page.evaluate(() => {
      const from = window.__railWatchFrom;
      return {
        opened: typeof from === 'number',
        events: (window.__railWatchEvents ?? [])
          .filter((e) => e.at >= (from ?? Infinity))
          .filter((e) => e.revalidates),
      };
    });
    expect(
      seen.opened,
      `${where}: the untouched window's start marker is gone, which means the document was `
        + 'replaced — this measurement is void, not green',
    ).toBe(true);
    expect(
      seen.events,
      `${where}: the rail must refresh on its own cadence, not because the window regained `
        + `focus. These events reached the page during the measured window: `
        + `${JSON.stringify(seen.events)}. SWR revalidates on every one of them `
        + '(main.tsx:45), so whatever the rail did next proves nothing. Re-run headless, '
        + 'with nothing else driving this browser context.',
    ).toEqual([]);
  };

  // ── elsewhere, step 1: a conversation appears ─────────────────────────────
  const startedAt = Date.now();
  const started = await api.startConversation(String(agent.agent_id));
  sessions.push(started.session_id);
  await api.waitForSessionReady(started.session_id);

  // The control, taken from the platform rather than from the page. Without it
  // a red below could be read as "the conversation was never created" or "this
  // user cannot see it" rather than as "the open page never asked again".
  expect(
    await listedByPlatform(),
    'the product must publish the new conversation to this same signed-in user',
  ).toContain(started.session_id);

  // ── ASSERT A: the rail grows ──────────────────────────────────────────────
  // The polled value carries the diagnosis, so the failure diff reads
  // `{listed: false, railFetchesSinceCreate: 0, …}` — the page did not render
  // late, it never asked.
  await expect
    .poll(
      async () => ({
        listed: (await railRow(started.session_id).count()) > 0,
        railFetchesSinceCreate: railFetches.filter((at) => at > startedAt).length,
        railFetchesTotal: railFetches.length,
        railReadsRefused: railFailures.length,
      }),
      {
        timeout: RAIL_FRESHNESS_MS,
        intervals: [500, 1000, 2000],
        message:
          'a conversation created outside this tab must reach the rail without a navigation, '
          + `a reload or a click; ${RAIL_FRESHNESS_MS}ms after it went READY the sidebar still `
          + `does not list ${started.session_id}. \`railFetchesSinceCreate\` counts first-page `
          + `GET ${listPath} issued by this tab since the create: if it is 0 the page never `
          + "re-read the list at all, so the rail's cadence is gone — the 10s refreshInterval "
          + 'on App.tsx:122 is the only producer this route has, and refreshOverview '
          + '(App.tsx:154) is reachable only from the in-tab conversation create and the '
          + 'SessionPage route. This tab is on /agents and touched neither.',
      },
    )
    .toMatchObject({ listed: true });

  await expectNoFocusSignals('ASSERT A');
  expect(
    uncaught,
    `uncaught exception in the watched tab — a crashed tree renders no rows either:\n${uncaught.join('\n')}`,
  ).toEqual([]);
  await expect(
    railRow(started.session_id),
    'the learned conversation must be on screen, not merely in the document',
  ).toBeVisible();

  // ── elsewhere, step 2: the conversation does some work ────────────────────
  // `postTurnInput` admits the turn and returns a receipt without reading its
  // stream, so nothing here waits on a body and no interaction can block it.
  // The reply text is never asserted, so no `insist` and no model-conditional
  // skip is needed.
  const marker = `RAILFRESH_${Date.now()}`;
  const assistantCount = await api.assistantCount(started.session_id);
  await api.postTurnInput(
    started.session_id,
    `Reply with exactly ${marker} and use no tools.`,
  );
  await api.waitForAssistantMessageCount(started.session_id, assistantCount);
  await api.waitForSession(
    started.session_id,
    (session) => session.state === 'READY' && session.last_turn_status === 'COMPLETED',
  );

  // ── ASSERT B: the row is not frozen at the state it was born in ───────────
  // `data-state` is the raw session state the list projection produced
  // (AstraConsole.tsx:122, App.tsx:305), not a label, so this reads the same
  // in every locale. See the header for exactly which half-fix this catches.
  await expect(
    railRow(started.session_id).getByTestId('status-pill'),
    'the rail row must settle with the conversation it names, not keep the state it was '
      + 'first rendered with',
  ).toHaveAttribute('data-state', 'READY', { timeout: RAIL_FRESHNESS_MS });

  // ── elsewhere, step 3: the conversation leaves ────────────────────────────
  const archivedAt = Date.now();
  await api.archiveSession(started.session_id);
  expect(
    await listedByPlatform(),
    'the product must stop publishing the archived conversation to this same signed-in user',
  ).not.toContain(started.session_id);

  // ── ASSERT C: the rail shrinks ────────────────────────────────────────────
  await expect
    .poll(
      async () => ({
        listed: (await railRow(started.session_id).count()) > 0,
        railFetchesSinceArchive: railFetches.filter((at) => at > archivedAt).length,
        railFetchesTotal: railFetches.length,
        railReadsRefused: railFailures.length,
      }),
      {
        timeout: RAIL_FRESHNESS_MS,
        intervals: [500, 1000, 2000],
        message:
          'a conversation archived outside this tab must leave the rail without a reload; '
          + `${RAIL_FRESHNESS_MS}ms after the archive the sidebar still offers a row for `
          + `${started.session_id}, whose record the list query no longer returns `
          + '(archive sets hidden: true, session_repository.py:923, and the page query '
          + 'filters on it at :579 and :602). `railFetchesSinceArchive` is the same single '
          + 'cause as the create half: a 0 means the page never asked again.',
      },
    )
    .toMatchObject({ listed: false });

  // ── ASSERT D: the verdict must name its own run ───────────────────────────
  // Everything here exists so a green cannot have been bought some other way.
  await expectNoFocusSignals('ASSERT D');
  expect(
    await page.evaluate(() => performance.timeOrigin),
    'no document reload may stand in for the rail noticing — the frontend release guard '
      + 'reloads a resident tab when the served bundle changes (frontendRelease.ts:100), '
      + 'and a reload refetches everything for free',
  ).toBe(mountedAt);
  expect(
    documentLoads.length,
    `the tab must not have navigated; documents requested: ${documentLoads.join(', ')}`,
  ).toBe(loadedDocuments);
  expect(
    new URL(page.url()).pathname,
    'the reader never left /agents — the rail must follow where it already was',
  ).toBe(appUrl);
  await expect(
    railRow(watched.session_id),
    'the conversation that was listed before any of this must still be listed: a rail that '
      + 'emptied and re-rendered is not a rail that followed',
  ).toBeVisible();
  expect(
    uncaught,
    `uncaught exception in the watched tab before the verdict:\n${uncaught.join('\n')}`,
  ).toEqual([]);

  await attachEvidence(page, {
    watchedSessionId: watched.session_id,
    startedSessionId: started.session_id,
    listPath,
    railFreshnessMs: RAIL_FRESHNESS_MS,
    railFetchesTotal: railFetches.length,
    railFetchesSinceCreate: railFetches.filter((at) => at > startedAt).length,
    railFetchesSinceArchive: railFetches.filter((at) => at > archivedAt).length,
    railReadsRefused: railFailures,
    documentLoads,
  });
});

/** The numbers behind the verdict, so a green is auditable and a red is readable. */
async function attachEvidence(page: Page, evidence: Record<string, unknown>): Promise<void> {
  const focusSignals = await page.evaluate(() => window.__railWatchEvents ?? []);
  await test.info().attach('rail-freshness', {
    body: JSON.stringify({ ...evidence, focusSignals }, null, 2),
    contentType: 'application/json',
  });
}
