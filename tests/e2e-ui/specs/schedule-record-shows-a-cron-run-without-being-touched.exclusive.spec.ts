/**
 * E2E: a schedule's record page shows the Run its cron made while nobody touched it.
 *
 * The journey is an operator's, and it contains no gestures after the first:
 * create a `* * * * *` schedule on /manage/deployments/new, land on its record
 * page, and sit there. No click, no reload, no navigation, no keystroke. The
 * cron fires server-side, and the Runs table must show the Run it made. A table
 * that still reads "No runs yet" while the ledger holds a Run is not merely
 * late — it states a fact ("Runs appear after this schedule is invoked for the
 * first time" — `deployments.runs_empty_hint`,
 * frontend/src/i18n/locales/en/manage.json:276) that is false as it is read.
 *
 * WHY A REAL DEPLOYMENT IS THE ONLY PLACE THIS CAN BE ASKED. Three parties have
 * to agree and only one of them is the page: DBOS's scheduler thread inside the
 * server decides when the minute is up (deployment_run_runtime.py:226-228,
 * automatic_backfill False at :290, so the first tick is strictly the next
 * boundary); the Run has to be *listable* the instant it is enqueued, which is
 * the ENQUEUED→QUEUED projection at deployment_service.py:1070 reading the DBOS
 * workflow ledger (list_workflows_async, deployment_run_runtime.py:487) before
 * any Session, sandbox or model turn exists; and only then does the page's own
 * re-read have something to find. `useKeepCurrent.test.ts` proves the hook's
 * timer in isolation. Nothing proves the page arms it for a table that started
 * EMPTY, which is the only state a fresh schedule's record page is ever in.
 *
 * WHAT MAKES THE PROPERTY HOLD, AND THE SHAPE THAT BREAKS IT. The page
 * follows its runs for as long as the record is open, gated on the record being
 * a schedule rather than on its contents:
 * `useKeepCurrent(reloadRuns, { follow: deployment?.scene === 'schedule' })`
 * (DeploymentDetailPage.tsx:151-160, FOLLOW_INTERVAL_MS 2s). The shape that
 * fails is a poll armed from the ALREADY-LOADED runs array.
 * An empty table arms no timer and nothing else
 * writes that array, so it can never re-arm itself — and the page offers no
 * non-destructive way to ask again (`refreshRuns` has two callers, Run now and a
 * row's Replay, and `load()` runs on mount and after Enable/Disable/Save). That
 * is why every step below stays off the page: clicking Disable, the obvious way
 * to stop the cron, calls `load()` and would repopulate the table itself.
 *
 * THE FALSE-GREEN THIS SPEC HAS TO CLOSE. If the first tick lands between the
 * Create POST and the page's first runs read, the table mounts already holding
 * a Run — which would arm even the broken shape, and this spec would pass
 * having proved nothing. Creating on a fresh minute boundary is what keeps that
 * window shut, and the explicit "No runs yet plus zero rows" precondition is
 * what would catch it if the alignment ever stopped working.
 *
 * NOT COVERED, and not implied by a green result: (1) the page keeps following
 * after every Run has reached a terminal status — proving that needs a Run to
 * COMPLETE first, which is a full model turn and does not fit one test's wall;
 * (2) recovery from a failed poll. Both are separate journeys.
 *
 * Engine: none. The oracle is the ledger row, which exists before any engine
 * does, and this spec sends no prompt and reads no model answer — so it runs
 * under whichever profile the matrix selected. It does require a deployment
 * whose db_backend is PostgreSQL or SQLite, since scheduled Deployments are
 * otherwise unavailable (deployment_run_runtime.py:196-211); that precondition
 * fails loudly at Create, exactly as it does for the sibling schedule spec.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi, type DeploymentRunRecord } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

// The localized surfaces this spec reads are exact: the form's labels, the
// "Runs" card heading and the "No runs yet" empty state. The console detects
// language as ['localStorage','navigator'] with fallbackLng 'en', so an unpinned
// runner locale decides which spelling appears — and a spec that accepted two
// spellings would accept a third. Pinned the way agent-per-session-sandbox does:
// a fixed navigator locale plus the persisted 'astrabox-lang' the app's own
// switch writes, which outranks navigator.
test.use({ locale: 'en-US' });

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');

/**
 * How long the cron is given to produce its first Run.
 *
 * Deliberately NOT ASTRABOX_E2E_SCHEDULE_RUN_TIMEOUT_MS, which the sibling spec
 * defaults to 150_000: that one waits for a Run to COMPLETE, and a lane that
 * tuned it globally would blow this test's wall on the tick alone. The tick is
 * at most one minute away by construction (see the alignment below), so 75s
 * covers the boundary plus DBOS's 1s scheduler poll and the enqueue.
 */
const TICK_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SCHEDULE_TICK_TIMEOUT_MS', 75_000);

/**
 * How long the untouched table is given to catch up once the backend has the
 * Run. Stated as intent: the page follows at FOLLOW_INTERVAL_MS (2s), so this
 * is an order of magnitude of headroom for a loaded host — not a cadence this
 * spec has any standing to pin.
 *
 * The waits here are sequential and their ceilings sum to 137s (22 alignment +
 * 75 tick + 15 freeze + 25 catch-up), which leaves the rest of the lane's 180s
 * wall for setup and the form. A test that reaches the wall does not fail
 * alone: the budget reporter kills the process group with it.
 */
const TABLE_CATCHUP_MS = parseTimeoutEnv('ASTRABOX_E2E_RUNS_TABLE_CATCHUP_MS', 25_000);

/**
 * Seconds that must remain in the wall-clock minute when Create is clicked, or
 * the test waits for the next boundary first.
 *
 * Unix epoch milliseconds put minute boundaries at exact multiples of 60_000
 * and a cron minute is a wall-clock minute, so seconds-into-the-minute is the
 * same arithmetic in any timezone — 'UTC' in the form below does not change it.
 * Worst case this spends ~22s of the wall to buy a guaranteed-empty mount.
 */
const MIN_TICK_HEADROOM_MS = 20_000;

/** Clear of the boundary before creating, so the tick is a whole minute away. */
const BOUNDARY_GRACE_MS = 2_000;

/** The backend must return the same Run set twice, this far apart, to be frozen. */
const FREEZE_QUIET_MS = 2_000;

/** Bound on the freeze, which covers at most one straggler tick. */
const FREEZE_BUDGET_MS = 15_000;

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';

// A retained failure keeps its records for diagnosis, but it must not keep
// firing new Runs — so the schedule is stopped on every result, pass or fail.
test.afterEach(async ({ request }) => {
  if (!deploymentId || !agentId) return;
  await new PlatformApi(request).updateDeployment(agentId, deploymentId, { enabled: false });
});
// Registered after the tracker, so sessions go before the records they ran on.
onPassOnly(async ({ request }) => {
  const platform = new PlatformApi(request);
  if (deploymentId && agentId) await platform.deleteDeployment(agentId, deploymentId);
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function sleep(ms: number): Promise<void> {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** Run ids as a stable, comparable value. */
function runIds(runs: DeploymentRunRecord[]): string[] {
  return runs.map((item) => String(item.run_id || '')).filter(Boolean).sort();
}

function sameIds(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

test("a scheduled Deployment's record page shows the Run its cron just made, without being touched", async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: a crashed React tree also stops
  // updating its table, so a red must be able to tell "never caught up" from
  // "died", and a listener added later misses the render that mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // Every GET the page issues, in order, keyed by pathname (which drops the
  // frontend's `?limit=50`). Reads the API fixtures make go through the
  // APIRequestContext, not the page, so the watch below can interrogate the
  // platform as often as it likes without disturbing this meter.
  const gets: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'GET') gets.push(new URL(req.url()).pathname);
  });
  const hits = (pathname: string) => gets.filter((seen) => seen === pathname).length;

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── setup: an Agent for the schedule to run, on the API ───────────────────
  // Arrangement, not the journey. Model resolution copies the sibling schedule
  // spec: the campaign Agent's own route, or a concrete id from the Environment
  // when that route is a wildcard.
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'the seeded Agent must name an Environment').not.toEqual('');
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((item) => item && !item.includes('*')) || 'deepseek-chat';
  }
  const agentName = `__e2e_watched_schedule_${RUN_ID}`;
  const agent = await api.createAgent({
    name: agentName,
    model,
    environment_name: environmentName,
  });
  agentId = String(agent.agent_id || '').trim();
  expect(agentId, 'the schedule needs an Agent to run').not.toEqual('');

  // ── the operator creates the schedule, on the page ────────────────────────
  const deploymentName = `Unattended schedule ${RUN_ID}`;
  await page.goto(appPath('/manage/deployments/new'));
  await page.getByLabel('Agent').selectOption({ label: agentName });
  await page.getByLabel('Trigger').selectOption('schedule');
  await page.getByLabel('Name').fill(deploymentName);
  await page.getByLabel('Prompt').fill('Reply with one short sentence.');
  await page.getByLabel('Cron expression').fill('* * * * *');
  await page.getByLabel('Timezone').fill('UTC');

  // Immediately before the click, so nothing spends the headroom it buys.
  const headroomMs = 60_000 - (Date.now() % 60_000);
  let alignedByMs = 0;
  if (headroomMs < MIN_TICK_HEADROOM_MS) {
    alignedByMs = headroomMs + BOUNDARY_GRACE_MS;
    await sleep(alignedByMs);
  }

  await page.getByRole('button', { name: 'Create' }).click();
  await page.waitForURL((url) => {
    const segment = url.pathname.split('/').filter(Boolean).pop();
    return segment !== undefined && segment !== 'new';
  });
  deploymentId = decodeURIComponent(page.url().split('/').pop() || '');
  expect(deploymentId, 'Create must navigate to the scheduled Deployment').not.toEqual('');
  const recordUrl = page.url();

  const runsPath = apiPath(
    `/admin/agents/${encodeURIComponent(agentId)}/deployments/${encodeURIComponent(deploymentId)}/runs`,
  );
  const deploymentsPath = apiPath('/admin/deployments');

  // ── the starting condition, asserted rather than assumed ──────────────────
  // Without this the alignment above is unverified and a spec that mounted on
  // an already-populated table would report success for a page that can only
  // follow a table it did not start empty.
  const runsCard = page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: 'Runs', exact: true }) });
  const runIdCells = runsCard.locator('[data-testid="console-table"] tbody tr td:first-child');
  await expect(
    runsCard.getByRole('heading', { name: 'Runs', exact: true }),
    'the record page of a schedule must carry a Runs card',
  ).toBeVisible();
  await expect(
    runIdCells,
    'the Runs table must start empty — the cron fired between Create and the first read, '
      + 'so this run of the spec cannot distinguish a page that follows its runs from one '
      + 'that merely rendered what it was handed. Check the minute alignment above and the '
      + 'clock skew between this runner and the server.',
  ).toHaveCount(0);
  await expect(
    runsCard.getByText('No runs yet', { exact: true }),
    'an empty Runs table must say so',
  ).toBeVisible();

  // Prove the meter is armed before a later reading is trusted: the Runs card
  // rendered, so `load()` has read this route at least once. A zero here means
  // this spec is counting the wrong path — most likely the console's API base
  // has diverged from ASTRABOX_E2E_APP_PREFIX — and an unarmed counter reports
  // whatever the assertions want to hear.
  const runsBaseline = hits(runsPath);
  expect(
    runsBaseline,
    `no GET ${runsPath} was seen while the record page mounted, so nothing below is measuring `
      + 'the page. Check the app prefix before reading any of it as good news.',
  ).toBeGreaterThanOrEqual(1);
  const deploymentsBaseline = hits(deploymentsPath);
  expect(deploymentsBaseline, 'the record page reads its deployment on mount').toBeGreaterThanOrEqual(1);

  // A reload clears this; an SPA remount does not, which is what the
  // deployments-GET count above covers. `page.evaluate` observes the page — it
  // neither navigates nor raises focus or visibilitychange — so stamping and
  // reading this marker is not a touch.
  const marker = `untouched-${RUN_ID}`;
  await page.evaluate((value: string) => {
    (window as unknown as Record<string, unknown>).__astraboxUntouchedMarker = value;
  }, marker);

  // The follow timer is visibility-gated (useKeepCurrent.ts:46). A hidden page
  // resting is correct behaviour, not the defect this spec is about, so a red
  // must not be able to mean that.
  expect(
    await page.evaluate(() => document.visibilityState),
    'the page must be visible for the whole watch — a hidden tab is meant to rest',
  ).toEqual('visible');

  // ── the watch: from here the page is not touched at all ───────────────────
  const watchStartedAt = Date.now();
  const tickDeadline = watchStartedAt + TICK_TIMEOUT_MS;
  let observed: DeploymentRunRecord[] = [];
  let scheduled: DeploymentRunRecord | undefined;
  while (Date.now() < tickDeadline) {
    observed = await platform.listDeploymentRuns(agentId, deploymentId);
    scheduled = observed.find((item) => String(item.trigger) === 'schedule');
    if (scheduled) break;
    await sleep(1_000);
  }
  // The one claim in this spec allowed to blame the substrate. Separating it
  // from the assertion below is what keeps "the cron never fired" from being
  // read as "the page did not notice".
  expect(
    scheduled,
    `no schedule-triggered Run was enqueued within ${TICK_TIMEOUT_MS}ms of creating a `
      + `\`* * * * *\` Deployment (aligned by ${alignedByMs}ms, so the first tick was under a `
      + 'minute away). This is a cron or substrate failure, not a console one — check that '
      + 'the deployment\'s db_backend supports schedules and that DBOS registered '
      + `${deploymentId}. Runs seen: ${JSON.stringify(observed)}`,
  ).toBeDefined();

  // ── stop the cron out of band, so the expected set can be frozen ──────────
  // Through the API and never the page's Disable button: that button awaits
  // `load()`, which refills the table itself and is exactly the side effect
  // that would make this spec unable to see what it exists to see.
  await platform.updateDeployment(agentId, deploymentId, { enabled: false });

  // A tick already enqueued when the pause landed still arrives. Freeze on two
  // reads that agree, so the expected set is the one the paused schedule leaves
  // behind rather than a snapshot taken mid-arrival.
  const freezeDeadline = Date.now() + FREEZE_BUDGET_MS;
  let expectedIds = runIds(await platform.listDeploymentRuns(agentId, deploymentId));
  while (Date.now() < freezeDeadline) {
    await sleep(FREEZE_QUIET_MS);
    const again = runIds(await platform.listDeploymentRuns(agentId, deploymentId));
    if (sameIds(expectedIds, again)) break;
    expectedIds = again;
  }
  expect(
    expectedIds.length,
    'the frozen Run set must hold the Run the cron made',
  ).toBeGreaterThanOrEqual(1);

  // The cron Run goes on to claim a real Session and a real sandbox in the
  // background. Track them BEFORE the assertions below, so a red keeps them and
  // names them in the report tail rather than leaving an unnamed box behind.
  const trackRunSessions = async () => {
    for (const item of await platform.listDeploymentRuns(agentId, deploymentId)) {
      const sessionId = String(item.session_id || '').trim();
      if (sessionId && !sessions.includes(sessionId)) sessions.push(sessionId);
    }
  };
  await trackRunSessions();

  // ── the property: the untouched table caught up on its own ────────────────
  const catchUpDeadline = Date.now() + TABLE_CATCHUP_MS;
  let rendered: string[] = [];
  for (;;) {
    rendered = (await runIdCells.allTextContents())
      .map((text) => text.trim())
      .filter(Boolean)
      .sort();
    if (sameIds(rendered, expectedIds)) break;
    if (Date.now() >= catchUpDeadline) break;
    await sleep(500);
  }

  const watchedMs = Date.now() - watchStartedAt;
  const furtherRunsGets = hits(runsPath) - runsBaseline;
  await test.info().attach('unattended-runs-table', {
    body: JSON.stringify(
      {
        agentId,
        deploymentId,
        alignedByMs,
        watchedMs,
        runsPath,
        runsBaseline,
        furtherRunsGets,
        expectedIds,
        rendered,
        deploymentsGets: hits(deploymentsPath),
        deploymentsBaseline,
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });

  // ── validity guards run BEFORE the property ───────────────────────────────
  // An invalid measurement must fail as an invalid measurement.
  expect(
    uncaught,
    'uncaught exception while the page sat untouched — a crashed tree never catches up '
      + `either:\n${uncaught.join('\n')}`,
  ).toEqual([]);
  expect(page.url(), 'the watch must not have navigated away from the record').toEqual(recordUrl);
  expect(
    await page.evaluate(() => document.visibilityState),
    'the page must have stayed visible — a hidden tab is meant to rest, and would '
      + 'fail the property below for a reason that is not a defect',
  ).toEqual('visible');

  let diagnosis = '';
  if (!sameIds(rendered, expectedIds)) {
    const now = runIds(await platform.listDeploymentRuns(agentId, deploymentId));
    diagnosis = sameIds(now, expectedIds)
      ? ''
      : ` A Run arrived after the schedule was paused and the set was frozen (backend now `
        + `holds ${now.length}: ${now.join(', ')}), so the expected set may be stale — widen `
        + 'FREEZE_BUDGET_MS before reading this as a console defect.';
  }

  expect(
    rendered,
    `the Runs table did not show what the ledger holds. The page was never touched after `
      + `the record loaded: it issued ${furtherRunsGets} further GET(s) to ${runsPath} during `
      + `${watchedMs}ms while ${expectedIds.length} Run(s) existed, and ${rendered.length} `
      + `row(s) are on screen. A schedule's Runs are written by the cron, not by this page, `
      + 'so the table must follow them for as long as the record is open — check that '
      + "`follow` is still gated on the record being a schedule rather than on the runs it "
      + 'already loaded (DeploymentDetailPage.tsx:151-160).'
      + diagnosis,
  ).toEqual(expectedIds);

  await expect(
    runsCard.getByText('No runs yet', { exact: true }),
    'the empty state must be gone: it tells the reader Runs appear after the first '
      + 'invocation, and the first invocation has happened',
  ).toHaveCount(0);

  // ── it was unattended ─────────────────────────────────────────────────────
  // `/admin/deployments` is read only by this page's `load()` and the list page
  // (api.ts:1336 has two callers), and `load()` runs on mount and after
  // Enable/Disable/Save. An unchanged count therefore rules out a reload, a
  // remount and every button that would have refilled the table — and stops a
  // future editor from "fixing" a red here with a page.reload().
  expect(
    hits(deploymentsPath),
    'the table caught up only if nothing re-read the deployment: a reload, a remount or '
      + 'an Enable/Disable/Save would all show as further GETs here',
  ).toEqual(deploymentsBaseline);
  expect(
    await page.evaluate(() => {
      const held = (window as unknown as Record<string, unknown>).__astraboxUntouchedMarker;
      return typeof held === 'string' ? held : null;
    }),
    'the document must be the one the watch started on — a reload loses this marker',
  ).toEqual(marker);

  // Once more at the end: a Run is listable as QUEUED before it binds a
  // Session, so the read above can have been too early. A Session created after
  // THIS read would be a leftover — bounded to one Run's worth, because the
  // schedule was paused before a second tick could fire.
  await trackRunSessions();
});
