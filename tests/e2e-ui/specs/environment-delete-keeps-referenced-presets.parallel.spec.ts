/** Environment deletion uses the real console, API and stored Agent binding.
 * Owns one preset and one disabled, non-prewarmed Agent; creates no runtime.
 */
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

let agentId = '';

onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('an Environment can be deleted only after its last Agent moves away', async ({ page, request }) => {
  const api = new AstraApi(request);
  const seeded = await api.defaultAgent();
  const originalName = String(seeded.environment_name || '').trim();
  expect(originalName).not.toBe('');
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const original = environments.find((item) => item.name === originalName);
  expect(original).toBeDefined();
  const model = await api.configuredAgentModel(seeded.name, originalName);
  const name = `__e2e_delete_environment_${randomUUID()}`;
  const ownerName = `__e2e_environment_owner_${randomUUID()}`;
  const route = `/admin/environments/${encodeURIComponent(name)}`;
  const recordPath = appPath(`/manage/environments/${encodeURIComponent(name)}`);
  const deletePath = apiPath(route);
  const uncaught: string[] = [];
  const deleteRequests: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));
  page.on('request', (outgoing) => {
    if (outgoing.method() === 'DELETE' && new URL(outgoing.url()).pathname === deletePath) {
      deleteRequests.push(outgoing.url());
    }
  });

  // Only the engine/backend identifiers are borrowed. This authoring fixture
  // needs no provider credentials, image changes or shared Environment edits.
  await api.data('PUT', route, {
    name, enabled: true, engine_kind: original!.engine_kind,
    sandbox_backend: original!.sandbox_backend || 'open_sandbox',
    networking: { type: 'limited', allowed_hosts: [], allow_mcp_servers: false },
  });
  const agent = await api.createAgent({
    name: ownerName, model, environment_name: name, enabled: false, prewarm_enabled: false,
  });
  agentId = agent.agent_id;
  expect(agent.environment_name).toBe(name);

  await page.goto(recordPath);
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toContainText(`Delete Environment “${name}”?`);
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Delete', exact: true })).toBeVisible();
  expect(deleteRequests).toHaveLength(0);
  expect((await api.data<Array<{ name: string }>>('GET', '/admin/environments'))
    .some((item) => item.name === name)).toBe(true);

  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  const refused = page.waitForResponse((response) =>
    response.request().method() === 'DELETE' && new URL(response.url()).pathname === deletePath);
  await page.getByTestId('environment-delete').click();
  const refusal = await refused;
  expect(refusal.status()).toBe(409);
  const failure = await refusal.json();
  expect(failure.code).toBe('ENVIRONMENT_IN_USE');
  expect(failure.data.holders).toEqual([
    { target_type: 'agent', target_id: agentId, target_name: ownerName },
  ]);
  await expect(page.getByRole('alert').filter({ hasText: failure.message })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe(recordPath);
  expect((await api.getAgent(agentId)).environment_name).toBe(name);
  expect((await api.data<Array<{ name: string }>>('GET', '/admin/environments'))
    .some((item) => item.name === name)).toBe(true);

  const current = await api.getAgent(agentId);
  await api.updateAgent(agentId, {
    name: current.name, model, version: current.version, environment_name: originalName,
    enabled: false, prewarm_enabled: false,
  });
  expect((await api.getAgent(agentId)).environment_name).toBe(originalName);
  await page.reload();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  const deleted = page.waitForResponse((response) =>
    response.request().method() === 'DELETE' && new URL(response.url()).pathname === deletePath);
  await page.getByTestId('environment-delete').click();
  const success = await deleted;
  expect(success.status()).toBe(200);
  expect((await success.json()).data).toEqual({ name, deleted: true });
  await expect.poll(() => new URL(page.url()).pathname).toBe(appPath('/manage/environments'));
  await expect(page.getByRole('row').filter({ hasText: name })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('row').filter({ hasText: name })).toHaveCount(0);
  const remaining = await api.data<Array<{ name: string }>>('GET', '/admin/environments');
  expect(remaining.some((item) => item.name === name)).toBe(false);
  expect(remaining.some((item) => item.name === originalName)).toBe(true);
  expect((await api.getAgent(agentId)).environment_name).toBe(originalName);
  expect(deleteRequests).toHaveLength(2);
  expect(uncaught).toEqual([]);
});
