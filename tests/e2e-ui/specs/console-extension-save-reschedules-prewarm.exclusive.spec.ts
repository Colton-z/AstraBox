/**
 * Product E2E for what a console save does to the Agent's warm pool.
 *
 * Pool identity is the preparation fingerprint: exactly what the preparer
 * installs into a box (Skills, Plugins) plus the baked infrastructure
 * (image, network policy, tenancy). Pure configuration — a managed MCP
 * selection on the admitted gateway — replaces the prepared engine slot but
 * must NOT rotate a warm pool. A Skills edit is a preparation change and MUST rotate it
 * and start the replacement by itself. Both failure modes are silent in the
 * UI: the save reads back correctly either way, and only the pool identity
 * tells wasted churn from a pool nobody restarts.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField } from '../fixtures/dbOracle';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';

const SHARED_ENVIRONMENT = String(
  process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '',
).trim();
const RESEARCH_AGENT = String(
  process.env.ASTRABOX_E2E_RESEARCH_AGENT || '',
).trim();
const SKILL_DESCRIPTOR = String(
  process.env.ASTRABOX_E2E_REAL_SKILL_DESCRIPTOR
    || 'https://github.com/anthropics/skills.git@main#skills/skill-creator',
).trim();
const POOL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_POOL_TIMEOUT_MS', 60_000);
// The rotation trigger spawns as a background task inside the same write, so
// a wrongful rotation surfaces within a poll or two; this window is patience,
// not precision.
const STABILITY_WINDOW_MS = 12_000;

interface ExtensionCatalog {
  mcp_servers?: Array<{ id?: string; name?: string }>;
  selected_mcp_server_ids?: string[];
}

interface PoolStatus {
  poolName: string;
  sandboxId: string;
  runtimeGeneration: string;
}

function preparedSlot(agentId: string, pool: PoolStatus): Record<string, unknown> {
  const rows = documentsByField('agents', '$.agent_id', agentId);
  expect(rows, 'preparation must belong to the test Agent').toHaveLength(1);
  expect(rows[0]._client_pool_name).toBe(pool.poolName);
  const slot = rows[0]._prepared_slot as Record<string, unknown>;
  expect(slot, 'the READY shared preparation must have a durable slot').toBeTruthy();
  expect(slot.state).toBe('prepared');
  expect(slot.placement).toBe('shared_slot');
  expect(slot.claimed_session_id, 'only an unclaimed prepared slot is a retirement target').toBeFalsy();
  expect(String(slot.slot_id || '')).not.toBe('');
  expect(String(slot.isolated_session_id || '')).not.toBe('');
  expect(slot.sandbox_id).toBe(pool.sandboxId);
  expect(slot.runtime_generation).toBe(pool.runtimeGeneration);
  return slot;
}

function requiredConfiguration(): void {
  expect(
    SHARED_ENVIRONMENT,
    'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT must name the deployed prewarm Environment',
  ).not.toEqual('');
  expect(
    RESEARCH_AGENT,
    'ASTRABOX_E2E_RESEARCH_AGENT must name the deployed Claude Agent whose model route was proven',
  ).not.toEqual('');
}

async function concreteModel(api: AstraApi, environmentName: string): Promise<string> {
  return api.configuredAgentModel(RESEARCH_AGENT, environmentName);
}

async function waitForReadyPool(
  platform: PlatformApi,
  agentId: string,
  options: { differentFrom?: string; unchangedPool?: PoolStatus } = {},
): Promise<PoolStatus> {
  const deadline = Date.now() + POOL_TIMEOUT_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await platform.preparedRuntime(agentId);
    const poolName = String(last.client_pool_name || '').trim();
    const sandboxId = String(last.sandbox_id || '').trim();
    const runtimeGeneration = String(last.runtime_generation || '').trim();
    if (options.unchangedPool) {
      expect(poolName, 'engine preparation must retain the supplier pool').toBe(
        options.unchangedPool.poolName,
      );
      if (sandboxId) {
        expect(sandboxId, 'engine preparation must reuse the resident box').toBe(
          options.unchangedPool.sandboxId,
        );
      }
    }
    if (
      last.ready === true
      && Number(last.prepared_count || 0) > 0
      && poolName
      && sandboxId
      && runtimeGeneration
      && (!options.differentFrom || poolName !== options.differentFrom)
      && (!options.unchangedPool || runtimeGeneration !== options.unchangedPool.runtimeGeneration)
    ) {
      return { poolName, sandboxId, runtimeGeneration };
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(
    `Agent pool for ${agentId} did not become ready`
    + `${options.differentFrom ? ` under a new identity (old: ${options.differentFrom})` : ''}`
    + ` within ${POOL_TIMEOUT_MS}ms; last=${JSON.stringify(last)}`,
  );
}

async function catalogServerName(api: AstraApi, agentId: string): Promise<string> {
  const catalog = await api.data<ExtensionCatalog>('GET', `/agents/${agentId}/extensions`);
  const names = (catalog.mcp_servers ?? [])
    .map((item) => String(item.name || '').trim())
    .filter(Boolean);
  expect(
    names.length,
    'the deployment must expose a managed MCP catalogue entry',
  ).toBeGreaterThanOrEqual(1);
  return names[0];
}

async function createPrewarmAgent(api: AstraApi, name: string): Promise<string> {
  const agent = await api.createAgent({
    name,
    model: await concreteModel(api, SHARED_ENVIRONMENT),
    environment_name: SHARED_ENVIRONMENT,
    prewarm_enabled: true,
  });
  const agentId = String(agent.agent_id || '').trim();
  expect(agentId, 'the probe Agent must have an id').not.toEqual('');
  return agentId;
}

async function openAgentPage(page: Page, agentId: string): Promise<void> {
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
  await expect(page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-extension-mcp') })).toBeVisible();
}

async function pickServer(page: Page, serverName: string): Promise<void> {
  const input = page.locator('#agent-extension-mcp');
  await input.click();
  await input.fill(serverName);
  await page.getByRole('option', { name: serverName }).click();
  await page.keyboard.press('Escape');
  await expect(
    page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-extension-mcp') }).locator(`[aria-label="${serverName}"]`),
  ).toBeVisible();
}

async function saveExtensions(page: Page): Promise<void> {
  const saved = page.waitForResponse(
    (response) =>
      response.url().includes('/extensions')
      && response.request().method() === 'PUT'
      && response.ok(),
  );
  await page.locator('[data-slot="card"]').filter({
    has: page.locator('#agent-extension-mcp'),
  }).getByRole('button', { name: 'Save', exact: true }).click();
  await saved;
}

test.describe.serial('console saves and the Agent prewarm pool', () => {
  // Serial tests each reset these at their start; the hook below is the only
  // reader. A failing run keeps its Agent and pool — the pool's Pod is the
  // scene a scheduling defect is visible in.
  let sessions = new Set<string>();
  let agentId = '';
  onPassOnly(async ({ request }) => {
    const api = new AstraApi(request);
    for (const sessionId of sessions) await api.deleteSession(sessionId);
    if (agentId) await api.deleteAgent(agentId);
    sessions = new Set<string>();
  });

  test('a managed MCP selection is pure configuration and keeps the warm pool', async ({
    page,
    request,
  }) => {
    requiredConfiguration();
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    sessions = new Set<string>();
    agentId = '';
    agentId = await createPrewarmAgent(api, `__e2e_console_mcp_keeps_pool_${Date.now()}`);
    const serverName = await catalogServerName(api, agentId);
    const before = await waitForReadyPool(platform, agentId);

    await openAgentPage(page, agentId);
    await pickServer(page, serverName);
    await saveExtensions(page);

    const catalog = await api.data<ExtensionCatalog>('GET', `/agents/${agentId}/extensions`);
    const selectedNames = (catalog.mcp_servers ?? [])
      .filter((item) => (catalog.selected_mcp_server_ids ?? []).includes(String(item.id)))
      .map((item) => String(item.name));
    expect(selectedNames, 'the console save must persist the selection').toContain(serverName);

    // The waiting engine must learn the new MCP configuration, but its
    // resident box and the supplier's base-box pool remain reusable.
    const deadline = Date.now() + STABILITY_WINDOW_MS;
    let last: Record<string, unknown> = {};
    while (Date.now() < deadline) {
      last = await platform.preparedRuntime(agentId);
      expect(
        String(last.client_pool_name || ''),
        'a managed MCP selection must not rotate the warm pool',
      ).toBe(before.poolName);
      if (last.sandbox_id) {
        expect(String(last.sandbox_id), 'the resident box must survive the save').toBe(
          before.sandboxId,
        );
      }
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    }
    const after = await waitForReadyPool(platform, agentId, { unchangedPool: before });
    expect(after.sandboxId, 'the updated engine must be ready in the original box').toBe(
      before.sandboxId,
    );
    expect(after.runtimeGeneration, 'the prepared engine must reflect the saved configuration')
      .not.toBe(before.runtimeGeneration);

    await page.reload();
    await expect(
      page.locator('[data-slot="card"]').filter({ has: page.locator('#agent-extension-mcp') }).locator(`[aria-label="${serverName}"]`),
      'the saved selection must read back into the form',
    ).toBeVisible();
  });

  test('a console Skills edit is a preparation change and rotates the pool', async ({
    page,
    request,
  }) => {
    requiredConfiguration();
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    sessions = new Set<string>();
    agentId = '';
    agentId = await createPrewarmAgent(api, `__e2e_console_skills_rotate_${Date.now()}`);
    const before = await waitForReadyPool(platform, agentId);
    const oldSlot = preparedSlot(agentId, before);
    expect(
      documentsByField('sessions', '$.agent_id', agentId),
      'this retirement scene has not handed a placement to any user Session',
    ).toHaveLength(0);
    const oldSandbox = await requireSandboxHandle(api, before.sandboxId);
    expect(sandboxRunning(oldSandbox), 'the old unclaimed preparation must really exist before the save').toBe(true);
    test.info().annotations.push({
      type: 'retired_preparation',
      description: JSON.stringify({ slotId: oldSlot.slot_id, sandboxId: before.sandboxId, poolName: before.poolName }),
    });

    await openAgentPage(page, agentId);
    // Custom Skills owns Add; the LiteLLM catalog owns selection.
    const skillsGroup = page
      .getByRole('group', { name: 'Skill configuration', exact: true })
      .getByRole('group', { name: 'Custom Skills', exact: true })
      .filter({ has: page.getByRole('button', { name: 'Add' }) });
    await skillsGroup.getByRole('button', { name: 'Add' }).click();
    await skillsGroup.getByRole('textbox').last().fill(SKILL_DESCRIPTOR);
    // The runtime card renders one Save once its direct configuration is dirty.
    const saved = page.waitForResponse(
      (response) =>
        response.url().includes(`/agents/${agentId}`)
        && response.request().method() === 'PUT'
        && response.ok(),
    );
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await saved;

    // Skills are installed by the preparer, so this save must rotate the pool
    // identity and start the replacement by itself — the console write is the
    // only trigger an operator's edit has.
    const rotated = await waitForReadyPool(platform, agentId, {
      differentFrom: before.poolName,
    });
    expect(
      rotated.sandboxId,
      'a Skills edit must prepare a replacement resident box',
    ).not.toEqual(before.sandboxId);
    const newSlot = preparedSlot(agentId, rotated);
    expect(newSlot.slot_id, 'the old prepared slot must no longer be the claimable manifest').not.toBe(oldSlot.slot_id);
    // No Session owns this old carrier. The existing generation reconciler
    // retires such an empty resident; occupied shared boxes are not this target.
    await waitForSandboxStopped(oldSandbox);

    const created = await api.startConversation(agentId);
    sessions.add(created.session_id);
    test.info().annotations.push({ type: 'e2e_session_id', description: created.session_id });
    const ready = await api.waitForSessionReady(created.session_id);
    expect(ready.sandbox_id, 'the next Session must acquire the replacement preparation').toBe(rotated.sandboxId);
    expect(ready.sandbox_id).not.toBe(before.sandboxId);
    const claimed = await api.adminSessionDetail(created.session_id);
    expect(
      claimed.runtime_identity?.isolated_session_id,
      'the Session must adopt the prepared isolation session, not merely cold-start in the same box',
    ).toBe(newSlot.isolated_session_id);
    expect(ready.slash_commands, 'the saved Skill must be available on the acquired prepared runtime')
      .toContain('skill-creator');
    expect((await api.getMessages(created.session_id)).messages).toHaveLength(0);
    expect(sandboxRunning(oldSandbox), 'claiming current capacity must not revive the retired carrier').toBe(false);
  });
});
