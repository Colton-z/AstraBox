/**
 * A saved JSON block reads as saved, whatever order the server keeps its keys in.
 *
 * The server's JSON columns keep no key order, so the record read back after a
 * save lists an object's keys in the database's order rather than the typed
 * one. The card compares that record with what the reader typed; read as text,
 * the two differ, and a write the server accepted keeps its Save and Revert and
 * never shows Saved.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

test('a JSON block saved in a key order the server does not keep reads as saved', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_json_key_order_${Date.now()}`);
  agentId = agent.agent_id;
  // Longer keys first, inside and out: the database orders the shorter first.
  const typed = {
    zz_server: { command: 'true', type: 'stdio', args: ['--first', '--second'] },
    a_server: { command: 'true', type: 'stdio', args: [] },
  };

  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
  const mcp = page.locator('#agent-mcp_servers');
  const card = page.locator('[data-slot="card"]').filter({ has: mcp });
  const save = card.getByRole('button', { name: 'Save', exact: true });
  const revert = card.getByRole('button', { name: 'Revert', exact: true });
  await expect(mcp).toBeVisible();

  await mcp.fill(JSON.stringify(typed, null, 2));
  await mcp.press('Tab');
  const saved = page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(`/agents/${agentId}`));
  await save.click();
  expect((await saved).status()).toBe(200);
  const record = await api.data<{ mcp_servers: Record<string, Record<string, unknown>> }>(
    'GET', `/agents/${agentId}`,
  );
  expect(record.mcp_servers).toEqual(typed);
  expect(
    [Object.keys(record.mcp_servers), Object.keys(record.mcp_servers.zz_server)],
    'the server returns the keys in its own order, which is the condition under test',
  ).not.toEqual([Object.keys(typed), Object.keys(typed.zz_server)]);

  await expect(card.getByText('Saved', { exact: true }), 'the accepted write reads as saved').toBeVisible();
  await expect(save).toHaveCount(0);
  await expect(revert).toHaveCount(0);

  await test.step('a list in another order is still a change', async () => {
    await mcp.fill(JSON.stringify({
      ...typed,
      zz_server: { ...typed.zz_server, args: ['--second', '--first'] },
    }, null, 2));
    await mcp.press('Tab');
    await expect(save).toBeEnabled();
    await revert.click();
    await expect(save).toHaveCount(0);
    await expect.poll(async () => JSON.parse(await mcp.inputValue())).toEqual(typed);
  });
});
