/**
 * E2E for the conversation that dies on arrival because warm capacity outlived
 * its box.
 *
 * An Agent's shared box can go away without the platform being asked — a node
 * eviction, a lease lapse, a substrate failure. The agent-box reap notices,
 * probes the backend, finds NOT_FOUND and clears the row's resident pointer
 * (`reap_abandoned_agent_boxes`, runtime_manager.py:2980-2994). It does not
 * touch `_prepared_slot`. So the platform has recorded the death and still
 * holds a manifest naming the corpse.
 *
 * Nothing standing over that manifest asks whether its box exists. The renewal
 * sweep runs later in the SAME watcher tick (`keep_prewarmed_agents_ready`,
 * runtime_manager.py:2810, expiration_watcher.py:179) and does still see the
 * row — it selects on `list_prewarm_enabled_agents`, not on the pointer the
 * reap just cleared — but it acts only on age: `_manifest_is_reapable` judges
 * state, generation and age (prepared_slots.py:197-229) and
 * `prepared_slot_is_due_for_renewal` substitutes the manifest's own generation
 * and asks the same three (:268-283), so a slot prepared a minute ago is not
 * due for renewal for about twenty-eight minutes.
 * The readout's rule is `ready = enabled and state == "prepared"`
 * (agent_service.py:253-254) — it reads neither the box nor the row's cleared
 * pointer. And `claim_prepared_slot` gates on exactly the same three facts
 * (:1005-1044). Every reader of this document agrees it is healthy; none of
 * them can see that its box is gone.
 *
 * So the next person clicks the Agent. The claim succeeds, `connect()` raises
 * SANDBOX_GONE on a destroyed box (open_sandbox/sandbox.py:1117-1132), and the
 * pre-activation guard at provisioning.py:2142-2155 discards the claim and
 * RE-RAISES — there is no cold-start fallback on that arm. The lifecycle worker
 * writes TERMINATED with `runtime_unavailable`
 * (workers/lifecycle/startup.py:672-711). Warm capacity that should have made
 * the start faster is what stops it happening at all.
 *
 * TWO QUESTIONS, ASKED IN ONE RUN AND IN THIS ORDER:
 *
 *   1. FRESHNESS, captured with nobody in the product. Once the platform has
 *      converged the death by itself, does it stop advertising claimable
 *      capacity in that box? Captured BEFORE the card is clicked, because a
 *      failed start schedules its own refill (provisioning.py:2149) — the same
 *      read taken afterwards would be measuring the product's reaction to the
 *      user, not its background freshness.
 *   2. PRODUCT, load-bearing. Clicking that Agent must still open a
 *      conversation that reaches READY, on live compute, and answer one
 *      message. Prewarm is an optimisation; a stale optimisation must degrade
 *      to the cold path, never to a dead conversation.
 *
 * NOT THE SIBLING SCENES.
 *   `a-dead-prewarmed-box-is-rebuilt-before-the-next-person` kills the same box
 *   but then BACKDATES the manifest past its renewal lead, making renewal due,
 *   and asks whether the sweep rebuilds. That is the age-triggered question.
 *   This spec never backdates: the manifest is fresh, renewal is not due for
 *   half an hour, and the question is whether DEATH ITSELF withdraws the
 *   advertisement. A fix that only widens the renewal sweep's staleness rule
 *   turns that spec green and leaves this one red for the whole TTL.
 *   `an-untouched-prewarmed-agent-keeps-live-warm-capacity` already names this
 *   failure in a comment ("claim_prepared_slot hands back a manifest naming a
 *   destroyed box and the start errors with no cold-start fallback") while
 *   keeping its box alive, so nothing there exercises it. This is the spec that
 *   makes the box actually die under an unclaimed slot.
 *   `sandbox-oob-death-reborrow` kills a box a CONVERSATION already holds; the
 *   re-borrow path it proves is a different one from the claim path here.
 *
 * EXPECTED RED AGAINST THE CURRENT PRODUCT, at both halves.
 *
 * SCOPE — what this does NOT cover. The idle-park route reaches a neighbouring
 * failure by a different mechanism (a pause kills the engine child while the
 * manifest still reads `prepared`); that is
 * `idle-park-refuses-a-box-holding-a-prepared-slot`, which gates the park
 * itself, and it needs an Environment with `idle_action: pause` plus an idle
 * wait. Nor is the second-order symptom covered here: after the failed start
 * `discard_prepared_slot` releases a lease against a box that is gone, which
 * can leave the manifest `claimed` for up to CLAIMED_SLOT_ORPHAN_SECONDS (10
 * minutes) and charge cold starts to everyone in that window. Asserting that
 * needs a second conversation and does not fit 180s.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Agent tenancy only. The defect lives in the `shared_slot` branch of
// `claim_prepared_engine_sandbox` (provisioning.py:1702). Under the
// conversation-tenancy Environment the capacity is a supplier client pool that
// replenishes itself and the manifest is retired on purpose, so the scene would
// be vacuous rather than merely different — which is why the placement is a
// loud precondition below and not a silent branch.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 90_000);
// How long the platform may keep advertising the dead box after it has itself
// recorded the death. Not a product constant — the product has no timer for
// this at all, which is the point — so it is stated as "several whole scans of
// this deployment" and checked against the watcher's real period below.
const PREPARED_INVALIDATION_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_PREPARED_INVALIDATION_MS',
  30_000,
);

/**
 * The manifest's PUBLIC coordinates, plus the row pointer the sweeps key on.
 *
 * `_prepared_slot` also carries the activation token and the model credential.
 * A failed `expect()` retains and prints both sides of its comparison, so only
 * the named coordinates are ever lifted out of the document.
 */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  preparedAt: string;
  claimedSessionId: string;
  residentSandboxId: string | null;
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
    claimedSessionId: String(manifest.claimed_session_id || '').trim(),
    residentSandboxId: String(row.sandbox_id || '').trim() || null,
  };
}

/** One reading of the capacity the product advertises to an operator. */
interface CapacityReading {
  at: string;
  ready: boolean;
  preparedCount: number;
  state: string;
  placement: string;
  sandboxId: string;
  /** The single state that must never persist: ready, naming the corpse. */
  stillNamesTheCorpse: boolean;
  /** Recorded, not asserted: whether whatever it names can actually run work. */
  advertisedBoxRuns: boolean | null;
}

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('an out-of-band Agent box death withdraws its prepared slot and the next conversation still starts', async ({
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

  // This spec waits for the real scheduler instead of calling a sweep by hand,
  // so every window it grants the background is measured against that
  // scheduler's own period. Without this, a red would be indistinguishable from
  // "the sweep had not run yet".
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const scanInterval = Number(execFileSync('docker', [
    'exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS',
  ], { encoding: 'utf8', timeout: 10_000 }).trim());
  expect(
    scanInterval,
    'the deployed testbed must configure a positive background scan interval',
  ).toBeGreaterThan(0);
  expect(
    scanInterval * 1_000,
    'the real background scan must fit inside the death-convergence budget this spec waits out',
  ).toBeLessThan(KILL_CONVERGE_MS / 2);
  expect(
    scanInterval * 1_000,
    'the freshness window must span at least two whole background scans of this deployment',
  ).toBeLessThanOrEqual(PREPARED_INVALIDATION_MS / 2);

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  agentId = '';
  const agentName = `__e2e_dead_box_slot_${Date.now()}`;
  // No conversation, no visibility override: the box about to be killed belongs
  // to nobody but this test. A shared-box kill that could land on a stranger is
  // how one spec takes down another (tests/e2e/test_durable_workspace.py's
  // `_assert_the_box_is_this_conversation_alone` is the same precaution).
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  // ── The warm capacity this spec is about ────────────────────────────────
  let status: Record<string, unknown> = {};
  await expect.poll(async () => {
    status = await platform.preparedRuntime(agentId);
    return Boolean(
      status.ready === true
      && Number(status.prepared_count || 0) > 0
      && String(status.sandbox_id || '').trim(),
    );
  }, {
    timeout: POOL_TIMEOUT_MS,
    intervals: [2_000],
    // An engine adapter with no input-free prepare seam never publishes a
    // manifest. That is a deployment fact and it belongs here, before any
    // fault, rather than read back later as a missing withdrawal.
    message: `Agent ${agentId} must prepare a claimable slot on ${SHARED_ENVIRONMENT} before any fault is injected`,
  }).toBe(true);
  const deadBox = String(status.sandbox_id || '').trim();

  // ── The preconditions that make this the defect's scene ─────────────────
  const before = preparedSlotFacts(agentId);
  expect(before.state, 'the prepared manifest must be claimable before the fault').toBe('prepared');
  expect(
    before.placement,
    'the defect lives in the shared-slot branch of the claim; a conversation_box placement '
    + 'means this run is measuring a different mechanism and proves nothing about it',
  ).toBe('shared_slot');
  expect(before.claimedSessionId, 'the slot must be unclaimed when its box dies').toEqual('');
  expect(before.sandboxId, 'the manifest must name the box prepared-runtime reports').toBe(deadBox);
  expect(
    before.residentSandboxId,
    "a shared slot lives in the Agent's own resident box",
  ).toBe(deadBox);
  expect(
    documentsByField('sessions', '$.agent_id', agentId),
    'no conversation may be in this box: the kill must not be able to land on somebody else',
  ).toHaveLength(0);

  const deadHandle = await requireSandboxHandle(api, deadBox);
  expect(sandboxRunning(deadHandle), 'the prepared box must really exist before it is killed').toBe(true);

  // ── The death, out of band: the platform is never told ──────────────────
  killSandbox(deadHandle);
  await waitForSandboxStopped(deadHandle, KILL_CONVERGE_MS);
  expect(sandboxRunning(deadHandle), 'the prepared box must be gone before the question is asked').toBe(false);
  test.info().annotations.push({ type: 'e2e_dead_prepared_sandbox_id', description: deadBox });
  test.info().annotations.push({
    type: 'e2e_dead_prepared_slot_scene',
    description: JSON.stringify({ agentId, agentName, deadBox, slotId: before.slotId }),
  });

  // ── The platform records the death by itself ────────────────────────────
  // The browser has not been navigated: `page` is still about:blank, no message
  // has been sent and no recovery was requested. This is what separates
  // staleness from a race — while the pointer stands, the product has not yet
  // admitted the box is gone and a surviving manifest would be merely early.
  await expect.poll(
    () => preparedSlotFacts(agentId).residentSandboxId,
    {
      timeout: KILL_CONVERGE_MS,
      intervals: [1_000],
      message:
        'the background agent-box reap must clear the pointer to the confirmed-dead box '
        + 'with nobody in the product; without that this run cannot tell staleness from a race',
    },
  ).toBeNull();
  const stranded = preparedSlotFacts(agentId);
  expect(stranded.slotId, 'clearing the dead pointer must not have retired the manifest').toBe(before.slotId);

  // ── QUESTION 1: freshness, captured before any user action ──────────────
  // Captured, not yet asserted. A failed start schedules its own refill, so the
  // same read taken after the click would measure the product reacting to the
  // user rather than keeping its own advertisement honest.
  const freshnessDeadline = Date.now() + PREPARED_INVALIDATION_MS;
  let freshness: CapacityReading;
  for (;;) {
    const advertised = await platform.preparedRuntime(agentId);
    const sandboxId = String(advertised.sandbox_id || '').trim();
    const ready = advertised.ready === true;
    let advertisedBoxRuns: boolean | null = null;
    if (ready && sandboxId && sandboxId !== deadBox) {
      try {
        advertisedBoxRuns = sandboxRunning(await requireSandboxHandle(api, sandboxId));
      } catch {
        // Advertised capacity the platform's own backend cannot resolve. A
        // second ghost is not this spec's subject, so it is recorded rather
        // than asserted — the verdict below is about the corpse alone.
        advertisedBoxRuns = false;
      }
    }
    freshness = {
      at: new Date().toISOString(),
      ready,
      preparedCount: Number(advertised.prepared_count || 0),
      state: String(advertised.state || '').trim(),
      placement: String(advertised.placement || '').trim(),
      sandboxId,
      stillNamesTheCorpse: ready && sandboxId === deadBox,
      advertisedBoxRuns,
    };
    if (!freshness.stillNamesTheCorpse) break;
    if (Date.now() >= freshnessDeadline) break;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  const manifestAtClick = preparedSlotFacts(agentId);

  // ── QUESTION 2: the person who arrives ──────────────────────────────────
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must be offered on the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  // The real entry point. `api.startConversation` takes the same server path
  // but would not show whether a user can get there.
  await card.getByRole('button').click();

  // Resolve the click with a union rather than a bare wait, so a start the
  // picker refuses reports what the page said instead of a navigation timeout.
  // The note is AgentHome's ErrorNote, which Alert renders with role=alert; it
  // is read for its text, never matched against a localized string.
  const startFailure = page.getByRole('alert');
  await expect(
    page.getByTestId('run-view').or(startFailure).first(),
    'clicking a prewarmed Agent must reach the conversation or say why not',
  ).toBeVisible({ timeout: READY_TIMEOUT_MS });
  if (await startFailure.count()) {
    expect(
      (await startFailure.first().innerText()).trim(),
      `the picker refused to open a conversation on Agent ${agentId} after its prepared box `
      + `${deadBox} died; a stale slot must degrade to a cold start, not to a refusal`,
    ).toEqual('');
  }

  await page.waitForURL(/\/sessions\/[^/]+$/, { timeout: READY_TIMEOUT_MS });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  // Pushed before anything else can throw, so a later failure still retains it.
  sessions.push(sessionId);

  // Throws at once on a terminal state and quotes `last_error`, so the
  // failure — TERMINATED with runtime_unavailable, written by the lifecycle
  // worker after the claim connected to a destroyed box — is reported in
  // seconds with its cause instead of as a silent timeout.
  const ready = await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const liveBox = String(ready.sandbox_id || '').trim();
  expect(liveBox, 'the started conversation must hold a sandbox').not.toEqual('');
  expect(
    liveBox,
    'the conversation must run on replacement compute, never on the box that was destroyed',
  ).not.toBe(deadBox);
  expect(
    sandboxRunning(await requireSandboxHandle(api, liveBox)),
    'READY that names a box nothing runs is not a start',
  ).toBe(true);
  test.info().annotations.push({ type: 'e2e_replacement_sandbox_id', description: liveBox });

  // READY is not the same as usable: a conversation that cannot answer has not
  // started. One prompt, one reply, settled, with no failure surface in it.
  await expectComposerEnabled(page);
  const prompt = '请简短回复一句话，不要使用工具。';
  await sendPrompt(page, sessionId, prompt);
  await expect(
    page.getByTestId('user-message').filter({ hasText: prompt }),
    'the delivered input must land as exactly one transcript row',
  ).toHaveCount(1, { timeout: TURN_TIMEOUT_MS });
  const reply = page.getByTestId('assistant-message').last();
  await expect(reply, 'the conversation must answer').toBeVisible({ timeout: TURN_TIMEOUT_MS });
  await expect(reply).not.toBeEmpty();
  // A bubble that appeared is not a settled reply: while it carries
  // data-streaming=true a runtime error can still be appended after a negative
  // text assertion has already passed.
  await expect(reply).not.toHaveAttribute('data-streaming', 'true', { timeout: TURN_TIMEOUT_MS });
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'a reply that leaves the turn looking live is still a broken screen',
  ).toHaveAttribute('data-pulse', 'false', { timeout: TURN_TIMEOUT_MS });
  await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

  // ── The freshness verdict, on the reading taken before the click ─────────
  // Both readings are attached so a reviewer can see that the failed start's
  // own refill is not what made this green.
  const afterJourney = await platform.preparedRuntime(agentId);
  await test.info().attach('prepared-capacity-after-confirmed-death', {
    body: JSON.stringify({
      agentId,
      deadBox,
      liveBox,
      sessionId,
      strandedSlotId: before.slotId,
      beforeTheClick: freshness,
      manifestAtClick,
      afterTheJourney: {
        ready: afterJourney.ready,
        prepared_count: afterJourney.prepared_count,
        state: afterJourney.state,
        sandbox_id: afterJourney.sandbox_id,
      },
      manifestAfterTheJourney: preparedSlotFacts(agentId),
    }),
    contentType: 'application/json',
  });
  expect(
    freshness.stillNamesTheCorpse,
    `Agent ${agentId} was still advertising claimable prepared capacity in ${deadBox} `
    + `${PREPARED_INVALIDATION_MS}ms after the platform had itself converged that box as dead `
    + '(the row pointer was already cleared). Withdrawing the manifest, renewing it into a live '
    + 'box, or reaping it on a liveness probe all satisfy this; continuing to publish warm '
    + `capacity that does not exist does not. reading=${JSON.stringify(freshness)}`,
  ).toBe(false);
});
