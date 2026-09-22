/**
 * E2E: a transport-lost Agent conversation create is retried safely.
 *
 * The first browser POST is allowed to reach the real production backend, then
 * its response is discarded. This is the ambiguous failure ordinary POST
 * retries get wrong: the server may already have committed. The console must
 * retry with the same Idempotency-Key, receive the same session, and navigate
 * once — never leave the user on the Agent card or create a duplicate.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let retainedAgentId = '';
onPassOnly(async ({ request }) => {
  if (retainedAgentId) await new AstraApi(request).deleteAgent(retainedAgentId);
});

test('Agent conversation create survives a lost response without creating a duplicate', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  const models = await api.listEnvironmentModels(environmentName);
  const name = `__e2e_idempotent_create_${Date.now()}`;
  const agent = await api.createAgent({
    name,
    model: models.find((model) => model && !model.includes('*')) || 'deepseek-chat',
    environment_name: environmentName,
  });
  retainedAgentId = String(agent.agent_id || '');
  expect(retainedAgentId).not.toEqual('');

  const observedKeys: string[] = [];
  let discarded = false;
  await page.route(`**${apiPath(`/agents/${retainedAgentId}/conversations`)}`, async (route) => {
    observedKeys.push(route.request().headers()['idempotency-key'] || '');
    if (!discarded) {
      discarded = true;
      await route.fetch();
      await route.abort('connectionreset');
      return;
    }
    await route.continue();
  });

  await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${name}"]`);
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: 30_000,
  });

  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).at(-1) || '';
  expect(sessionId).not.toEqual('');
  sessions.push(sessionId);
  expect(observedKeys).toHaveLength(2);
  expect(observedKeys[0]).not.toEqual('');
  expect(observedKeys[1]).toEqual(observedKeys[0]);

  const rows = await api.data<Array<{ session_id?: string }>>(
    'GET',
    `/agents/${retainedAgentId}/sessions`,
  );
  expect(rows, 'the ambiguous retry must create exactly one conversation').toHaveLength(1);
  expect(String(rows[0]?.session_id || '')).toEqual(sessionId);
});

test('Agent picker recovers when its first safe read loses the network route', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const expected = await api.defaultAgent();
  const expectedName = String(expected.name || '').trim();
  expect(expectedName).not.toEqual('');

  let attempts = 0;
  await page.route(`**${apiPath('/agents')}`, async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.abort('connectionreset');
      return;
    }
    await route.continue();
  });

  await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
  await expect(
    page.locator(`[data-testid="agent-option"][data-agent-name="${expectedName}"]`),
    'a transient failed GET must recover without leaving the picker empty',
  ).toBeVisible({ timeout: 15_000 });
  expect(attempts).toEqual(2);
  await expect(page.getByText('No Agents are available.')).toHaveCount(0);
});
