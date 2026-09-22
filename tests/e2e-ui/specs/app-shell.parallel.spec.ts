/**
 * E2E: the app shell lists a real conversation and keeps its navigation and
 * account controls usable when the sidebar collapses and expands.
 */
import { test, expect, type Locator } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();

async function expectCollapsedControl(control: Locator, label: string): Promise<void> {
  await expect(control, `${label} remains visible`).toBeVisible();
  await expect(control, `${label} keeps an accessible name without visible text`).toHaveAccessibleName(/\S/);
  await expect.poll(() => control.evaluate((element) => {
    const rail = element.closest<HTMLElement>('[data-slot="sidebar-container"]');
    const icon = element.querySelector<HTMLElement>('[data-slot="avatar"], svg');
    if (!rail || !icon) return ['missing sidebar container or icon'];

    const failures: string[] = [];
    const railBox = rail.getBoundingClientRect();
    const buttonBox = element.getBoundingClientRect();
    const iconBox = icon.getBoundingClientRect();
    if (buttonBox.width < 31.5 || buttonBox.height < 31.5) {
      failures.push(`control is only ${buttonBox.width} × ${buttonBox.height}px`);
    }
    for (const [name, node] of [['control', element], ['icon', icon]] as const) {
      const box = node.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) failures.push(`${name} has no visible area`);
      if (Math.abs(box.left + box.width / 2 - railBox.left - railBox.width / 2) > 1) {
        failures.push(`${name} is not centered in the rail`);
      }
      if (box.left < railBox.left - 0.5 || box.right > railBox.right + 0.5) {
        failures.push(`${name} extends outside the rail`);
      }
      // A link may fit the rail while its narrower session row clips it.
      for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        const clips = (overflow: string) => ['hidden', 'clip', 'auto', 'scroll'].includes(overflow);
        const outer = ancestor.getBoundingClientRect();
        const left = outer.left + ancestor.clientLeft;
        const top = outer.top + ancestor.clientTop;
        if (clips(style.overflowX) && (box.left < left - 0.5 || box.right > left + ancestor.clientWidth + 0.5)) {
          failures.push(`${name} is horizontally clipped by ${ancestor.dataset.slot || ancestor.tagName}`);
        }
        if (clips(style.overflowY) && (box.top < top - 0.5 || box.bottom > top + ancestor.clientHeight + 0.5)) {
          failures.push(`${name} is vertically clipped by ${ancestor.dataset.slot || ancestor.tagName}`);
        }
        if (ancestor === rail) break;
      }
    }
    if (Math.abs(iconBox.top + iconBox.height / 2 - buttonBox.top - buttonBox.height / 2) > 1) {
      failures.push('icon is not vertically centered in its control');
    }
    return failures;
  }), { message: `${label} must remain whole and centered after collapse` }).toEqual([]);
}

test('console shell loads and lists the seeded agent', async ({ page, request }) => {
  await page.goto(appPath('/'));
  await expect(page.getByTestId('sessions-page')).toBeVisible();

  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  expect(agent.agent_id).toBeTruthy();
  const created = await api.startConversation(String(agent.agent_id));
  sessions.push(created.session_id);
  await api.waitForSessionReady(created.session_id);
  await openSessionView(page, created.session_id);

  const rail = page.locator('[data-slot="sidebar"]');
  const railContainer = rail.locator('[data-slot="sidebar-container"]');
  const trigger = page.locator('[data-slot="sidebar-trigger"]').first();
  const headerLinks = rail.locator('[data-slot="sidebar-header"] [data-sidebar="menu-button"]');
  const sessionLink = rail.locator(`[data-session-id="${created.session_id}"] a`);
  const sessionText = sessionLink.locator(':scope > span');
  const account = rail.locator('[data-slot="sidebar-footer"] [data-slot="dropdown-menu-trigger"]');
  const accountText = account.locator(':scope > div');
  const accountSettings = account.locator(':scope > svg');

  if (await rail.getAttribute('data-state') === 'collapsed') await trigger.click();
  await expect(rail).toHaveAttribute('data-state', 'expanded');
  await expect(headerLinks).toHaveCount(2);
  await expect(sessionText).toBeVisible();
  await expect(accountText).toBeVisible();
  await expect(accountSettings).toBeVisible();
  const expandedWidth = await railContainer.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished));
    return element.getBoundingClientRect().width;
  });
  const expandedAccountText = await accountText.innerText();
  expect(expandedAccountText.trim()).not.toEqual('');

  await trigger.click();
  await expect(rail).toHaveAttribute('data-state', 'collapsed');
  await expect.poll(async () => (await railContainer.boundingBox())!.width, {
    message: 'collapse must actually release horizontal space to the conversation',
  }).toBeLessThan(expandedWidth / 2);
  for (let index = 0; index < 2; index += 1) {
    await expectCollapsedControl(headerLinks.nth(index), `header destination ${index + 1}`);
  }
  await expectCollapsedControl(sessionLink, 'conversation');
  await expectCollapsedControl(account, 'account');
  await expect(accountText).toBeHidden();
  await expect(accountSettings).toBeHidden();

  await account.click();
  const accountMenu = page.locator('[data-slot="dropdown-menu-content"]');
  await expect(accountMenu).toBeVisible();
  await expect(accountMenu.getByRole('combobox', { name: /Language|语言/ })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(accountMenu).toBeHidden();

  await headerLinks.first().click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/agents'));
  await expect(rail).toHaveAttribute('data-state', 'collapsed');
  await sessionLink.click();
  await expect(page).toHaveURL((url) => url.pathname === appPath(`/sessions/${created.session_id}`));
  await expect(page.getByTestId('run-view')).toBeVisible();

  await trigger.click();
  await expect(rail).toHaveAttribute('data-state', 'expanded');
  await expect.poll(async () => (await railContainer.boundingBox())!.width).toBeCloseTo(expandedWidth, 0);
  await expect(sessionText).toBeVisible();
  await expect(accountText).toBeVisible();
  await expect(accountText).toHaveText(expandedAccountText, { useInnerText: true });
  await expect(accountSettings).toBeVisible();
  expect((await api.getMessages(created.session_id)).messages.filter((message) => (
    message.role === 'user'
  )), 'sidebar navigation must not submit a conversation turn').toHaveLength(0);

  await headerLinks.nth(1).click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents'));
  if (await rail.getAttribute('data-state') === 'collapsed') await trigger.click();
  const managementLink = rail.locator('[data-slot="sidebar-content"] a[href$="/manage/agents"]');
  await expect(managementLink.locator(':scope > span')).toBeVisible();
  const managementWidth = await railContainer.evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished));
    return element.getBoundingClientRect().width;
  });

  await trigger.click();
  await expect(rail).toHaveAttribute('data-state', 'collapsed');
  await expectCollapsedControl(managementLink, 'management section');
  await expectCollapsedControl(account, 'console settings');
  await account.click();
  await expect(accountMenu).toBeVisible();
  await expect(accountMenu.getByRole('combobox', { name: /Language|语言/ })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(accountMenu).toBeHidden();

  await trigger.click();
  await expect(rail).toHaveAttribute('data-state', 'expanded');
  await expect.poll(async () => (await railContainer.boundingBox())!.width).toBeCloseTo(managementWidth, 0);
  await expect(managementLink.locator(':scope > span')).toBeVisible();
});

test('resident app tab adopts a new hashed frontend entry despite local activity', async ({ baseURL, page }) => {
  const appUrl = new URL(appPath('/'), baseURL);
  await page.goto(appUrl.href, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toBeVisible();

  const loadedEntry = await page
    .locator('script[type="module"][src*="/assets/main-"]')
    .getAttribute('src');
  expect(loadedEntry, 'the running production tab must expose its immutable Vite entry').toBeTruthy();

  let servedNextReleaseCount = 0;
  let reloadRequests = 0;
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (
      request.resourceType() === 'document'
      && url.origin === appUrl.origin
      && url.pathname.startsWith(appUrl.pathname)
    ) {
      reloadRequests += 1;
    }
  });
  await page.route(`${appUrl.origin}${appUrl.pathname}**`, async (route) => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    if (
      servedNextReleaseCount < 1
      && request.resourceType() === 'fetch'
      && requestUrl.pathname.startsWith(appUrl.pathname)
      && request.headers().accept?.includes('text/html')
    ) {
      servedNextReleaseCount += 1;
      const nextEntry = new URL(appPath('/assets/main-E2ENextRelease.js'), appUrl).pathname;
      await route.fulfill({
        status: 200,
        contentType: 'text/html; charset=utf-8',
        headers: { 'Cache-Control': 'no-store, no-cache, must-revalidate' },
        body: [
          '<!doctype html>',
          '<html><head>',
          `<script type="module" src="${nextEntry}"></script>`,
          '</head><body></body></html>',
        ].join(''),
      });
      return;
    }
    await route.continue();
  });

  await page.evaluate((managementPath) => {
    window.history.replaceState({}, '', managementPath);
    const localDraft = document.createElement('textarea');
    localDraft.setAttribute('data-testid', 'frontend-release-local-draft');
    localDraft.value = 'this local draft must not block release adoption';
    document.body.appendChild(localDraft);
  }, appPath('/manage/e2e-frontend-release'));
  const initialTimeOrigin = await page.evaluate(() => performance.timeOrigin);
  const documentReplaced = page.waitForEvent('framenavigated', {
    predicate: (frame) => frame === page.mainFrame(),
    timeout: 15_000,
  });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));

  await expect.poll(() => servedNextReleaseCount, {
    timeout: 10_000,
    intervals: [100, 250, 500],
    message: 'the resident tab must observe the new server entry once',
  }).toBe(1);
  await expect.poll(() => reloadRequests, {
    timeout: 15_000,
    intervals: [100, 250, 500],
    message: 'the resident tab must reload after the entry hash changes',
  }).toBe(1);
  await documentReplaced;
  await page.waitForLoadState('domcontentloaded');
  expect(await page.evaluate((initial) => performance.timeOrigin !== initial, initialTimeOrigin)).toBe(true);
  await expect(page.locator('body')).toBeVisible();
});
