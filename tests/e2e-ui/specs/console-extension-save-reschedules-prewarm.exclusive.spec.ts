/**
 * Product E2E for what a console save does to the Agent's warm pool.
 *
 * The waiting engines are started from the Agent's whole runtime recipe inside
 * the supplier's pool boxes, so any save that changes that recipe — a managed
 * MCP selection the engines load, or a Skills edit the preparer installs — must
 * replace the pool and start the replacement by itself. Both failure modes are
 * silent in the UI: the save reads back correctly either way, and only the
 * waiting engine's own generation tells a current seat from one that would
 * hand the next conversation the old configuration.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { nextPreparedSeat } from '../fixtures/preparedSeats';
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

interface ExtensionCatalog {
  mcp_servers?: Array<{ id?: string; name?: string }>;
  selected_mcp_server_ids?: string[];
}

interface PoolStatus {
  poolName: string;
  sandboxId: string;
  runtimeGeneration: string;
}

/** The client pool the Agent's runtime record names (`agent_runtimes`). */
function recordedPoolNames(agentId: string): Array<string | null> {
  return postgresRows<{ client_pool_name: string | null }>(
    `SELECT client_pool_name FROM agent_runtimes WHERE agent_id = ${sqlLiteral(agentId)}`,
  ).map((row) => row.client_pool_name);
}

/** The conversations the Agent has, deleted or not (`sessions`). */
function agentSessionIds(agentId: string): string[] {
  return postgresRows<{ session_id: string }>(
    `SELECT session_id FROM sessions WHERE agent_id = ${sqlLiteral(agentId)} ORDER BY session_id`,
  ).map((row) => row.session_id);
}

function preparedSlot(agentId: string, pool: PoolStatus): Record<string, unknown> {
  expect(
    recordedPoolNames(agentId),
    'preparation must belong to the test Agent, whose runtime record names the ready pool',
  ).toEqual([pool.poolName]);
  const slot = nextPreparedSeat(agentId, pool.sandboxId);
  expect(slot, 'the READY shared preparation must have a prepared seat').toBeTruthy();
  if (!slot) throw new Error('unreachable: asserted above');
  expect(slot.state).toBe('prepared');
  expect(slot.claimed_session_id, 'only an unclaimed prepared seat is a retirement target').toBeFalsy();
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
  options: { differentFrom?: string } = {},
): Promise<PoolStatus> {
  const deadline = Date.now() + POOL_TIMEOUT_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await platform.preparedRuntime(agentId);
    const poolName = String(last.client_pool_name || '').trim();
    const sandboxId = String(last.sandbox_id || '').trim();
    const runtimeGeneration = String(last.runtime_generation || '').trim();
    if (
      last.ready === true
      && Number(last.prepared_count || 0) > 0
      && poolName
      && sandboxId
      && runtimeGeneration
      && (!options.differentFrom || poolName !== options.differentFrom)
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
    prepared_capacity: 1,
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
  // A pick closes the popup. Escape would not: Base UI's ComboboxInput clears
  // the whole selection on Escape once the popup has unmounted.
  await expect(input).toHaveAttribute('aria-expanded', 'false');
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

  test('a managed MCP selection replaces the waiting engines with ones that load it', async ({
    page,
    request,
  }) => {
    requiredConfiguration();
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    sessions = new Set<string>();
    agentId = '';
    agentId = await createPrewarmAgent(api, `__e2e_console_mcp_rotates_pool_${Date.now()}`);
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

    // The engines that wait for the next conversation were started inside the
    // pool's boxes without this server; only a replacement pool carries engines
    // that load it, and the console save is the only trigger it has.
    const after = await waitForReadyPool(platform, agentId, { differentFrom: before.poolName });
    expect(after.runtimeGeneration, 'the saved selection must change the runtime recipe')
      .not.toBe(before.runtimeGeneration);
    // The seat the next conversation claims must carry the saved generation.
    preparedSlot(agentId, after);

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
      agentSessionIds(agentId),
      'this retirement scene has not handed a placement to any user Session',
    ).toHaveLength(0);
    const oldSandbox = await requireSandboxHandle(api, before.sandboxId);
    expect(sandboxRunning(oldSandbox), 'the old unclaimed preparation must really exist before the save').toBe(true);
    test.info().annotations.push({
      type: 'retired_preparation',
      description: JSON.stringify({ slotId: oldSlot.slot_id, sandboxId: before.sandboxId, poolName: before.poolName }),
    });

    await openAgentPage(page, agentId);
    // Custom Skills is the Agent's own JSON array of sources; the LiteLLM
    // catalog owns selection. The descriptor joins whatever is already there.
    const customSkills = page
      .getByRole('group', { name: 'Skill configuration', exact: true })
      .getByRole('textbox', { name: 'Custom Skills', exact: true });
    const current = (await customSkills.inputValue()).trim();
    const existing: unknown[] = current ? JSON.parse(current) : [];
    await customSkills.fill(JSON.stringify([...existing, SKILL_DESCRIPTOR]));
    await customSkills.blur();
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
      'a Skills edit must prepare a replacement pool box',
    ).not.toEqual(before.sandboxId);
    const newSlot = preparedSlot(agentId, rotated);
    expect(newSlot.slot_id, 'the old prepared slot must no longer be the claimable manifest').not.toBe(oldSlot.slot_id);
    // No Session owns this old carrier: it is an idle member of the retired
    // pool, which takes it down with the namespace.
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
