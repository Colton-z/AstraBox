/** Disabled Environments retire waiting capacity without destroying claimed Sessions. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
const agents: string[] = [];
let environmentName = '';
let environment: Record<string, unknown> = {};

onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of agents) await api.deleteAgent(id);
  // Environments have no DELETE route. Leave this test-owned record disabled.
  if (environmentName) {
    await new PlatformApi(request).putEnvironment(environmentName, { ...environment, enabled: false });
  }
});

interface SupplierRead {
  probes: Record<string, string>;
  isolationIds: string[];
  ownedBoxes: string[];
}

function supplierRead(backend: string, poolName: string, liveBox: string, spareBox: string): SupplierRead {
  const program = `
import asyncio
import hashlib
import json
import sys
from astrabox.bootstrap import bootstrap
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
from astrabox.seams.sandbox import sandbox_for_name

async def main():
    ensure_database_wiring()
    if needs_sandbox_server():
        _export_backend_wiring()
    bootstrap()
    given = json.loads(sys.argv[1])
    provider = sandbox_for_name(given['backend'])
    probes = {}
    for box in set((given['liveBox'], given['spareBox'])):
        probes[box] = (await provider.probe(box)).probe_status
    isolation_ids = []
    if given['shared'] and probes[given['liveBox']] == 'OK':
        handle = await provider.connect(given['liveBox'])
        try:
            native = handle.sidecar_faces
            isolation_ids = [item.session_id for item in await native.isolation.list()
                             if item.status.lower() == 'active']
        finally:
            await handle.close()
    owned = []
    pool_prefix = 'client-pool-' + hashlib.sha256(given['poolName'].encode()).hexdigest()[:12] + '-'
    page = 1
    while True:
        inventory = await provider.list_sandboxes(page=page, page_size=100)
        for item in inventory.items:
            if item.sandbox_id == given['liveBox'] or str(item.session_id or '').startswith(pool_prefix):
                owned.append(item.sandbox_id)
        if not inventory.has_next_page:
            break
        page += 1
    print('E2E_RETIREMENT=' + json.dumps({
        'probes': probes, 'isolationIds': sorted(isolation_ids), 'ownedBoxes': sorted(owned),
    }))

asyncio.run(main())
`;
  const output = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', program,
    JSON.stringify({ backend, poolName, liveBox, spareBox, shared: liveBox === spareBox }),
  ], { encoding: 'utf8', timeout: 20_000, stdio: ['ignore', 'pipe', 'pipe'] });
  const line = output.split('\n').find((item) => item.startsWith('E2E_RETIREMENT='));
  expect(line, 'supplier read must return physical sandbox and isolation identities').toBeTruthy();
  return JSON.parse(line!.slice('E2E_RETIREMENT='.length)) as SupplierRead;
}

async function readyCapacity(platform: PlatformApi, agentId: string): Promise<Record<string, unknown>> {
  let status: Record<string, unknown> = {};
  await expect.poll(async () => {
    status = await platform.preparedRuntime(agentId);
    return status.ready === true && Number(status.prepared_count) > 0 && Boolean(status.sandbox_id);
  }, { timeout: 60_000, message: 'the test Agent must publish real prepared capacity' }).toBe(true);
  return status;
}

for (const tenancy of ['agent', 'conversation'] as const) {
  test(`${tenancy} tenancy: disabled Environment retires waiting capacity and permits Agent deletion`, async ({ page, request }) => {
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    agents.length = 0;
    environmentName = '';
    environment = {};
    const donorName = String(process.env[tenancy === 'agent'
      ? 'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT'
      : 'ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT'] || '').trim();
    const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
    expect(donorName).not.toBe('');
    expect(researchAgent).not.toBe('');
    const model = await api.configuredAgentModel(researchAgent, donorName);
    const donor = (await platform.listEnvironments()).find((row) => row.name === donorName);
    expect(donor, 'the selected deployment must supply this tenancy').toBeTruthy();
    expect(donor!.sandbox_tenancy).toBe(tenancy);
    const fields = new Set((await platform.environmentSchema()).fields!.map((field) => String(field.key)));
    environmentName = `__e2e_retire_${tenancy}_${randomUUID()}`;
    environment = {
      ...Object.fromEntries(Object.entries(donor!).filter(([key]) => fields.has(key))),
      name: environmentName,
      display_name: environmentName,
      enabled: true,
      provider_access: {
        base_url: (donor!.provider_access as Record<string, unknown>).base_url,
        api_key: 'retirement-probe-no-model-turn',
        api_key_secret_name: '',
      },
      tracing: null,
    };
    await platform.putEnvironment(environmentName, environment);
    for (const prewarm of [true, false]) {
      const agent = await api.createAgent({
        name: `${environmentName}_${prewarm ? 'warm' : 'cold'}`,
        model, environment_name: environmentName, prewarm_enabled: prewarm,
      });
      agents.push(agent.agent_id);
    }
    const [warmAgent, coldAgent] = agents;
    const first = await readyCapacity(platform, warmAgent);
    const created = await api.startConversation(warmAgent);
    const sessionId = created.session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const live = await api.adminSessionDetail(sessionId);
    const liveBox = String(live.sandbox_id);
    expect(liveBox, 'the Session must claim the previously prepared box').toBe(first.sandbox_id);
    const spare = await readyCapacity(platform, warmAgent);
    const spareBox = String(spare.sandbox_id);
    const backend = String(live.sandbox_backend);
    const poolName = String(spare.client_pool_name || '');
    expect(poolName).not.toBe('');
    const manifest = (documentsByField('agents', '$.agent_id', warmAgent)[0]._prepared_slot || {}) as Record<string, unknown>;
    const spareSlots = tenancy === 'agent'
      ? [String(manifest.isolated_session_id), String(manifest.terminal_isolated_session_id)] : [];
    const liveSlot = String(live.runtime_identity?.isolated_session_id || '');
    if (tenancy === 'agent') {
      expect(spareBox).toBe(liveBox);
      expect(spareSlots.every((id) => Boolean(id) && id !== 'undefined' && id !== liveSlot)).toBe(true);
    } else {
      expect(spareBox).not.toBe(liveBox);
    }
    const before = supplierRead(backend, poolName, liveBox, spareBox);
    expect(before.probes[liveBox]).toBe('OK');
    expect(before.probes[spareBox]).toBe('OK');
    expect(before.ownedBoxes).toContain(spareBox);
    for (const slot of spareSlots) expect(before.isolationIds).toContain(slot);
    if (tenancy === 'agent') expect(before.isolationIds).toContain(liveSlot);
    await test.info().attach('retirement-identities', {
      body: JSON.stringify({ environmentName, agents, sessionId, liveBox, spareBox, spareSlots, liveSlot, before }),
      contentType: 'application/json',
    });

    await platform.putEnvironment(environmentName, { ...environment, enabled: false });
    for (const agentId of agents) {
      const response = await request.get(apiPath(`/agents/${agentId}/prepared-runtime`));
      expect(response.status(), 'disabled Environment must not break operational reads').toBe(200);
      expect((await response.json()).data).toMatchObject({ enabled: false, ready: false, prepared_count: 0 });
    }
    let retired: SupplierRead = before;
    await expect.poll(() => {
      retired = supplierRead(backend, poolName, liveBox, spareBox);
      expect(retired.probes[liveBox], 'retirement must preserve the claimed Session box').toBe('OK');
      if (tenancy === 'agent') {
        expect(retired.isolationIds).toContain(liveSlot);
        return spareSlots.every((slot) => !retired.isolationIds.includes(slot));
      }
      return retired.probes[spareBox] === 'NOT_FOUND';
    }, { timeout: 30_000, intervals: [500, 1000], message: 'supplier must retire only unclaimed capacity' }).toBe(true);
    expect((await api.adminSessionDetail(sessionId)).sandbox_id).toBe(liveBox);

    await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    const serverErrors: string[] = [];
    page.on('response', (response) => {
      if (response.status() >= 500 && response.url().includes('/api/v1/')) serverErrors.push(response.url());
    });
    await page.goto(appPath(`/manage/agents/${warmAgent}`));
    await expect(page.getByTestId('prewarm-state')).toHaveText('Disabled');
    await expect(page.getByTestId('prewarm-count')).toHaveText('Available: 0');
    expect(serverErrors, 'the management page must load without server errors').toEqual([]);

    for (const [route, code] of [
      [`/agents/${warmAgent}/conversations`, 'AGENT_ENVIRONMENT_DISABLED'],
      [`/agents/${warmAgent}/prepared-runtime/refresh`, 'AGENT_PREWARM_CONFIG_INVALID'],
    ]) {
      const response = await request.post(apiPath(route));
      expect(response.status(), 'disabled configuration must be explicitly rejected, not fail internally').toBe(409);
      expect((await response.json()).code).toBe(code);
    }
    const afterRefusal = supplierRead(backend, poolName, liveBox, spareBox);
    expect(afterRefusal).toEqual(retired);
    expect(documentsByField('sessions', '$.agent_id', warmAgent).map((row) => row.session_id)).toEqual([sessionId]);
    expect((await api.adminSessionDetail(sessionId)).sandbox_id).toBe(liveBox);

    // Delete the Session before its Agent; keep the Environment disabled throughout.
    await api.deleteSession(sessionId);
    sessions.splice(sessions.indexOf(sessionId), 1);
    for (const agentId of [warmAgent, coldAgent]) {
      expect((await platform.listEnvironments()).find((row) => row.name === environmentName)?.enabled).toBe(false);
      await api.deleteAgent(agentId);
      const response = await request.get(apiPath(`/agents/${agentId}`));
      expect(response.status(), 'explicit Agent deletion must make the record unreadable').toBe(404);
      agents.splice(agents.indexOf(agentId), 1);
    }
  });
}
