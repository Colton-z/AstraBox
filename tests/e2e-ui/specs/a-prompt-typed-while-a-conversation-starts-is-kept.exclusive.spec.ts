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

// A new conversation opens from its first history read and shows its composer
// while the Session is still creating, so the reader starts typing at once. The
// read needs nothing from the starting box: it lands first, and only then is
// there a composer to type into. The Session becoming ready afterwards must not
// swap that composer out and drop the keystrokes.
test('a prompt typed while a conversation starts survives its first history read', async ({ page, request }) => {
  const api = new AstraApi(request);
  const name = `__e2e_typing_while_starting_${Date.now()}`;
  agentId = (await api.createColdTestAgent(name)).agent_id;

  // Hold the first history read: the page opens from it, so until it lands
  // there must be no composer whose keystrokes a late read could lose.
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
  await expect.poll(() => held, { message: 'the page asks for its history as it opens' }).toBe(true);
  await expect(composer, 'nothing to type into before the conversation\'s history lands').toHaveCount(0);
  const historyRead = page.waitForResponse(
    (response) => new URL(response.url()).pathname.endsWith(`/${sessionId}/history-blocks`),
  );
  releaseHistory();
  await historyRead;

  await expect(composer).toBeVisible();
  // A cold Agent takes seconds to start and the history read takes none, so
  // the reader types while the page still says the conversation is starting;
  // a Session already ready here would leave nothing below to prove.
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(pill, 'the reader types while the conversation is still starting')
    .toHaveAttribute('data-state', 'CREATING', { timeout: 5_000 });
  const composerNode = await composer.elementHandle();
  await composer.click();
  await page.keyboard.type('typed while starting');

  await expect.poll(
    async () => (await api.getSession(sessionId)).state,
    { timeout: 150_000 },
  ).not.toBe('CREATING');
  // Wait until the page itself has applied the ready Session, or has already
  // swapped the composer out, which the assertions below then report.
  await expect.poll(async () => {
    if (!(await composerNode!.evaluate((node) => node.isConnected))) return 'composer-replaced';
    return (await pill.getAttribute('data-state', { timeout: 1_000 }).catch(() => null)) ?? 'CREATING';
  }, { timeout: 30_000 }).not.toBe('CREATING');
  await page.keyboard.type(' and after it is ready');

  expect(await composerNode!.evaluate((node) => node.isConnected), 'the composer stayed mounted').toBe(true);
  await expect(composer).toHaveValue('typed while starting and after it is ready');
  await expect(composer).toBeFocused();
});
