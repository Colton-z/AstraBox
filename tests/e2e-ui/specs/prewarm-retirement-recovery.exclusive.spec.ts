/** A real SDK state-store failure must leave pool retirement recoverable. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

test.use({ locale: 'en-US' });
const sessions = trackSessions();

interface PoolIdentity {
  agentId: string;
  agentName: string;
  backend: string;
  poolName: string;
  liveBox: string;
  spareBox: string;
  marker: string;
}

interface SupplierRead {
  sdkVersion: string;
  key: string;
  keyType: string;
  keyTtlMs: number;
  faultMarker: string | null;
  destroyState: string | null;
  stateStoreError: { type: string; operation: string; causeType: string; cause: string } | null;
  idleIds: string[];
  probes: Record<string, string>;
  owners: Record<string, { sessionId: string; assignmentId: string }>;
}

// The SDK's destroy-state GET is before begin_destroy and idle removal. A hash
// at this otherwise absent key makes Redis reject that real GET with WRONGTYPE.
// This tests the SDK state-store protocol, not failure of physical sandbox DELETE.
// Derive the key with the installed SDK, never a copied prefix/encoding recipe.
const SUPPLIER_PROGRAM = `
import asyncio
import json
import sys
from importlib.metadata import version
from opensandbox.exceptions import PoolStateStoreUnavailableException
from astrabox.bootstrap import bootstrap
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.seams.sandbox import SANDBOX_ASSIGNMENT_ID_METADATA_KEY, sandbox_for_name

ARM = '''
if redis.call('EXISTS', KEYS[1]) ~= 0 then
  return redis.error_reply('retirement fault refuses an existing destroy-state key')
end
redis.call('HSET', KEYS[1], 'e2e-marker', ARGV[1])
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
'''
RESTORE = '''
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('TYPE', KEYS[1]).ok ~= 'hash' or
   redis.call('HLEN', KEYS[1]) ~= 1 or
   redis.call('HGET', KEYS[1], 'e2e-marker') ~= ARGV[1] then
  return redis.error_reply('retirement fault refuses to remove another owner state')
end
return redis.call('DEL', KEYS[1])
'''

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    registry = provider._client_pool_registry()
    store = await registry._ensure_state_store()
    redis = store._redis
    key = store._destroy_state_key(given['poolName'])
    try:
        if given['action'] == 'arm':
            from astrabox.persistence.repository.agent_repository import AgentRepository
            agent = await AgentRepository().get_agent(given['agentId'])
            assert agent is not None and agent['name'] == given['agentName']
            assert agent['_client_pool_name'] == given['poolName']
            assert agent['_client_pool_backend'] == given['backend']
            assert agent['prewarm_enabled'] is True
            idle = await store.snapshot_idle_entries(given['poolName'])
            assert [entry.sandbox_id for entry in idle] == [given['spareBox']]
            assert await redis.eval(ARM, 1, key, given['marker']) == 1
        elif given['action'] == 'restore':
            await redis.eval(RESTORE, 1, key, given['marker'])
        else:
            assert given['action'] == 'read'
        key_type = (await redis.type(key)).decode()
        marker = await redis.hget(key, 'e2e-marker') if key_type == 'hash' else None
        state = None
        state_error = None
        try:
            state = (await store.get_destroy_state(given['poolName'])).value
        except PoolStateStoreUnavailableException as exc:
            state_error = {
                'type': type(exc).__name__, 'operation': str(exc),
                'causeType': type(exc.__cause__).__name__, 'cause': str(exc.__cause__),
            }
        idle = await store.snapshot_idle_entries(given['poolName'])
        probes = {}
        owners = {}
        for box in (given['liveBox'], given['spareBox']):
            probes[box] = (await provider.probe(box)).probe_status
        page = 1
        while True:
            inventory = await provider.list_sandboxes(page=page, page_size=100)
            for item in inventory.items:
                if item.sandbox_id in probes:
                    owners[item.sandbox_id] = {
                        'sessionId': item.session_id,
                        'assignmentId': item.metadata[SANDBOX_ASSIGNMENT_ID_METADATA_KEY],
                    }
            if not inventory.has_next_page:
                break
            page += 1
        print('E2E_RETIREMENT_RECOVERY=' + json.dumps({
            'sdkVersion': version('opensandbox'), 'key': key,
            'keyType': key_type, 'keyTtlMs': await redis.pttl(key),
            'faultMarker': marker.decode() if marker else None,
            'destroyState': state, 'stateStoreError': state_error,
            'idleIds': [entry.sandbox_id for entry in idle],
            'probes': probes, 'owners': owners,
        }))
    finally:
        await redis.aclose()

asyncio.run(main())
`;

function supplier(identity: PoolIdentity, action: 'arm' | 'read' | 'restore'): SupplierRead {
  const output = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', SUPPLIER_PROGRAM,
    JSON.stringify({ ...identity, action }),
  ], { encoding: 'utf8', timeout: 15_000, stdio: ['ignore', 'pipe', 'pipe'] });
  const line = output.split('\n').find((item) => item.startsWith('E2E_RETIREMENT_RECOVERY='));
  expect(line, 'the real supplier probe must return its observations').toBeTruthy();
  return JSON.parse(line!.slice('E2E_RETIREMENT_RECOVERY='.length)) as SupplierRead;
}

function agentRetirement(agentId: string): Record<string, unknown> {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows).toHaveLength(1);
  const row = rows[0];
  return {
    poolName: row._client_pool_name,
    backend: row._client_pool_backend,
    epoch: row._client_pool_epoch,
    error: row._prepared_runtime_error,
  };
}

async function readyCapacity(platform: PlatformApi, agentId: string): Promise<Record<string, unknown>> {
  let status: Record<string, unknown> = {};
  await expect.poll(async () => {
    status = await platform.preparedRuntime(agentId);
    return status.ready === true && Number(status.prepared_count) > 0 && Boolean(status.sandbox_id);
  }, { timeout: 45_000, message: 'the Agent must publish real idle capacity' }).toBe(true);
  return status;
}

test('failed pool retirement retains its address and error, then recovers without killing a claimed box', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const environmentName = String(process.env.ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT || '').trim();
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(environmentName).not.toBe('');
  expect(researchAgent).not.toBe('');
  const environment = (await platform.listEnvironments()).find((row) => row.name === environmentName);
  expect(environment?.sandbox_tenancy).toBe('conversation');
  const model = await api.configuredAgentModel(researchAgent, environmentName);
  const agentName = `__e2e_retirement_recovery_${randomUUID()}`;
  const agent = await api.createAgent({ name: agentName, model, environment_name: environmentName, prewarm_enabled: true });
  test.info().annotations.push({ type: 'retirement_agent', description: agent.agent_id });
  const first = await readyCapacity(platform, agent.agent_id);
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  const live = await api.adminSessionDetail(sessionId);
  const liveBox = String(live.sandbox_id || '');
  expect(liveBox).toBe(first.sandbox_id);
  const spare = await readyCapacity(platform, agent.agent_id);
  const identity: PoolIdentity = {
    agentId: agent.agent_id, agentName, backend: String(live.sandbox_backend || ''),
    poolName: String(spare.client_pool_name || ''), liveBox,
    spareBox: String(spare.sandbox_id || ''), marker: randomUUID(),
  };
  expect(identity.backend).not.toBe('');
  expect(identity.poolName).not.toBe('');
  expect(identity.spareBox).not.toBe(liveBox);
  const markerFile = `retirement-${randomUUID()}.txt`;
  const markerText = `claimed Session owns this file: ${sessionId}\n`;
  await api.uploadFileText(sessionId, '/workspace', markerFile, markerText, 10_000);
  expect(await api.downloadFileText(sessionId, `/workspace/${markerFile}`, 10_000)).toBe(markerText);
  const before = supplier(identity, 'read');
  expect(before.keyType).toBe('none');
  expect(before.destroyState).toBe('ACTIVE');
  expect(before.idleIds).toEqual([identity.spareBox]);
  expect(before.probes).toEqual({ [liveBox]: 'OK', [identity.spareBox]: 'OK' });
  expect(before.owners[liveBox].sessionId).toBe(sessionId);
  expect(before.owners[identity.spareBox].sessionId).not.toBe(sessionId);
  const address = agentRetirement(agent.agent_id);
  expect(address).toMatchObject({ poolName: identity.poolName, backend: identity.backend });
  await test.info().attach('retirement-before-fault', {
    body: JSON.stringify({ identity, sessionId, address, supplier: before }), contentType: 'application/json',
  });

  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await page.goto(appPath(`/manage/agents/${agent.agent_id}`));
  await expect(page.getByTestId('prewarm-state')).toHaveText('Ready');

  let restoreRequired = true;
  try {
    const armed = supplier(identity, 'arm');
    expect(armed.keyType).toBe('hash');
    expect(armed.faultMarker).toBe(identity.marker);
    expect(armed.keyTtlMs).toBeGreaterThan(0);
    expect(armed.stateStoreError).toMatchObject({
      type: 'PoolStateStoreUnavailableException', causeType: 'ResponseError',
    });
    expect(armed.stateStoreError!.cause).toContain('WRONGTYPE');
    const current = await api.getAgent(agent.agent_id);
    await api.updateAgent(agent.agent_id, {
      name: current.name, model: current.model, environment_name: current.environment_name,
      version: current.version, prewarm_enabled: false,
    });
    let failed: Record<string, unknown> = {};
    await expect.poll(async () => {
      failed = await platform.preparedRuntime(agent.agent_id);
      return String(failed.last_error || '');
    }, { timeout: 15_000, message: 'the platform must publish the real SDK retirement failure' })
      .toContain('PoolStateStoreUnavailableException');
    const failedAddress = agentRetirement(agent.agent_id);
    expect(failedAddress).toEqual({ ...address, error: failed.last_error });
    expect(failed).toMatchObject({ enabled: false, ready: false, prepared_count: 0, client_pool_name: identity.poolName });
    expect(String(failed.last_error)).toContain('operation=get_destroy_state');
    expect(String(failed.last_error)).toContain(identity.poolName);
    const blocked = supplier(identity, 'read');
    expect(blocked.faultMarker).toBe(identity.marker);
    expect(blocked.keyTtlMs).toBeGreaterThan(0);
    expect(blocked.idleIds).toEqual([identity.spareBox]);
    expect(blocked.probes).toEqual(before.probes);
    expect(blocked.owners).toEqual(before.owners);
    await test.info().attach('retirement-failure', {
      body: JSON.stringify({ address: failedAddress, status: failed, supplier: blocked }), contentType: 'application/json',
    });
    await page.reload();
    await expect(page.getByTestId('agent-prewarm-status').getByRole('alert')).toHaveText(String(failed.last_error));

    const restored = supplier(identity, 'restore');
    expect(restored.keyType).toBe('none');
    expect(restored.destroyState).toBe('ACTIVE');
    expect(restored.stateStoreError).toBeNull();
    expect(restored.idleIds).toEqual([identity.spareBox]);
    restoreRequired = false;
  } finally {
    // Fault cleanup is unconditional; product rows and boxes stay on failure.
    // The Redis TTL also removes this test-only key if the test worker is killed.
    if (restoreRequired) {
      const restored = supplier(identity, 'restore');
      expect(restored.faultMarker).toBeNull();
      expect(restored.keyType).toBe('none');
      expect(restored.stateStoreError).toBeNull();
    }
  }

  const retry = await api.getAgent(agent.agent_id);
  await api.updateAgent(agent.agent_id, {
    name: retry.name, model: retry.model, environment_name: retry.environment_name,
    version: retry.version, prewarm_enabled: false,
  });
  await expect.poll(() => agentRetirement(agent.agent_id), {
    timeout: 20_000, message: 'successful retirement must clear its address and previous error',
  }).toEqual({ poolName: null, backend: null, epoch: null, error: null });
  const recoveredStatus = await platform.preparedRuntime(agent.agent_id);
  expect(recoveredStatus).toMatchObject({ enabled: false, ready: false, prepared_count: 0, client_pool_name: null, last_error: null });
  let recovered = supplier(identity, 'read');
  await expect.poll(() => {
    recovered = supplier(identity, 'read');
    expect(recovered.probes[liveBox]).toBe('OK');
    return recovered.probes[identity.spareBox];
  }, { timeout: 15_000, message: 'the supplier must confirm that only the waiting box disappeared' }).toBe('NOT_FOUND');
  expect(recovered.destroyState).toBe('DESTROYED');
  expect(recovered.idleIds).toEqual([]);
  expect(recovered.owners).toEqual({ [liveBox]: before.owners[liveBox] });
  const stillLive = await api.adminSessionDetail(sessionId);
  expect(stillLive.sandbox_id).toBe(liveBox);
  expect(stillLive.state).toBe('READY');
  expect(await api.downloadFileText(sessionId, `/workspace/${markerFile}`, 10_000)).toBe(markerText);
  await page.reload();
  await expect(page.getByTestId('prewarm-state')).toHaveText('Disabled');
  await expect(page.getByTestId('agent-prewarm-status').getByRole('alert')).toHaveCount(0);
  await test.info().attach('retirement-recovered', {
    body: JSON.stringify({ address: agentRetirement(agent.agent_id), status: recoveredStatus, supplier: recovered, sessionId, liveBox }),
    contentType: 'application/json',
  });

  // This is reached only after all assertions; failures retain the Agent too.
  await api.deleteSession(sessionId);
  sessions.splice(sessions.indexOf(sessionId), 1);
  await api.deleteAgent(agent.agent_id);
});
