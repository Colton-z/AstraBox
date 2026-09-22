/**
 * E2E: an Agent that advertises prepared capacity must make the SECOND
 * conversation typeable as fast as the first, not only whoever arrived first.
 *
 * THE JOURNEY. A user opens the Agents page and clicks Start on a prewarmed
 * Agent. While still reading conversation one they open a second tab and click
 * Start on the same Agent again. The two conversations are created a few
 * seconds apart — before anything has had time to rebuild the Agent's one
 * prepared unit. Both composers are the promise; this spec times both.
 *
 * WHAT THE PRODUCT DOES, read in the code rather than inferred. An Agent
 * carries exactly one prepared unit, the `_prepared_slot` manifest on its row.
 * Conversation one claims it (`claim_prepared_slot`, prepared_slots.py:930)
 * and the manifest flips to `state: "claimed"`. The refill that follows a
 * successful claim (engine/startup.py:456) reaches `prepare_slot_for_agent`,
 * which refuses to begin a replacement while the manifest is not `prepared` —
 * "A live claimed manifest belongs to its Session … This refill has nothing to
 * add" (prepared_slots.py:669-672) — and a replacement build is seconds of
 * work in any case. Conversation two therefore arrives at an Agent whose
 * advertised capacity is spoken for, misses the claim
 * (`claim_prepared_slot` → None, provisioning.py:1605-1615), and builds its
 * runtime inline while the user watches a composer that is disabled with an
 * empty placeholder (SessionPage.tsx:343 `composerCanSend = canSend &&
 * lifecycleState !== 'creating'`; SessionComposerBox.tsx:223 `disabled={…||
 * !canSend}`).
 *
 * WHY THIS IS THE SAME FAMILY as the prepared-slot TTL that only healed when a
 * user showed up: the replacement for a consumed unit is built only after
 * somebody has already consumed it, so the cost of the staleness is paid by
 * the next person through the door rather than by the background.
 *
 * WHAT IS ASSERTED AND WHAT IS ONLY RECORDED. The gate is wall-clock: both
 * conversations must become sendable inside the same prepared budget, measured
 * from each one's own create response. Conversation one is asserted against the
 * SAME budget on purpose — it is the discriminator. Both over budget means the
 * deployment's prepared capacity is broken and this is not the finding; the
 * second alone means it is. What is deliberately NOT asserted is which path
 * conversation two took: pinning "conversation two missed the claim" would make
 * the single-unit behaviour the expected one and go red the day it is fixed, and
 * pinning "conversation two also claimed a prepared unit" would guess the shape
 * of a fix nobody has designed. Both readings go into the attachment so a
 * reader of a red run knows instantly which it was.
 *
 * THIS IS A SPECIFICATION GATE, not a regression guard. That the manifest holds
 * exactly one unit is a recorded decision, and the two remedies that record
 * names are a depth of two or more, or the burst absorbed by the cold path
 * (docs/maintainers/claude-runtime-preparation.md:481). Landing this red forces
 * that decision. It must not be landed muted or skipped.
 *
 * ENGINE. Environment-pinned, not matrix-pinned. The Agent-shared prepared slot
 * only exists for an engine whose Environment declares Agent tenancy and whose
 * adapter overrides the refusing default `prepare_runtime` seam:
 * `prepare_slot_for_agent` returns None for conversation tenancy
 * (prepared_slots.py:620-625) and for an adapter that inherits the default
 * (prepared_slots.py:629-636), so under those profiles there is no single slot
 * to contend for and the journey is vacuous. The spec therefore creates its own
 * Agent on ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT — the recipe both existing
 * prewarm specs use — and refuses loudly when that deployment fixture is
 * absent instead of skipping. It reads those fixtures and never writes them.
 *
 * NARROW REACH, said plainly. This covers the Agent-shared-slot arm only. The
 * same journey exists under conversation tenancy through a different mechanism
 * (the vendor client pool's idle depth and its reconcile tick) and needs its
 * own spec; nothing here touches it. It also never waits for a refill, so it
 * does not re-prove refill-after-claim — opensandbox-agent-prewarm-real-
 * extensions and ownerless-sandbox-reaping-preserves-prepared-capacity already
 * assert that in both tenancy modes.
 *
 * NO LOCALE PIN. Every surface this spec drives is addressed structurally — the
 * picker card by `data-agent-name`, its single action by role, the composer and
 * the status pill by test id — so no localized string is read and nothing here
 * depends on which language the runner negotiated.
 */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { closePageAfterAssertions, sendPrompt } from '../fixtures/sessionPage';

// Deployment fixtures. Read and asserted present, never created or rewritten:
// the lane resolves the Agent-tenancy prewarm Environment per engine profile
// and validates it against the running agent image, and the research Agent is
// where a model route this deployment has actually proven comes from.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

// Every per-step wait is tunable so the lane can trade budget against a loaded
// node. The test budget itself is never stated here — the runner owns it.
const PREPARED_SLOT_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_SLOT_TIMEOUT_MS', 90_000);
const CONVERSATION_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_START_TIMEOUT_MS', 120_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
const SHARED_TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SHARED_TURN_TIMEOUT_MS', 120_000);

/**
 * The promise, in milliseconds, measured from a conversation's own create
 * response to the moment its composer accepts typing.
 *
 * The number is from the deployed stack, not from taste: a claimed prepared
 * slot was measured at 0.573s / 0.616s / 0.592s and 0.800–0.818s create-to-
 * READY, on top of which the console's own CREATING poll is 500ms
 * (useSessionLifecycle.ts:34). Four seconds is several times the healthy path
 * and far under an inline runtime build. It is an env knob because the first
 * calibration run on a given deployment, not this file, is what should set it.
 */
const PREPARED_SENDABLE_BUDGET_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_PREPARED_SENDABLE_BUDGET_MS',
  4_000,
);

/**
 * How long a composer is WAITED on before the clock gives up.
 *
 * The ceiling is the wait; the budget above is the assertion. Keeping them
 * apart is what makes a miss report the real elapsed number instead of a
 * Playwright timeout, and it is why the clock below never throws.
 */
const SENDABLE_CEILING_MS = parseTimeoutEnv('ASTRABOX_E2E_SENDABLE_CEILING_MS', 60_000);

/**
 * How far apart the two Start clicks may land and still be this journey.
 *
 * A console slow enough to exceed this has quietly turned the spec into a test
 * of two conversations started a minute apart — by which time a refill could
 * legitimately have completed — so it is asserted rather than assumed.
 */
const SECOND_START_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_SECOND_START_WINDOW_MS', 15_000);

const PROMPT = 'Reply with just the word READY.';

/** The manifest's PUBLIC coordinates — what identifies a prepared unit, and nothing else. */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  homeDir: string;
  uid: number | null;
}

/**
 * Read one Agent's prepared unit without letting its secrets into an assertion.
 *
 * `_prepared_slot` also carries the activation token and the model credential,
 * and a failed `expect()` retains and prints both sides of its comparison. Only
 * the named coordinates are lifted out of it — the same projection
 * ownerless-sandbox-reaping-preserves-prepared-capacity.exclusive.spec.ts:40
 * takes over the same field.
 */
function preparedIdentity(agentId: string): SlotFacts {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'the preparation under test must belong to exactly one Agent row').toHaveLength(1);
  const manifest = (rows[0]._prepared_slot || {}) as Record<string, unknown>;
  const uid = manifest.uid;
  return {
    slotId: String(manifest.slot_id || '').trim(),
    state: String(manifest.state || '').trim(),
    placement: String(manifest.placement || '').trim(),
    sandboxId: String(manifest.sandbox_id || '').trim(),
    isolatedSessionId: String(manifest.isolated_session_id || '').trim(),
    homeDir: String(manifest.home_dir || '').trim(),
    uid: typeof uid === 'number' ? uid : null,
  };
}

/** One composer's answer to "when could the user type?". Never an exception. */
interface SendableClock {
  /** Did the composer ever accept typing inside the ceiling? */
  sendable: boolean;
  /** Elapsed from this conversation's own create response, in milliseconds. */
  ms: number;
}

/** One arrival through the picker, with its clock already running. */
interface Arrival {
  sessionId: string;
  /** When the platform answered this tab's create — each clock's own zero. */
  createdAtMs: number;
  sendable: Promise<SendableClock>;
}

/**
 * Start a conversation the way a person does, and time the composer from the
 * create response.
 *
 * Two details carry weight. The clock is started BEFORE the URL wait, because
 * on the fast path the composer can enable while this function is still
 * resolving the session id off the URL, and a clock started after that would
 * charge its own bookkeeping to the product. And the clock cannot throw: a
 * composer that never enables returns `sendable: false` with the elapsed it
 * actually spent, so the assertion below reports a number rather than a
 * Playwright timeout — and an unawaited rejection cannot take the worker down
 * while the other tab is mid-flight.
 *
 * The picker card carries exactly one button (AgentHome.tsx:185), so the action
 * is addressable without pinning a locale for its label.
 */
async function arriveFromCard(
  page: Page,
  agentName: string,
  agentId: string,
): Promise<Arrival> {
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  const created = page.waitForResponse(
    (response) => response.request().method() === 'POST'
      && response.url().includes(apiPath(`/agents/${agentId}/conversations`)),
    { timeout: CONVERSATION_START_TIMEOUT_MS },
  );
  await card.getByRole('button').click();
  const response = await created;
  const createdAtMs = Date.now();
  expect(
    response.ok(),
    `the picker's Start must be accepted, got ${response.status()} — a refused start is a `
    + 'different failure from a slow one and must not be timed as latency',
  ).toBe(true);

  const sendable: Promise<SendableClock> = (async () => {
    try {
      await expect(page.getByTestId('composer-prompt')).toBeEnabled({
        timeout: SENDABLE_CEILING_MS,
      });
      return { sendable: true, ms: Date.now() - createdAtMs };
    } catch {
      return { sendable: false, ms: Date.now() - createdAtMs };
    }
  })();

  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: CONVERSATION_START_TIMEOUT_MS,
  });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  return { sessionId, createdAtMs, sendable };
}

/**
 * What the Agent advertises right now, as data.
 *
 * Only ever evidence in this spec, never a gate, so a read that fails becomes a
 * field instead of an exception — the sample must not be the thing that decides
 * the run.
 */
async function preparedSample(
  platform: PlatformApi,
  agentId: string,
): Promise<Record<string, unknown>> {
  try {
    const status = await platform.preparedRuntime(agentId);
    return {
      ready: status.ready ?? null,
      sandbox_id: String(status.sandbox_id || ''),
      client_pool_name: String(status.client_pool_name || ''),
      manifest: preparedIdentity(agentId),
    };
  } catch (error) {
    return { unreadable: String((error as Error).message).slice(0, 200) };
  }
}

/** Session detail for the report. Never a gate, so a failed read is a field. */
async function detailSample(api: AstraApi, sessionId: string): Promise<Record<string, unknown>> {
  try {
    const detail = await api.adminSessionDetail(sessionId);
    const identity = (detail.runtime_identity || {}) as Record<string, unknown>;
    return {
      sessionId,
      state: String(detail.state || ''),
      sandbox_id: String(detail.sandbox_id || ''),
      isolated_session_id: String(identity.isolated_session_id || ''),
      home_dir: String(identity.home_dir || ''),
      uid: identity.uid ?? null,
    };
  } catch (error) {
    return { sessionId, unreadable: String((error as Error).message).slice(0, 200) };
  }
}

const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, because afterEach hooks run in registration
// order: the conversations go before the Agent they ran on. A failure keeps
// both — including the box that had to host the burst, which IS the scene.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('two conversations opened seconds apart on one prewarmed Agent are both sendable inside the prepared budget', async ({
  page,
  request,
}) => {
  expect(
    SHARED_ENVIRONMENT,
    'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT must name the deployed Agent-tenancy prewarm Environment',
  ).not.toEqual('');
  expect(
    RESEARCH_AGENT,
    'ASTRABOX_E2E_RESEARCH_AGENT must name the deployed Agent whose model route was proven',
  ).not.toEqual('');

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);

  // ── An Agent with real warm capacity ─────────────────────────────────────
  // Authoring stays on the API: a user picks an Agent that already exists
  // rather than creating one in order to have a conversation. The Agent is this
  // spec's own so the burst lands on capacity nobody else is competing for.
  const agentName = `__e2e_burst_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  await expect.poll(async () => {
    const status = await platform.preparedRuntime(agentId);
    return Boolean(
      status.ready === true
      && String(status.sandbox_id || '').trim()
      && String(status.client_pool_name || '').trim(),
    );
  }, {
    timeout: PREPARED_SLOT_TIMEOUT_MS,
    intervals: [2_000],
    message:
      `Agent ${agentId} must publish real claimable capacity on ${SHARED_ENVIRONMENT} before the `
      + 'burst arrives — without it neither conversation is fast and the spec measures nothing',
  }).toBe(true);

  // The coordinates of the one prepared unit, read before either conversation
  // exists. This is what conversation one must be shown to have claimed.
  const publishedSlot = preparedIdentity(agentId);
  expect(
    publishedSlot.placement,
    'this journey needs the Agent-shared slot — a conversation-tenancy Agent has no single '
    + 'manifest for two arrivals to contend for',
  ).toBe('shared_slot');
  expect(publishedSlot.state, 'the Agent must advertise a prepared unit, not a claimed one').toBe('prepared');
  expect(publishedSlot.sandboxId, 'the prepared unit must name a box').not.toEqual('');
  // Every coordinate A4 compares against is required to be real here, or A4
  // would compare two empties and pass while proving nothing.
  expect(publishedSlot.isolatedSessionId, 'the prepared unit must name its isolated session').not.toEqual('');
  expect(publishedSlot.homeDir, 'the prepared unit must name the account home it was built under').not.toEqual('');
  expect(
    typeof publishedSlot.uid,
    'the prepared unit must name the uid it was built under',
  ).toBe('number');

  // ── User one arrives ─────────────────────────────────────────────────────
  // Nothing here waits for READY and nothing waits for a refill. That wait is
  // exactly what makes the existing prewarm coverage blind to this journey, and
  // skipping it is the whole mechanism by which the scene becomes true: a
  // replacement unit cannot even begin while the manifest is claimed, so a
  // conversation started a second or two later has nothing to claim.
  const first = await arriveFromCard(page, agentName, agentId);
  sessions.push(first.sessionId);

  // ── User two arrives, immediately ────────────────────────────────────────
  const second = await page.context().newPage();
  const secondArrival = await arriveFromCard(second, agentName, agentId);
  sessions.push(secondArrival.sessionId);

  // Evidence, sampled at the moment user two exists and never asserted: with
  // the Agent's one unit spoken for this reads `ready: false` or a `claimed`
  // manifest, but an Agent carrying a depth of two would legitimately read
  // ready here, so a gate on this field would refuse the very shape that
  // satisfies A2.
  const advertisedAtSecondStart = await preparedSample(platform, agentId);

  // ── Both clocks ──────────────────────────────────────────────────────────
  const firstClock = await first.sendable;
  const secondClock = await secondArrival.sendable;
  const startGapMs = secondArrival.createdAtMs - first.createdAtMs;

  test.info().annotations.push(
    { type: 'first-sendable-ms', description: `${firstClock.ms} (sendable=${firstClock.sendable})` },
    { type: 'second-sendable-ms', description: `${secondClock.ms} (sendable=${secondClock.sendable})` },
    { type: 'start-gap-ms', description: String(startGapMs) },
  );
  await test.info().attach('prepared-burst-evidence', {
    body: JSON.stringify({
      agentId,
      agentName,
      budgetMs: PREPARED_SENDABLE_BUDGET_MS,
      publishedSlot,
      startGapMs,
      first: { ...firstClock, sessionId: first.sessionId },
      second: { ...secondClock, sessionId: secondArrival.sessionId },
      advertisedAtSecondStart,
      firstDetail: await detailSample(api, first.sessionId),
      secondDetail: await detailSample(api, secondArrival.sessionId),
    }, null, 2),
    contentType: 'application/json',
  });

  // The two numbers travel together in every message below: one over budget and
  // one under is this finding; both over is a broken deployment.
  const both =
    `first=${firstClock.ms}ms(sendable=${firstClock.sendable}) `
    + `second=${secondClock.ms}ms(sendable=${secondClock.sendable}) `
    + `budget=${PREPARED_SENDABLE_BUDGET_MS}ms gap=${startGapMs}ms`;

  // ── A3: this really was a burst ──────────────────────────────────────────
  expect(
    startGapMs,
    `PRECONDITION: the two Start clicks must land within ${SECOND_START_WINDOW_MS}ms of each `
    + `other or this is not the journey — a slower console gives the platform time to rebuild a `
    + `prepared unit and the result below would be about something else (${both})`,
  ).toBeLessThanOrEqual(SECOND_START_WINDOW_MS);

  // ── A1: calibration — the first arrival IS fast ──────────────────────────
  expect(
    firstClock.sendable && firstClock.ms <= PREPARED_SENDABLE_BUDGET_MS,
    `CALIBRATION: the first conversation on a prewarmed Agent must be sendable inside the `
    + `prepared budget. If this fails, this deployment's prepared capacity is broken and the `
    + `second conversation's number below says nothing about the product (${both})`,
  ).toBe(true);

  // ── A2: the gate ─────────────────────────────────────────────────────────
  expect(
    secondClock.sendable && secondClock.ms <= PREPARED_SENDABLE_BUDGET_MS,
    `PREPARED CAPACITY IS FOR EVERY ARRIVAL: the second conversation, opened ${startGapMs}ms `
    + `after the first on the same prewarmed Agent, must be sendable inside the same budget. An `
    + `Agent with one prepared unit makes only its first arrival fast; the second builds its `
    + `runtime inline while the user watches a disabled composer. See the attachment for which `
    + `path each conversation actually took (${both})`,
  ).toBe(true);

  // ── A4: why the first one was fast ───────────────────────────────────────
  // Without this A1 could pass on luck — a cold start that happened to be quick
  // reads identically from a stopwatch. This stays green after a fix: it says
  // the first conversation claimed the advertised unit, not that the second did
  // not.
  await api.waitForSessionReady(first.sessionId, READY_TIMEOUT_MS);
  const firstDetail = await api.adminSessionDetail(first.sessionId);
  const firstIdentity = (firstDetail.runtime_identity || {}) as Record<string, unknown>;
  expect(
    String(firstDetail.sandbox_id || '').trim(),
    'the first conversation must land in the box the Agent advertised, not a cold one built for it',
  ).toBe(publishedSlot.sandboxId);
  expect(
    {
      isolatedSessionId: String(firstIdentity.isolated_session_id || '').trim(),
      homeDir: String(firstIdentity.home_dir || '').trim(),
      uid: typeof firstIdentity.uid === 'number' ? firstIdentity.uid : null,
    },
    'the first conversation must be running ON the prepared unit — the same isolated session, '
    + 'account and home the Agent published before either conversation existed',
  ).toEqual({
    isolatedSessionId: publishedSlot.isolatedSessionId,
    homeDir: publishedSlot.homeDir,
    uid: publishedSlot.uid,
  });

  // ── A5: the at-risk conversation actually works ──────────────────────────
  // Latency is the headline, but the class this also guards is worse: a second
  // conversation that never becomes usable at all. Typed into its own composer
  // on its own tab, because that is where the user is.
  const secondBefore = await second.getByTestId('assistant-message').count();
  await sendPrompt(second, secondArrival.sessionId, PROMPT);
  await expect
    .poll(() => second.getByTestId('assistant-message').count(), {
      timeout: SHARED_TURN_TIMEOUT_MS,
      message: 'the second conversation must answer a real prompt typed into its own composer',
    })
    .toBeGreaterThan(secondBefore);
  // Count alone would be satisfied by a failed turn rendering its error into the
  // transcript, so the text is read back too. The wording is the model's own
  // business; that there is any is not.
  await expect(
    second.getByTestId('assistant-message').last(),
    'the second conversation must render a non-empty reply',
  ).not.toBeEmpty();
  await expect(
    second.getByTestId('run-view').getByTestId('status-pill').first(),
    'the second conversation\'s turn must settle, not hang with the meter running',
  ).toHaveAttribute('data-pulse', 'false', { timeout: SHARED_TURN_TIMEOUT_MS });

  // ── A6: the first one is not collateral damage ───────────────────────────
  // The burst put a claimed child, a cold-built child and a refill child into
  // one Agent box — the configuration the maintainer record names as having
  // OOM-killed a testbed box at three-way concurrency. If that is what breaks,
  // it breaks here, and the attachment above carries both boxes and identities
  // so the failure is attributable to the envelope rather than to latency.
  const firstBefore = await page.getByTestId('assistant-message').count();
  await sendPrompt(page, first.sessionId, PROMPT);
  await expect
    .poll(() => page.getByTestId('assistant-message').count(), {
      timeout: SHARED_TURN_TIMEOUT_MS,
      message: 'the first conversation must survive the second one being started beside it',
    })
    .toBeGreaterThan(firstBefore);
  await expect(
    page.getByTestId('assistant-message').last(),
    'the first conversation must render a non-empty reply after the burst',
  ).not.toBeEmpty();

  await closePageAfterAssertions(second);
});
