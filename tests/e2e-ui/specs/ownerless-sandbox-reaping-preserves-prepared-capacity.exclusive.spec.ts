/**
 * Reclaim a real box whose Session lost its binding without reclaiming another
 * conversation, its prepared shared slot, or the supplier's unclaimed box, nor
 * a pool box taken for its seats before the seats are recorded.
 * The worker uses the production reaper's zero-grace argument so this test
 * exercises ownership rather than spending ten minutes aging a new sandbox.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { nextLedgerSeat } from '../fixtures/preparedSeats';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

/** The columns that bind a Session to its box: what the fault clears and the restore puts back. */
interface SessionBinding {
  sandbox_id: string | null;
  sandbox_backend: string | null;
  /** `startup_allocation` as JSON text, so it is restored byte for byte. */
  startup_allocation: string | null;
}

let orphanSessionId = '';
let orphanBefore: SessionBinding | undefined;
const agents: string[] = [];

/** Run one write against the deployment database; the key column of each row it changed. */
function writeRows(sql: string): string[] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    sql,
  );
}

function sessionBinding(sessionId: string): SessionBinding {
  const rows = postgresRows<SessionBinding>(
    `SELECT sandbox_id, sandbox_backend, startup_allocation::text AS startup_allocation `
      + `FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  );
  expect(rows, `session ${sessionId} must have exactly one row`).toHaveLength(1);
  return rows[0];
}

function nullableLiteral(value: string | null): string {
  return value === null ? 'NULL' : sqlLiteral(value);
}

// Restore only the binding the fault cleared, and only while the row still
// carries the cleared binding: a row something re-bound meanwhile is left as it is.
onPassOnly(async () => {
  if (!orphanBefore) return;
  const restored = writeRows(
    `UPDATE sessions SET sandbox_id = ${nullableLiteral(orphanBefore.sandbox_id)}, `
      + `sandbox_backend = ${nullableLiteral(orphanBefore.sandbox_backend)}, `
      + `startup_allocation = ${nullableLiteral(orphanBefore.startup_allocation)}::jsonb `
      + `WHERE session_id = ${sqlLiteral(orphanSessionId)} AND sandbox_id IS NULL `
      + `AND sandbox_backend IS NULL AND startup_allocation IS NULL RETURNING session_id`,
  );
  if (restored.length !== 1) {
    throw new Error(`the cleared binding of session ${orphanSessionId} was not restored: ${JSON.stringify(restored)}`);
  }
});
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agents) await api.deleteAgent(agentId);
});

async function prepared(platform: PlatformApi, agentId: string): Promise<Record<string, unknown>> {
  let last: Record<string, unknown> = {};
  await expect.poll(async () => {
    last = await platform.preparedRuntime(agentId);
    return Boolean(last.ready && last.sandbox_id && last.client_pool_name);
  }, { timeout: 60_000, message: `Agent ${agentId} must prepare real claimable capacity` }).toBe(true);
  return last;
}

function preparedIdentity(agentId: string): Record<string, unknown> {
  // The spare seat left in the box the live conversation took.
  const manifest: Record<string, unknown> = nextLedgerSeat(agentId) || {};
  // Preparation also contains credentials; only public runtime coordinates may
  // reach an assertion's retained expected/actual values.
  return Object.fromEntries([
    'slot_id', 'state', 'sandbox_id', 'isolated_session_id',
    'terminal_isolated_session_id', 'home_dir',
  ].map((key) => [key, manifest[key]]));
}

function reapWithoutAgeDelay(input: {
  backend: string;
  orphan: string;
  shared: string;
  idle: string;
  poolName: string;
}): { summary: Record<string, number>; probes: Record<string, string>; idleIds: string[] } {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.seams.sandbox import sandbox_for_name

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    session_repo = SessionRepository()
    agent_repo = AgentRepository()
    orphan = given['orphan']
    assert await session_repo.find_session_by_sandbox_id(orphan) is None, 'orphan still has a Session owner'
    assert not await agent_repo.list_agents_by_sandbox_id(orphan), 'orphan still has an Agent owner'
    assert await provider.count_active_isolated_sessions(orphan) == 0, 'orphan is not empty in execd'
    inventory = await provider.list_sandboxes(page=1, page_size=100)
    assert not inventory.has_next_page, 'test inventory exceeds the reaper page'
    ids = {item.sandbox_id for item in inventory.items}
    assert {given['orphan'], given['shared'], given['idle']} <= ids, 'fixture box missing before sweep'
    pool = await provider.describe_client_pool(given['poolName'])
    assert given['idle'] in pool.idle_sandbox_ids, 'supplier has not published the protected idle box'
    summary = await RemoteAgentRuntimeManager().reap_ownerless_sandboxes(
        backend=given['backend'], grace_seconds=0, page_size=100,
    )
    probes = {}
    for name in ('orphan', 'shared', 'idle'):
        probes[name] = (await provider.probe(given[name])).probe_status
    pool = await provider.describe_client_pool(given['poolName'])
    print('E2E_OWNERLESS_RESULT=' + json.dumps({
        'summary': summary, 'probes': probes, 'idleIds': list(pool.idle_sandbox_ids),
    }))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8',
    timeout: 60_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const result = raw.split('\n').find((line) => line.startsWith('E2E_OWNERLESS_RESULT='));
  expect(result, 'the real reaper worker must publish its result').toBeTruthy();
  return JSON.parse(result!.slice('E2E_OWNERLESS_RESULT='.length));
}

function reapDuringRealPoolAdoption(input: {
  backend: string;
  agentId: string;
  poolName: string;
}): Record<string, unknown> {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys
from unittest.mock import patch

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.agent import prepared_slots
from astrabox.core.service.orchestrator.agent.client_pool import ensure_agent_client_pool
from astrabox.core.service.orchestrator.agent.runtime_generation import agent_runtime_owner_id
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.runtime.sandbox_client import extract_sandbox_id
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.seams.sandbox import sandbox_for_name

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    agent_repo = AgentRepository()
    template = await AgentConfigService(agent_repo, EnvironmentRepository()).resolve_agent_harness(given['agentId'])
    assert template is not None, 'the probe Agent must resolve through production configuration'
    manager = RemoteAgentRuntimeManager()
    plan = await ensure_agent_client_pool(template, runtime_manager=manager)
    assert plan is not None and plan.spec.pool_name == given['poolName'], 'adoption must use the existing Agent pool'
    async def wait_for_idle():
        while True:
            status = await provider.describe_client_pool(given['poolName'])
            if status.idle_sandbox_ids:
                return
            await asyncio.sleep(0.25)
    await asyncio.wait_for(wait_for_idle(), timeout=60)
    evidence = []
    original_adopt = type(provider).adopt_sandbox_identity

    # The window under test: the box has left the pool and lost its pool
    # identity, and its seats are live in it, but no Agent ledger names them yet.
    async def adopt_then_reap(adapter, handle, *, session_id, assignment_id):
        await original_adopt(adapter, handle, session_id=session_id, assignment_id=assignment_id)
        sandbox_id = extract_sandbox_id(handle)
        descriptor = await adapter.describe_sandbox(sandbox_id)
        assert not adapter.owns_unclaimed_sandbox(descriptor), 'probe must run after pool identity is removed'
        owner = await adapter.claim_of(sandbox_id, expected_session_id=agent_runtime_owner_id(given['agentId']))
        assert owner.may_destroy, 'adoption must publish the real Agent runtime owner'
        assert await agent_repo.get_agent(given['agentId']) is not None, 'adopting Agent disappeared'
        slots = await agent_repo.list_slots(given['agentId'])
        assert all(seat.get('sandbox_id') != sandbox_id for seat in slots), 'the seats were recorded before the handoff window'
        assert not await agent_repo.list_agents_by_sandbox_id(sandbox_id), 'the box is already an Agent resident box'
        live = await adapter.count_active_isolated_sessions(sandbox_id)
        assert live > 0, 'a pool box must arrive carrying its seats'
        inventory = await adapter.list_sandboxes(page=1, page_size=100)
        assert not inventory.has_next_page, 'test inventory exceeds the reaper page'
        assert sandbox_id in {item.sandbox_id for item in inventory.items}, 'sweep cannot see the candidate'
        summary = await manager.reap_ownerless_sandboxes(backend=given['backend'], grace_seconds=0, page_size=100)
        probe = (await adapter.probe(sandbox_id)).probe_status
        assert probe == 'OK', 'the real reaper destroyed a pool box between its adoption and its seat record'
        evidence.append({'sandboxId': sandbox_id, 'liveSessions': live, 'summary': summary, 'probe': probe})

    with patch.object(type(provider), 'adopt_sandbox_identity', adopt_then_reap):
        adopted = await prepared_slots._adopt_pool_box(template, runtime_manager=manager, repo=agent_repo)
    assert adopted and len(evidence) == 1, 'production adoption did not cross the observed handoff'
    sandbox_id = evidence[0]['sandboxId']
    slots = await agent_repo.list_slots(given['agentId'])
    entered = [seat for seat in slots if seat.get('sandbox_id') == sandbox_id]
    assert entered and all(seat.get('state') == 'prepared' for seat in entered), 'the adopted box seats must reach the ledger'
    kept = await manager.reap_ownerless_sandboxes(backend=given['backend'], grace_seconds=0, page_size=100)
    assert (await provider.probe(sandbox_id)).probe_status == 'OK', 'a box whose seats are recorded is not an orphan'
    print('E2E_POOL_HANDOFF_RESULT=' + json.dumps({'adoptions': evidence, 'enteredSeats': len(entered), 'kept': kept}))

asyncio.run(main())
`;
  const raw = execFileSync('docker', ['exec', server, 'python', '-c', program, JSON.stringify(input)], {
    encoding: 'utf8', timeout: 60_000, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const result = raw.split('\n').find((line) => line.startsWith('E2E_POOL_HANDOFF_RESULT='));
  expect(result, 'production pool acquisition must publish its observed reaper handoff').toBeTruthy();
  return JSON.parse(result!.slice('E2E_POOL_HANDOFF_RESULT='.length));
}

test('ownerless sweep reclaims an empty box and preserves live and prepared capacity', async ({ request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  agents.length = 0;
  orphanSessionId = '';
  orphanBefore = undefined;

  const sharedEnvironment = String(process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '').trim();
  const conversationEnvironment = String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim();
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(sharedEnvironment).not.toEqual('');
  expect(conversationEnvironment).not.toEqual('');
  expect(researchAgent).not.toEqual('');

  async function createPreparedAgent(environment: string, suffix: string, preparedCapacity: number): Promise<string> {
    const model = await api.configuredAgentModel(researchAgent, environment);
    const agent = await api.createAgent({
      name: `__e2e_ownerless_${runId}_${suffix}`,
      model,
      environment_name: environment,
      prepared_capacity: preparedCapacity,
    });
    const id = String(agent.agent_id || '');
    expect(id).not.toEqual('');
    agents.push(id);
    await prepared(platform, id);
    return id;
  }

  // One shared seat is claimed below; the other stays prepared in that box.
  const sharedAgent = await createPreparedAgent(sharedEnvironment, 'shared', 2);
  const conversationAgent = await createPreparedAgent(conversationEnvironment, 'conversation', 1);
  const live = await api.startConversation(sharedAgent);
  const liveId = String(live.session_id || '');
  sessions.push(liveId);
  await api.waitForSessionReady(liveId);
  const liveDetail = await api.adminSessionDetail(liveId);
  const sharedBox = String(liveDetail.sandbox_id || '');
  const slot = await prepared(platform, sharedAgent);
  expect(slot.sandbox_id, 'a spare shared slot must occupy the live conversation box').toBe(sharedBox);
  const manifestBefore = preparedIdentity(sharedAgent);
  expect(manifestBefore.state).toBe('prepared');
  expect(manifestBefore.sandbox_id).toBe(sharedBox);

  const abandoned = await api.startConversation(conversationAgent);
  orphanSessionId = String(abandoned.session_id || '');
  sessions.push(orphanSessionId);
  await api.waitForSessionReady(orphanSessionId);
  const orphanDetail = await api.adminSessionDetail(orphanSessionId);
  const orphanBox = String(orphanDetail.sandbox_id || '');
  const idle = await prepared(platform, conversationAgent);
  const idleBox = String(idle.sandbox_id || '');
  expect(new Set([orphanBox, sharedBox, idleBox]).size).toBe(3);
  expect(orphanBox).not.toEqual('');
  expect(String(orphanDetail.sandbox_backend || '')).not.toEqual('');

  const marker = `ownerless-protected-${runId}.txt`;
  await api.uploadFileText(liveId, '.', marker, 'live conversation survives ownerless reap');
  // The fault: the Session loses its binding while its box keeps running, so
  // no row names the box any more. Cleared only while the row still names the
  // box this scene recorded, and the original binding is kept for the restore.
  const binding = sessionBinding(orphanSessionId);
  expect(binding.sandbox_id, 'the abandoned Session must be bound to the box the fault orphans').toBe(orphanBox);
  orphanBefore = binding;
  expect(
    writeRows(
      `UPDATE sessions SET sandbox_id = NULL, sandbox_backend = NULL, startup_allocation = NULL `
        + `WHERE session_id = ${sqlLiteral(orphanSessionId)} AND sandbox_id = ${sqlLiteral(orphanBox)} `
        + 'RETURNING session_id',
    ),
    'the fault must clear exactly the abandoned Session\'s binding',
  ).toEqual([orphanSessionId]);
  test.info().annotations.push({
    type: 'ownerless_reap_scene',
    description: JSON.stringify({ orphanSessionId, orphanBox, sharedBox, idleBox, agents }),
  });

  const result = reapWithoutAgeDelay({
    backend: String(orphanDetail.sandbox_backend),
    orphan: orphanBox,
    shared: sharedBox,
    idle: idleBox,
    poolName: String(idle.client_pool_name),
  });
  await test.info().attach('ownerless-reap-result', {
    body: JSON.stringify(result), contentType: 'application/json',
  });
  expect(result.probes.orphan, 'an empty box without an owner must be physically reclaimed').toBe('NOT_FOUND');
  expect(result.summary.ownerless_reaped).toBeGreaterThanOrEqual(1);
  expect(result.probes.shared, 'the real shared conversation and prepared slot must survive').toBe('OK');
  expect(result.probes.idle, 'supplier-owned unclaimed capacity is not an orphan').toBe('OK');
  expect(result.idleIds).toContain(idleBox);

  expect(preparedIdentity(sharedAgent)).toEqual(manifestBefore);
  expect((await api.adminSessionDetail(liveId)).sandbox_id).toBe(sharedBox);
  expect(await api.downloadFileText(liveId, marker)).toBe('live conversation survives ownerless reap');

  const sharedNext = await api.startConversation(sharedAgent);
  const sharedNextId = String(sharedNext.session_id || '');
  sessions.push(sharedNextId);
  await api.waitForSessionReady(sharedNextId);
  const sharedClaim = await api.adminSessionDetail(sharedNextId);
  expect(sharedClaim.sandbox_id).toBe(sharedBox);
  expect(sharedClaim.runtime_identity?.isolated_session_id,
    'the protected prepared slot must remain claimable').toBe(manifestBefore.isolated_session_id);

  const next = await api.startConversation(conversationAgent);
  const nextId = String(next.session_id || '');
  sessions.push(nextId);
  await api.waitForSessionReady(nextId);
  expect((await api.adminSessionDetail(nextId)).sandbox_id, 'the protected SDK box must remain claimable').toBe(idleBox);

  const handoff = reapDuringRealPoolAdoption({
    backend: String(orphanDetail.sandbox_backend), agentId: sharedAgent,
    poolName: String(slot.client_pool_name),
  });
  await test.info().attach('sdk-pool-ownerless-handoff', {
    body: JSON.stringify(handoff), contentType: 'application/json',
  });
  expect((await api.adminSessionDetail(liveId)).sandbox_id).toBe(sharedBox);
  expect(await api.downloadFileText(liveId, marker)).toBe('live conversation survives ownerless reap');
});
