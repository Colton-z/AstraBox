/** A saved Environment card adopts masked values without losing another card's draft. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });
let environmentName = '';
onPassOnly(async ({ request }) => {
  if (environmentName) {
    await new AstraApi(request).data('DELETE', `/admin/environments/${environmentName}`);
  }
});

test('saving Environment credentials masks both inputs and releases only the saved draft', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const environments = await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments');
  const donor = environments.find((item) => item.name === agent.environment_name);
  expect(donor).toBeDefined();
  environmentName = `__e2e_secret_draft_${randomUUID()}`;
  const route = `/admin/environments/${environmentName}`;
  await api.data('PUT', route, {
    name: environmentName, enabled: false, engine_kind: donor!.engine_kind,
    sandbox_backend: donor!.sandbox_backend,
    networking: { type: 'limited', allowed_hosts: [], allow_mcp_servers: false },
  });
  // This disabled, unbound preset allocates no runtime and uses synthetic credentials.
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  await page.goto(appPath(`/manage/environments/${environmentName}`));
  const provider = page.locator('#environment-provider_access');
  const tracing = page.locator('#environment-tracing');
  const description = page.locator('#environment-description');
  const runtimeCard = page.locator('[data-slot="card"]').filter({ has: provider });
  const basicCard = page.locator('[data-slot="card"]').filter({ has: description });
  const runtimeSave = runtimeCard.getByRole('button', { name: 'Save', exact: true });
  const basicSave = basicCard.getByRole('button', { name: 'Save', exact: true });
  await expect(provider).toBeVisible();
  await expect(runtimeSave).toHaveCount(0);

  const pendingDescription = 'Keep this other card draft until its own Save';
  await description.fill(pendingDescription);
  await provider.fill(JSON.stringify({ api_key: 'e2e-synthetic-invalid-provider-key' }));
  await tracing.fill(JSON.stringify({
    enabled: false, endpoint: 'https://collector.example.invalid/api/public/otel',
    auth_token: 'Bearer e2e-synthetic-invalid-tracing-token',
  }));
  await tracing.blur();
  const saved = page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(route));
  await runtimeSave.click();
  expect((await saved).status()).toBe(200);
  await expect.poll(async () => JSON.parse(await provider.inputValue()))
    .toEqual({ api_key: '••••••••' });
  await expect.poll(async () => JSON.parse(await tracing.inputValue()))
    .toMatchObject({ auth_token: '••••••••', enabled: false });
  await expect(runtimeSave, 'the saved card must be clean without a refresh').toHaveCount(0);
  await expect(description).toHaveValue(pendingDescription);
  await expect(basicSave, 'the other card must still own its unsaved draft').toBeEnabled();
  const readback = async () => (await api.data<Array<Record<string, unknown>>>('GET', '/admin/environments'))
    .find((item) => item.name === environmentName)!;
  const stored = await readback();
  expect(stored.provider_access).toEqual({ api_key: '••••••••' });
  expect(stored.tracing).toMatchObject({ auth_token: '••••••••' });
  expect(stored.description).not.toBe(pendingDescription);

  // Advertise a new release only to the guard's HTML probe. API saves and
  // document navigations keep using the real deployment, as in the Agent draft E2E.
  const loadedEntry = await page.locator('script[type="module"][src*="/assets/main-"]').getAttribute('src');
  expect(loadedEntry, 'release holds require a production frontend entry').toBeTruthy();
  const documentOrigin = await page.evaluate(() => performance.timeOrigin);
  const recordUrl = new URL(page.url());
  let advertiseRelease = true;
  let probes = 0;
  await page.route((url) => url.origin === recordUrl.origin && url.pathname === recordUrl.pathname, async (intercept) => {
    const outgoing = intercept.request();
    if (advertiseRelease && outgoing.resourceType() === 'fetch' && outgoing.headers().accept?.includes('text/html')) {
      probes += 1;
      await intercept.fulfill({
        status: 200, contentType: 'text/html; charset=utf-8',
        body: `<script type="module" src="${appPath('/assets/main-E2ENextRelease.js')}"></script>`,
      });
    } else {
      if (outgoing.resourceType() === 'document') advertiseRelease = false;
      await intercept.continue();
    }
  });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => probes).toBeGreaterThan(0);
  await expect(description).toHaveValue(pendingDescription);
  expect(await page.evaluate(() => performance.timeOrigin), 'the remaining draft must hold the release').toBe(documentOrigin);

  const basicSaved = page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === apiPath(route));
  await basicSave.click();
  expect((await basicSaved).status()).toBe(200);
  await expect(basicSave).toHaveCount(0);
  await expect(runtimeSave).toHaveCount(0);
  const after = await readback();
  expect(after.description).toBe(pendingDescription);
  expect(after.provider_access).toEqual(stored.provider_access);
  expect(after.tracing).toEqual(stored.tracing);

  const reloaded = page.waitForEvent('framenavigated', { predicate: (frame) => frame === page.mainFrame() });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await reloaded;
  await page.waitForLoadState('domcontentloaded');
  expect(await page.evaluate(() => performance.timeOrigin)).not.toBe(documentOrigin);
  await expect(description).toHaveValue(pendingDescription);
  await expect.poll(async () => JSON.parse(await provider.inputValue()))
    .toEqual({ api_key: '••••••••' });
  await expect.poll(async () => JSON.parse(await tracing.inputValue()))
    .toMatchObject({ auth_token: '••••••••' });
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
});
