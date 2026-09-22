/**
 * Reclaim a real box whose Session lost its binding without reclaiming another
 * conversation, its prepared shared slot, or the supplier's unclaimed box.
 * The worker uses the production reaper's zero-grace argument so this test
 * exercises ownership rather than spending ten minutes aging a new sandbox.
 */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField, patchSessionDoc, restoreDoc } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

let orphanSessionId = '';
let orphanBefore: Record<string, unknown> | undefined;
const agents: string[] = [];

onPassOnly(async () => {
  if (orphanBefore) restoreDoc('sessions', { '$.session_id': orphanSessionId }, orphanBefore);
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
  const agent = documentsByField('agents', '$.agent_id', agentId)[0];
  const manifest = (agent?._prepared_slot || {}) as Record<string, unknown>;
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
    assert await provider.count_live_isolated_sessions(orphan) == 0, 'orphan is not empty in execd'
    inventory = await provider.list_sandboxes(page=1, page_size=100)
    assert not inventory.has_next_page, 'test inventory exceeds the reaper page'
    ids = {item.sandbox_id for item in inventory.items}
    assert {given['orphan'], given['shared'], given['idle']} <= ids, 'fixture box missing before sweep'
    pool = await provider.describe_client_pool(given['poolName'])
    assert given['idle'] in pool.idle_sandbox_ids, 'supplier has not published the protected idle box'
    summary = await RemoteAgentRuntimeManager().reap_ownerless_sandboxes(
        backend=given['backend'], grace_seconds=0, limit=100,
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
  sourceSessionId: string;
  claimSessionId: string;
  assignmentId: string;
  poolName: string;
}): Record<string, unknown> {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const program = `
import asyncio
import json
import sys
from unittest.mock import patch

from astrabox.bootstrap import bootstrap
from astrabox.core.service.orchestrator.agent.client_pool import acquire_agent_client_pool, ensure_agent_client_pool
from astrabox.core.service.orchestrator.agent.runtime_generation import agent_runtime_owner_id
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.runtime.sandbox_client import extract_sandbox_id
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
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
    source = await session_repo.get_session(given['sourceSessionId'])
    assert source and source['agent_id'] == given['agentId'], 'claim fixture must belong to the live Agent'
    # Only the Session is a fixture. Production acquisition must create its
    # allocation; writing that record here would hide the handoff defect.
    await session_repo.create_session({
        'session_id': given['claimSessionId'], 'agent_id': given['agentId'],
        'user_id': source['user_id'], 'session_kind': source['session_kind'],
        'engine_kind': source['engine_kind'], 'state': 'CREATING',
        'sandbox_id': None, 'runtime_unavailable': False,
        'title': 'ownerless pool handoff probe',
    })
    assert not (await session_repo.get_session(given['claimSessionId'])).get('startup_allocation')
    template = await AgentConfigService(agent_repo, EnvironmentRepository()).resolve_agent_harness(given['agentId'])
    assert template is not None, 'claim fixture Agent must resolve through production configuration'
    manager = RemoteAgentRuntimeManager()
    plan = await ensure_agent_client_pool(template, runtime_manager=manager)
    assert plan is not None and plan.spec.pool_name == given['poolName'], 'claim must use the existing Agent pool'
    async def wait_for_idle():
        while True:
            status = await provider.describe_client_pool(given['poolName'])
            if status.idle_sandbox_ids:
                return
            await asyncio.sleep(0.25)
    await asyncio.wait_for(wait_for_idle(), timeout=30)
    adoption_evidence = []
    original_adopt = type(provider).adopt_sandbox_identity

    async def adopt_then_reap(adapter, handle, *, session_id, assignment_id):
        await original_adopt(adapter, handle, session_id=session_id, assignment_id=assignment_id)
        assert assignment_id == given['assignmentId'], 'unexpected adoption intercepted'
        row = await session_repo.get_session(given['claimSessionId'])
        allocation = (row or {}).get('startup_allocation')
        assert isinstance(allocation, dict), 'SDK claim lost pool ownership before recording its Session allocation'
        sandbox_id = allocation['sandbox_id']
        assert sandbox_id == extract_sandbox_id(handle), 'allocation must name the actual SDK-acquired box'
        assert allocation['scope'] == 'sandbox' and allocation['sandbox_backend'] == given['backend']
        descriptor = await adapter.describe_sandbox(sandbox_id)
        assert not adapter.owns_unclaimed_sandbox(descriptor), 'probe must run after pool identity is removed'
        owner = await adapter.claim_of(sandbox_id, expected_session_id=agent_runtime_owner_id(given['agentId']))
        assert owner.may_destroy, 'acquisition must publish the real Agent runtime owner'
        assert row.get('sandbox_id') is None, 'claiming Session already published its final sandbox binding'
        allocation_owner = await session_repo.find_session_by_sandbox_id(sandbox_id)
        assert allocation_owner and allocation_owner['session_id'] == given['claimSessionId'], 'startup allocation must resolve to the claiming Session'
        assert allocation_owner['startup_allocation'] == allocation, 'backend lookup must preserve the exact in-flight allocation'
        assert not await agent_repo.list_agents_by_sandbox_id(sandbox_id), 'candidate was already published to an Agent'
        agent = await agent_repo.get_agent(given['agentId'])
        assert agent is not None, 'claiming Agent disappeared'
        assert not any(item.get('sandbox_id') == sandbox_id for item in (agent.get('box_admissions') or [])), 'candidate was already admitted'
        assert (agent.get('_prepared_slot') or {}).get('sandbox_id') != sandbox_id, 'candidate already owns a prepared slot'
        assert await adapter.count_live_isolated_sessions(sandbox_id) == 0, 'candidate already has a placement'
        inventory = await adapter.list_sandboxes(page=1, page_size=100)
        assert not inventory.has_next_page, 'test inventory exceeds the reaper page'
        assert sandbox_id in {item.sandbox_id for item in inventory.items}, 'sweep cannot see the candidate'
        summary = await manager.reap_ownerless_sandboxes(backend=given['backend'], grace_seconds=0, limit=100)
        probe = (await adapter.probe(sandbox_id)).probe_status
        assert probe == 'OK', 'the real reaper destroyed a Session-owned in-flight SDK claim'
        assert (await session_repo.get_session(given['claimSessionId']))['startup_allocation'] == allocation
        adoption_evidence.append({'sandboxId': sandbox_id, 'allocation': allocation, 'summary': summary, 'probe': probe})

    with patch.object(type(provider), 'adopt_sandbox_identity', adopt_then_reap):
        claim = await acquire_agent_client_pool(
            template, runtime_manager=manager, session_id=given['claimSessionId'], assignment_id=given['assignmentId'],
        )
    assert claim is not None and len(adoption_evidence) == 1, 'production acquisition did not cross the observed handoff'
    assert claim.sandbox_id == adoption_evidence[0]['sandboxId'], 'returned claim differs from the protected candidate'
    await claim.sandbox.close()
    # Cancelling this acquisition retires only the startup reservation. The
    # existing orphan reaper, not Session rollback, owns the shared base box.
    cleanup = await manager.cleanup_startup_allocation(given['claimSessionId'])
    assert cleanup.released and cleanup.record_cleared, 'real claim reservation did not release'
    assert cleanup.destruction is not None and cleanup.destruction.outcome == 'RETAINED'
    assert not (await session_repo.get_session(given['claimSessionId'])).get('startup_allocation')
    assert (await provider.probe(claim.sandbox_id)).probe_status == 'OK', 'rollback destroyed Agent-owned compute'
    released = await manager.reap_ownerless_sandboxes(backend=given['backend'], grace_seconds=0, limit=100)
    assert (await provider.probe(claim.sandbox_id)).probe_status == 'NOT_FOUND', 'released empty candidate remains orphaned'
    print('E2E_POOL_HANDOFF_RESULT=' + json.dumps({'adoptions': adoption_evidence, 'released': released}))

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

  async function createPreparedAgent(environment: string, suffix: string): Promise<string> {
    const model = await api.configuredAgentModel(researchAgent, environment);
    const agent = await api.createAgent({
      name: `__e2e_ownerless_${runId}_${suffix}`,
      model,
      environment_name: environment,
      prewarm_enabled: true,
    });
    const id = String(agent.agent_id || '');
    expect(id).not.toEqual('');
    agents.push(id);
    await prepared(platform, id);
    return id;
  }

  const sharedAgent = await createPreparedAgent(sharedEnvironment, 'shared');
  const conversationAgent = await createPreparedAgent(conversationEnvironment, 'conversation');
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
  orphanBefore = patchSessionDoc(orphanSessionId, {
    sandbox_id: null,
    sandbox_backend: null,
    startup_allocation: null,
  })[0];
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

  const claimSessionId = randomUUID();
  sessions.push(claimSessionId);
  const handoff = reapDuringRealPoolAdoption({
    backend: String(orphanDetail.sandbox_backend), agentId: sharedAgent,
    sourceSessionId: liveId, claimSessionId, assignmentId: randomUUID(),
    poolName: String(slot.client_pool_name),
  });
  await test.info().attach('sdk-pool-ownerless-handoff', {
    body: JSON.stringify(handoff), contentType: 'application/json',
  });
  expect((await api.adminSessionDetail(liveId)).sandbox_id).toBe(sharedBox);
  expect(await api.downloadFileText(liveId, marker)).toBe('live conversation survives ownerless reap');
});
