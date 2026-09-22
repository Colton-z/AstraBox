/**
 * E2E: an Agent whose warm capacity failed to build gets it back without a user.
 *
 * Preparing a slot is a build, and a build can fail — a node with no room for a
 * moment, a provider blip, a gateway that answered 503 once. When it does,
 * `_reconcile_agent_runtime` records the reason on the Agent row under
 * `_prepared_runtime_error` and re-raises (agent_service.py:484-494); the task's
 * done-callback only logs (:348-364). That leaves the Agent with
 * `prewarm_enabled: true`, no manifest, and — once the agent-box reaper finds
 * the emptied box unoccupied and destroys it — no resident box either. Neither
 * the claim path nor any Session-paid refill will ever look at this row, because
 * every one of those is triggered by somebody arriving: a Session that missed
 * its claim (engine/provisioning.py:1614), an activation outcome
 * (engine/startup.py:453,456), an Agent config or extension write, or the
 * process bootstrap that reconciles every Agent at start
 * (agent_service.py:96-98). Without a background keeper the staleness is charged
 * to the next person: they open a conversation, find no slot, and pay a cold
 * start whose cost is a whole engine child, against a claim of milliseconds.
 *
 * THE KEEPER THIS EXERCISES. The expiration watcher calls
 * `keep_prewarmed_agents_ready` on every tick (expiration_watcher.py:179,
 * runtime_manager.py:2831). It lists candidates with
 * `list_prewarm_enabled_agents` (agent_repository.py:122), which selects on
 * `prewarm_enabled` alone — a row with no box and no manifest is exactly what it
 * is meant to reach — and for a row holding no manifest it schedules a
 * reconciliation once per `PREPARED_SLOT_REBUILD_RETRY_SECONDS`
 * (runtime_manager.py:2892-2899), so a build that keeps failing is retried on a
 * cadence rather than on every tick. This spec is the end-to-end gate on that
 * branch: the one a repair of the TTL-renewal rule alone does not reach, because
 * there is no manifest here for a staleness rule to judge.
 *
 * WHAT MAKES THE SCENE REAL. The state is composed from production functions
 * rather than by forcing a raise: the product's own `discard_prepared_slot`
 * closes both isolated sessions, releases the box admission and clears the
 * manifest under CAS, and then the exact write the failure path makes records
 * why. Within a tick the agent-box reaper finds the emptied box unoccupied and
 * destroys it, nulling `sandbox_id` (`reap_abandoned_agent_boxes`,
 * runtime_manager.py:2922), and it runs earlier in the same tick than the
 * prewarm sweep (expiration_watcher.py:159 vs :179) — so by the time the sweep
 * looks, the row is in the shape a real failed build leaves. What this spec does
 * not prove is that the failure path itself writes that state; that belongs to a
 * unit test over `_reconcile_agent_runtime`.
 *
 * SCOPE. This is about a TRANSIENT failure whose cause has cleared. A permanent
 * one — a bad Environment, a genuinely full node — fails every sweep tick
 * identically and no background path can fix it; read `last_error` on a red run
 * before blaming the sweep. Conversation tenancy is excluded by the placement
 * assertion for a related reason: there the capacity is a supplier client pool
 * that replenishes itself, a different mechanism this sweep does not touch.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Deployment fixtures: read and asserted present, never created or rewritten.
// Agent tenancy only — under the conversation-tenancy Environment there is no
// manifest to lose, so the scene would be vacuous rather than merely different.
const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();

const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
// How long the background alone may take to notice an Agent that lost its slot
// and put claimable capacity back. The cost is the watcher's own period plus a
// full replacement build — a box acquired from the pool and an engine child
// started in it — which is tens of seconds for every engine, not milliseconds.
const SELF_HEAL_MS = parseTimeoutEnv('ASTRABOX_E2E_SLOT_SELF_HEAL_MS', 90_000);
// Ceiling on the user-visible arrival once warm capacity exists. Coarse on
// purpose: it catches a 33s cold start and is a budget guard for the rest. It is
// NOT what proves the claim — the isolated-session identity below is, and that
// one discriminates for every engine.
const CLAIM_READY_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_CLAIM_READY_MS', 20_000);

// How many watcher ticks must fit inside the self-heal window for a red result
// to mean anything. Fewer than this and the sweep never got its chance, so the
// spec would be measuring its own impatience.
const REQUIRED_WATCHER_TICKS = 4;

/** The manifest's PUBLIC coordinates, plus the row pointer the sweeps key on. */
interface SlotFacts {
  slotId: string;
  state: string;
  placement: string;
  sandboxId: string;
  isolatedSessionId: string;
  runtimeGeneration: string;
  residentSandboxId: string | null;
}

/**
 * Read one Agent's preparation without letting its secrets into an assertion.
 *
 * `_prepared_slot` also carries the activation token and the model credential.
 * A failed `expect()` retains and prints both sides of the comparison, so only
 * the named coordinates are lifted out of it.
 */
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
    runtimeGeneration: String(row._runtime_generation || '').trim(),
    residentSandboxId: String(row.sandbox_id || '').trim() || null,
  };
}

/**
 * The deployed expiration watcher's tick interval, read from the server rather
 * than assumed.
 *
 * `printenv` exits non-zero when the variable is unset, which `execFileSync`
 * raises. That is the right outcome — an unset interval means the deployment
 * runs the 300s default and no sweep can tick inside this spec's window — but it
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
      + "background sweep can tick inside this spec's self-heal window. Set a few-second "
      + 'interval on the deployment, or raise ASTRABOX_E2E_SLOT_SELF_HEAL_MS above '
      + `${REQUIRED_WATCHER_TICKS} ticks. This is not a product defect. `
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

/** What the driver left behind, read back through the repository it wrote with. */
interface FailedBuildScene {
  slotId: string;
  sandboxId: string;
  discarded: boolean;
  errorRecorded: boolean;
  hasManifest: boolean;
  rowSandboxId: string | null;
  lastError: string | null;
}

/**
 * Leave the row in the state a failed slot build leaves it in, using the
 * product's own teardown and the product's own failure write.
 *
 * `discard_prepared_slot` closes both isolated sessions, releases the box
 * admission and clears the manifest under CAS — the same call the refill path
 * uses to retire a slot it could not keep. The `_prepared_runtime_error` write
 * that follows is the one `_reconcile_agent_runtime` makes when a build raises
 * (agent_service.py:487-493), version-guarded the same way, so a lost CAS is
 * reported rather than silently skipped.
 */
function recordAFailedSlotBuild(input: {
  agentId: string;
  reason: string;
  message: string;
}): FailedBuildScene {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.agent.prepared_slots import (
    PREPARED_SLOT_FIELD,
    discard_prepared_slot,
)
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository import AgentRepository

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    agent_id = given['agentId']
    repo = AgentRepository()
    row = await repo.get_agent(agent_id)
    assert isinstance(row, dict), 'the probe Agent must have a row'
    manifest = row.get(PREPARED_SLOT_FIELD)
    assert isinstance(manifest, dict), 'there is no prepared slot to lose'
    slot_id = str(manifest.get('slot_id') or '')
    sandbox_id = str(manifest.get('sandbox_id') or '')
    await discard_prepared_slot(
        agent_id, manifest, reason=given['reason'], agent_repo=repo
    )
    row = await repo.get_agent(agent_id)
    assert isinstance(row, dict), 'the probe Agent row vanished during the discard'
    discarded = not isinstance(row.get(PREPARED_SLOT_FIELD), dict)
    version = row.get('version') if 'version' in row else {'$exists': False}
    recorded = await repo.compare_and_update_agent(
        agent_id,
        expected={'version': version},
        updates={'_prepared_runtime_error': given['message']},
    )
    row = await repo.get_agent(agent_id)
    assert isinstance(row, dict), 'the probe Agent row vanished after the failure write'
    print('E2E_FAILED_BUILD_RESULT=' + json.dumps({
        'slotId': slot_id,
        'sandboxId': sandbox_id,
        'discarded': discarded,
        'errorRecorded': bool(recorded),
        'hasManifest': isinstance(row.get(PREPARED_SLOT_FIELD), dict),
        'rowSandboxId': str(row.get('sandbox_id') or '') or None,
        'lastError': str(row.get('_prepared_runtime_error') or '') or None,
    }))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8',
    timeout: 120_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const line = raw.split('\n').find((entry) => entry.startsWith('E2E_FAILED_BUILD_RESULT='));
  expect(line, 'the failed-build driver must publish its result').toBeTruthy();
  return JSON.parse(line!.slice('E2E_FAILED_BUILD_RESULT='.length)) as FailedBuildScene;
}

let agentId = '';
const sessions = trackSessions();
// Registered after the tracker, because afterEach hooks run in registration
// order: the conversation goes before the Agent it ran on. A failure keeps both,
// including whatever box the Agent holds — that row is the scene.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('an Agent whose slot build failed warms itself again before the next conversation arrives', async ({
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

  // ── Premise, made visible before anything is spent ──────────────────────
  // A watcher that is not running produces a timeout indistinguishable from the
  // defect, so "we waited long enough" has to be a fact about this deployment.
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const watcherIntervalSeconds = deployedWatcherIntervalSeconds(server);
  expect(
    watcherIntervalSeconds * REQUIRED_WATCHER_TICKS * 1_000,
    `HARNESS PREREQUISITE: ${REQUIRED_WATCHER_TICKS} expiration-watcher ticks of `
    + `${watcherIntervalSeconds}s must fit inside the ${SELF_HEAL_MS}ms self-heal window, or a red `
    + 'result would only prove the sweep had no chance to run',
  ).toBeLessThanOrEqual(SELF_HEAL_MS);

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  agentId = '';
  const agentName = `__e2e_slot_self_heal_${Date.now()}_${test.info().workerIndex}`;
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
    // An engine adapter that does not implement the input-free prepare seam
    // never publishes a manifest. That is a deployment fact, and it belongs
    // here — before the fault — rather than read back as a missing rebuild.
    message: `Agent ${agentId} must prepare a claimable slot on ${SHARED_ENVIRONMENT} before any fault is injected`,
  }).toBe(true);

  const before = preparedSlotFacts(agentId);
  expect(before.state, 'the prepared manifest must be claimable before the fault').toBe('prepared');
  expect(
    before.placement,
    'Agent tenancy prepares a shared slot; a conversation-tenancy pool replenishes itself and is a '
    + 'different mechanism from the one under test',
  ).toBe('shared_slot');
  expect(before.sandboxId, 'the manifest must name the box prepared-runtime reports').toBe(
    String(status.sandbox_id || '').trim(),
  );
  expect(before.isolatedSessionId, 'the prepared slot must name its isolated session').not.toEqual('');
  test.info().annotations.push({
    type: 'prewarm_failed_build_scene',
    description: JSON.stringify({
      agentId, agentName, slotId: before.slotId, sandboxId: before.sandboxId,
    }),
  });

  // ── The last build failed ───────────────────────────────────────────────
  const failureMessage = 'RuntimeError: e2e simulated slot build failure';
  const scene = recordAFailedSlotBuild({
    agentId,
    reason: 'e2e: slot build failed',
    message: failureMessage,
  });
  expect(scene.slotId, 'the driver must have acted on the slot this spec prepared').toBe(before.slotId);
  expect(scene.discarded, 'the manifest must really be gone — a lost CAS is not this scene').toBe(true);
  expect(scene.hasManifest, 'the row must hold no manifest once the build has failed').toBe(false);
  expect(
    scene.errorRecorded,
    'the failure write must land, or the row would not say why this Agent has no capacity',
  ).toBe(true);
  expect(scene.lastError, 'the row must remember the failure the way the product records it').toBe(failureMessage);

  // ── Prove the scene from the product's own read, not from the database ──
  const broken = await platform.preparedRuntime(agentId);
  expect(broken.enabled, 'the Agent must still want warm capacity').toBe(true);
  expect(broken.ready, 'the Agent must advertise no claimable capacity').toBe(false);
  expect(Number(broken.prepared_count ?? -1), 'nothing is prepared').toBe(0);
  expect(String(broken.state ?? ''), 'there is no manifest left to have a state').toEqual('');
  expect(
    String(broken.last_error ?? ''),
    'the product itself must say this Agent\'s last slot build failed',
  ).toBe(failureMessage);

  // Recorded, not asserted. Within a tick the agent-box reaper finds the emptied
  // box unoccupied, destroys it and nulls the row pointer, so the row is read
  // here either still holding its box or already stripped of it. The prewarm
  // sweep reaches it under both shapes — `list_prewarm_enabled_agents` selects
  // on `prewarm_enabled` and not on the pointer — so pinning one of them would
  // make this spec fail on the reaper's timing instead of on its own question.
  await test.info().attach('agent-row-after-the-failed-build', {
    body: JSON.stringify({ agentId, scene, residentSandboxId: preparedSlotFacts(agentId).residentSandboxId }),
    contentType: 'application/json',
  });

  // ── THE QUESTION, with nobody in the product ────────────────────────────
  // From here to the verdict nothing may be touched: a conversation, an Agent
  // or extension write and a server restart are each a refill trigger, and any
  // one of them would rebuild the slot for a harness reason.
  await page.goto('about:blank');
  expect(
    documentsByField('sessions', '$.agent_id', agentId),
    'no Session may exist when the poll starts: a Session that misses its claim schedules the '
    + 'identical reconciliation (engine/provisioning.py:1614) and would turn this green for the '
    + 'wrong reason',
  ).toHaveLength(0);

  let restored: Record<string, unknown> = {};
  await expect.poll(async () => {
    restored = await platform.preparedRuntime(agentId);
    return Boolean(
      restored.ready === true
      && Number(restored.prepared_count || 0) > 0
      && String(restored.sandbox_id || '').trim(),
    );
  }, {
    timeout: SELF_HEAL_MS,
    intervals: [2_000],
    message:
      'an Agent that lost its prepared slot must be rebuilt in the background, not at the next '
      + `user's expense: Agent ${agentId} recorded "${failureMessage}" after slot ${before.slotId} `
      + 'went away, and with no Session, no config write and no restart it is still advertising '
      + 'nothing — the only thing left to rebuild it is the next person paying the cold start',
  }).toBe(true);

  const after = preparedSlotFacts(agentId);
  expect(after.slotId, 'a rebuild publishes a new manifest, not the one that was lost').not.toBe(before.slotId);
  expect(after.state, 'the rebuilt slot must be claimable, not merely present').toBe('prepared');
  expect(after.placement, 'the rebuild must be the same kind of capacity that was lost').toBe('shared_slot');
  expect(after.isolatedSessionId, 'the rebuilt slot must name its isolated session').not.toEqual('');
  expect(after.sandboxId, 'the rebuilt manifest must name the box prepared-runtime reports').toBe(
    String(restored.sandbox_id || '').trim(),
  );
  expect(
    documentsByField('sessions', '$.agent_id', agentId),
    'the repair must be background-paid: no conversation existed to pay for it',
  ).toHaveLength(0);
  await test.info().attach('prewarm-rebuilt-after-a-failed-build', {
    body: JSON.stringify({
      agentId,
      lostSlotId: before.slotId,
      lostSandboxId: before.sandboxId,
      rebuiltSlotId: after.slotId,
      rebuiltSandboxId: after.sandboxId,
    }),
    contentType: 'application/json',
  });

  // ── The person who arrives afterwards ───────────────────────────────────
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `${agentName} must appear in the Agent picker`).toBeVisible({
    timeout: READY_TIMEOUT_MS,
  });
  const clickedAt = Date.now();
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname));
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  // Pushed before anything else can throw, so a later failure still retains it.
  sessions.push(sessionId);
  await expectComposerEnabled(page);
  await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const arrivalMs = Date.now() - clickedAt;

  // Landing in the rebuilt box is not yet a claim: a cold start would reach the
  // same box and build its own isolation session beside the prepared one. The
  // isolated-session identity is what separates the two, for every engine.
  const claimed = await api.adminSessionDetail(sessionId);
  expect(
    String(claimed.sandbox_id || ''),
    'the person must land on the rebuilt warm capacity',
  ).toBe(after.sandboxId);
  expect(
    String((claimed.runtime_identity || {}).isolated_session_id || '').trim(),
    'the conversation must claim the rebuilt slot rather than cold-start beside it',
  ).toBe(after.isolatedSessionId);
  await test.info().attach('arrival-on-the-rebuilt-slot', {
    body: JSON.stringify({ sessionId, arrivalMs, slotId: after.slotId, sandboxId: after.sandboxId }),
    contentType: 'application/json',
  });
  expect(
    arrivalMs,
    'claiming prepared capacity was measured at 56ms against cold starts of 17s '
    + '(deepseek_harness) and 33s (pi); an arrival this slow means the conversation paid for a '
    + 'runtime rather than taking the one that was waiting',
  ).toBeLessThan(CLAIM_READY_MS);
});
