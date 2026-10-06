import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
let agentId = '';

test.afterEach(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId).catch(() => undefined);
  agentId = '';
});

// A new conversation shows its composer while the Session is still creating, so
// the reader starts typing at once. Its first history read can land after the
// Session is ready; that late history must fill the open page instead of
// swapping the composer out and dropping the keystrokes.
test('a prompt typed while a conversation starts survives its first history read', async ({ page, request }) => {
  const api = new AstraApi(request);
  const name = `__e2e_typing_while_starting_${Date.now()}`;
  agentId = (await api.createColdTestAgent(name)).agent_id;

  // Hold the first history read until the page has observed the ready Session:
  // this is the order in which a slow history response meets a fast start.
  let releaseHistory!: () => void;
  const historyHeld = new Promise<void>((resolve) => { releaseHistory = resolve; });
  let held = false;
  await page.route('**/api/v1/sessions/*/history-blocks**', async (route) => {
    if (!held && route.request().method() === 'GET') {
      held = true;
      await historyHeld;
    }
    await route.continue();
  });

  await page.goto(appPath('/agents'));
  const card = page.getByTestId('agent-option').and(page.locator(`[data-agent-name="${name}"]`));
  await card.getByRole('button', { name: /^(Start conversation|开始对话)$/ }).click();
  await page.waitForURL(/\/sessions\/[^/]+$/);
  const sessionId = new URL(page.url()).pathname.split('/').pop()!;
  sessions.push(sessionId);

  const composer = page.getByTestId('composer-prompt');
  await expect(composer).toBeVisible();
  const composerNode = await composer.elementHandle();
  await composer.click();
  await page.keyboard.type('typed while starting');

  await expect.poll(
    async () => (await api.data<{ state: string }>('GET', `/sessions/${sessionId}`)).state,
    { timeout: 150_000 },
  ).not.toBe('CREATING');
  // Wait until the page itself has applied the ready Session, or has already
  // swapped the composer out, which the assertions below then report.
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect.poll(async () => {
    if (!(await composerNode!.evaluate((node) => node.isConnected))) return 'composer-replaced';
    return (await pill.getAttribute('data-state', { timeout: 1_000 }).catch(() => null)) ?? 'CREATING';
  }, { timeout: 30_000 }).not.toBe('CREATING');
  await page.keyboard.type(' and after it is ready');

  const historyRead = page.waitForResponse(
    (response) => new URL(response.url()).pathname.endsWith(`/${sessionId}/history-blocks`),
  );
  releaseHistory();
  await historyRead;

  expect(await composerNode!.evaluate((node) => node.isConnected), 'the composer stayed mounted').toBe(true);
  await expect(composer).toHaveValue('typed while starting and after it is ready');
  await expect(composer).toBeFocused();
});
