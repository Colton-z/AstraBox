/** Account-menu management of a real owner's schedule. OIDC login itself is not under test. */
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type BrowserContext, type Page, type Response } from '@playwright/test';

import { AstraApi, answersInput, messageText } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath, appPath, appPrefix } from '../fixtures/env';
import { deploymentRow, sessionRow } from '../fixtures/ingressRecords';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import type { DeploymentRunRecord } from '../fixtures/platformApi';
import { assistantTranscript } from '../fixtures/renderedTranscript';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

let ownerContext: BrowserContext | undefined;
let agentId = '';
let assistantId = '';
let createdAssistant = false;
let deploymentId = '';
onPassOnly(async ({ request }) => {
  if (ownerContext) {
    const api = new AstraApi(ownerContext.request);
    if (deploymentId) await api.data('DELETE', `/me/schedules/${deploymentId}`);
    for (const sessionId of sessions) await api.deleteSession(sessionId);
    sessions.length = 0;
    if (createdAssistant) await api.deleteAssistant(assistantId);
  }
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
const sessions = trackSessions();
test.afterEach(async () => { await ownerContext?.close(); });

/** Use the deployed signer for the principal already verified by real Casdoor credentials. */
function browserSession(userId: string, orgId: string): string {
  const result = spawnSync('docker', ['exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE),
    'python', '-c', [
      'import json, os, sys',
      'from astrabox.providers.identity_oidc import mint_session_token',
      'from astrabox.common.utils.user_context import default_org_id',
      'identity = json.load(sys.stdin)',
      'assert identity["org_id"] == default_org_id(), "browser and machine organization differ"',
      'print(mint_session_token(user_id=identity["user_id"], email=None, display_name="Schedule owner",',
      '    roles=[], casdoor_organization=os.getenv("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
    ].join('\n')], {
    input: JSON.stringify({ user_id: userId, org_id: orgId }), encoding: 'utf8', timeout: 15_000,
  });
  // Never include the process output: it contains the signed session cookie.
  if (result.error || result.status !== 0 || !result.stdout.trim()) throw new Error('Owner browser-session fixture failed');
  return result.stdout.trim();
}

async function openSchedules(page: Page) {
  await page.getByTestId('user-menu-trigger').click();
  await page.getByRole('menuitem', { name: 'Personal schedules', exact: true }).click();
  await expect(page.getByTestId('personal-schedules')).toBeVisible();
}

async function receipt(response: Response): Promise<DeploymentRunRecord> {
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  expect(body.data?.run_id).toBeTruthy();
  return body.data as DeploymentRunRecord;
}

test('an owner edits, runs, replays and deletes a personal schedule from the account menu', async ({
  browser, request, playwright,
}, info) => {
  const started = Date.now();
  const remaining = () => {
    const budget = 155_000 - (Date.now() - started);
    expect(budget, 'both real invocations and the menu journey share the fixed budget').toBeGreaterThan(0);
    return budget;
  };
  const adminApi = new AstraApi(request);
  const issuer = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  let member: APIRequestContext | undefined;
  let ownerId = '';
  let ownerOrg = '';
  try {
    const discovery = await json(await issuer.get(`${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`));
    const credentials = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(issuer, String(discovery.token_endpoint), credentials.clientId,
      credentials.clientSecret, 'astrabox:read astrabox:write');
    member = await playwright.request.newContext({
      baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'), storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: { Authorization: `Bearer ${token.value}` },
    });
    const memberApi = new AstraApi(member);
    const owner = await memberApi.data<{ user_id: string; is_admin: boolean }>('GET', '/user/current');
    expect(owner.is_admin).toBe(false);
    ownerId = owner.user_id;
    const priorAssistants = await memberApi.data<Array<{ assistant_id: string }>>('GET', '/assistants');
    assistantId = (await memberApi.data<{ assistant_id: string }>('POST', '/assistants/primary', {
      display_name: 'Assistant', timezone: 'UTC',
    })).assistant_id;
    createdAssistant = !priorAssistants.some((row) => row.assistant_id === assistantId);
    const [assistant] = postgresRows<{ owner_id: string; org_id: string | null }>(
      `SELECT owner_id, org_id FROM assistants WHERE assistant_id = ${sqlLiteral(assistantId)}`,
    );
    expect(assistant?.owner_id).toBe(ownerId);
    ownerOrg = String(assistant?.org_id || '');
    expect(ownerOrg).not.toBe('');
  } finally {
    await issuer.dispose();
    await member?.dispose();
  }
  const consoleUrl = requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL');
  ownerContext = await browser.newContext({ baseURL: consoleUrl, locale: 'en-US',
    storageState: { cookies: [], origins: [] } });
  await ownerContext.addCookies([{
    name: 'astrabox_session', value: browserSession(ownerId, ownerOrg),
    domain: new URL(consoleUrl).hostname, path: appPrefix || '/', httpOnly: true,
    secure: new URL(consoleUrl).protocol === 'https:', sameSite: 'Lax',
  }]);
  const api = new AstraApi(ownerContext.request);
  expect(await api.data('GET', '/user/current')).toMatchObject({ user_id: ownerId, is_admin: false });
  const marker = `MENU_${randomUUID().replaceAll('-', '')}`;
  agentId = (await adminApi.createColdTestAgent(`__e2e_schedule_menu_${marker}`)).agent_id;
  await adminApi.setAgentAccess(agentId, { visibility: 'allowlist', admins: [], allowed_user_ids: [ownerId] });
  const originalName = `Original ${marker}`;
  const plan = await api.data<{ deployment_id: string }>('POST', '/me/schedules', {
    agent_id: agentId, source_assistant_id: assistantId, name: originalName,
    prompt_prefix: 'Reply with ORIGINAL. Do not use tools.',
    schedule: { cron: '0 0 1 1 *', timezone: 'UTC' },
  });
  deploymentId = plan.deployment_id;
  const stored = () => deploymentRow(deploymentId);
  expect(stored()).toMatchObject({ owner_user_id: ownerId, owner_org_id: ownerOrg,
    source_assistant_id: assistantId, agent_id: agentId });

  const page = await ownerContext.newPage();
  await page.goto(appPath('/'));
  await openSchedules(page);
  await info.attach('personal-schedules-list', {
    body: await page.screenshot(), contentType: 'image/png',
  });
  await page.getByTestId('personal-schedules').getByRole('button', { name: originalName, exact: true }).click();
  const form = page.getByTestId('personal-schedule-form');
  const name = `Edited ${marker}`;
  const prompt = `Reply with exactly ${marker}. Do not use tools.`;
  await form.getByLabel('Name', { exact: true }).fill(name);
  await api.data('PUT', `/me/schedules/${deploymentId}`, {
    schedule: { cron: '0 0 3 1 *', timezone: 'UTC' },
  });
  // A reconnect after the app's 2 s request-deduplication window refreshes server state.
  await page.evaluate(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2100));
    window.dispatchEvent(new Event('online'));
  });
  await expect(form.getByLabel('Cron expression', { exact: true })).toHaveValue('0 0 3 1 *');
  await expect(form.getByLabel('Name', { exact: true })).toHaveValue(name);
  await form.getByLabel('Prompt', { exact: true }).fill(prompt);
  await form.getByLabel('Cron expression', { exact: true }).fill('0 0 2 1 *');
  await form.getByLabel('Timezone', { exact: true }).fill('Asia/Shanghai');
  await form.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(stored).toMatchObject({ name, prompt_prefix: prompt,
    schedule_cron: '0 0 2 1 *', schedule_timezone: 'Asia/Shanghai' });
  await page.reload();
  await openSchedules(page);
  await page.getByTestId('personal-schedules').getByRole('button', { name, exact: true }).click();
  await expect(form.getByLabel('Name', { exact: true })).toHaveValue(name);
  await expect(form.getByLabel('Prompt', { exact: true })).toHaveValue(prompt);
  await expect(form.getByLabel('Cron expression', { exact: true })).toHaveValue('0 0 2 1 *');
  await expect(form.getByLabel('Timezone', { exact: true })).toHaveValue('Asia/Shanghai');
  await page.getByRole('button', { name: 'Disable', exact: true }).click();
  await expect.poll(stored).toMatchObject({ enabled: false });
  await page.getByRole('button', { name: 'Back to plans', exact: true }).click();
  const disabledPlan = page.getByTestId('personal-schedules').getByRole('button', { name, exact: true });
  await expect(disabledPlan).toContainText('Disabled');
  await disabledPlan.click();
  await page.getByRole('button', { name: 'Enable', exact: true }).click();
  await expect.poll(stored).toMatchObject({ enabled: true });

  const runResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/me/schedules/${deploymentId}/runs`));
  await page.getByRole('button', { name: 'Run now', exact: true }).click();
  const first = await receipt(await runResponse);
  async function completed(runId: string): Promise<DeploymentRunRecord> {
    let found: DeploymentRunRecord | undefined;
    await expect.poll(async () => {
      found = (await api.data<DeploymentRunRecord[]>('GET', `/me/schedules/${deploymentId}/runs`))
        .find((run) => run.run_id === runId);
      expect(['FAILED', 'CANCELLED']).not.toContain(found?.status);
      if (found?.session_id && !sessions.includes(found.session_id)) sessions.push(found.session_id);
      if (found?.status !== 'COMPLETED' || !found.session_id || !found.input_id) return false;
      // The Run's status is read from the response to its input; the conversation shows that response ended.
      const reply = (await api.getMessages(found.session_id, 20)).messages
        .find((message) => answersInput(message, String(found!.input_id)));
      return reply?.metadata?.status === 'ended' && reply.metadata.outcome === 'completed';
    }, { timeout: remaining(), intervals: [500, 1000] }).toBe(true);
    const session = sessionRow(String(found!.session_id));
    expect(session).toMatchObject({ user_id: ownerId, agent_id: agentId });
    const messages = await api.getMessages(String(found!.session_id), 20);
    expect(messages.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
    expect(messages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);
    return found!;
  }
  const original = await completed(first.run_id);
  expect(original.trigger).toBe('manual');
  await page.reload();
  await openSchedules(page);
  await page.getByTestId('personal-schedules').getByRole('button', { name, exact: true }).click();
  const runs = page.getByTestId('personal-schedule-runs');
  const row = runs.locator(`tr[data-run-id="${original.run_id}"]`);
  await expect(row).toContainText('Completed');
  await row.scrollIntoViewIfNeeded();
  await info.attach('personal-schedule-history', {
    body: await page.screenshot(), contentType: 'image/png',
  });
  await row.getByRole('link', { name: 'Open conversation', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${original.session_id}$`));
  await expect.poll(() => assistantTranscript(page), { timeout: Math.min(15_000, remaining()) }).toContain(marker);

  await openSchedules(page);
  await page.getByTestId('personal-schedules').getByRole('button', { name, exact: true }).click();
  const replayResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === apiPath(`/me/schedules/${deploymentId}/runs/${original.run_id}/replay`));
  await row.getByRole('button', { name: 'Replay', exact: true }).click();
  const replayReceipt = await receipt(await replayResponse);
  expect(replayReceipt.run_id).not.toBe(original.run_id);
  expect(replayReceipt.replayed_from_run_id).toBe(original.run_id);
  const replay = await completed(replayReceipt.run_id);
  expect(replay).toMatchObject({ trigger: 'replay', replayed_from_run_id: original.run_id });
  expect(replay.session_id).not.toBe(original.session_id);
  await expect(runs.locator(`tr[data-run-id="${replay.run_id}"]`)).toContainText('Completed');
  await info.attach('personal-schedules-menu-evidence', {
    body: JSON.stringify({ ownerId, ownerOrg, assistantId, deploymentId, storedPlan: stored(), original, replay }),
    contentType: 'application/json',
  });

  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.getByTestId('personal-schedules').getByRole('button', { name, exact: true })).toHaveCount(0);
  await expect.poll(async () => (await api.data<Array<{ deployment_id: string }>>('GET', '/me/schedules'))
    .some((item) => item.deployment_id === deploymentId)).toBe(false);
  deploymentId = '';
});
