/**
 * E2E: a conversation created moments before a deploy reaches a verdict.
 *
 * The journey is ordinary. A user clicks the start action on an Agent card, the
 * console opens the conversation and shows Creating, and the deploy lands one
 * moment later: SIGTERM cancels the in-flight startup task, which re-raises the
 * cancellation without writing a terminal state
 * (session_kernel/workers/lifecycle/startup.py:100,459). When the deploy
 * finishes, the user comes back to the conversation from the rail.
 *
 * What the conversation owes that user is a verdict, and either one will do:
 * usable again — READY, and a typed message is delivered — or visibly over —
 * TERMINATED, with the reason on the page and a composer that refuses rather
 * than swallowing the click. A spinner that never resolves is neither.
 *
 * The only code that converges a CREATING row is the boot sweep
 * (bootstrap_reconciler.py:91-121), which runs once per process and skips any
 * row touched inside STARTUP_ALLOCATION_GRACE_SECONDS — five minutes,
 * bootstrap_reconciler.py:25,168-189. A row abandoned by a restart is inside
 * that window at the boot that follows it, and the periodic watcher only
 * releases the startup sandbox, leaving the row's state alone
 * (expiration_watcher.py:136-150), so the boot that skipped the row is the last
 * pass that ever looks at it. The user has no self-rescue either: recovery
 * reads the row with reject_creating=True
 * (session_kernel/service_mixins/lifecycle.py:323), so the Recover control
 * answers SESSION_BUSY on a CREATING row. The verdict has to come from the
 * platform.
 *
 * The deploy is made to land inside the creating window instead of being raced
 * against a cold start: the create POST is held at the browser edge until the
 * server container has stopped, so the row exists and its owner is gone
 * milliseconds apart. The Agent runs with prewarming disabled
 * (astraApi.createColdTestAgent) because a prepared slot would claim in tens of
 * milliseconds and reach READY before any restart could land on it.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { sessionDoc } from '../fixtures/dbOracle';
import { absoluteBaseUrl, apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// How long the conversation may keep saying Creating after the user is back on
// its page. Tunable per deployment; a slow restart is told apart from the real
// defect by the attached phase timings rather than by raising this.
const VERDICT_MS = parseTimeoutEnv('ASTRABOX_E2E_ABANDONED_CREATE_VERDICT_MS', 60_000);
// The deploy itself: container start plus a real /healthz.
const REDEPLOY_MS = parseTimeoutEnv('ASTRABOX_E2E_ABANDONED_CREATE_REDEPLOY_MS', 90_000);
// Creating the conversation and taking the process down: one create round trip
// plus however long the container takes to answer SIGTERM, held in one wait
// because the console does not navigate until the held response is released.
const CREATE_AND_STOP_MS = parseTimeoutEnv('ASTRABOX_E2E_ABANDONED_CREATE_STOP_MS', 60_000);
// Coming back to the conversation: the rail lists it, the page opens.
const RETURN_MS = parseTimeoutEnv('ASTRABOX_E2E_ABANDONED_CREATE_RETURN_MS', 30_000);
// The startup sandbox is released by the periodic sweep, so the coherence read
// is given a few of its passes rather than a single instant.
const ALLOCATION_RELEASE_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_ABANDONED_CREATE_ALLOCATION_MS',
  30_000,
);
// bootstrap_reconciler.STARTUP_ALLOCATION_GRACE_SECONDS, restated here because
// the precondition this spec establishes is exactly the condition that constant
// governs: a CREATING row younger than this is the row the boot sweep skips.
const STARTUP_GRACE_MS = 5 * 60_000;

function docker(args: string[], timeoutMs = 60_000): string {
  return execFileSync('docker', args, {
    encoding: 'utf8',
    timeout: timeoutMs,
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

/**
 * The instant the conversation row was last touched, in epoch milliseconds.
 *
 * Reads the same two keys, in the same order of preference, that the boot
 * sweep ages the row by (bootstrap_reconciler.py:168-189). The offset check is
 * not decoration: a stamp without one is read as local time by `Date.parse`,
 * which would shift the age by whole hours and answer the precondition with a
 * number nobody measured.
 */
function latestTouchMs(row: Record<string, unknown>): number {
  const stamps: number[] = [];
  for (const key of ['updated_at', 'created_at']) {
    const raw = String(row[key] ?? '').trim();
    if (!raw) continue;
    expect(
      /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw),
      `${key} must carry an explicit UTC offset to be comparable: ${JSON.stringify(raw)}`,
    ).toBe(true);
    const parsed = Date.parse(raw);
    expect(Number.isFinite(parsed), `${key} must be a parseable instant: ${JSON.stringify(raw)}`).toBe(true);
    stamps.push(parsed);
  }
  expect(stamps.length, 'the conversation row must carry an instant the startup sweep can age it by')
    .toBeGreaterThan(0);
  return Math.max(...stamps);
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a conversation created moments before a deploy reaches a verdict instead of a permanent creating spinner', async ({
  page,
  request,
}) => {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  // The budget being judged has to be proven to contain the background passes it
  // is judging, not assumed to: a verdict poll shorter than one scan interval
  // would report the scheduler's period as a product defect.
  const scanInterval = Number(docker(['exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS'], 30_000));
  expect(scanInterval, 'the deployment must configure a positive background scan interval')
    .toBeGreaterThan(0);
  expect(
    scanInterval * 1_000,
    'at least two real background passes must fit inside the verdict budget',
  ).toBeLessThan(VERDICT_MS / 2);

  const api = new AstraApi(request);
  const agentName = `__e2e_deploy_during_create_${Date.now()}_${test.info().workerIndex}`;
  const agent = await api.createColdTestAgent(agentName);
  agentId = agent.agent_id;
  expect(agentId, 'the cold Agent must have an id').not.toEqual('');

  const evidence: Record<string, unknown> = { agentId, agentName, scanInterval };
  const timings: Record<string, number> = {};
  let sessionId = '';
  let stopped = false;

  try {
    // The create POST is held at the browser edge. The real server has already
    // committed the conversation row and spawned its startup worker by the time
    // `route.fetch()` returns, so stopping the container here puts the deploy
    // inside the creating window instead of racing an 8-17s cold start.
    //
    // `docker stop` blocks this process until the container exits, and nothing
    // else needs the loop while it does: the page is idle awaiting exactly the
    // response this handler still owns.
    await page.route(
      `**${apiPath(`/agents/${agentId}/conversations`)}`,
      async (route) => {
        if (route.request().method() !== 'POST') {
          await route.continue();
          return;
        }
        const response = await route.fetch();
        const payload = JSON.parse(await response.text()) as Record<string, unknown>;
        const created = (payload.data ?? payload) as { session_id?: string };
        sessionId = String(created.session_id || '').trim();
        expect(sessionId, 'the create must commit a conversation row before the deploy lands')
          .not.toEqual('');
        sessions.push(sessionId);
        const stopBeganAt = Date.now();
        docker(['stop', '--time', '10', server]);
        stopped = true;
        timings.stopMs = Date.now() - stopBeganAt;
        await route.fulfill({ response });
      },
      { times: 1 },
    );

    // The user's own action creates the conversation; the route above is only
    // what makes the deploy land on it.
    const clickedAt = Date.now();
    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
    await expect(card, 'the Agent must be offered in the picker').toBeVisible({ timeout: RETURN_MS });
    await card.getByRole('button').click();
    await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
      timeout: CREATE_AND_STOP_MS,
    });
    timings.createAndStopMs = Date.now() - clickedAt;
    expect(
      new URL(page.url()).pathname.endsWith(`/sessions/${sessionId}`),
      'the console must open the conversation the server committed',
    ).toBe(true);

    // The precondition, read from the document store while the process is down.
    // The oracle runs psql inside the database container, so it answers with no
    // server at all.
    expect(
      docker(['inspect', '--format', '{{.State.Running}}', server], 30_000),
      'the deploy must have taken the process down',
    ).toBe('false');
    const before = sessionDoc(sessionId);
    expect(before, 'the conversation row must exist while its owner is gone').not.toBeNull();
    const abandoned = before as Record<string, unknown>;
    evidence.rowWhileDown = abandoned;
    expect(
      String(abandoned.state ?? ''),
      'the deploy must have landed inside the creating window, not after it',
    ).toBe('CREATING');
    const age = Date.now() - latestTouchMs(abandoned);
    evidence.rowAgeMsWhileDown = age;
    expect(
      age,
      'the abandoned row must be inside the startup grace, which is the exact condition the boot sweep skips',
    ).toBeLessThan(STARTUP_GRACE_MS);
    evidence.startupAllocationWhileDown = abandoned.startup_allocation ?? null;

    // The deploy completes.
    const redeployStartedAt = Date.now();
    await restartServerContainer(absoluteBaseUrl(), REDEPLOY_MS, server);
    stopped = false;
    timings.redeployMs = Date.now() - redeployStartedAt;

    // The user comes back — a fresh arrival rather than a stale tab, which is
    // also what drives the lazy bootstrap under the user's own traffic.
    const returnedAt = Date.now();
    await page.goto(appPath('/'), { waitUntil: 'domcontentloaded' });
    const railRow = page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`);
    await expect(railRow, 'the conversation must still be listed after the deploy')
      .toBeVisible({ timeout: RETURN_MS });
    await railRow.getByRole('link').click();
    await page.waitForURL((url) => url.pathname.endsWith(`/sessions/${sessionId}`), { timeout: RETURN_MS });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: RETURN_MS });
    timings.returnMs = Date.now() - returnedAt;

    // The verdict, from the seat. Scoped to the conversation header: every rail
    // row renders a status pill of its own, and the rail precedes the content.
    const header = page.getByTestId('run-view').locator('header').first();
    const pill = header.getByTestId('status-pill');
    await expect(pill, 'the conversation header must carry a status').toBeVisible({ timeout: RETURN_MS });
    const verdictStartedAt = Date.now();
    await expect(
      pill,
      'a conversation abandoned by a deploy must stop saying Creating once the user is back on it',
    ).not.toHaveAttribute('data-state', 'CREATING', { timeout: VERDICT_MS });
    timings.verdictMs = Date.now() - verdictStartedAt;

    // The page and the durable row must agree, so a page that merely looks
    // settled is not accepted as a verdict.
    await expect
      .poll(() => String(sessionDoc(sessionId)?.state ?? ''), {
        timeout: RETURN_MS,
        message: 'the conversation row must carry the same verdict the page shows',
      })
      .not.toBe('CREATING');
    const settled = String(sessionDoc(sessionId)?.state ?? '');
    evidence.settledState = settled;
    evidence.rowAfterVerdict = sessionDoc(sessionId);
    expect(
      ['READY', 'TERMINATED'],
      `a conversation abandoned by a deploy must settle usable or over, and this one settled ${JSON.stringify(settled)}`,
    ).toContain(settled);

    // A verdict the user can act on, which is a different claim from a verdict
    // the row records.
    if (settled === 'READY') {
      const prompt = `Say READY-AFTER-DEPLOY and nothing else. ${sessionId}`;
      const delivered = await sendPrompt(page, sessionId, prompt);
      evidence.turnInputStatus = delivered.status();
      const queued = page.getByTestId('composer-queue').filter({ hasText: prompt });
      const sent = page.getByTestId('user-message').filter({ hasText: prompt });
      await expect(
        queued.or(sent).first(),
        'a conversation that came back usable must keep the message the user typed',
      ).toBeVisible({ timeout: RETURN_MS });
    } else {
      await expect(pill, 'a conversation that is over must read terminated')
        .toHaveAttribute('data-state', 'TERMINATED', { timeout: RETURN_MS });
      const reason = header.locator('[data-slot="verbatim"]');
      await expect(reason, 'a conversation that is over must say why on the page')
        .toBeVisible({ timeout: RETURN_MS });
      await expect(reason, 'the reason line must carry the deployment\'s own words').not.toBeEmpty();
      expect(
        String(sessionDoc(sessionId)?.last_error ?? '').trim(),
        'the durable row must name the reason the page is showing',
      ).not.toEqual('');
      await expect(
        page.getByTestId('composer-prompt'),
        'the composer must refuse a message it cannot deliver rather than accept a click that goes nowhere',
      ).toBeDisabled({ timeout: RETURN_MS });
    }

    // A row that has reached a verdict must not still point at a box nobody
    // will adopt. The release is the periodic sweep's work, so it is given a few
    // of its passes rather than one instant.
    await expect
      .poll(() => sessionDoc(sessionId)?.startup_allocation ?? null, {
        timeout: ALLOCATION_RELEASE_MS,
        message: 'a settled conversation must not still hold the sandbox its startup allocated',
      })
      .toBeNull();
    evidence.startupAllocationAfterVerdict = sessionDoc(sessionId)?.startup_allocation ?? null;
  } finally {
    try {
      if (stopped) docker(['start', server], 30_000);
    } finally {
      try {
        evidence.serverRunningAtCleanup = docker(
          ['inspect', '--format', '{{.State.Running}}', server],
          30_000,
        );
      } catch (error) {
        evidence.serverRunningAtCleanup = `unavailable: ${String(error)}`;
      }
      await test.info().attach('deploy-during-conversation-create', {
        body: JSON.stringify({ sessionId, timings, ...evidence }, null, 2),
        contentType: 'application/json',
      });
    }
  }
});
