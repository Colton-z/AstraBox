/**
 * E2E: a console list left open must answer again when the operator comes back.
 *
 * THE JOURNEY. An operator leaves `/manage/sandboxes` open in a background tab.
 * The deployment changes underneath it — a conversation's box is reclaimed —
 * and when the operator returns they act on the row in front of them. The
 * product answer under test is that coming back to the tab makes the table tell
 * the truth, without the operator pressing Refresh or reloading the page.
 *
 * WHAT THE PRODUCT DOES. A `/manage` list holds its read in `useState` and
 * re-issues it from `useKeepCurrent` (frontend/src/hooks/useKeepCurrent.ts),
 * which listens for `visibilitychange` and `focus`; on SandboxesListPage.tsx:112
 * that re-read is a bump of the key the load effect depends on (`:89-110`).
 * Two properties of that hook shape every step below. The re-read is throttled
 * to `RETURN_THROTTLE_MS` — 5s, SWR's `focusThrottleInterval` — so a return
 * delivered sooner than that after the page's own last read is dropped, which
 * is why each return here is timed rather than sent the instant the page has
 * settled. And a list re-reads on return only, never on a cadence, so a table
 * whose reader has not come back has nothing that will repair it.
 *
 * NO TIME HOOK IS NEEDED, and that is what makes the journey fit a lane. There
 * is no TTL to backdate and no sweep to drive: the staleness starts the moment
 * the response lands and lasts as long as the tab is open, so the operator's
 * forty-minute absence contributes nothing that the stimulus below does not
 * supply directly. A foregrounded, actively watched page is in the same
 * position: an operator staring at `/manage/sandboxes` through a deploy is
 * given no return to re-read on.
 *
 * TWO ACTS, AND WHY BOTH.
 * Act 1 is the journey with its content: one real box, destroyed out of band,
 * and the row that must stop describing it. Act 2 is the enumeration — a single
 * passing page cannot stand in for eight, so every route the rail publishes is
 * classified, and each route that carries the return-freshness convention is
 * asked whether returning to the tab makes it re-issue its own data request.
 * Act 2 proves each page RE-ASKS, not that it re-renders what came back; a
 * regression that refetched and discarded the response would pass Act 2 on the
 * pages Act 1 does not cover. That is the honest ceiling of one churned
 * resource inside this budget.
 *
 * THE STIMULUS is a `visibilitychange` on the document followed by a `focus` on
 * the window — the pair `useKeepCurrent` listens on, the pair the release guard
 * binds (`frontendRelease.ts:117-118`), and the pair SWR's `initFocus` binds.
 * Headless Chromium does not reliably background a Playwright page, so a
 * synthetic dispatch is the honest stimulus rather than a shortcut; the upgrade
 * path, if a real tab switch is ever wanted, is `context.newPage()` +
 * `bringToFront()` gated on `document.visibilityState` actually reaching
 * 'hidden'. Each dispatch is held back until the page's re-read throttle has
 * expired, because a return inside that window is dropped by the product and
 * would be indistinguishable here from a page that ignores returns entirely.
 *
 * FALSE-GREEN GUARDS, each for a way this could pass while proving nothing:
 *  1. the main-frame document-navigation counter. `frontendRelease.ts:88-100`
 *     listens to the same two events and replaces the document when the served
 *     hashed entry changed, so a deploy landing mid-run would refresh the table
 *     for a reason that has nothing to do with this behaviour. Such a run is
 *     reported red as inconclusive, never green;
 *  2. `/admin/navigation-summary` is excluded from every route's request set,
 *     because that key genuinely revalidates on focus by design and counting it
 *     would hand every route a free pass;
 *  3. the BEFORE row count of exactly 1 while the box is alive — "the row is
 *     gone" is also what a table that never loaded says;
 *  4. the route classification — a loop that enumerates nothing passes every
 *     assertion inside it, and a rail route that matches no class is red rather
 *     than quietly outside the walk.
 *
 * THE DOMAIN OF ACT 2 is read off the rail and split in two, so the walk cannot
 * shrink without saying so. `RETURN_FRESH_ROUTES` are the pages that carry the
 * return-freshness convention (`useKeepCurrent`), and they are the verdict.
 * `SELF_PACED_ROUTES` are the two rail routes that do not carry it, each with
 * the reason it is out; they are not walked and not judged. A route in neither
 * list fails the classification, because a page added to the console is a page
 * whose freshness nobody has decided.
 *
 * WHAT A GREEN ROUTE IN ACT 2 DOES AND DOES NOT PROVE. `/manage/assistants`
 * also follows on a 2s cadence while any workspace is materializing
 * (`AssistantsListPage.tsx:67-69`), so on a deployment that happens to be
 * materializing one it can pass through that cadence rather than through its
 * return handling. That is not a false green for the journey — a page that
 * re-reads itself IS fresh — but it is not evidence for the routes that have
 * no cadence.
 *
 * WHY PARALLEL. It creates its own cold Agent and its own conversation-tenancy
 * box and terminates only that box — the same out-of-band reclaim
 * session-list-history.parallel.spec.ts already performs in this lane. Every
 * assertion is scoped to its own sandbox id or to its own browser context's
 * request log, so co-running workers cannot move it, and it needs no restart,
 * no idle timing and no TTL. Conversation tenancy is required rather than
 * incidental: the campaign Agent's box is Pool-backed and shared, and
 * terminating a shared box would break the conversations co-running on it.
 *
 * DELIBERATELY NOT ASSERTED: clicking the stale row through to the record page.
 * `SandboxDetailPage` renders its `manage:sandboxes.not_found` state for a box
 * it cannot find, so that assertion passes whether or not the list ever re-read
 * — it cannot fail for the reason this spec exists. The user's cost belongs in
 * the failure message instead.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { RETURN_THROTTLE_MS, returnToTab } from '../fixtures/tabReturn';

// The one localized string this spec reads is the Refresh control's accessible
// name (`common:refresh` → "Refresh"). The console resolves language from
// ['localStorage','navigator'], so both are pinned, exactly as the sibling
// lifecycle specs do.
test.use({ locale: 'en-US' });

// Sessions created here are deleted only when the test passes. A failure keeps
// the box and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
// Registered after trackSessions so the session is deleted before its Agent.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/** How long a returning operator may wait for the page to answer again. */
const REVALIDATE_MS = parseTimeoutEnv('ASTRABOX_E2E_LIST_REVALIDATE_MS', 6_000);
/** How long the backend may take to stop listing a box it has reclaimed. */
const RECLAIM_VISIBLE_MS = parseTimeoutEnv('ASTRABOX_E2E_SANDBOX_RECLAIM_VISIBLE_MS', 60_000);


/**
 * The rail routes whose page holds its read in local state and re-issues it
 * when the reader returns: AgentsListPage.tsx:66, AssistantsListPage.tsx:67,
 * EnvironmentsListPage.tsx:67, CredentialsListPage.tsx:90,
 * DeploymentsListPage.tsx:82, SessionsListPage.tsx:114,
 * SandboxesListPage.tsx:112, ErrorsListPage.tsx:93, McpTokensPage.tsx:78. These are
 * the verdict.
 */
const RETURN_FRESH_ROUTES = [
  '/manage/agents',
  '/manage/assistants',
  '/manage/environments',
  '/manage/credentials',
  '/manage/deployments',
  '/manage/sessions',
  '/manage/sandboxes',
  '/manage/errors',
  '/manage/mcp-tokens',
];

/**
 * The rail routes that carry no return handler, each with what its page does
 * instead. They are enumerated so that the walk below cannot silently shrink,
 * and they are not walked: on `/manage/system` a re-read on its own cadence is
 * indistinguishable from one caused by the return, and `/manage/logs` would be
 * measuring a page whose window is the operator's choice.
 */
const SELF_PACED_ROUTES: Record<string, string> = {
  '/manage/system': 'SystemPage re-reads every 10s of its own accord (SystemPage.tsx:42,121-137)',
  '/manage/logs':
    'LogsPage reads the tail its filters name, once per mount and then on Apply or '
    + 'Refresh (LogsPage.tsx:100-104), so its window is the operator\'s choice rather '
    + 'than a listing of what exists',
};

/** Everything this deployment's API answers on, as a request pathname prefix. */
const API_PREFIX = apiPath('/');
/**
 * The one key that revalidates on focus by design: ManageApp's rail summary is
 * `useSWR` under the app's global `revalidateOnFocus`. Counting it would make
 * every route pass on a request the table had nothing to do with.
 */
const NAV_SUMMARY_PATH = apiPath('/admin/navigation-summary');

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * READ how many rows still describe the box, giving the page its whole window
 * to drop them. This is a reading, not a verdict: the judgement below happens
 * after the integrity of the window has been judged, so an inconclusive run is
 * reported as inconclusive rather than as a stale table.
 */
async function rowsAfterReturning(row: Locator, windowMs: number): Promise<number> {
  const deadline = Date.now() + windowMs;
  for (;;) {
    const count = await row.count();
    if (count === 0 || Date.now() >= deadline) return count;
    await pause(250);
  }
}

/**
 * READ the qualifying data requests recorded so far, waiting up to *windowMs*
 * for the first one. A route whose Refresh produces none is reported as not
 * judged rather than silently skipped, so `accept` never throws.
 */
async function dataRequestsWithin(
  accept: () => string[],
  windowMs: number,
): Promise<string[]> {
  const deadline = Date.now() + windowMs;
  for (;;) {
    const found = accept();
    if (found.length > 0 || Date.now() >= deadline) return found;
    await pause(200);
  }
}

test('a console list left open answers again when the operator returns, and stops describing a sandbox that is gone', async ({
  page,
  request,
  baseURL,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const appOrigin = new URL(appPath('/'), baseURL).origin;

  // ── observers, installed before the first navigation ──────────────────────
  // Every GET's pathname, in order. Each act reads its own window by emptying
  // the log first, so a sibling widget's mount request cannot earn credit for
  // the page under test.
  const gets: string[] = [];
  // Main-frame document navigations. The release guard listens to the same two
  // events this spec dispatches and reloads the whole document when the served
  // entry hash changed; that reload would refresh the table for the wrong
  // reason. A request counted here is the record that it happened.
  let documentNavigations = 0;
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (req.method() === 'GET' && url.pathname.startsWith(API_PREFIX)) {
      gets.push(url.pathname);
    }
    if (req.resourceType() === 'document' && url.origin === appOrigin) {
      documentNavigations += 1;
    }
  });
  /** The page's own data requests in the current window — never the rail's. */
  const pageData = (): string[] => gets.filter((path) => path !== NAV_SUMMARY_PATH);

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── setup: one cold conversation with a box of its own, zero model turns ──
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const agent = await api.createColdTestAgent(`__e2e_stale_row_${runId}`);
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation-tenancy Agent must not own a shared sandbox',
  ).toEqual('');
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '');
  expect(sandboxId, 'a READY session names the box it claimed').not.toEqual('');

  // ── ACT 1 · BEFORE: the operator is looking at a row for a live box ───────
  await page.goto(appPath('/manage/sandboxes'));
  const refresh = page.getByRole('button', { name: 'Refresh' });
  // The list disables its own Refresh until the first request has settled, which
  // is this repo's "the list has loaded" oracle (console-sidebar-counts.audit).
  await expect(refresh, 'the sandbox list must finish its first load').toBeEnabled();
  // Taken after the load settles, so it is never earlier than the read
  // `useKeepCurrent` throttles against — the hook stamps its clock at mount.
  const listReadAt = Date.now();

  // `manage:sandboxes.meta` — the backend's own `total_items`, not a tally of
  // the loaded page.
  const meta = page.getByText(/^\d+ sandboxes$/);
  await expect(meta, 'the listing header must report the backend inventory count').toBeVisible();
  expect(
    Number.parseInt(await meta.innerText(), 10),
    `the inventory count must include the target box ${sandboxId}`,
  ).toBeGreaterThanOrEqual(1);

  // A row is a <tr> carrying tabIndex only when it opens a record
  // (ConsoleTable.tsx:250), and what it shows of an id is `shortId` — the first
  // eight characters, an ellipsis, the last four.
  const row = page
    .locator('tr[tabindex], [role="row"][tabindex]')
    .filter({ hasText: sandboxId.slice(0, 8) });
  await expect(
    row,
    `exactly one row must be sandbox ${sandboxId} while it is alive. The page asks ` +
      'for 50 rows, so on any sane host it is on the first one; check GET ' +
      '/api/v1/admin/sandboxes before reading this as a table defect.',
  ).toHaveCount(1);

  // From here to the verdict this spec does not navigate, reload, press
  // Refresh, or touch the pager. That abstinence IS the experiment, and the
  // counter below is what proves it held.
  const navigationsWhenLeft = documentNavigations;

  // ── ACT 1 · the world changes while the tab sits there ────────────────────
  const reclaim = await api.terminateSandbox(sessionId);
  expect(reclaim.session_id, 'the reclaim receipt must name this conversation').toEqual(sessionId);
  expect(reclaim.status, 'the box must actually have been reclaimed').toEqual('sandbox-reclaimed');
  expect(reclaim.sandbox_id, 'the reclaim must have taken the box the row describes').toEqual(
    sandboxId,
  );

  // ── ACT 1 · the oracle: the backend's answer has changed ──────────────────
  // Only once the deployment itself has stopped listing the box can the console
  // be called wrong. `listSandboxes()` asks page 1 at the backend's default
  // page size — the same window the console's own list request asks for.
  await expect
    .poll(
      async () => {
        const listing = await platform.listSandboxes();
        return (listing.items ?? []).filter(
          (item) => String(item.sandbox_id || '') === sandboxId,
        ).length;
      },
      {
        timeout: RECLAIM_VISIBLE_MS,
        intervals: [500, 1_000, 2_000],
        message:
          `the backend still lists sandbox ${sandboxId} after reclaiming it. This is a ` +
          'reclaim question, not a console one — nothing below can be judged until the ' +
          'deployment itself stops reporting the box.',
      },
    )
    .toBe(0);

  // ── ACT 1 · the operator returns, and the table must tell the truth ───────
  await returnToTab(page, listReadAt);
  const rowsOnReturn = await rowsAfterReturning(row, REVALIDATE_MS);
  const reloadsWhileWatching = documentNavigations - navigationsWhenLeft;

  // Integrity before journey: a run whose tab was reloaded by a release landing
  // mid-test learned nothing about freshness either way.
  expect(
    reloadsWhileWatching,
    'a release adoption reloaded the tab between the row being read and the verdict, ' +
      'so this run cannot judge whether returning to the tab refreshed the list',
  ).toBe(0);
  expect(
    rowsOnReturn,
    `the deployment has stopped listing sandbox ${sandboxId}, but /manage/sandboxes still ` +
      `shows its row ${REVALIDATE_MS}ms after the operator came back to the tab. That row ` +
      'is one the operator will click into “No sandbox called …”. The return is supposed ' +
      'to reach SandboxesListPage.tsx:112, which bumps the key its load effect depends on ' +
      '(:89-110); check that the re-read left the tab at all before reading this as a ' +
      'rendering defect.',
  ).toBe(0);

  // ── ACT 2 · the enumeration ───────────────────────────────────────────────
  // The domain is read off the product, not hand-written, so a page added to
  // the console cannot slip past the classification below.
  // `[data-slot="sidebar-content"]` is the rail itself: the breadcrumb is also
  // a <nav>, and integration links are absolute URLs on another origin (and
  // carry target=_blank), so neither is enumerated here.
  // The hrefs are read back as rail-relative routes, so a deployment behind a
  // path-prefixing proxy classifies the same set as one served at the root.
  const manageHref = appPath('/manage/');
  const published = [
    ...new Set(
      await page.evaluate(
        (prefix) =>
          [...document.querySelectorAll<HTMLAnchorElement>('[data-slot="sidebar-content"] a')]
            .filter((link) => link.target !== '_blank')
            .map((link) => link.getAttribute('href') || '')
            .filter((href) => href.startsWith(prefix)),
        manageHref,
      ),
    ),
  ].map((href) => `/manage/${href.slice(manageHref.length)}`);

  const unclassified = published.filter(
    (route) => !RETURN_FRESH_ROUTES.includes(route) && !(route in SELF_PACED_ROUTES),
  );
  expect(
    unclassified,
    'the rail publishes console routes this spec has not classified. Wire the page to ' +
      '`useKeepCurrent` and add it to RETURN_FRESH_ROUTES, or record in SELF_PACED_ROUTES ' +
      'what it does instead — an unclassified page is one whose freshness nobody decided',
  ).toEqual([]);
  expect(
    RETURN_FRESH_ROUTES.filter((route) => !published.includes(route)),
    'the rail must publish every route this walk judges; a route it stopped publishing is ' +
      'one this spec would otherwise report as green without visiting it',
  ).toEqual([]);

  // Recorded, not judged, so that a reader of the verdict below can see which
  // console pages were outside the walk and on what grounds.
  test.info().annotations.push({
    type: 'e2e_console_routes_outside_return_freshness',
    description: JSON.stringify(SELF_PACED_ROUTES),
  });

  // Read every route, judge at the end: with maxFailures: 1, asserting at the
  // point of reading would abort the walk on the first stale page and leave the
  // rest of the domain unmeasured.
  const judged: string[] = [];
  const unjudged: string[] = [];
  const stale: string[] = [];

  for (const route of RETURN_FRESH_ROUTES) {
    await page.goto(appPath(route));
    const control = page.getByRole('button', { name: 'Refresh' });
    await expect(control, `${route}: the page must finish its first load`).toBeEnabled();
    // The throttle runs from the page's own read, and pressing Refresh does not
    // move it: `useKeepCurrent` stamps its clock at mount and on the re-reads it
    // causes, never on the page's other loads. So the window below is measured
    // from here, and the Refresh probe spends part of it.
    const mountReadAt = Date.now();

    // P — the requests this page's own Refresh makes. Self-calibrating: what an
    // operator's manual Refresh asks for is by definition this page's data
    // request, so no endpoint table is hardcoded here.
    gets.length = 0;
    await control.click();
    const refreshed = [...new Set(await dataRequestsWithin(pageData, REVALIDATE_MS))];
    if (refreshed.length === 0) {
      // Not a verdict either way: a page whose Refresh asks for nothing leaves
      // no statement of what returning to the tab ought to re-ask for.
      unjudged.push(route);
      continue;
    }
    // The control re-enables only once its own request has settled, so by now
    // every GET that Refresh issued is already in the log and the window below
    // starts clean.
    await expect(control, `${route}: Refresh must settle before the window opens`).toBeEnabled();

    gets.length = 0;
    await returnToTab(page, mountReadAt);
    const reAsked = await dataRequestsWithin(
      () => pageData().filter((path) => refreshed.includes(path)),
      REVALIDATE_MS,
    );
    judged.push(route);
    if (reAsked.length === 0) stale.push(`${route} (never re-asked ${refreshed.join(', ')})`);
  }

  expect(
    unjudged,
    'these console pages offer a Refresh that issues no data request, so the return that ' +
      'follows it has nothing to be compared against and the route went unjudged',
  ).toEqual([]);
  expect(
    judged.length,
    `the walk judged ${judged.length} of ${RETURN_FRESH_ROUTES.length} return-fresh routes`,
  ).toBe(RETURN_FRESH_ROUTES.length);
  expect(
    stale,
    'these console pages did not re-issue their own data request when the operator ' +
      'returned to the tab, so each one keeps showing whatever the deployment looked ' +
      'like when it was opened. Each is wired to `useKeepCurrent`, which re-reads on ' +
      '`visibilitychange` and `focus` once its 5s throttle has passed — and the return ' +
      'above waited that out, so a red here is the page, not the timing',
  ).toEqual([]);
});
