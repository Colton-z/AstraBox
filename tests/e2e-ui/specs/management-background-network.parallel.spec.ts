/**
 * E2E: a management page left open through a lost network keeps what it last
 * read, says nothing about the background reads that failed, and reads what
 * changed on the server by itself once the network is back.
 *
 * The journey is the passive one: open a console list or record, stop touching
 * it, and let the machine's network drop and return. Every management page
 * keeps its locally held read current through one hook
 * (frontend/src/hooks/useKeepCurrent.ts): it re-reads on a tab return, on a 2s
 * follow interval while a record is in motion, and when the browser reports
 * `online`. A re-read that fails for transport reasons must not replace the
 * rows, the rail count or an unsaved edit with an error card, a note or a blank
 * badge. A read the operator asks for keeps its failure: Refresh over a dead
 * network must show the error card, or a dead deployment would look healthy.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. The classification lives at the transport
 * (`api.ts` wraps a browser fetch rejection in `NetworkRequestError`), the
 * retention in each page's own catch, and the restore in the hook's `online`
 * listener; a jsdom test can hand any one of them a hand-made error and watch
 * it handled. Only a real browser can show that a real `fetch` rejection
 * reaches the page as that class after the transport has spent its own replays
 * (networkRecovery.ts replays a thrown safe read at 100ms and 300ms), and that
 * Chromium's own `online` event is what brings the record back.
 *
 * TWO FAULTS, ON PURPOSE. `route.abort()` is a transport failure the browser
 * still reports as online, so a page that consulted `navigator.onLine` instead
 * of the error it was handed would misread it. `context.setOffline()` is the
 * real thing and the only source of the `online` event. Both are induced in
 * each journey, and the requests each one cost are counted, so a quiet page is
 * distinguishable from a page nothing reached.
 *
 * THE ALERT WATCH. A failure that flashes and clears is still the failure the
 * operator reported, and a final-DOM assertion cannot see it. An init script
 * records every `[role="alert"]` the document gains — the attribute both
 * shared failure primitives carry (ConsoleErrorState and ErrorNote) — so the
 * claim "nothing was shown" covers the whole quiet window.
 *
 * The first journey drives the Agents list: its rows, the rail count the list
 * publishes from the same response (AgentsListPage.tsx), and a change made
 * through the API while the tab was offline. The second drives a schedule
 * Deployment record: its runs are polled every 2s, an unsaved Name edit is on
 * screen, and the poll's failure must leave no note beside the table. Neither
 * needs a model turn or a sandbox; both run under whichever profile the matrix
 * selected. A schedule that can never fire keeps the second journey free of
 * Runs, so what it proves about the poll is the poll, not a Run's progress.
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { returnToTab } from '../fixtures/tabReturn';

declare global {
  interface Window {
    /** What the watch installed at document start has seen, see watchTheTab. */
    __managementNetwork?: {
      /** `'alive'` once the spec's only navigation has landed; a reload loses it. */
      tab: string;
      alerts: { at: number; slot: string; text: string }[];
      connectivity: { type: string; at: number; onLine: boolean }[];
    };
  }
}

// The strings read off the page are the console's own chrome — Refresh, Retry,
// Save, the Name label and the list's error title. The console detects language
// as ['localStorage','navigator'], so an unpinned runner locale decides which
// spelling appears. Pinned the way the lifecycle specs pin it.
test.use({ locale: 'en-US' });

const RUN_ID = Date.now();
const AGENT_NAME = `__e2e_bg_network_${RUN_ID}`;
const DESCRIPTION_BEFORE = `read before the network dropped ${RUN_ID}`;
const DESCRIPTION_AFTER = `changed by a colleague while this tab was offline ${RUN_ID}`;
const DEPLOYMENT_NAME = `__e2e_bg_network_runs_${RUN_ID}`;
/** Typed as keystrokes so the last thing the field saw is real input. */
const DRAFT_TAIL = ' (renamed, not yet saved)';

/**
 * Requests one failed read costs at the route: the first attempt plus the two
 * replays `fetchWithNetworkRecovery` makes for a thrown safe read
 * (networkRecovery.ts SAFE_RETRY_DELAYS_MS). A page hears of the failure only
 * after all three, so this is the count that means "the page was told".
 */
const TRANSPORT_ATTEMPTS = 3;

/** How long an armed fault may take to be served to the browser. */
const FAULT_SERVED_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_NETWORK_FAULT_MS', 15_000);

/**
 * How long the page gets to re-read after the browser reports `online`.
 *
 * A page that listens for the event re-reads inside one request; a page that
 * waits for the next tab return never does, so this only decides how long a
 * red takes to arrive.
 */
const RECONNECT_READ_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_NETWORK_RECONNECT_MS', 15_000);

/**
 * How long the page gets to render whatever it decided after its last failed
 * attempt. The request count above is what proves the fault was exercised; this
 * is only the gap between the transport rejecting and React committing the
 * page's answer to it.
 */
const SETTLE_MS = 750;

/**
 * The follow cadence of a schedule record's runs (useKeepCurrent
 * FOLLOW_INTERVAL_MS), with room for two ticks and their replays.
 */
const TWO_POLLS_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_NETWORK_TWO_POLLS_MS', 10_000);

/**
 * Watch the tab from document start.
 *
 * Installed before the first navigation so nothing the page renders or hears
 * is missed: every `[role="alert"]` the document gains, and every `online` /
 * `offline` event the window receives, are recorded with the time. The
 * language pin lives here too so that it precedes the first render.
 */
async function watchTheTab(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
    const seen: NonNullable<Window['__managementNetwork']> = {
      tab: '',
      alerts: [],
      connectivity: [],
    };
    window.__managementNetwork = seen;
    const note = (element: Element) => {
      seen.alerts.push({
        at: Date.now(),
        slot: element.getAttribute('data-slot') ?? '',
        text: (element.textContent ?? '').trim().slice(0, 200),
      });
    };
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        mutation.addedNodes.forEach((added) => {
          if (!(added instanceof Element)) return;
          if (added.matches('[role="alert"]')) note(added);
          added.querySelectorAll('[role="alert"]').forEach(note);
        });
      }
    }).observe(document, { childList: true, subtree: true });
    for (const type of ['online', 'offline'] as const) {
      window.addEventListener(type, () => {
        seen.connectivity.push({ type, at: Date.now(), onLine: navigator.onLine });
      });
    }
  });
}

/** Every alert the tab has shown, in order. */
function alertsShown(page: Page) {
  return page.evaluate(() => window.__managementNetwork?.alerts ?? []);
}

/** The window's `online` / `offline` events so far. */
function connectivityEvents(page: Page) {
  return page.evaluate(() => window.__managementNetwork?.connectivity ?? []);
}

/** The document's identity: the marker set after navigation and its birth time. */
function documentIdentity(page: Page) {
  return page.evaluate(() => ({
    tab: window.__managementNetwork?.tab ?? '',
    born: performance.timeOrigin,
  }));
}

interface ReadFault {
  /** `abort` rejects every GET at the route; `pass` lets it through. */
  mode: 'pass' | 'abort';
  /** GETs rejected by this route. */
  aborted: number;
  /** GETs the route let through, offline ones included. */
  continued: number;
  /** GETs that came back 2xx, the only count that means the page was answered. */
  answered: number;
  /** GETs the browser reported failed, whether by this route or by being offline. */
  failed: number;
}

/**
 * Take the GETs of one API path under the spec's control.
 *
 * A pathname predicate rather than a glob, built from apiPath so a deployment
 * behind ASTRABOX_E2E_APP_PREFIX still matches. Only GETs are touched: the
 * same paths take writes this spec must not intercept. `route.abort()` is the
 * transport failure under test; a fulfilled 5xx would exercise the HTTP branch,
 * which must keep showing its error and is not what this spec is about.
 */
async function controlReads(page: Page, pathname: string): Promise<ReadFault> {
  const fault: ReadFault = { mode: 'pass', aborted: 0, continued: 0, answered: 0, failed: 0 };
  const isRead = (url: string, method: string) =>
    method === 'GET' && new URL(url).pathname === pathname;
  page.on('response', (response) => {
    if (isRead(response.url(), response.request().method()) && response.ok()) fault.answered += 1;
  });
  page.on('requestfailed', (request) => {
    if (isRead(request.url(), request.method())) fault.failed += 1;
  });
  await page.route(
    (url) => url.pathname === pathname,
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.continue();
      if (fault.mode === 'abort') {
        fault.aborted += 1;
        return route.abort('failed');
      }
      fault.continued += 1;
      return route.continue();
    },
  );
  return fault;
}

/** The rail, expanded so its counts are on screen (a collapsed rail hides them). */
async function expandedRail(page: Page) {
  const rail = page.locator('[data-slot="sidebar"]');
  if ((await rail.getAttribute('data-state')) === 'collapsed') {
    await page.locator('[data-slot="sidebar-trigger"]').first().click();
  }
  await expect(rail, 'the rail must be expanded for its counts to be readable').toHaveAttribute(
    'data-state',
    'expanded',
  );
  return rail;
}

/** The collection total the open list states beside its own title. */
async function statedTotal(page: Page, where: string): Promise<number> {
  const meta = page
    .getByRole('heading', { level: 1 })
    .locator('xpath=following-sibling::span[1]');
  const text = (await meta.innerText()).trim();
  const digits = text.match(/\d+/);
  expect(digits, `${where}: the list must state a collection total beside its title: ${text}`)
    .not.toBeNull();
  return Number(digits![0]);
}

// A browser context is discarded with its test, but a failed body must not
// leave the next fixture in the state it stopped in.
test.afterEach(async ({ context }) => {
  await context.setOffline(false).catch(() => {});
});

let createdAgentId = '';
// The Agent outlives a failure on purpose (fixtures/sessionCleanup): a record
// deleted in teardown is a record nobody can look at afterwards.
onPassOnly(async ({ request }) => {
  if (createdAgentId) await new AstraApi(request).deleteAgent(createdAgentId);
  createdAgentId = '';
});

let scheduleAgentId = '';
let scheduleDeploymentId = '';
// A retained failure keeps its Deployment row for reading, but it must never be
// able to fire. The `0 3 1 1 *` cron cannot come round inside a test run; this
// teardown also closes the window between create and the disable in the body.
test.afterEach(async ({ request }) => {
  if (!scheduleAgentId || !scheduleDeploymentId) return;
  await new PlatformApi(request)
    .updateDeployment(scheduleAgentId, scheduleDeploymentId, { enabled: false })
    .catch(() => {});
});
onPassOnly(async ({ request }) => {
  if (scheduleAgentId && scheduleDeploymentId) {
    await new PlatformApi(request).deleteDeployment(scheduleAgentId, scheduleDeploymentId);
  }
  scheduleAgentId = '';
  scheduleDeploymentId = '';
});

test('an idle Agents list keeps its rows and rail count through a lost network, and reads a change made meanwhile once it is back', async ({
  context,
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Before the first navigation: a tree that crashed during a render shows no
  // rows either, and "no rows" must not be reported as a dropped list.
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);

  // ── setup: one Agent this spec owns, on the matrix Agent's Environment ────
  const seeded = await api.defaultAgent();
  const environmentName = String(seeded.environment_name || '').trim();
  expect(environmentName, 'the matrix-selected Agent must name an Environment').not.toEqual('');
  // The model is taken from the catalogue the Environment offers, not from the
  // seeded Agent's pin: a pin the gateway does not enumerate is refused on
  // create, and the list would then have nothing of this spec's to show.
  const offered = await api.listEnvironmentModels(environmentName);
  const seededModel = String(seeded.model || '').trim();
  const model = offered.includes(seededModel)
    ? seededModel
    : (offered.find((candidate) => candidate && !candidate.includes('*')) ?? '');
  expect(
    model,
    `Environment ${JSON.stringify(environmentName)} offers no concrete model; it returned: `
      + `${offered.join(', ') || '(nothing)'}`,
  ).not.toEqual('');
  const created = await api.createAgent({
    name: AGENT_NAME,
    model,
    environment_name: environmentName,
    prewarm_enabled: false,
    description: DESCRIPTION_BEFORE,
  });
  createdAgentId = String(created.agent_id || '').trim();
  expect(createdAgentId, 'the test-owned Agent must have an id').not.toEqual('');

  await watchTheTab(page);
  const listPath = apiPath('/agents');
  const fault = await controlReads(page, listPath);

  // ── THE ONLY page.goto IN THIS TEST ──────────────────────────────────────
  await page.goto(appPath('/manage/agents'), { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => {
    window.__managementNetwork!.tab = 'alive';
  });
  const born = await documentIdentity(page);

  const refresh = page.getByRole('button', { name: 'Refresh' });
  const row = page.getByRole('row').filter({ hasText: AGENT_NAME });
  const alerts = () => alertsShown(page);
  const diagnostics: Record<string, unknown> = { listPath, agentId: createdAgentId };

  try {
    // ── the precondition, established on the page and not assumed ───────────
    // AgentsListPage disables Refresh while its request is in flight and
    // publishes its total on the way out, so this is the settle signal the
    // list itself offers.
    await expect(refresh, 'the Agents list must finish its mount read').toBeEnabled();
    let readAt = Date.now();
    await expect(row, 'the test-owned Agent must be listed before anything is broken').toHaveCount(1);
    await expect(row, 'with the description it was created with').toContainText(DESCRIPTION_BEFORE);
    expect(
      fault.answered,
      `no GET ${listPath} was answered through this tab while the list loaded, so this spec `
        + 'is not observing anything. Check the app prefix before reading anything below.',
    ).toBeGreaterThanOrEqual(1);

    const rail = await expandedRail(page);
    // The SurfaceNav "Console" row points at the same route, so the menu item
    // is identified by the badge it owns rather than by the href alone.
    const badge = rail.locator(
      '[data-slot="sidebar-menu-item"]:has(a[href$="/manage/agents"]) [data-slot="sidebar-menu-badge"]',
    );
    const total = await statedTotal(page, 'before the fault');
    await expect(
      badge,
      'on its own list page the badge is the total the list published — the number a '
        + 'failed background read must not blank',
    ).toHaveText(String(total));
    expect(await alerts(), 'a healthy mount shows no failure').toEqual([]);

    // ── fault 1: the transport fails while the browser says it is online ────
    expect(
      await page.evaluate(() => navigator.onLine),
      'this fault is the one navigator.onLine cannot see, so it must report online',
    ).toBe(true);
    fault.mode = 'abort';
    // The returning operator: the hook's own visibilitychange + focus pair,
    // sent after its throttle (fixtures/tabReturn).
    await returnToTab(page, readAt);
    readAt = Date.now();
    await expect
      .poll(() => fault.aborted, {
        timeout: FAULT_SERVED_MS,
        message:
          'the armed transport failure was never served, so no background read failed and '
          + 'nothing below would prove anything. A tab return re-reads the list through '
          + 'useKeepCurrent after RETURN_THROTTLE_MS.',
      })
      .toBeGreaterThanOrEqual(TRANSPORT_ATTEMPTS);
    await page.waitForTimeout(SETTLE_MS);
    fault.mode = 'pass';
    diagnostics.abortedOnReturnWhileOnline = fault.aborted;
    expect(
      fault.aborted,
      'one return costs one read: the transport\'s own replays and nothing more. More means a '
        + 'second reader of this path this spec does not know about; fewer means the page '
        + 'was never told.',
    ).toBe(TRANSPORT_ATTEMPTS);

    await expect(
      row,
      'a background read that lost the network keeps the rows the operator was looking at',
    ).toHaveCount(1);
    await expect(
      badge,
      'and keeps the count the list published into the rail (AgentsListPage clears it in '
        + 'its catch for every error, transport ones included, when this is red)',
    ).toHaveText(String(total));
    await expect(refresh, 'and asks nothing of the operator').toBeEnabled();
    expect(
      await alerts(),
      'no failure surface may be shown for a background read that lost the network — this '
        + 'is the error the operator reported. The list is what it was; the read simply '
        + 'did not happen.',
    ).toEqual([]);

    // ── fault 2: the browser is offline, and a colleague changes the record ─
    await context.setOffline(true);
    await expect
      .poll(() => page.evaluate(() => navigator.onLine), {
        message: 'the browser must report offline for this half to be the real thing',
      })
      .toBe(false);
    // Made through the test's own request context, which page.route and the
    // browser's emulation never touch: the record changes on the server while
    // this tab cannot know it.
    const latest = await api.getAgent(createdAgentId);
    await api.updateAgent(createdAgentId, {
      name: AGENT_NAME,
      model,
      environment_name: environmentName,
      prewarm_enabled: false,
      description: DESCRIPTION_AFTER,
      version: Number(latest.version || 1),
    });
    expect(
      String((await api.getAgent(createdAgentId)).description || ''),
      'the server must hold the change before the tab can be asked to show it',
    ).toBe(DESCRIPTION_AFTER);

    const failedBeforeOfflineReturn = fault.failed;
    await returnToTab(page, readAt);
    readAt = Date.now();
    await page.waitForTimeout(SETTLE_MS);
    // Either design is correct here — a page that skips the read while the
    // browser reports offline, and one that lets it fail quietly — so the
    // attempts are recorded rather than asserted.
    diagnostics.requestsFailedOnReturnWhileOffline = fault.failed - failedBeforeOfflineReturn;
    await expect(row, 'offline, the rows stay').toHaveCount(1);
    await expect(
      row,
      'offline, the tab can only show what it last read — a newer description here means '
        + 'a request got through a browser that reports offline',
    ).toContainText(DESCRIPTION_BEFORE);
    await expect(badge, 'offline, the rail count stays').toHaveText(String(total));
    expect(await alerts(), 'offline, no failure surface may be shown for a background read')
      .toEqual([]);

    // ── the network returns: the tab must catch up by itself ────────────────
    const answeredBeforeOnline = fault.answered;
    await context.setOffline(false);
    await expect
      .poll(async () => (await connectivityEvents(page)).map((event) => event.type), {
        message:
          'Chromium must deliver the window `online` event when emulation ends; without it '
          + 'there is no reconnect signal for any page to act on',
      })
      .toContain('online');
    await expect
      .poll(() => fault.answered, {
        timeout: RECONNECT_READ_MS,
        message:
          'the list must re-read on its own when the browser comes back online — no click, '
          + 'no tab return, no reload. useKeepCurrent listens for `online` the way SWR\'s '
          + 'revalidateOnReconnect does; a page that waits for the next focus shows a '
          + 'colleague\'s change only when the operator happens to come back.',
      })
      .toBeGreaterThan(answeredBeforeOnline);
    await expect(
      row,
      'the change made while the tab was offline is on screen, in the tab that was never '
        + 'reloaded',
    ).toContainText(DESCRIPTION_AFTER);
    await expect(
      badge,
      'the reconnect read re-publishes the rail count from the same response',
    ).toHaveText(String(await statedTotal(page, 'after reconnect')));
    expect(await alerts(), 'reconnecting shows no failure either').toEqual([]);
    expect(
      await documentIdentity(page),
      'the document must be the one this test opened: a reload would make every reading '
        + 'above true for a reason that has nothing to do with the property',
    ).toEqual({ tab: 'alive', born: born.born });

    // ── a read the operator asks for keeps its failure ──────────────────────
    // The other half of the rule. Retaining the last read on a background
    // failure must not turn into hiding failures: Refresh over a dead network
    // must say so, with the product's own error card, and Retry must recover.
    fault.mode = 'abort';
    const refreshedAt = Date.now();
    await refresh.click();
    const errorCard = page.getByRole('alert').filter({ hasText: "Couldn't load agents" });
    await expect(
      errorCard,
      'Refresh over a dead network must show the list\'s error card — a page that keeps '
        + 'its rows here has hidden a failure the operator asked about',
    ).toBeVisible({ timeout: FAULT_SERVED_MS });
    fault.mode = 'pass';
    const shownForRefresh = await alerts();
    expect(
      shownForRefresh.length,
      'the card the operator asked for is the first alert this tab has shown',
    ).toBeGreaterThanOrEqual(1);
    expect(
      shownForRefresh.every((alert) => alert.at >= refreshedAt - 1),
      `every alert must follow the Refresh click: ${JSON.stringify(shownForRefresh)}`,
    ).toBe(true);
    await errorCard.getByRole('button', { name: 'Retry' }).click();
    await expect(row, 'Retry brings the list back').toHaveCount(1);
    await expect(row).toContainText(DESCRIPTION_AFTER);
    await expect(errorCard, 'and takes the card with it').toHaveCount(0);
    await expect(badge).toHaveText(String(await statedTotal(page, 'after retry')));
  } finally {
    await test.info().attach('agents-list-background-network', {
      body: JSON.stringify(
        {
          ...diagnostics,
          aborted: fault.aborted,
          continued: fault.continued,
          answered: fault.answered,
          failed: fault.failed,
          connectivity: await connectivityEvents(page).catch(() => 'unavailable'),
          alerts: await alertsShown(page).catch(() => 'unavailable'),
          uncaught,
        },
        null,
        2,
      ),
      contentType: 'application/json',
    });
  }

  expect(uncaught, `uncaught exception on the Agents list:\n${uncaught.join('\n')}`).toEqual([]);
});

test('a schedule record keeps an unsaved edit and shows no note while its runs poll loses the network, and polls again when it is back', async ({
  context,
  page,
  request,
}) => {
  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  // ── setup: one schedule Deployment on the matrix Agent, never able to fire ─
  // Bound to defaultAgent() on purpose: no Run is triggered, so no box is
  // touched, and a Deployment row is all that is written. The `0 3 1 1 *` cron
  // cannot come round inside a test run, and the row is disabled before the
  // page opens, so the runs table this journey polls stays empty by
  // construction — what changes across the fault is the poll, not the rows.
  const agent = await api.defaultAgent();
  scheduleAgentId = String(agent.agent_id || '').trim();
  expect(scheduleAgentId, 'the matrix Agent must have an id').not.toEqual('');
  const deployment = await platform.createDeployment(scheduleAgentId, {
    scene: 'schedule',
    name: DEPLOYMENT_NAME,
    prompt_prefix: 'Reply with the single word ok. Do not use tools.',
    schedule: { cron: '0 3 1 1 *', timezone: 'UTC' },
  });
  scheduleDeploymentId = String(deployment.deployment_id || '').trim();
  expect(scheduleDeploymentId, 'the schedule Deployment must have an id').not.toEqual('');
  await platform.updateDeployment(scheduleAgentId, scheduleDeploymentId, { enabled: false });

  await watchTheTab(page);
  const runsPath = apiPath(
    `/admin/agents/${scheduleAgentId}/deployments/${scheduleDeploymentId}/runs`,
  );
  const fault = await controlReads(page, runsPath);

  // ── THE ONLY page.goto IN THIS TEST ──────────────────────────────────────
  await page.goto(appPath(`/manage/deployments/${scheduleDeploymentId}`), {
    waitUntil: 'domcontentloaded',
  });
  await page.evaluate(() => {
    window.__managementNetwork!.tab = 'alive';
  });
  const born = await documentIdentity(page);

  // The runs table is the only table this record renders; the note a failed
  // poll would leave is rendered inside the same card, above the table.
  const table = page.getByTestId('console-table');
  const runsCard = page.locator('[data-slot="card"]').filter({ has: table });
  const nameField = page.getByRole('textbox', { name: 'Name', exact: true });
  const save = page.getByRole('button', { name: 'Save', exact: true });
  const alerts = () => alertsShown(page);
  const diagnostics: Record<string, unknown> = {
    runsPath,
    agentId: scheduleAgentId,
    deploymentId: scheduleDeploymentId,
  };

  try {
    // ── the precondition: a live poll, and an edit the operator has not saved ─
    await expect(table, 'the schedule record renders exactly one table').toHaveCount(1);
    await expect(nameField, 'the schedule section offers exactly one Name field').toHaveCount(1);
    await expect(nameField).toHaveValue(DEPLOYMENT_NAME);
    // ConsoleCard renders Save only for a dirty section, so its absence is the
    // page's own statement that nothing is unsaved yet.
    await expect(save).toHaveCount(0);
    await nameField.click();
    await nameField.press('End');
    await nameField.pressSequentially(DRAFT_TAIL);
    const typed = `${DEPLOYMENT_NAME}${DRAFT_TAIL}`;
    await expect(nameField).toHaveValue(typed);
    await expect(save, 'a typed Name is unsaved work the page must keep').toBeVisible();
    expect(await alerts(), 'a healthy record shows no failure').toEqual([]);
    // The follow poll is confirmed running before it is broken: a page that
    // never polled would pass every "no note" assertion below for free.
    await expect
      .poll(() => fault.answered, {
        timeout: TWO_POLLS_MS,
        message:
          'a schedule record polls its runs every 2s while the tab is visible '
          + '(DeploymentDetailPage wires useKeepCurrent with follow); without a second '
          + 'answered read there is no poll to break',
      })
      .toBeGreaterThanOrEqual(2);

    // ── fault 1: the transport fails while the browser says it is online ────
    expect(await page.evaluate(() => navigator.onLine)).toBe(true);
    fault.mode = 'abort';
    // Two failed polls, not one: the second proves the first did not stop the
    // timer, and each costs the transport's full replay budget.
    await expect
      .poll(() => fault.aborted, {
        timeout: TWO_POLLS_MS,
        message:
          'the armed transport failure was never served twice, so the poll either stopped '
          + 'after its first failed read or never ran',
      })
      .toBeGreaterThanOrEqual(TRANSPORT_ATTEMPTS * 2);
    await page.waitForTimeout(SETTLE_MS);
    fault.mode = 'pass';
    diagnostics.abortedWhileOnline = fault.aborted;
    await expect(nameField, 'the unsaved edit survives the failed polls').toHaveValue(typed);
    await expect(save, 'and still reads as unsaved').toBeVisible();
    await expect(
      runsCard.getByRole('alert'),
      'a poll that lost the network leaves no note beside the runs table — this is the '
        + 'per-panel error spam an idle record page shows when its poll\'s catch records '
        + 'every failure',
    ).toHaveCount(0);
    expect(await alerts(), 'no failure surface anywhere for a background poll').toEqual([]);

    // ── fault 2: the browser is offline ─────────────────────────────────────
    await context.setOffline(true);
    await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false);
    const failedBeforeOffline = fault.failed;
    const continuedBeforeOffline = fault.continued;
    // Long enough for two follow ticks. Whether the page skips them while the
    // browser reports offline or lets them fail quietly, both are correct; the
    // attempts are recorded, the silence is asserted.
    await page.waitForTimeout(TWO_POLLS_MS / 2);
    diagnostics.requestsAttemptedWhileOffline = fault.continued - continuedBeforeOffline;
    diagnostics.requestsFailedWhileOffline = fault.failed - failedBeforeOffline;
    await expect(nameField, 'offline, the unsaved edit stays').toHaveValue(typed);
    await expect(save).toBeVisible();
    await expect(runsCard.getByRole('alert'), 'offline, no note beside the table').toHaveCount(0);
    expect(await alerts(), 'offline, no failure surface anywhere').toEqual([]);

    // ── the network returns: the poll must be answered again by itself ──────
    const answeredBeforeOnline = fault.answered;
    await context.setOffline(false);
    await expect
      .poll(async () => (await connectivityEvents(page)).map((event) => event.type), {
        message: 'Chromium must deliver the window `online` event when emulation ends',
      })
      .toContain('online');
    await expect
      .poll(() => fault.answered, {
        timeout: RECONNECT_READ_MS,
        message:
          'the runs must be read again once the browser is back online — the follow timer '
          + 'and the `online` listener are each enough; a page that stopped asking after '
          + 'its failed polls is the defect',
      })
      .toBeGreaterThan(answeredBeforeOnline);
    await expect(nameField, 'reconnecting does not reset the unsaved edit').toHaveValue(typed);
    await expect(save).toBeVisible();
    await expect(runsCard.getByRole('alert'), 'and leaves no note behind').toHaveCount(0);
    expect(await alerts(), 'reconnecting shows no failure').toEqual([]);
    expect(
      await documentIdentity(page),
      'the document must be the one this test opened',
    ).toEqual({ tab: 'alive', born: born.born });
  } finally {
    await test.info().attach('schedule-record-background-network', {
      body: JSON.stringify(
        {
          ...diagnostics,
          aborted: fault.aborted,
          continued: fault.continued,
          answered: fault.answered,
          failed: fault.failed,
          connectivity: await connectivityEvents(page).catch(() => 'unavailable'),
          alerts: await alertsShown(page).catch(() => 'unavailable'),
          uncaught,
        },
        null,
        2,
      ),
      contentType: 'application/json',
    });
  }

  expect(uncaught, `uncaught exception on the schedule record:\n${uncaught.join('\n')}`)
    .toEqual([]);
});
