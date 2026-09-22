import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();

for (const failure of ['404', 'network', 'none'] as const) {
  test(`entry ${failure} reaches a real conversation without another navigation`, async ({ page, request }) => {
    const api = new AstraApi(request);
    const agent = await api.defaultAgent();
    const session = await api.startConversation(agent.agent_id);
    sessions.push(session.session_id);
    await api.waitForSessionReady(session.session_id);
    let entryRequests = 0;
    let documents = 0;
    const urls: string[] = [];
    const pageErrors: string[] = [];
    page.on('request', (outgoing) => {
      if (outgoing.isNavigationRequest() && outgoing.frame() === page.mainFrame()) documents += 1;
    });
    page.on('pageerror', (error) => { pageErrors.push(error.message); });
    await page.route(/\/assets\/main-[\w-]+\.js(?:\?.*)?$/, async (route) => {
      entryRequests += 1;
      urls.push(route.request().url());
      if (failure !== 'none' && entryRequests <= 3) {
        if (failure === 'network') await route.abort('connectionreset');
        else await route.fulfill({ status: 404, body: '' });
      } else {
        await route.continue();
      }
    });
    await page.goto(appPath(`/sessions/${session.session_id}`), { waitUntil: 'domcontentloaded' });
    if (failure !== 'none') {
      await expect(page.locator('#app-loading')).toContainText('Retrying');
      await expect.poll(() => entryRequests, { timeout: 10_000 }).toBeGreaterThanOrEqual(3);
      await expect(page.locator('#app-loading')).toContainText('Retrying');
    }
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    await expect(page.locator('#app-loading')).toHaveCount(0);
    expect(documents).toBe(1);
    expect(entryRequests).toBe(failure === 'none' ? 1 : 4);
    expect(new Set(urls).size).toBe(entryRequests);
    expect(pageErrors).toEqual([]);
    expect((await api.getSession(session.session_id)).session_id).toBe(session.session_id);
  });
}
