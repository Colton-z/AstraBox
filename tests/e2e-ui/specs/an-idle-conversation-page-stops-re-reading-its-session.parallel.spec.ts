/**
 * E2E: a conversation page left open with nothing in flight comes to rest.
 *
 * The journey is the ordinary one — open a conversation, stop typing, leave the
 * tab open — and the property is that the console stops asking. With no turn,
 * no pending interaction and an unsent draft, GET /api/v1/sessions/{id} must not
 * keep firing at the live cadence for as long as the tab lives.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. The predicate the poll waits for is
 * `agent_runtime.sandbox_id` (sessionPolling.ts:50). That field is sourced from
 * the AGENT document (session_read.py:259-289), and its own comment says
 * conversation tenancy leaves it empty — conversation tenancy being the product
 * default (environment_schema.py:201-202). So on a default deployment the quiesce
 * condition is one a READY conversation can never satisfy, and only a real
 * deployment can show that. The sibling helper in the same frontend directory
 * already states the opposite as settled fact and reads the SESSION's own
 * sandbox_id (utils/format.ts:143-154) — two helpers disagreeing about one field,
 * and only the untested one is wrong.
 *
 * WHY IT MUST NOT USE api.defaultAgent() — READ BEFORE EDITING. Every campaign
 * profile in agent-engine-matrix.json declares sandbox_tenancy "agent". Under
 * Agent tenancy the shared lease keeps the box pointer ON the Agent row
 * (shared_sandbox_lease.py:834 reads it, :950-1072 write it), so the runtime view
 * DOES carry a sandbox_id, hasReadyAgentRuntime() is true, the poll quiesces and
 * this spec would pass while the product default stayed broken. Swapping in the
 * campaign Agent here does not weaken the spec — it inverts it, certifying the
 * one deployment shape the defect cannot reach. The cold Agent below is the
 * subject of the test, not a convenience.
 *
 * The spec runs zero model turns, so it carries no engine requirement and works
 * under whichever profile the matrix selected.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';

// Sessions created here are deleted only when the test passes. A failure keeps
// the box and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, so the session is removed before the Agent it
// ran on — the order a-sandbox-record-reports-the-box-not-an-empty-card uses.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/**
 * How long the page is left alone, bounded on both sides.
 *
 * It must span enough of the live 1500ms ticks to be a rate measurement rather
 * than an anecdote — 30s is twenty of them, and the guard below refuses a tuned
 * window shorter than fifteen. It must also stay well inside the lane's 180s
 * wall, which a cold sandbox provision has already spent from. Idiom copied from
 * WAITING_IDLE_MS in pending-interaction-durable-after-tool-input.
 */
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_DETAIL_POLL_WINDOW_MS', 30_000);

/** The live cadence this spec exists to distinguish itself from. */
const LIVE_POLL_INTERVAL_MS = 1_500;

/**
 * The slowest an idle page may re-read its session.
 *
 * Stated as intent, not as a magic count: 7.5s sits 5x above the live cadence
 * and 25% below a 10s heartbeat, so this passes BOTH candidate designs — full
 * quiescence and a slow heartbeat — while failing the live 1.5s cadence and any
 * mere back-off to 5s. It deliberately does not assert "the count stops growing",
 * because that would pre-decide a design question this spec has no standing to
 * settle.
 */
const MIN_IDLE_PERIOD_MS = 7_500;

/** Ticks the window must be able to contain, or it measures nothing. */
const MIN_TICKS_MEASURED = 15;
if (IDLE_WINDOW_MS < MIN_TICKS_MEASURED * LIVE_POLL_INTERVAL_MS) {
  throw new Error(
    `ASTRABOX_E2E_IDLE_DETAIL_POLL_WINDOW_MS=${IDLE_WINDOW_MS} is too short to be a rate `
      + `measurement. The idle poll re-arms every ${LIVE_POLL_INTERVAL_MS}ms `
      + '(useSessionLifecycle.ts:274), so a window under '
      + `${MIN_TICKS_MEASURED * LIVE_POLL_INTERVAL_MS}ms can read as quiet on timing alone.`,
  );
}

/** Let the mount's own reads land before the window opens. */
const MOUNT_SETTLE_MS = 3_000;

test('an idle conversation page rests instead of re-reading its session every 1.5s', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation. A crashed React tree also stops
  // issuing requests, so the count assertion needs to be able to tell "came to
  // rest" from "died", and a listener added later misses the render that
  // mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // ── setup: one conversation-tenancy Agent, zero model turns ───────────────
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_idle_poll_${new Date().toISOString().replace(/[:.]/g, '-')}`,
  );
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');

  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);

  // The finding stated as data, asserted rather than assumed: the box exists,
  // it just does not live on the record the polling predicate reads.
  // waitForSessionReady already required state READY and a non-empty session
  // sandbox_id, so the platform itself has said this conversation has a box.
  expect(
    String(ready.sandbox_id || '').trim(),
    'a READY conversation names the box it claimed',
  ).not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation-tenancy Agent must not own a shared sandbox — if this is non-empty the '
      + 'Environment behind ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT is not conversation '
      + 'tenancy, and everything below would measure the deployment shape the defect misses',
  ).toEqual('');

  // ── the meter: every session-detail GET the PAGE issues ───────────────────
  // Exact pathname equality, which excludes /history-blocks, /ai-stream and
  // /turn-inputs. getSession() has exactly one caller in the app
  // (useSessionLifecycle.refresh), so every hit here is a lifecycle read.
  // Reads the AstraApi fixture makes go through the APIRequestContext, not the
  // page, and are not counted — which is what lets the guards below re-ask the
  // platform mid-measurement without disturbing it.
  const detailPath = apiPath(`/sessions/${sessionId}`);
  const hits: number[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET') return;
    if (new URL(req.url()).pathname === detailPath) hits.push(Date.now());
  });

  // ── the journey: open it, leave a draft, stop ─────────────────────────────
  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);

  // "Stop typing" literally: text in the composer that is never submitted. It
  // is the journey's own step, and it settles the competing reading that an
  // unsent draft is what keeps the page busy.
  const draft = `DRAFT_${Date.now()}: left unsent while the page sits idle.`;
  const composer = page.getByTestId('composer-prompt');
  await composer.fill(draft);
  await expect(composer).toHaveValue(draft);

  // ── the platform agrees nothing is in flight ──────────────────────────────
  const beforeWindow = await api.getSession(sessionId);
  expect(String(beforeWindow.state || ''), 'the conversation is READY before measuring').toEqual(
    'READY',
  );
  expect(
    String(beforeWindow.current_turn_id || '').trim(),
    'no turn may be in flight before measuring',
  ).toEqual('');
  expect(
    beforeWindow.pending_interaction ?? null,
    'no interaction may be pending before measuring',
  ).toBeNull();
  // Not decoration: shouldPollOverlayTruthGap (sessionPolling.ts:112-124) drives
  // a SECOND 1.5s read of this same endpoint whenever an overlay turn_id is
  // present. Ruling it out is what leaves a red result exactly one cause.
  expect(
    (await api.getMessages(sessionId)).active_turn_overlay ?? null,
    'no active-turn overlay may be present before measuring',
  ).toBeNull();

  // ── measure ───────────────────────────────────────────────────────────────
  await page.waitForTimeout(MOUNT_SETTLE_MS);
  const before = hits.length;
  // Prove the meter works before trusting a low reading from it. Mounting the
  // view always reads the session at least once, so a zero here means this spec
  // is counting the wrong path — and an unarmed counter reports perfect quiet
  // forever. The likely cause is the console's API base having diverged from
  // ASTRABOX_E2E_APP_PREFIX, which no assertion below would notice.
  expect(
    before,
    `no GET ${detailPath} was seen while the view mounted, so this spec is not measuring `
      + 'anything. Check the app prefix before reading the idle count as good news.',
  ).toBeGreaterThanOrEqual(1);
  const startedAt = Date.now();
  await page.waitForTimeout(IDLE_WINDOW_MS);
  const observed = hits.length - before;
  // The TRUE elapsed, not the nominal window: the allowance is a rate, and a
  // loaded host that overshoots must not be charged for the overshoot.
  const elapsedMs = Date.now() - startedAt;
  const gapsMs = hits
    .slice(before)
    .map((at, index, seen) => (index === 0 ? at - startedAt : at - seen[index - 1]));

  await test.info().attach('idle-session-detail-reads', {
    body: JSON.stringify(
      {
        sessionId,
        agentId,
        detailPath,
        observed,
        elapsedMs,
        meanPeriodMs: observed > 0 ? Math.round(elapsedMs / observed) : null,
        gapsMs,
        liveCadenceMs: LIVE_POLL_INTERVAL_MS,
        minIdlePeriodMs: MIN_IDLE_PERIOD_MS,
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });

  // ── validity guards run BEFORE the rate assertion ─────────────────────────
  // An invalid measurement must fail as an invalid measurement, never pass as a
  // quiet page.

  // A low count must not be a corpse.
  expect(
    uncaught,
    `uncaught exception while the page sat idle — a crashed tree stops polling too:\n${uncaught.join('\n')}`,
  ).toEqual([]);
  await expect(page.getByTestId('run-view'), 'the conversation is still rendered').toBeVisible();
  await expectComposerEnabled(page);
  await expect(composer, 'the page still holds the unsent draft').toHaveValue(draft);

  // The window must have spanned what it claims to have spanned.
  expect(
    elapsedMs,
    'the idle window must actually have elapsed before its rate means anything',
  ).toBeGreaterThanOrEqual(IDLE_WINDOW_MS - LIVE_POLL_INTERVAL_MS);

  // It must still be the measurement it claims. If the cold Environment's idle
  // action reclaimed the box mid-window the session reads TERMINATED — which IS
  // steady state, so the poll would legitimately stop and this spec would pass
  // for a reason that has nothing to do with an idle page coming to rest.
  const afterWindow = await api.getSession(sessionId);
  expect(
    String(afterWindow.state || ''),
    'the conversation must still be READY — a reclaimed box is steady state and would '
      + 'quiesce the poll for the wrong reason',
  ).toEqual('READY');
  expect(
    String(afterWindow.current_turn_id || '').trim(),
    'nothing may have started a turn during the idle window',
  ).toEqual('');
  expect(
    afterWindow.pending_interaction ?? null,
    'nothing may have opened an interaction during the idle window',
  ).toBeNull();

  // ── the property ──────────────────────────────────────────────────────────
  const allowed = Math.ceil(elapsedMs / MIN_IDLE_PERIOD_MS);
  expect(
    observed,
    `an idle conversation must not re-read its session faster than once per ${MIN_IDLE_PERIOD_MS}ms; `
      + `observed ${observed} reads of ${detailPath} in ${elapsedMs}ms`
      + `${observed > 0 ? ` (one every ${Math.round(elapsedMs / observed)}ms)` : ''}, `
      + `at most ${allowed} allowed. The page has no turn, no pending interaction and an `
      + 'unsent draft: there is nothing for it to learn. Check hasReadyAgentRuntime '
      + '(sessionPolling.ts:38-51) — it waits on agent_runtime.sandbox_id, which conversation '
      + 'tenancy structurally never fills (session_read.py:259-289), so isSessionSteadyState '
      + 'never becomes true and useSessionLifecycle.ts:274 re-arms forever.',
  ).toBeLessThanOrEqual(allowed);
});
