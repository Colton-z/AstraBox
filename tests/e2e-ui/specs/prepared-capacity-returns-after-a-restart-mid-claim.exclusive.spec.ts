/**
 * E2E for the warm capacity a deploy takes away in the middle of handing it out.
 *
 * THE JOURNEY. A user opens a conversation on a prewarm-enabled Agent at the
 * moment the server goes down — a deploy, an OOM kill, a graceful shutdown
 * cancelling an in-flight start. The process dies between the claim CAS in
 * `claim_prepared_slot` (prepared_slots.py:1038-1046) and the
 * `clear_claimed_slot` that closes the hand-off at the end of startup
 * (engine/startup.py:434). The Agent's manifest is left reading `claimed` for a
 * Session that never adopted anything. The server comes back healthy and stays
 * up. Ten minutes later the same person returns and starts their conversation.
 *
 * The promise under test is that they do not pay for the outage: with the
 * deployment idle and nobody touching the console, prepared capacity comes back
 * on its own, and the conversation they then start claims it — 56ms instead of
 * the 17s (deepseek_harness) or 33s (pi) a cold start costs.
 *
 * WHERE TIME ENTERS, AND WHY IT IS SIMULATED. A claimed manifest belongs to its
 * claimer for `CLAIMED_SLOT_ORPHAN_SECONDS` — a hardcoded ten minutes
 * (prepared_slots.py:124) with no env-registry row, so no deployment setting
 * brings it inside a 180s wall. `retire_prepared_runtime` returns early inside
 * that fence on purpose (prepared_slots.py:405-422): until it expires, a
 * generation change or a prewarm toggle must not mistake a live hand-off for
 * debris. `claimed_at` is the single field that decides which side of the fence
 * a manifest is on, so ageing that one key is the deterministic hook —
 * `orphanClaimedSlot` (dbOracle.ts), one guarded statement, the way
 * `backdatePreparedSlot` is for a prepared slot's TTL and `lapseSessionSandboxLease`
 * is for a Session's box lease. This proves that a manifest PAST its fence is
 * revisited; it cannot prove the fence's own duration, and it deliberately does
 * not pin what happens INSIDE the fence — that boundary is unit-test work over
 * `_manifest_is_reapable`, and this spec would pass against a fix that reaped
 * eagerly and killed a live conversation.
 *
 * WHY THE AGEING HAPPENS AFTER THE RESTART. `ensure_bootstrap` schedules a
 * runtime reconciliation for every Agent on the first API read after a boot
 * (agent_service.py:86-99). That reconciliation is real and it runs here — but
 * with a fresh `claimed_at` it finds the manifest inside its fence, and
 * `prepare_slot_for_agent` returns early with "this refill has nothing to add"
 * (prepared_slots.py:670-672). Ageing afterwards is what keeps the verdict
 * honest: the product is equally broken for an orphan that ages while the
 * process stays up, and a spec that aged first would be testing boot-time
 * reconciliation instead. The read taken between the restart and the ageing
 * asserts exactly that — the restart alone did not heal it — so a green at the
 * end names the sweep and nothing else.
 *
 * NOT THE SIBLING SCENES. `_manifest_is_reapable` branches on the manifest's
 * own state (prepared_slots.py:211-229), and the other prepared-slot specs all
 * enter the `prepared` branch or leave no manifest at all:
 * prewarm-rebuilds-a-lost-slot-before-the-next-conversation discards the slot
 * and lets the box reaper take the box, so the row holds neither;
 * prepared-slot-survives-a-claim-during-its-renewal collides a claim with a
 * renewal and ends with the manifest CLEARED. `claimed` is the one state a
 * dying process can strand, it is judged against a different fence and a
 * different timestamp from the TTL, and a fix on the `prepared` branch leaves
 * this arm untested.
 *
 * WHAT IS ASSERTED, AND WHAT IS NOT. The verdict is an end state — claimable
 * capacity exists again, it is a different unit from the orphan, and a real
 * conversation takes it — never a function name or an error code. Reaping the
 * orphan and rebuilding, rebuilding beside it and clearing it after, or
 * refusing to strand it in the first place all satisfy this spec, because all
 * three leave the user fast. `ready` is never read alone: it is derived from
 * `state == "prepared"` (agent_service.py:253-254) and would go true for any
 * manifest at all.
 *
 * BUDGET, stated so a later edit cannot quietly push it over the 180s the
 * runner kills at. Agent create and first build ~30s, the claim worker ~15s,
 * the restart ~30s, the ageing ~1s, the background rebuild one watcher tick
 * (asserted to fit four times into its window) plus a measured 33s pi build,
 * and the returning user's conversation ~15s: about 141s. The margin is bought
 * by NOT opening a baseline conversation first. The control it would have given
 * — "the fast path worked before the fault" — is bought back more cheaply and
 * more strictly by the plant itself: the orphan is created by calling the
 * product's own `claim_prepared_slot`, which is the exact function a real start
 * calls, so a manifest that reads `claimed` afterwards IS the proof that this
 * Agent's capacity was claimable.
 */
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField, orphanClaimedSlot } from '../fixtures/dbOracle';
import { absoluteBaseUrl, appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Agent tenancy only. Conversation tenancy has no manifest to strand —
// `prepare_slot_for_agent` returns None for it (prepared_slots.py:620-625) and
// prepared-runtime answers out of the supplier's self-replenishing client pool,
// so this scene would be vacuous there.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
const RESTART_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_SERVER_RESTART_TIMEOUT_MS', 180_000);
// How long the background alone may take to put claimable capacity back: one
// watcher tick to notice the aged claim, plus a whole replacement engine child,
// measured at 17s (deepseek_harness) and 33s (pi), plus room to read a failure.
// A knob because the engine profile is the matrix's choice, not this file's.
const RESTORE_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_RESTORE_TIMEOUT_MS', 60_000);
// How far past its ten-minute fence the orphan is aged. Comfortably clear of it,
// so a clock skew of seconds between this runner and the server cannot decide
// the verdict.
const ORPHAN_AGE_MS = 30 * 60 * 1_000;
// The manifest's PUBLIC coordinates. `_prepared_slot` also carries the
// activation token and the model credential, and a failed `expect()` retains
// and prints both sides of its comparison.
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  claimedSessionId: string;
  claimedAt: string;
  runtimeGeneration: string;
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
    claimedSessionId: String(manifest.claimed_session_id || '').trim(),
    claimedAt: String(manifest.claimed_at || '').trim(),
    runtimeGeneration: String(row._runtime_generation || '').trim(),
  };
}

/**
 * The deployed sweep's tick interval, read from the server rather than assumed.
 *
 * `printenv` exits non-zero when the variable is unset, which `execFileSync`
 * raises. That is the right outcome — an unset interval means this deployment
 * runs the 300s default and no sweep can tick inside the restore window — but it
 * has to say so in words, because "the spec threw" and "this deployment cannot
 * host this journey" send a reader to different places.
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
      + `container ${JSON.stringify(server)}, so the deployment runs the 300s default and no `
      + 'background sweep can tick inside this spec\'s restore window. Set a few-second interval '
      + 'on the deployment, or raise ASTRABOX_E2E_PREPARED_RESTORE_TIMEOUT_MS. This is not a '
      + `product defect. (${String((error as Error).message).slice(0, 200)})`,
    );
  }
  const seconds = Number.parseInt(raw, 10);
  expect(
    Number.isFinite(seconds) && seconds > 0,
    `the deployed expiration-watcher interval must be a positive integer, got ${JSON.stringify(raw)}`,
  ).toBe(true);
  return seconds;
}

/** What the dying process left behind, as the product itself wrote it. */
interface OrphanedClaim {
  slotId: string;
  state: string;
  isolatedSessionId: string;
  sandboxId: string;
  claimedSessionId: string;
  claimedAt: string;
}

/**
 * Leave a real claimed manifest behind, the way a killed process leaves one.
 *
 * Everything here is production code: the Session row is the one a start writes
 * before it provisions anything (state CREATING, no sandbox, no
 * `startup_allocation`), the generation comes off the resolved harness exactly
 * as `claim_prepared_engine_sandbox` reads it (provisioning.py:1591), and the
 * claim is the product's own CAS. What the worker does NOT do is the point:
 * `clear_claimed_slot` is the line at engine/startup.py:434 that a dying
 * process never reaches, so it is never called.
 *
 * Writing the manifest by hand instead would fabricate the state and prove
 * nothing about whether the product can produce it.
 */
function plantOrphanedClaim(input: { agentId: string; sessionId: string }): OrphanedClaim {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.agent.prepared_slots import claim_prepared_slot
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.repository.session_repository import SessionRepository

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    agent_repo = AgentRepository()
    session_repo = SessionRepository()
    row = await agent_repo.get_agent(given['agentId'])
    assert isinstance(row, dict), 'the probe Agent must exist before its slot is claimed'
    template = await AgentConfigService(agent_repo, EnvironmentRepository()).resolve_agent_harness(
        given['agentId']
    )
    assert template is not None, 'the probe Agent must resolve through production configuration'
    generation = str(getattr(template, 'runtime_generation', '') or '').strip()
    assert generation, 'a prewarmed Agent carries a published runtime generation'
    # The row a start writes before it provisions: no allocation, no identity.
    await session_repo.create_session({
        'session_id': given['sessionId'], 'agent_id': given['agentId'],
        'user_id': str(row.get('created_by') or ''),
        'session_kind': 'agent_chat',
        'engine_kind': str(getattr(template, 'engine_kind', '') or ''),
        'state': 'CREATING', 'sandbox_id': None, 'runtime_unavailable': False,
        'title': 'restart mid-claim orphan probe',
    })
    planted = await session_repo.get_session(given['sessionId'])
    assert planted is not None, 'the claiming Session row must exist'
    assert not planted.get('startup_allocation'), 'the claim must precede any allocation'
    assert not planted.get('runtime_identity'), 'the claim must precede any adoption'
    claimed = await claim_prepared_slot(
        agent_id=given['agentId'],
        session_id=given['sessionId'],
        expected_runtime_generation=generation,
    )
    assert claimed is not None, 'the Agent advertised no claimable prepared slot'
    # Deliberately no clear_claimed_slot: that is the line the dying process
    # does not reach, and reaching it here would erase the scene.
    print('E2E_ORPHANED_CLAIM=' + json.dumps({
        'slotId': str(claimed.get('slot_id') or ''),
        'state': str(claimed.get('state') or ''),
        'isolatedSessionId': str(claimed.get('isolated_session_id') or ''),
        'sandboxId': str(claimed.get('sandbox_id') or ''),
        'claimedSessionId': str(claimed.get('claimed_session_id') or ''),
        'claimedAt': str(claimed.get('claimed_at') or ''),
    }))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8',
    timeout: 120_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const line = raw.split('\n').find((entry) => entry.startsWith('E2E_ORPHANED_CLAIM='));
  expect(line, 'the production claim worker must publish what it claimed').toBeTruthy();
  return JSON.parse(line!.slice('E2E_ORPHANED_CLAIM='.length)) as OrphanedClaim;
}

let agentId = '';
const sessions = trackSessions();
// Registered after the tracker, because afterEach hooks run in registration
// order: the sessions go before the Agent they ran on. A failure keeps both —
// the orphaned manifest and the planted CREATING row ARE the evidence.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('prepared capacity orphaned by a restart mid-claim is rebuilt in the background, and the next conversation claims it', async ({
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
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const scanInterval = deployedWatcherIntervalSeconds(server);
  // "We waited long enough" has to be a fact about this deployment. A sweep
  // whose period does not fit several times into the restore window would make
  // a red mean "the sweep had not run yet" instead of "the sweep does not
  // revisit an orphaned claim".
  expect(
    scanInterval * 1_000,
    'the restore window must span several real background sweeps of this deployment',
  ).toBeLessThanOrEqual(RESTORE_MS / 4);

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const agentName = `__e2e_restart_claim_${Date.now()}_${test.info().workerIndex}`;
  const created = await api.createAgent({
    name: agentName,
    model: await api.configuredAgentModel(RESEARCH_AGENT, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  agentId = String(created.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');

  // ── The warm capacity this spec is about ────────────────────────────────
  // An engine adapter without the input-free preparation seam never publishes a
  // manifest (prepared_slots.py:629-636). That is a deployment fact and it
  // belongs here, before any fault, rather than read back later as a missing
  // rebuild.
  await expect.poll(async () => {
    const status = await platform.preparedRuntime(agentId);
    return Boolean(
      status.ready === true
      && Number(status.prepared_count || 0) > 0
      && String(status.sandbox_id || '').trim(),
    );
  }, {
    timeout: POOL_TIMEOUT_MS,
    intervals: [2_000],
    message: `Agent ${agentId} must prepare a claimable slot on ${SHARED_ENVIRONMENT} before any fault is injected`,
  }).toBe(true);

  const before = preparedSlotFacts(agentId);
  expect(before.state, 'the manifest must be claimable before the claim').toBe('prepared');
  expect(before.placement, 'Agent tenancy prepares a shared slot, not a pooled box').toBe('shared_slot');
  expect(before.sandboxId, 'the manifest must name a box').not.toEqual('');
  expect(before.isolatedSessionId, 'the manifest must name its isolated session').not.toEqual('');

  // ── The start that never finished ───────────────────────────────────────
  // The id is minted here so the planted row is retained on a failure even if
  // the worker throws after creating it.
  const deadSessionId = randomUUID();
  sessions.push(deadSessionId);
  const orphan = plantOrphanedClaim({ agentId, sessionId: deadSessionId });
  expect(
    orphan.state,
    'the plant must leave the product\'s own claimed manifest, not a hand-written one',
  ).toBe('claimed');
  expect(orphan.slotId, 'the claim must have taken the slot this spec observed').toBe(before.slotId);
  expect(orphan.claimedSessionId, 'the manifest must name the Session that died holding it').toBe(deadSessionId);
  const stranded = preparedSlotFacts(agentId);
  expect(stranded.state, 'the Agent row must now carry the claimed manifest').toBe('claimed');
  expect(
    stranded.claimedSessionId,
    'the durable row — not just the worker\'s return value — must name the Session that died holding it',
  ).toBe(deadSessionId);
  expect(stranded.claimedAt, 'a claimed manifest is stamped with the moment of its hand-off').not.toEqual('');
  test.info().annotations.push({
    type: 'orphaned_claim_scene',
    description: JSON.stringify({ agentId, agentName, deadSessionId, orphan }),
  });

  // ── The journey's event ─────────────────────────────────────────────────
  // A real `docker restart` plus the container-health and /healthz wait. The
  // round splits its files on whether the source names this helper and runs
  // that group in a pass of its own (run-round.mjs:175-176, 195-197), so the
  // deployment is not restarted under another worker.
  await restartServerContainer(absoluteBaseUrl(), RESTART_TIMEOUT_MS);
  test.info().annotations.push({
    type: 'server_returned',
    description: 'the server container restarted and answered /healthz before the fence was aged',
  });

  // ── The restart alone did not heal it ───────────────────────────────────
  // Boot-time reconciliation really does run (agent_service.py:86-99), and with
  // a fresh `claimed_at` it finds the manifest inside its fence and declines.
  // Asserting that here is what lets the verdict below name the sweep: without
  // it, a green could be the boot doing the work.
  const afterRestart = await platform.preparedRuntime(agentId);
  expect(
    String(afterRestart.state || ''),
    'the restart must leave the orphaned claim exactly where the dead process left it',
  ).toBe('claimed');
  expect(
    afterRestart.ready,
    'a claimed manifest is not claimable capacity, and the readout must not call it ready',
  ).toBe(false);
  expect(preparedSlotFacts(agentId).slotId, 'the restart must not have replaced the orphan').toBe(orphan.slotId);

  // ── Ten minutes later, where time enters the mechanism ──────────────────
  const agedAt = new Date(Date.now() - ORPHAN_AGE_MS).toISOString();
  expect(
    orphanClaimedSlot(agentId, orphan.slotId, agedAt),
    'the ageing must land on exactly the orphaned claim and leave everything else alone',
  ).toEqual([{
    agent_id: agentId,
    slot_id: orphan.slotId,
    state: 'claimed',
    claimed_at: agedAt,
    claimed_session_id: deadSessionId,
  }]);

  // ── The verdict, with nobody in the product ─────────────────────────────
  // No page, no conversation, no Agent write — each of those is a refill
  // trigger and any one of them would manufacture a green. `ready` is never
  // read alone: identity first, then claimability.
  await page.goto('about:blank');
  await expect.poll(async () => {
    const restored = await platform.preparedRuntime(agentId);
    const sandboxId = String(restored.sandbox_id || '').trim();
    return Boolean(
      restored.ready === true
      && String(restored.state || '') === 'prepared'
      && Number(restored.prepared_count || 0) > 0
      && sandboxId,
    );
  }, {
    timeout: RESTORE_MS,
    intervals: [2_000],
    message:
      `no background path rebuilt prewarm for Agent ${agentId}: slot ${orphan.slotId} still reads `
      + `claimed for Session ${deadSessionId}, which died in the restart and will never clear it. `
      + 'The only thing left to collect it is the next person missing their claim',
  }).toBe(true);

  const rebuilt = preparedSlotFacts(agentId);
  expect(rebuilt.state, 'the republished manifest must be claimable').toBe('prepared');
  expect(
    rebuilt.slotId,
    'a rebuild publishes a NEW unit; the orphan relabelled would hand the next conversation the dead claim\'s placement',
  ).not.toBe(orphan.slotId);
  expect(
    rebuilt.isolatedSessionId,
    'the rebuilt slot must be a new engine child, not the one the dead claim took',
  ).not.toBe(orphan.isolatedSessionId);
  expect(
    rebuilt.runtimeGeneration,
    'nothing about this Agent was configured, so this is a renewal of capacity and not a rotation',
  ).toBe(before.runtimeGeneration);
  await test.info().attach('prepared-capacity-after-restart-mid-claim', {
    body: JSON.stringify({ agentId, orphan, rebuilt, watcherIntervalSeconds: scanInterval }),
    contentType: 'application/json',
  });

  // ── The person who comes back ───────────────────────────────────────────
  const claimAt = Date.now();
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname));
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  sessions.push(sessionId);
  await expectComposerEnabled(page);
  await api.waitForSessionReady(sessionId);
  // Evidence, not an assertion: a wall-clock threshold here would be engine
  // dependent (17s deepseek_harness against 33s pi, cold) and would go flaky.
  // The identity oracle below decides warm versus cold deterministically.
  test.info().annotations.push({
    type: 'click_to_usable_composer_ms',
    description: String(Date.now() - claimAt),
  });

  // ── WARM, NOT COLD, AND NOT THE DEAD CLAIM'S ───────────────────────────
  // Landing in the right box is not yet a claim: a cold start reaches the same
  // Agent box and builds its own isolation session there. The isolated session
  // is what separates the two.
  const detail = await api.adminSessionDetail(sessionId);
  const adopted = String(
    (detail.runtime_identity || {}).isolated_session_id || '',
  ).trim();
  expect(
    adopted,
    'the returning conversation must claim the rebuilt slot rather than cold-start beside it — '
    + 'the person who arrives after an outage is not the one who should pay to re-arm prewarm',
  ).toBe(rebuilt.isolatedSessionId);
  expect(
    adopted,
    'and it must never inherit the placement the dead claim was holding',
  ).not.toBe(orphan.isolatedSessionId);
  expect(
    String(detail.sandbox_id || ''),
    'the conversation must land in the box the rebuilt slot names',
  ).toBe(rebuilt.sandboxId);
});
