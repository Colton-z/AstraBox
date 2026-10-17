/** A stored process group exposes its entries through one disclosure. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

test('ordinary process groups open once while whole-response summaries retain their groups', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const ids = { ordinary: randomUUID(), summary: randomUUID() };
  sessions.push(...Object.values(ids));
  const records = JSON.parse(execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-',
    JSON.stringify({ user_id: user.user_id, agent_id: agent.agent_id, sessions: ids }),
  ], {
    input: readFileSync(resolve(__dirname, '../fixtures/process_groups.py'), 'utf8'),
    encoding: 'utf8', timeout: 30_000,
  })) as Array<{ session_id: string; response_id: string; chunks: Record<string, unknown>[] }>;
  for (const record of records) {
    const rows = postgresRows<{ part: Record<string, unknown> }>(
      `SELECT part FROM session_output WHERE session_id=${sqlLiteral(record.session_id)} ORDER BY seq`,
    );
    expect(rows.map((row) => row.part)).toEqual(record.chunks);
    const state = await api.getSession(record.session_id);
    expect(state.state).toBe('TERMINATED');
    expect(state.sandbox_id).toBeFalsy();
  }
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  const history = await api.getHistoryBlocks(ids.ordinary);
  const headers = history.messages.flatMap((message) => message.parts)
    .filter((part) => part.type === 'data-process-block')
    .map((part) => part.data as { block_id: string; summarize: boolean; tool_count: number });
  expect(headers.map((header) => [header.summarize, header.tool_count])).toEqual([[false, 2], [false, 1]]);
  const detailPath = apiPath(`/sessions/${ids.ordinary}/history-blocks/${encodeURIComponent(headers[0].block_id)}`);
  let reads = 0;
  await page.route((url) => url.pathname === detailPath, async (route) => {
    reads += 1;
    if (reads === 1) {
      await route.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ code: 'UNEXPECTED_SERVER_ERROR', message: 'process detail fixture fault' }) });
    } else await route.continue();
  });
  await openSessionView(page, ids.ordinary);
  const blocks = page.getByTestId('assistant-turn-process');
  await expect(blocks).toHaveCount(2);
  for (const text of ['Beginning inspection', 'Intermediate conclusion', 'Final conclusion']) {
    await expect(page.getByTestId('run-view')).toContainText(text);
  }
  await expect(page.locator('[data-tool-call-id="failed"]')).toBeVisible();
  expect(reads).toBe(0);
  const first = blocks.nth(0);
  const trigger = first.getByTestId('assistant-turn-process-trigger');
  const closedWidth = (await first.boundingBox())!.width;
  await trigger.click();
  await expect(first).toContainText('could not be loaded');
  await first.getByRole('button', { name: 'Retry', exact: true }).click();
  for (const id of ['first', 'second']) await expect(first.locator(`[data-tool-call-id="${id}"]`)).toBeVisible();
  await expect(first.getByTestId('reasoning-part')).toBeVisible();
  await expect(first.getByTestId('assistant-process')).toHaveCount(0);
  await expect(blocks.nth(1).getByTestId('assistant-turn-process-trigger')).toHaveAttribute('aria-expanded', 'false');
  expect(await first.locator('[data-testid="reasoning-part"], [data-tool-call-id]').evaluateAll(
    (entries) => entries.map((entry) => entry.getAttribute('data-tool-call-id') || 'thinking'),
  )).toEqual(['thinking', 'first', 'second']);
  expect(Math.abs((await first.boundingBox())!.width - closedWidth)).toBeLessThanOrEqual(1);
  await first.locator('[data-tool-call-id="first"]').getByRole('button').first().click();
  await expect(first).toContainText('FIRST_OUTPUT');
  await first.getByTestId('reasoning-part').getByRole('button').first().click();
  await expect(first).toContainText('PROCESS_THINKING');
  await trigger.focus();
  await expect(trigger).toBeFocused();
  await trigger.press('Enter');
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await trigger.press('Enter');
  await expect(first.locator('[data-tool-call-id="first"]')).toBeVisible();
  expect(reads).toBe(2);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('[data-tool-call-id="first"]')).not.toBeVisible();
  await expect(page.locator('[data-tool-call-id="failed"]')).toBeVisible();
  await trigger.click();
  await expect(first.locator('[data-tool-call-id="first"]')).toBeVisible();
  expect(reads).toBe(3);

  await openSessionView(page, ids.summary);
  const summary = page.getByTestId('assistant-turn-process');
  await expect(summary).toHaveCount(1);
  await summary.getByTestId('assistant-turn-process-trigger').click();
  await expect(summary.getByTestId('assistant-process')).toHaveCount(2);
  await expect(summary.locator('[data-tool-call-id="first"]')).not.toBeVisible();
  await summary.getByTestId('assistant-process-trigger').first().click();
  await expect(summary.locator('[data-tool-call-id="first"]')).toBeVisible();
  await expect(summary.locator('[data-tool-call-id="second"]')).toBeVisible();
  await expect(summary.locator('[data-tool-call-id="third"]')).not.toBeVisible();
});
