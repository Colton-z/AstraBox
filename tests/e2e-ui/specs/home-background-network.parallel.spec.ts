import { expect, test, type Page } from '@playwright/test';
import { apiPath, appPath } from '../fixtures/env';
import { returnToTab } from '../fixtures/tabReturn';

test.describe.configure({ mode: 'parallel' });
test.use({ locale: 'en-US' });

async function watchAlerts(page: Page) {
  await page.evaluate(() => {
    const state = window as typeof window & { backgroundAlerts: string[] };
    state.backgroundAlerts = [];
    new MutationObserver(() => {
      for (const alert of document.querySelectorAll('[role="alert"]')) {
        const text = alert.textContent?.trim();
        if (text) state.backgroundAlerts.push(text);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
}

async function expectNoAlertFlashes(page: Page) {
  expect(await page.evaluate(() => (
    window as typeof window & { backgroundAlerts: string[] }
  ).backgroundAlerts)).toEqual([]);
}

for (const subject of ['agents', 'assistants'] as const) {
  test(`${subject} home retains its cards across background transport and gateway failures but reports initial-load failures`, async ({ page, context }) => {
    let fault: 'none' | 'network' | 'http' = 'none';
    let failed = 0;
    let answered = 0;
    let gatewayStatus = 502;
    const path = apiPath(`/${subject}`);
    page.on('response', response => {
      if (new URL(response.url()).pathname === path && response.ok()) answered += 1;
    });
    await page.route(url => url.pathname === path, async route => {
      if (route.request().method() !== 'GET' || fault === 'none') return route.continue();
      failed += 1;
      if (fault === 'network') return route.abort('connectionreset');
      return route.fulfill({ status: gatewayStatus, contentType: 'text/plain', body: 'Gateway unavailable' });
    });
    await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
    // Agents are the home page's cards; Assistants are the rail's rows beside
    // it, and every user has at least their primary one.
    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    await expect.poll(() => answered).toBeGreaterThan(0);
    const cards = page.getByTestId(subject === 'agents' ? 'agent-option' : 'assistant-row');
    await expect(cards.first()).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    const before = await cards.allTextContents();
    await watchAlerts(page);
    const readAt = Date.now();
    try {
      fault = 'network';
      await returnToTab(page, readAt);
      await expect.poll(() => failed).toBeGreaterThanOrEqual(3);
      await page.waitForTimeout(750);
      expect(await cards.allTextContents()).toEqual(before);
      await expect(page.getByRole('alert')).toHaveCount(0);
      await context.setOffline(true);
      fault = 'none';
      const beforeReconnect = answered;
      await context.setOffline(false);
      await expect.poll(() => answered).toBeGreaterThan(beforeReconnect);
      await expect(page.getByRole('alert')).toHaveCount(0);
      // The reconnect read succeeded, so the page now holds its answer. The
      // list is shared with the lane's other specs, which create and delete
      // Agents, so the cards to keep are these, not the first load's.
      await page.waitForTimeout(750);
      const reconnected = await cards.allTextContents();
      for (const status of [502, 503, 504]) {
        gatewayStatus = status;
        fault = 'http';
        const beforeFailure = failed;
        await returnToTab(page, Date.now());
        await expect.poll(() => failed).toBeGreaterThan(beforeFailure);
        await page.waitForTimeout(750);
        expect(await cards.allTextContents()).toEqual(reconnected);
        await expect(page.getByRole('alert')).toHaveCount(0);
      }
      fault = 'none';
      const beforeRecovery = answered;
      await returnToTab(page, Date.now());
      await expect.poll(() => answered).toBeGreaterThan(beforeRecovery);
      await expect(page.getByRole('alert')).toHaveCount(0);
      await expectNoAlertFlashes(page);
      fault = 'http';
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('alert').filter({ hasText: 'HTTP_504' })).toBeVisible();
      // Cached background retention must not turn an unreachable first load
      // into a successful empty catalog. This is an isolated test-owned tab.
      fault = 'network';
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.getByRole('alert').filter({ hasText: 'NETWORK_ERROR' })).toBeVisible();
    } finally {
      fault = 'none';
      await context.setOffline(false);
      await test.info().attach('home-network-evidence', {
        body: JSON.stringify({ subject, failed, answered, before }), contentType: 'application/json',
      });
    }
  });
}

test('primary Assistant recovers transient reads without online events and retains its last successful row', async ({ page, context, request }, info) => {
  const path = apiPath('/assistants/primary');
  let failNext = true;
  let failures = 0;
  let answered = 0;
  const primaryIds: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem('astrabox-lang', 'en');
    const state = window as typeof window & { primaryDocument: string; primaryOnlineEvents: number };
    state.primaryDocument = String(performance.timeOrigin);
    state.primaryOnlineEvents = 0;
    window.addEventListener('online', () => { state.primaryOnlineEvents += 1; });
  });
  await page.route(url => url.pathname === path, async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    primaryIds.push((await response.json()).data.assistant_id);
    if (failNext) {
      failNext = false;
      failures += 1;
      // The server committed its answer; only its delivery to this tab is lost.
      return route.abort('connectionreset');
    }
    answered += 1;
    return route.fulfill({ response });
  });
  await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
  const rail = page.getByTestId('assistant-rail');
  await expect(rail.getByRole('alert').filter({ hasText: 'NETWORK_ERROR' })).toBeVisible();
  const documentId = await page.evaluate(() => (
    window as typeof window & { primaryDocument: string }
  ).primaryDocument);
  await expect.poll(() => answered, { timeout: 30_000 }).toBeGreaterThan(0);
  await expect(rail.getByTestId('assistant-row').first()).toBeVisible();
  await expect(rail.getByRole('alert')).toHaveCount(0);
  expect(await page.evaluate(() => (
    window as typeof window & { primaryOnlineEvents: number }
  ).primaryOnlineEvents)).toBe(0);

  const rows = rail.getByTestId('assistant-row');
  const before = await rows.allTextContents();
  await watchAlerts(page);
  const beforeRecovery = answered;
  failNext = true;
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect.poll(() => failures).toBe(2);
  expect(await rows.allTextContents()).toEqual(before);
  await expect(rail.getByRole('alert')).toHaveCount(0);
  await expect.poll(() => answered, { timeout: 30_000 }).toBeGreaterThan(beforeRecovery);
  expect(await rows.allTextContents()).toEqual(before);
  await expectNoAlertFlashes(page);
  expect(await page.evaluate(() => ({
    document: (window as typeof window & { primaryDocument: string }).primaryDocument,
    online: (window as typeof window & { primaryOnlineEvents: number }).primaryOnlineEvents,
  }))).toEqual({ document: documentId, online: 1 });
  expect(new Set(primaryIds).size).toBe(1);
  const owned = await request.get(apiPath('/assistants'));
  expect(owned.ok()).toBe(true);
  expect((await owned.json()).data.filter((row: { primary?: boolean }) => row.primary)
    .map((row: { assistant_id: string }) => row.assistant_id)).toEqual([primaryIds[0]]);

  const denied = await context.newPage();
  let permanentReads = 0;
  try {
    await denied.route(url => url.pathname === path, async route => {
      permanentReads += 1;
      return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({
        code: 'ASSISTANT_ACCESS_DENIED', message: 'Primary access denied', data: null,
        error: { code: 'ASSISTANT_ACCESS_DENIED', user_message: 'Primary access denied', retryable: false },
      }) });
    });
    await denied.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
    await expect(denied.getByTestId('assistant-rail').getByRole('alert')).toContainText('Primary access denied');
    // SWR2.5.1's first backoff is at most20s, including its slow-network interval.
    await denied.waitForTimeout(22_000);
    expect(permanentReads).toBe(1);
    await expect(denied.getByTestId('assistant-rail').getByRole('alert')).toContainText('Primary access denied');
    await info.attach('primary-network-recovery', {
      body: JSON.stringify({ failures, answered, primaryIds, permanentReads, documentId, before }),
      contentType: 'application/json',
    });
    await page.screenshot({ path: info.outputPath('primary-recovered.png'), fullPage: true });
  } finally {
    await denied.close();
  }
});
