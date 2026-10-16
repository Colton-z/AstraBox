import { test, expect, type Route } from '@playwright/test';

import { apiPath, appPath } from '../fixtures/env';

test.use({ locale: 'en-US' });

test('an identity probe failure stays on the entry page and a manual retry restores its destination', async ({ page, browser }, testInfo) => {
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  const destination = appPath('/agents?auth-probe=return');
  const login = appPath(`/login?next=${encodeURIComponent('/agents?auth-probe=return')}`);
  const endpoint = `**${apiPath('/auth/session')}`;
  const faults: Array<{ name: string; answer: (route: Route) => Promise<void> }> = [
    { name: 'service unavailable', answer: (route) => route.fulfill({ status: 503, body: 'unavailable' }) },
    { name: 'network unavailable', answer: (route) => route.abort('failed') },
    { name: 'invalid JSON', answer: (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{' }) },
    { name: 'empty response', answer: (route) => route.fulfill({ json: null }) },
    { name: 'missing authentication result', answer: (route) => route.fulfill({ json: {} }) },
    { name: 'nonboolean authentication result', answer: (route) => route.fulfill({ json: { authenticated: 'false' } }) },
    { name: 'missing user', answer: (route) => route.fulfill({ json: { authenticated: true } }) },
    { name: 'blank user', answer: (route) => route.fulfill({ json: { authenticated: true, user: { user_id: '  ' } } }) },
    { name: 'nonstring user', answer: (route) => route.fulfill({ json: { authenticated: true, user: { user_id: 123 } } }) },
  ];

  // Both entry surfaces must retain the destination through an outage. Only
  // the probe is faulted; retries and the resulting console use the real API.
  for (const [index, fault] of faults.entries()) {
    const entry = index % 2 === 0 ? destination : login;
    await test.step(`${entry}: ${fault.name}`, async () => {
      let failing = true;
      await page.route(endpoint, (route) => failing ? fault.answer(route) : route.continue());
      try {
        await page.goto(entry);
        const error = page.getByRole('alert');
        await expect(error).toContainText('Could not check your sign-in status');
        expect(new URL(page.url()).pathname + new URL(page.url()).search).toBe(entry);
        await expect(page.getByTestId('sessions-page')).toHaveCount(0);
        await expect(page.getByRole('button', { name: 'Continue with your organization account' })).toHaveCount(0);
        failing = false;
        await page.getByRole('button', { name: 'Retry', exact: true }).click();
        await expect(page.getByTestId('sessions-page')).toBeVisible();
        expect(new URL(page.url()).pathname + new URL(page.url()).search).toBe(destination);
        await expect(page.getByRole('alert').filter({ hasText: 'Could not check your sign-in status' })).toHaveCount(0);
      } finally {
        await page.unroute(endpoint);
      }
    });
  }

  // A route absent by deployment contract still passes through, including
  // when the user opens the login URL on a deployment with no built-in login.
  await page.route(endpoint, (route) => route.fulfill({ status: 404 }));
  try {
    for (const entry of [destination, login]) {
      await page.goto(entry);
      await expect(page.getByTestId('sessions-page')).toBeVisible();
      expect(new URL(page.url()).pathname + new URL(page.url()).search).toBe(destination);
    }
  } finally {
    await page.unroute(endpoint);
  }

  // Prove the normal signed-out path using the real OIDC session endpoint,
  // with no credential, and retain its original destination on the login URL.
  const signedOut = await browser.newContext({
    baseURL: String(testInfo.project.use.baseURL),
    storageState: { cookies: [], origins: [] },
    locale: 'en-US',
  });
  try {
    await signedOut.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
    const entry = await signedOut.newPage();
    const response = entry.waitForResponse((item) => new URL(item.url()).pathname === apiPath('/auth/session'));
    await entry.goto(destination);
    const probe = await response;
    expect(probe.status()).toBe(200);
    expect(await probe.json()).toEqual({ authenticated: false });
    await expect(entry.getByRole('button', { name: 'Continue with your organization account' })).toBeVisible();
    expect(new URL(entry.url()).pathname + new URL(entry.url()).search).toBe(login);
    await expect(entry.getByTestId('sessions-page')).toHaveCount(0);
  } finally {
    await signedOut.close();
  }
});
