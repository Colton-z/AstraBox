import { expect, test, type Request } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { openSessionView } from '../fixtures/sessionPage';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();

test('leaving a Session cancels its held history request without disturbing the next conversation', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const oldSession = await api.startConversation(agent.agent_id);
  sessions.push(oldSession.session_id);
  const nextSession = await api.startConversation(agent.agent_id);
  sessions.push(nextSession.session_id);
  await Promise.all([
    api.waitForSessionReady(oldSession.session_id),
    api.waitForSessionReady(nextSession.session_id),
  ]);
  const prompt = `HISTORY_CANCEL_${Date.now()}: Reply briefly without using tools.`;
  const reply = await api.sendTurn(nextSession.session_id, prompt);
  expect(reply.errorText).toBeNull();
  expect(reply.text.trim()).not.toEqual('');
  await api.waitForSession(nextSession.session_id, (session) => (
    session.state === 'READY' && session.last_turn_status === 'COMPLETED'
  ));
  const originalHistory = await api.getMessages(nextSession.session_id);

  const historyPath = apiPath(`/sessions/${oldSession.session_id}/history-blocks`);
  await page.addInitScript(({ path }) => {
    const observed = { started: false, aborted: false };
    Object.assign(window, { historyCancellationObservation: observed });
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname === path && !observed.started) {
        observed.started = true;
        const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
        observed.aborted = signal?.aborted === true;
        signal?.addEventListener('abort', () => { observed.aborted = true; }, { once: true });
      }
      return originalFetch(input, init);
    };
  }, { path: historyPath });

  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let heldRequest: Request | undefined;
  const failures: string[] = [];
  const pageErrors: string[] = [];
  page.on('requestfailed', (failed) => {
    if (failed === heldRequest) failures.push(failed.failure()?.errorText ?? 'missing failure');
  });
  page.on('pageerror', (error) => { pageErrors.push(error.message); });
  await page.route(`**${historyPath}?*`, async (route) => {
    if (heldRequest) return route.continue();
    heldRequest = route.request();
    const response = await route.fetch();
    expect(response.ok(), await response.text()).toBe(true);
    await gate;
    await route.fulfill({ response });
  });
  try {
    await page.goto(appPath(`/sessions/${oldSession.session_id}`), { waitUntil: 'domcontentloaded' });
    await expect.poll(() => Boolean(heldRequest)).toBe(true);
    await page.locator(`[data-session-id="${nextSession.session_id}"] a`).click();
    await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (
      window as unknown as { historyCancellationObservation: { aborted: boolean } }
    ).historyCancellationObservation.aborted)).toBe(true);
    release();
    await expect.poll(() => failures).toEqual(['net::ERR_ABORTED']);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    await expect(page.getByText(/NETWORK_ERROR|AbortError/)).toHaveCount(0);
    expect(pageErrors).toEqual([]);
    expect((await api.getMessages(nextSession.session_id)).messages).toEqual(originalHistory.messages);
    await openSessionView(page, oldSession.session_id);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    await expect(page.getByText(/NETWORK_ERROR|AbortError/)).toHaveCount(0);
  } finally {
    release();
  }
});
