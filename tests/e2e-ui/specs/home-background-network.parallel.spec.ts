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
    await page.goto(appPath(`/${subject}`), { waitUntil: 'domcontentloaded' });
    await expect.poll(() => answered).toBeGreaterThan(0);
    const cards = page.getByTestId(subject === 'agents' ? 'agent-option' : 'assistant-option');
    if (subject === 'agents') await expect(cards.first()).toBeVisible();
    else await expect(page.getByText('Loading Assistants…', { exact: true })).toHaveCount(0);
    // An empty Assistant catalog is a valid cached answer too.
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
      for (const status of [502, 503, 504]) {
        gatewayStatus = status;
        fault = 'http';
        const beforeFailure = failed;
        await returnToTab(page, Date.now());
        await expect.poll(() => failed).toBeGreaterThan(beforeFailure);
        await page.waitForTimeout(750);
        expect(await cards.allTextContents()).toEqual(before);
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

test('management background gateway failures retain rows while explicit Refresh reports failure', async ({ page }) => {
  let broken = false;
  let failed = 0;
  let answered = 0;
  const path = apiPath('/agents');
  page.on('response', response => {
    if (new URL(response.url()).pathname === path && response.ok()) answered += 1;
  });
  await page.route(url => url.pathname === path, async route => {
    if (!broken || route.request().method() !== 'GET') return route.continue();
    failed += 1;
    return route.fulfill({ status: 503, contentType: 'text/plain', body: 'Service unavailable' });
  });
  await page.addInitScript(() => localStorage.setItem('astrabox-lang', 'en'));
  await page.goto(appPath('/manage/agents'));
  const row = page.getByRole('row').filter({ has: page.getByText('Investment Research', { exact: true }) });
  await expect(row).toBeVisible();
  const baseline = await row.innerText();
  await watchAlerts(page);
  broken = true;
  await returnToTab(page, Date.now());
  await expect.poll(() => failed).toBeGreaterThan(0);
  await page.waitForTimeout(750);
  await expect.poll(() => row.innerText()).toBe(baseline);
  await expect(page.getByRole('alert')).toHaveCount(0);
  broken = false;
  const beforeRecovery = answered;
  await returnToTab(page, Date.now());
  await expect.poll(() => answered).toBeGreaterThan(beforeRecovery);
  await expectNoAlertFlashes(page);
  broken = true;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'HTTP_503' })).toBeVisible();
  broken = false;
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect.poll(() => row.innerText()).toBe(baseline);
  await expect(page.getByRole('alert')).toHaveCount(0);
});
