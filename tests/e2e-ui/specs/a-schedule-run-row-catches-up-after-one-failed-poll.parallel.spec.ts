/**
 * E2E: a schedule record page catches up on its own after one failed poll.
 *
 * The journey is the operator's most passive one. Open a schedule Deployment's
 * record while its Run is RUNNING, let exactly one of the page's `runs` reads
 * come back 503 — the answer a rolling restart or a transient persistence blip
 * gives — and then touch nothing. The Run finishes in the platform; the row on
 * screen must reach COMPLETED and stop breathing without a click, a keystroke,
 * a reload or a navigation.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. What is under test is the re-arming
 * behaviour of a timer whose input is an HTTP failure, read through the pill a
 * person actually looks at. A jsdom test can assert that `reloadRuns` catches;
 * it cannot say whether anything asks again afterwards, because the thing that
 * asks again is a `setInterval` in a different module (`useKeepCurrent`) and
 * the thing that stops being true is the operator's belief about a finished
 * Run. The only honest meter for "the page caught up by itself" is a real
 * browser holding a real Run that a second, independent reader has already
 * seen finish.
 *
 * WHAT IT GUARDS. The re-read is an unconditional 2s interval
 * (frontend/src/hooks/useKeepCurrent.ts:73-79) wired at
 * DeploymentDetailPage.tsx:160 and armed by the record being a schedule, not by
 * the runs it already holds; the poll's own catch keeps the loaded rows and
 * records only the message (DeploymentDetailPage.tsx:156). A 503 therefore
 * costs one reading and not the timer. The shape that breaks the property is a
 * self-rescheduling `setTimeout` chain whose only re-arming path is a
 * successful read: one failed poll ends it and the row shows RUNNING for the
 * life of the page. This spec is expected GREEN, and it is the only
 * browser-level coverage of this poll at all, success branch included —
 * scheduled-deployment-runs-and-replays reads Run status from its own API loop
 * and never reads a status cell.
 *
 * NOT ASSERTED, deliberately: that the page shows an error affordance for the
 * failed poll. A fix that retries silently and one that shows the note and then
 * clears it are both correct. Only "the row never catches up" is the defect.
 *
 * The Run's prompt asks for one word and no tool, so nothing engine-specific is
 * read — no tool card, no permission prompt, no vendor vocabulary — and the
 * spec carries no engine requirement. The only text asserted is the platform's
 * verbatim Run status (`children={item.status}`), not a translated string, so
 * it needs no locale pin either.
 */
import { expect, test, type Route } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi, type DeploymentRunRecord } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');

/**
 * How long the Run may take to reach RUNNING on screen.
 *
 * It covers the enqueue, the DBOS dequeue, the Session start on the campaign
 * Agent's warm box, and this page's own mount reads.
 */
const RUN_START_MS = parseTimeoutEnv('ASTRABOX_E2E_DEPLOYMENT_RUN_START_MS', 45_000);

/**
 * How long the Run itself may take to settle, measured off the API, not the
 * page — and counted from a Run this spec has already watched reach RUNNING.
 * The prompt asks for one word and forbids tools, so what remains is one model
 * call and the workflow's own settle.
 */
const RUN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_DEPLOYMENT_RUN_TIMEOUT_MS', 60_000);

/**
 * How long the page gets to catch up after the platform has already settled.
 *
 * Roughly ten of the follow interval's 2s ticks — generous for one re-read, and
 * far short of "forever", which is what a dead timer costs. The four ceilings
 * are sequential and sum to 140s (45 + 15 + 60 + 20), which leaves the rest of
 * the lane's 180s wall for setup: a test that reaches the wall does not fail
 * alone, the budget reporter kills the process group and the lane's remaining
 * tests die with it.
 */
const CATCHUP_MS = parseTimeoutEnv('ASTRABOX_E2E_DEPLOYMENT_RUN_CATCHUP_MS', 20_000);

/** How long to wait for the armed fault to actually be served to the browser. */
const FAULT_SERVED_MS = parseTimeoutEnv('ASTRABOX_E2E_DEPLOYMENT_RUN_FAULT_MS', 15_000);

/**
 * The product's own 503 envelope, not an invented one.
 *
 * `APIError.to_response_payload()` writes `{code, message, data, error}` with
 * the registry row for PERSISTENCE_UNAVAILABLE at
 * astrabox/common/utils/errors.py:285 supplying status/category/retryable/owner.
 * The console's `send()` reads `error.user_message` to build what the page then
 * displays, so a hand-shaped body would exercise a different branch than a real
 * persistence outage does.
 */
const PERSISTENCE_UNAVAILABLE_503 = {
  code: 'PERSISTENCE_UNAVAILABLE',
  message: 'the Run ledger is temporarily unavailable',
  data: null,
  error: {
    code: 'PERSISTENCE_UNAVAILABLE',
    status_code: 503,
    category: 'persistence',
    retryable: true,
    owner: 'mongo',
    user_message: 'the Run ledger is temporarily unavailable',
  },
};

const TERMINAL_RUN_STATUS = /^(COMPLETED|FAILED|CANCELLED)$/;

// Sessions the Run creates are deleted only when the test passes; a failure
// keeps the box and names it in the report tail — see fixtures/sessionCleanup.
const sessions = trackSessions();
let agentId = '';
let deploymentId = '';

// A retained failure keeps its Deployment row for reading, but it must never be
// able to fire. The `0 3 1 1 *` cron cannot come round inside a test run; this
// teardown also closes the window between create and the disable in the body.
test.afterEach(async ({ request }) => {
  if (!agentId || !deploymentId) return;
  await new PlatformApi(request)
    .updateDeployment(agentId, deploymentId, { enabled: false })
    .catch(() => {});
});
// Registered after the tracker, so the Run's session is removed before the
// Deployment that produced it. No Agent is created here, so none is deleted.
onPassOnly(async ({ request }) => {
  if (agentId && deploymentId) {
    await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  }
});

test('a RUNNING Run row on an open schedule page reaches COMPLETED after one failed poll', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  // ── setup: one schedule Deployment on the campaign Agent ──────────────────
  // Bound to defaultAgent() on purpose. The campaign Agent's Agent-tenancy box
  // is already warm, and that is what keeps a real model Run inside the lane's
  // wall; a cold Agent would spend the budget on provisioning. This spec claims
  // no sole occupancy, installs no box-level fault and destroys no sandbox, so
  // it does not need a box of its own.
  const agent = await api.defaultAgent();
  agentId = String(agent.agent_id || '').trim();
  expect(agentId, 'the campaign Agent must have an id').not.toEqual('');

  const deployment = await platform.createDeployment(agentId, {
    scene: 'schedule',
    name: `__e2e_runs_poll_${RUN_ID}`,
    prompt_prefix: 'Reply with the single word ok. Do not use tools.',
    // 03:00 on the first of January: the scheduler can never add a second Run
    // while this page is open, so "exactly one row" is a property of the setup
    // rather than of timing.
    schedule: { cron: '0 3 1 1 *', timezone: 'UTC' },
  });
  deploymentId = String(deployment.deployment_id || '').trim();
  expect(deploymentId, 'the schedule Deployment must have an id').not.toEqual('');

  // Disabled before anything is triggered. `trigger_now` deliberately ignores
  // `enabled` (deployment_service.py:916-922 takes only the schedule check, and
  // start_run_session refuses a disabled Deployment only for the `schedule`
  // trigger), so the manual Run below still starts.
  await platform.updateDeployment(agentId, deploymentId, { enabled: false });

  // ── the fault, installed before navigation and disarmed ───────────────────
  // A URL predicate rather than a glob, built from apiPath so a deployment
  // behind ASTRABOX_E2E_APP_PREFIX still matches. Only GETs are touched: this
  // path also takes the trigger POST, and a Replay button sits in every row.
  const runsPath = apiPath(`/admin/agents/${agentId}/deployments/${deploymentId}/runs`);
  let armed = false;
  let servedReads = 0;
  let faulted = 0;
  let readsAfterFault = 0;
  await page.route(
    (url) => url.pathname === runsPath,
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.continue();
      servedReads += 1;
      if (armed && faulted === 0) {
        faulted += 1;
        // A fulfilled 503, not route.abort(). fetchWithNetworkRecovery replays
        // a THROWN safe read twice at 100ms/300ms and returns HTTP responses as
        // received (networkRecovery.ts:48-68), so an abort would be absorbed by
        // the transport and never reach the page as a failed poll at all.
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify(PERSISTENCE_UNAVAILABLE_503),
        });
      }
      if (faulted > 0) readsAfterFault += 1;
      return route.continue();
    },
  );

  // ── the Run exists before the page opens ──────────────────────────────────
  // Not clicked from the page: "Run now" and "Replay" each re-read the runs
  // themselves, and the journey under test is the page that was merely opened
  // and watched.
  const triggered = await platform.triggerDeploymentRun(agentId, deploymentId);
  const runId = String(triggered.run_id || '').trim();
  expect(runId, 'Run now must mint an invocation id').not.toEqual('');
  expect(String(triggered.trigger || ''), 'a triggered Run is a manual one').toEqual('manual');

  await page.goto(appPath(`/manage/deployments/${deploymentId}`), {
    waitUntil: 'domcontentloaded',
  });

  // The runs table is the only table this record renders; the header's
  // enabled/disabled pill is outside it, which is why the pill is scoped here.
  const table = page.getByTestId('console-table');
  const status = table.getByTestId('status-pill');
  const runsCard = page.locator('[data-slot="card"]').filter({ has: table });

  // ── the precondition, established on the page and not assumed ─────────────
  await expect(table, 'the schedule record renders exactly one table').toHaveCount(1, {
    timeout: RUN_START_MS,
  });
  await expect(status, 'the runs table holds exactly one Run row').toHaveCount(1, {
    timeout: RUN_START_MS,
  });
  await expect(
    table,
    'the row on screen must be the Run this test triggered, not some other invocation',
  ).toContainText(runId);
  await expect(
    status,
    'the Run must be observed RUNNING before a failed poll can be said to have frozen it',
  ).toHaveText('RUNNING', { timeout: RUN_START_MS });
  await expect(
    status,
    'a RUNNING Run breathes — this is the dot the operator reads as "still working"',
  ).toHaveAttribute('data-pulse', 'true');

  // ── arm, and prove the browser was actually served a failure ──────────────
  // Before trusting anything the interception reports, prove it intercepts at
  // all. A predicate that never matches reports a perfectly quiet page forever;
  // the likeliest cause is the console's API base having diverged from
  // ASTRABOX_E2E_APP_PREFIX, which no assertion below would notice.
  expect(
    servedReads,
    `no GET ${runsPath} reached this route handler while the record loaded, so this spec `
      + 'is not intercepting anything. Check the app prefix before reading anything below '
      + 'as evidence.',
  ).toBeGreaterThanOrEqual(1);

  armed = true;
  await expect
    .poll(() => faulted, {
      timeout: FAULT_SERVED_MS,
      message:
        'the armed 503 was never served, so no failed poll happened and the rest of this '
        + 'test would prove nothing. The page follows a schedule every 2s '
        + '(useKeepCurrent.FOLLOW_INTERVAL_MS) while the tab is visible.',
    })
    .toBe(1);
  // Installed before goto and armed only after RUNNING was on screen, so the
  // faulted read is provably a poll and never the mount read.
  expect(faulted, 'exactly one poll may fail — this is a single-blip journey').toBe(1);

  // ── the independent oracle: the Run really finished ───────────────────────
  // Read from the test's own request context. PlatformApi never goes through
  // page.route, so this reader is untouched by the injected fault and the two
  // halves of the claim cannot fail for the same reason.
  let observed: DeploymentRunRecord | undefined;
  await expect
    .poll(
      async () => {
        const rows = await platform.listDeploymentRuns(agentId, deploymentId);
        observed = rows.find((row) => String(row.run_id || '') === runId);
        const sessionId = String(observed?.session_id || '').trim();
        if (sessionId && !sessions.includes(sessionId)) sessions.push(sessionId);
        return String(observed?.status || 'ABSENT');
      },
      {
        timeout: RUN_TIMEOUT_MS,
        intervals: [1_000],
        message:
          `Run ${runId} never reached a terminal status. This is the environment failing, `
          + 'not the page: nothing below has been asked yet.',
      },
    )
    .toMatch(TERMINAL_RUN_STATUS);
  expect(
    String(observed?.status || ''),
    `Run ${runId} settled ${String(observed?.status || '')} rather than COMPLETED `
      + `(error=${JSON.stringify(observed?.error ?? null)}, session=${String(observed?.session_id || '')}). `
      + 'That is a broken Run, which this spec cannot tell apart from a frozen page — '
      + 'fix the Run before reading this result as a console defect.',
  ).toBe('COMPLETED');

  // ── the claim ─────────────────────────────────────────────────────────────
  // No click, keystroke, reload or navigation has happened since the page was
  // opened. The platform has settled; the row must say so by itself.
  try {
    await expect(
      status,
      `the Run finished and the page never caught up. One poll was answered 503 and the `
        + 'row must still reach COMPLETED on its own. If the page stopped polling entirely '
        + 'the follow interval is not re-arming after a failed read (useKeepCurrent.ts:73-79); '
        + 'if it kept polling and still shows the wrong status, that is a different bug. '
        + 'The attached diagnostic says which.',
    ).toHaveText('COMPLETED', { timeout: CATCHUP_MS });
  } finally {
    await test.info().attach('runs-poll-after-fault', {
      body: JSON.stringify(
        {
          runId,
          deploymentId,
          agentId,
          runsPath,
          servedReads,
          faulted,
          // 0 means the page asked nothing after its failed poll: the timer
          // died. A positive count with a red result above means it kept asking
          // and rendered the wrong answer.
          readsAfterFault,
          runStatusFromApi: String(observed?.status || ''),
          sessionId: String(observed?.session_id || ''),
          catchupBudgetMs: CATCHUP_MS,
        },
        null,
        2,
      ),
      contentType: 'application/json',
    });
  }

  await expect(
    status,
    'a settled Run must stop breathing — a live dot on a finished Run is the same lie',
  ).toHaveAttribute('data-pulse', 'false');
  await expect(
    status,
    'the failed poll must not have duplicated, dropped or emptied the row',
  ).toHaveCount(1);
  await expect(
    runsCard.getByRole('alert'),
    'no stale read-failure banner may be left standing beside a COMPLETED row',
  ).toHaveCount(0);
});
