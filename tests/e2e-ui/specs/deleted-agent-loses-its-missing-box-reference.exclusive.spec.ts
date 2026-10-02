/** A deleted owner loses a stale box reference without reviving it or reaping live capacity. */
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentCountByField, documentsByField, patchDocs } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly } from '../fixtures/sessionCleanup';

const agents: string[] = [];
const deleted = new Set<string>();
onPassOnly(async ({ request }) => {
  for (const id of agents) if (!deleted.has(id)) await new AstraApi(request).deleteAgent(id);
});

function agentRow(id: string): Record<string, unknown> {
  const rows = documentsByField('agents', '$.agent_id', id);
  expect(rows.length).toBe(1);
  return rows[0];
}

function authoredState(row: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(['agent_id', 'name', 'deleted', 'state', 'version', 'updated_at']
    .map((key) => [key, row[key]]));
}

function slotIdentity(row: Record<string, unknown>): Record<string, unknown> {
  const slot = row._prepared_slot as Record<string, unknown>;
  return Object.fromEntries(['slot_id', 'sandbox_id', 'state', 'placement', 'isolated_session_id']
    .map((key) => [key, slot?.[key]]));
}

test('the idle sweep clears a deleted Agent missing-box reference once and preserves live prepared capacity', async ({ request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const environment = String(process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '');
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '');
  expect(environment).not.toBe('');
  expect(researchAgent).not.toBe('');
  const model = await api.configuredAgentModel(researchAgent, environment);
  async function prepare(label: string) {
    const id = String((await api.createAgent({
      name: `__e2e_deleted_binding_${label}_${randomUUID()}`, model,
      environment_name: environment, prewarm_enabled: true,
    })).agent_id);
    agents.push(id);
    await expect.poll(async () => Boolean((await platform.preparedRuntime(id)).ready), {
      timeout: 60_000,
    }).toBe(true);
    const row = agentRow(id);
    const slot = slotIdentity(row);
    expect(slot.placement).toBe('shared_slot');
    expect(slot.state).toBe('prepared');
    expect(String(row.sandbox_id || '')).not.toBe('');
    expect(slot.sandbox_id).toBe(row.sandbox_id);
    expect(documentCountByField('sessions', '$.agent_id', id)).toBe(0);
    return { id, row, slot, handle: await requireSandboxHandle(api, String(row.sandbox_id)) };
  }
  const owner = await prepare('owner');
  const control = await prepare('control');
  expect(owner.row.sandbox_id).not.toBe(control.row.sandbox_id);
  expect(sandboxRunning(owner.handle)).toBe(true);
  expect(sandboxRunning(control.handle)).toBe(true);

  await api.deleteAgent(owner.id);
  deleted.add(owner.id);
  await waitForSandboxStopped(owner.handle, 30_000);
  expect(sandboxRunning(owner.handle)).toBe(false);
  const tombstone = authoredState(agentRow(owner.id));
  expect(tombstone.deleted).toBe(true);
  expect(tombstone.state).toBe('DELETED');

  // Only the deleted owner's former binding is reintroduced; compute stays gone.
  patchDocs('agents', { '$.agent_id': owner.id }, {
    sandbox_id: owner.row.sandbox_id,
    sandbox_backend: owner.row.sandbox_backend,
    _resident_sandbox_generation: owner.row._resident_sandbox_generation,
  });
  expect(authoredState(agentRow(owner.id))).toEqual(tombstone);
  expect(agentRow(owner.id).sandbox_id).toBe(owner.row.sandbox_id);
  const ticks: Record<string, unknown>[] = [];
  await expect.poll(async () => {
    ticks.push(await platform.idleSweep());
    const row = agentRow(owner.id);
    return [row.sandbox_id, row.sandbox_backend, row._resident_sandbox_generation];
  }, { timeout: 30_000, intervals: [1_000] }).toEqual([null, null, null]);
  expect(authoredState(agentRow(owner.id))).toEqual(tombstone);
  expect((await request.get(apiPath(`/agents/${owner.id}`))).status()).toBe(404);

  ticks.push(await platform.idleSweep());
  expect(authoredState(agentRow(owner.id))).toEqual(tombstone);
  expect(agentRow(owner.id).sandbox_id).toBeNull();
  expect(slotIdentity(agentRow(control.id))).toEqual(control.slot);
  expect(agentRow(control.id).sandbox_id).toBe(control.row.sandbox_id);
  expect((await platform.preparedRuntime(control.id)).ready).toBe(true);
  expect(sandboxRunning(control.handle)).toBe(true);
  await test.info().attach('deleted-agent-reap.json', {
    contentType: 'application/json',
    body: JSON.stringify({ owner: owner.id, missingBox: owner.row.sandbox_id, control: control.id, tombstone, ticks }),
  });
});
