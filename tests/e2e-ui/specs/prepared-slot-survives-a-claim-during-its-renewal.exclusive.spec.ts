/**
 * A claim during renewal retains the published slot and leaves new warm capacity
 * for the next conversation. The exact Agent/slot barrier holds the replacement
 * before placement, whether the timer or the explicit sweep scheduled it.
 * Both conversations must adopt their observed isolated sessions; the second
 * warm start must remain within the original relative latency budget.
 */
import { execFileSync } from 'node:child_process';

import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { backdatePreparedSlot, documentsByField } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { preparedRenewalHold } from '../fixtures/preparedRenewalHold';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Agent tenancy only, and deliberately so. `schedule_prepared_runtime_refill`
// returns immediately for SANDBOX_TENANCY_CONVERSATION (prepared_slots.py:921)
// because the SDK client pool replenishes its own inventory, so running this
// against ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT would go green over
// capacity this spec never collided with.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

// Every per-step wait is tunable so the lane can trade budget against a loaded
// deployment. The test budget itself is never stated here — the runner owns it.
const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
// How long the background alone may take to put claimable capacity back. The
// cost is a whole replacement engine child, which is tens of seconds for every
// engine; the rest is room to read the failure. Cut this first if the file
// overruns — never the third conversation, which is the journey's payoff.
const RENEWAL_BUILD_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_RENEWAL_BUILD_MS', 45_000);
// How much slower the third conversation may be than the second. The second is
// a measured claim on this very deployment, so nothing absolute is baked in; a
// cold third conversation lands 17-33s above it.
const CLAIM_MARGIN_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_CLAIM_MARGIN_MS', 10_000);

// The product's own numbers, named so a drift fails here rather than silently
// making the scene vacuous: prepared_slots.py:118 and :175.
const PREPARED_SLOT_TTL_MS = 30 * 60 * 1_000;
const PREPARED_SLOT_RENEWAL_BUILD_MS = 120 * 1_000;
// How far past the renewal threshold the manifest is aged. The stamp is written
// from the runner's clock and judged against the server's, so this absorbs a
// second of skew between them rather than leaving the slot not-yet-due; it stays
// small so the rest of the TTL remains claim headroom.
const DUE_MARGIN_MS = 30_000;
// How much claimable life the slot must still have when the user clicks. Below
// this a miss could be an exhausted TTL (`claim_prepared_slot` refuses a slot
// older than the TTL, prepared_slots.py:1005) rather than the product, so it is
// reported as a broken scene instead.
const CLAIM_TTL_FLOOR_MS = 30_000;

// How many forced sweep ticks the background gets after the collision. Below
// this count a red result would only record that the sweep had not yet had a
// chance to act, which is a fact about the wait and not about the product.
const FORCED_TICKS_AFTER_COLLISION = 3;

/** The manifest's PUBLIC coordinates. Preparation also carries the activation
 * token and the model credential, and a failed `expect()` retains and prints
 * both sides of its comparison, so only these are ever lifted out of the row. */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  preparedAt: string;
  runtimeGeneration: string;
  /** The Agent row's own resident-box pointer — what `backdatePreparedSlot`
   * projects back, and the field the abandoned-box sweep clears. */
  residentSandboxId: string;
}

function preparedSlotFacts(agentId: string): SlotFacts {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'the preparation under test must belong to exactly one Agent row').toHaveLength(1);
  const row = rows[0];
  const manifest = (row._prepared_slot || {}) as Record<string, unknown>;
  return {
    slotId: String(manifest.slot_id || '').trim(),
    state: String(manifest.state || '').trim(),
    placement: String(manifest.placement || '').trim(),
    sandboxId: String(manifest.sandbox_id || '').trim(),
    isolatedSessionId: String(manifest.isolated_session_id || '').trim(),
    preparedAt: String(manifest.prepared_at || '').trim(),
    runtimeGeneration: String(row._runtime_generation || '').trim(),
    residentSandboxId: String(row.sandbox_id || '').trim(),
  };
}

/**
 * The deployed watcher's tick interval, read from the server rather than assumed.
 *
 * It decides the renewal lead (`prepared_slot_renewal_lead_seconds`,
 * prepared_slots.py:183-194) and therefore how far back the manifest has to be
 * aged to be due. `printenv` exits non-zero when the variable is unset, which
 * `execFileSync` raises; that is the right outcome — the deployment would run
 * the 300s default — but it has to say so in words, because "the spec threw"
 * and "this deployment cannot host this journey" send a reader to different
 * places.
 */
function deployedWatcherIntervalSeconds(server: string): number {
  let raw = '';
  try {
    raw = execFileSync('docker', [
      'exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS',
    ], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    throw new Error(
      'HARNESS PREREQUISITE: ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS is not set on server '
      + `container ${JSON.stringify(server)}, so this spec cannot compute the renewal lead the `
      + 'deployed sweep applies and could age the manifest to a point that is not yet due. Set '
      + 'the interval on the deployment. This is not a product defect. '
      + `(${String((error as Error).message).slice(0, 200)})`,
    );
  }
  const seconds = Number.parseInt(raw, 10);
  expect(
    Number.isFinite(seconds) && seconds > 0,
    `the deployed expiration-watcher interval must be a positive integer, got ${JSON.stringify(raw)}`,
  ).toBe(true);
  return seconds;
}

/** One forced tick of the real watcher, in the server process. */
async function forcedTick(platform: PlatformApi): Promise<Record<string, number>> {
  const result = await platform.idleSweep() as { summary?: Record<string, number> };
  return result.summary || {};
}

/**
 * Open a conversation the way a user does, and time it from click to usable.
 *
 * Only a testid and the card's single button are used, so no locale pin is
 * needed — the button's label is localized but nothing here reads it.
 */
async function startConversationFromCard(
  page: Page,
  agentName: string,
): Promise<{ sessionId: string; elapsedMs: number }> {
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must be offered on the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  const startedAt = Date.now();
  await card.getByRole('button').click();
  await page.waitForURL(/\/sessions\/[^/]+$/);
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  // Routed is not the same as usable: the payoff this journey measures is a
  // conversation the user can type into, not a row that exists.
  await expectComposerEnabled(page);
  return { sessionId, elapsedMs: Date.now() - startedAt };
}

let agentId = '';
const sessions = trackSessions();
// Registered after the tracker, because afterEach hooks run in registration
// order: the sessions go before the Agent they ran on. A failure keeps both,
// including the Agent's box, and the report names them.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('a conversation claiming the slot mid-renewal leaves prepared capacity for the next one', async ({
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
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const watcherIntervalSeconds = deployedWatcherIntervalSeconds(server);
  // The deployed sweep's own rule, recomputed here so the ageing below lands on
  // the right side of it: due once the slot is older than its TTL minus one
  // watcher interval plus a build.
  const renewalLeadMs = watcherIntervalSeconds * 1_000 + PREPARED_SLOT_RENEWAL_BUILD_MS;
  const backdateMs = Math.max(0, PREPARED_SLOT_TTL_MS - renewalLeadMs) + DUE_MARGIN_MS;
  const claimHeadroomMs = PREPARED_SLOT_TTL_MS - backdateMs;
  expect(
    claimHeadroomMs,
    'HARNESS PREREQUISITE: a manifest aged far enough to be due for renewal must still be inside '
    + 'its TTL when the user clicks, or the claim would miss for a reason this spec is not about',
  ).toBeGreaterThan(CLAIM_TTL_FLOOR_MS);

  // ── An Agent with real warm capacity, created through the API ────────────
  // A user picks an Agent that already exists rather than authoring one to have
  // a chat, so authoring is not part of the journey.
  const agentName = `__e2e_renewal_claim_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  // A1 — an Agent that never prepares means this Environment is not Agent
  // tenancy or this engine has no input-free preparation seam. That is a
  // deployment fact and it belongs here, before the collision is constructed,
  // rather than read back later as a missing rebuild.
  await expect.poll(async () => {
    const runtime = await platform.preparedRuntime(agentId);
    return Boolean(
      runtime.ready === true
      && String(runtime.state || '') === 'prepared'
      && String(runtime.sandbox_id || '').trim(),
    );
  }, {
    timeout: POOL_TIMEOUT_MS,
    intervals: [2_000],
    message: `Agent ${agentId} must prepare real claimable capacity on ${SHARED_ENVIRONMENT} `
      + 'before a renewal can be collided with',
  }).toBe(true);

  // The full manifest, read from the row because the HTTP projection carries no
  // slot id and no isolated session — and those two are what tell a claim apart
  // from a cold start that landed in the same box.
  const before = preparedSlotFacts(agentId);
  expect(before.state, 'the manifest must be claimable before anything is aged').toBe('prepared');
  expect(before.placement, 'Agent tenancy prepares a shared slot, not a pooled box').toBe('shared_slot');
  expect(before.slotId, 'the manifest must name its slot').not.toEqual('');
  expect(before.isolatedSessionId, 'the manifest must name its isolated session').not.toEqual('');
  // Ties the slot under test to the Agent's own resident pointer, so the
  // backdate's projection below is comparing against a known box rather than
  // whatever the row happened to hold.
  expect(
    before.residentSandboxId,
    'the Agent row must point at the same box its prepared slot occupies',
  ).toBe(before.sandboxId);
  test.info().annotations.push({
    type: 'renewal_claim_scene',
    description: JSON.stringify({ agentId, agentName, slotId: before.slotId, box: before.sandboxId }),
  });

  // ── Put the user where they would be BEFORE the race starts ─────────────
  // Loading the picker first leaves one click as the only work after the
  // rebuild begins, which is what keeps the claim inside the build window.
  await page.goto(appPath('/agents'));
  await expect(
    page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`),
    'the prewarmed Agent must be on the picker before the renewal is triggered',
  ).toBeVisible({ timeout: READY_TIMEOUT_MS });

  const hold = preparedRenewalHold(agentId, before.slotId);
  let second: { sessionId: string; elapsedMs: number };
  try {
    // ── Make the slot due for renewal ───────────────────────────────────────
    // One statement, guarded down to the slot id: a read-modify-write of the
    // whole Agent document would lose a concurrent server write in its window.
    const agedAtIso = new Date(Date.now() - backdateMs).toISOString();
    expect(
      backdatePreparedSlot(agentId, before.slotId, agedAtIso),
      'the backdate must land on exactly the manifest under test',
    ).toEqual([{
      agent_id: agentId,
      sandbox_id: before.residentSandboxId,
      slot_id: before.slotId,
      state: 'prepared',
      prepared_at: agedAtIso,
    }]);
    const agedAt = Date.now();

    // ── Start the renewal, in the server process ────────────────────────────
    const renewalTick = await forcedTick(platform);
    await test.info().attach('forced-tick-that-started-the-renewal', {
      body: JSON.stringify({ watcherIntervalSeconds, backdateMs, summary: renewalTick }),
      contentType: 'application/json',
    });
    // A global sweep count cannot identify this Agent: the periodic timer can
    // start its renewal before the forced tick and make that tick a no-op.
    await expect.poll(hold.consumed, {
      timeout: POOL_TIMEOUT_MS,
      intervals: [100, 250, 500],
      message: 'the exact Agent and published slot must reach the renewal barrier',
    }).toBe(true);
    await test.info().attach('held-prepared-slot-renewal', {
      body: JSON.stringify(hold.evidence()), contentType: 'application/json',
    });
    // A held replacement must leave the old preparation published and claimable.
    const duringBuild = preparedSlotFacts(agentId);
    expect(
      duringBuild.slotId,
      'the slot under renewal must stay published while its replacement is held',
    ).toBe(before.slotId);
    expect(duringBuild.state, 'and it must still read as claimable').toBe('prepared');

    // A3 — guard the headroom before the user acts, so a slow sweep is reported
    // as a broken scene and never misread as a missed claim at A4.
    expect(
      PREPARED_SLOT_TTL_MS - backdateMs - (Date.now() - agedAt),
      'HARNESS: the aged slot must still be inside its TTL when the user clicks',
    ).toBeGreaterThan(CLAIM_TTL_FLOOR_MS);

    // ── The user opens a conversation while the rebuild runs ────────────────
    second = await startConversationFromCard(page, agentName);
    sessions.push(second.sessionId);
    await api.waitForSessionReady(second.sessionId, READY_TIMEOUT_MS);
    const secondDetail = await api.adminSessionDetail(second.sessionId);

    // A4 — two things at once. The seed fix's promise, that a user arriving
    // mid-rebuild still gets the fast path; and proof that this run really
    // constructed the collision, because the claim's CAS can only have won
    // against the pre-renewal manifest if it landed before the renewal's publish.
    expect(
      String((secondDetail.runtime_identity || {}).isolated_session_id || '').trim(),
      'the conversation must adopt the original slot while its replacement is held',
    ).toBe(before.isolatedSessionId);
    hold.release();
  } finally {
    hold.clear();
  }

  // ── Let the background do everything it can ────────────────────────────
  // Three forced ticks is more watcher attention than this Agent would get in
  // fifteen real minutes of the deployment's own timer.
  const ticks: Array<Record<string, number>> = [];
  let restored: Record<string, unknown> = {};
  let ticksSpent = 0;
  try {
    await expect.poll(async () => {
      restored = await platform.preparedRuntime(agentId);
      const ready = Boolean(
        restored.ready === true
        && String(restored.state || '') === 'prepared'
        && String(restored.sandbox_id || '').trim(),
      );
      if (ready) return true;
      if (ticksSpent < FORCED_TICKS_AFTER_COLLISION) {
        ticksSpent += 1;
        ticks.push(await forcedTick(platform));
      }
      return false;
    }, {
      timeout: RENEWAL_BUILD_MS,
      intervals: [3_000],
      // A5, the load-bearing check.
      message:
        `nothing in the background re-armed Agent ${agentId} after a conversation claimed slot `
        + `${before.slotId} during its renewal: the renewal's build was discarded, the refill `
        + 'demand the claim raised was answered by that in-flight reconciliation and dropped, and '
        + `${FORCED_TICKS_AFTER_COLLISION} forced sweeps did not put a slot back. The only thing `
        + 'left that would is the next person missing their claim and paying the cold start',
    }).toBe(true);
  } finally {
    await test.info().attach('forced-ticks-after-the-collision', {
      body: JSON.stringify({ ticksSpent, ticks, preparedRuntime: restored }),
      contentType: 'application/json',
    });
  }

  const after = preparedSlotFacts(agentId);
  expect(
    after.slotId,
    'the capacity put back must be a NEW slot — the claimed one is the conversation\'s now',
  ).not.toBe(before.slotId);
  expect(after.state, 'the restored manifest must read as claimable').toBe('prepared');
  expect(
    after.isolatedSessionId,
    'the restored manifest must name its own isolated session, not the claimed one',
  ).not.toBe(before.isolatedSessionId);
  expect(
    after.runtimeGeneration,
    'nothing about this Agent was configured, so this is a renewal of capacity and not a rotation',
  ).toBe(before.runtimeGeneration);

  // ── The next person ────────────────────────────────────────────────────
  await page.goto(appPath('/agents'));
  const third = await startConversationFromCard(page, agentName);
  sessions.push(third.sessionId);
  await api.waitForSessionReady(third.sessionId, READY_TIMEOUT_MS);
  const thirdDetail = await api.adminSessionDetail(third.sessionId);

  // A6 — landing in the same box is not yet a claim: a cold start would reach
  // the same shared box and build its own isolation session beside the slot.
  // The restored slot's isolated session is what separates the two.
  expect(
    String((thirdDetail.runtime_identity || {}).isolated_session_id || '').trim(),
    'the next conversation must adopt the restored preparation rather than cold-start beside it',
  ).toBe(after.isolatedSessionId);

  // A7 — the user-seat cost, calibrated in-run. The second conversation is a
  // measured claim on this deployment, so nothing absolute is baked in; a cold
  // third conversation lands 17-33s above it.
  expect(
    third.elapsedMs - second.elapsedMs,
    `the next conversation must open about as fast as the claimed one (${second.elapsedMs}ms): a `
    + 'gap of tens of seconds is a cold start wearing the restored slot\'s box',
  ).toBeLessThan(CLAIM_MARGIN_MS);

  await test.info().attach('prepared-slot-survives-a-claim-during-its-renewal', {
    body: JSON.stringify({
      agentId,
      watcherIntervalSeconds,
      slotBefore: before.slotId,
      slotAfter: after.slotId,
      claimedIsolatedSession: before.isolatedSessionId,
      restoredIsolatedSession: after.isolatedSessionId,
      secondConversationMs: second.elapsedMs,
      thirdConversationMs: third.elapsedMs,
      ticksSpent,
    }),
    contentType: 'application/json',
  });
});
