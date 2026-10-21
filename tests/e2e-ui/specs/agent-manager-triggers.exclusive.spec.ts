import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { PlatformApi, type DeploymentRecord } from '../fixtures/platformApi';
import { SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const manager = `__e2e_trigger_manager_${randomUUID()}`;
const agents: string[] = [];
const deployments: DeploymentRecord[] = [];
const sessions = trackSessions();

// Retain failed scenes, but never leave a schedule firing after the case.
test.afterEach(async ({ request }) => {
  const platform = new PlatformApi(request);
  for (const row of deployments.filter((item) => item.scene === 'schedule')) {
    await platform.updateDeployment(row.agent_id, row.deployment_id, { enabled: false });
  }
});
onPassOnly(async ({ request }) => {
  const platform = new PlatformApi(request);
  for (const row of deployments) await platform.deleteDeployment(row.agent_id, row.deployment_id);
  for (const id of agents) await new AstraApi(request).deleteAgent(id);
});

test('Agent administrators manage only their Agents triggers and Runs, with immediate revocation', async ({ browser, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const names: string[] = [];
  for (let index = 0; index < 4; index++) {
    const agent = await api.createColdTestAgent(`${manager}_${index}`);
    agents.push(agent.agent_id);
    names.push(agent.name);
    await api.setAgentAccess(agent.agent_id, {
      admins: index < 3 ? [manager] : [], visibility: 'public', allowed_user_ids: [],
    });
    deployments.push(await platform.createDeployment(agent.agent_id, { scene: 'hmac' }));
  }
  // Use the deployed identity signer's ordinary session cookie; roles stay empty.
  const token = execFileSync('docker', ['exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import os; from astrabox.providers.identity_oidc import mint_session_token; '
    + `print(mint_session_token(user_id=${JSON.stringify(manager)}, email=None, display_name=None, roles=[], `
    + 'casdoor_organization=os.environ.get("ASTRABOX_CASDOOR_ORGANIZATION", "")))',
  ], { encoding: 'utf8' }).trim();
  const origin = String(process.env.ASTRABOX_E2E_CONSOLE_URL).replace(/\/$/, '');
  const context = await browser.newContext({ baseURL: origin, storageState: {
    cookies: [{ name: 'astrabox_session', value: token, domain: new URL(origin).hostname,
      path: '/', expires: -1, httpOnly: true, secure: origin.startsWith('https:'), sameSite: 'Lax' }], origins: [],
  } });
  const scoped = new PlatformApi(context.request);
  try {
    const who = await context.request.get(apiPath('/user/current'));
    expect((await who.json()).data).toMatchObject({ user_id: manager, is_admin: false });
    const page = await context.newPage();
    await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    await page.goto(appPath('/manage/deployments'));
    for (const name of names.slice(0, 3)) await expect(page.getByRole('row').filter({ hasText: name })).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: names[3] })).toHaveCount(0);
    const collection = await context.request.get(apiPath('/admin/deployments'));
    expect((await collection.json()).data.map((row: DeploymentRecord) => row.deployment_id).sort())
      .toEqual(deployments.slice(0, 3).map((row) => row.deployment_id).sort());
    await expect(page.locator('nav a[href$="/manage/sessions"]')).toBeVisible();
    await expect(page.locator('nav a[href$="/manage/system"]')).toHaveCount(0);

    await page.goto(appPath('/manage/deployments/new'));
    const targets = page.getByLabel('Agent or Assistant');
    await expect(targets.locator('option[value^="agent:"]')).toHaveCount(3);
    expect((await targets.locator('option[value^="agent:"]').allTextContents()).sort()).toEqual(names.slice(0, 3).sort());
    await targets.selectOption({ label: names[0] });
    await page.getByLabel('Trigger', { exact: true }).selectOption('schedule');
    const marker = `MANAGED_TRIGGER_${randomUUID()}`;
    await page.getByLabel('Name', { exact: true }).fill(marker);
    await page.getByLabel('Prompt', { exact: true }).fill(`Reply with exactly ${marker}. Do not use tools.`);
    await page.getByLabel('Cron expression').fill('0 0 1 1 *');
    await page.getByLabel('Timezone', { exact: true }).fill('UTC');
    const creation = page.waitForResponse((response) => response.request().method() === 'POST'
      && response.url().endsWith(`/api/v1/admin/agents/${agents[0]}/deployments`));
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    const response = await creation;
    expect(response.status()).toBe(200);
    const binding = (await response.json()).data as DeploymentRecord;
    deployments.push(binding);
    const id = binding.deployment_id;
    await expect(page).toHaveURL(new RegExp(`/manage/deployments/${id}$`));
    const created = (await scoped.listDeployments(agents[0])).find((row) => row.deployment_id === id)!;
    expect(created).toMatchObject({ name: marker, schedule: { cron: '0 0 1 1 *', timezone: 'UTC' } });
    await page.getByRole('button', { name: 'Disable', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Enable', exact: true })).toBeVisible();
    await page.getByLabel('Name', { exact: true }).fill(`${marker}_edited`);
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('heading', { name: `${marker}_edited`, exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Run now', exact: true }).click();
    await expect.poll(async () => (await scoped.listDeploymentRuns(agents[0], id))
      .filter((row) => row.trigger === 'manual' && row.status === 'COMPLETED').length,
    { timeout: 150_000 }).toBe(1);
    const manual = (await scoped.listDeploymentRuns(agents[0], id)).find((row) => row.trigger === 'manual')!;
    sessions.push(String(manual.session_id));
    const messages = await api.getMessages(String(manual.session_id), 50);
    expect(messages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);
    await page.getByRole('button', { name: 'Replay', exact: true }).click();
    await expect.poll(async () => (await scoped.listDeploymentRuns(agents[0], id))
      .filter((row) => row.replayed_from_run_id === manual.run_id && row.status === 'COMPLETED').length,
    { timeout: 150_000 }).toBe(1);
    const replay = (await scoped.listDeploymentRuns(agents[0], id)).find((row) => row.replayed_from_run_id === manual.run_id)!;
    expect(replay.session_id).not.toBe(manual.session_id);
    sessions.push(String(replay.session_id));
    expect((await api.getMessages(String(replay.session_id), 50)).messages
      .filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);

    const foreign = `/admin/agents/${agents[3]}/deployments`;
    for (const [method, suffix] of [['GET', ''], ['POST', ''], ['PUT', `/${deployments[3].deployment_id}`],
      ['DELETE', `/${deployments[3].deployment_id}`], ['GET', `/${deployments[3].deployment_id}/runs`],
      ['POST', `/${deployments[3].deployment_id}/runs`], ['POST', `/${deployments[3].deployment_id}/runs/${manual.run_id}/replay`]]) {
      expect((await context.request.fetch(apiPath(foreign + suffix), { method, data: { scene: 'hmac' } })).status()).toBe(404);
    }
    expect((await context.request.put(apiPath(`/admin/agents/${agents[1]}/deployments/${id}`), { data: { enabled: true } })).status()).toBe(404);
    expect((await context.request.get(apiPath(`/admin/agents/${agents[0]}/deployments/${id}/account-links`))).status()).toBe(403);
    for (const path of ['/admin/system/overview', '/admin/logs', '/admin/runtime-profiles', '/admin/vaults']) {
      expect((await context.request.get(apiPath(path))).status()).toBe(403);
    }
    // Creating this trigger did not make its author an owner independent of Agent ACL.
    await api.setAgentAccess(agents[0], { admins: [], visibility: 'public', allowed_user_ids: [] });
    expect((await context.request.get(apiPath(`/admin/agents/${agents[0]}/deployments`))).status()).toBe(404);
    expect((await context.request.post(apiPath(`/admin/agents/${agents[0]}/deployments/${id}/runs`))).status()).toBe(404);
    expect((await context.request.delete(apiPath(`/admin/agents/${agents[0]}/deployments/${id}`))).status()).toBe(404);
    await page.goto(appPath('/manage/deployments'));
    await expect(page.getByRole('row').filter({ hasText: names[0] })).toHaveCount(0);
    await page.goto(appPath(`/manage/deployments/${deployments[1].deployment_id}`));
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Confirm deletion', exact: true }).click();
    await expect(page).toHaveURL(/\/manage\/deployments$/);
    deployments.splice(1, 1);
    await expect(page.getByRole('row').filter({ hasText: names[1] })).toHaveCount(0);
  } finally {
    await context.close();
  }
});
