/**
 * E2E: a console page loads while another release answers some of its files.
 *
 * When two releases serve one origin, as replicas do during an upgrade, the
 * page a browser receives can name an entry, a module or a stylesheet that the
 * replica answering the next request does not have. A replica of a release
 * without the 404 for missing `assets/` files (`_mount_frontend` in
 * astrabox/api/app.py) answers with its own page as `200 text/html`, which the
 * browser refuses as a script or stylesheet. The boot script in
 * frontend/index.html then reloads the page until the files arrive, waits
 * while the server cannot be reached, and stops after five reloads in a row
 * with a manual retry.
 *
 * `page.route` plays the other release and answers exactly as such a replica
 * does. Each case reads what the user sees: the application running, styled,
 * at the address they opened, with no reload of their own; or, when the files
 * never arrive, a page that stops reloading and offers a retry.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { appPath } from '../fixtures/env';

type ShellFile = 'entry' | 'module' | 'stylesheet';

/** The boot script's bound on reloads in a row (MAX_RELOADS in frontend/index.html). */
const MAX_RELOADS = 5;
/** The longest wait between two boot attempts (PROBE_INTERVAL_MS in frontend/index.html). */
const LONGEST_WAIT_MS = 5_000;
const LOADED_MS = 60_000;

function attribute(tag: string, name: string): string | undefined {
  return tag.match(new RegExp(`\\s${name}="([^"]*)"`))?.[1];
}

/** The files the page served at *destination* names, as absolute URLs. */
async function shellFiles(
  request: APIRequestContext,
  destination: URL,
): Promise<{ html: string; files: Record<ShellFile, string> }> {
  const response = await request.get(destination.href);
  expect(response.status(), `GET ${destination.pathname}`).toBe(200);
  const html = await response.text();
  const tags = html.match(/<(?:script|link)\b[^>]*>/g) ?? [];
  const find = (file: ShellFile, matches: (tag: string) => boolean, name: 'src' | 'href') => {
    const tag = tags.find(matches);
    const value = tag && attribute(tag, name);
    expect(value, `the page names a ${file}`).toBeTruthy();
    return new URL(value!, destination).href;
  };
  return {
    html,
    files: {
      entry: find('entry', (tag) => tag.startsWith('<script')
        && attribute(tag, 'type') === 'module'
        && /\/assets\/main-[\w-]+\.js$/.test(attribute(tag, 'src') ?? ''), 'src'),
      module: find('module', (tag) => attribute(tag, 'rel') === 'modulepreload', 'href'),
      stylesheet: find('stylesheet', (tag) => attribute(tag, 'rel') === 'stylesheet'
        && (attribute(tag, 'href') ?? '').includes('/assets/'), 'href'),
    },
  };
}

/** Every main-frame document request the page makes, in order. */
function recordDocuments(page: Page): string[] {
  const documents: string[] = [];
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
      documents.push(request.url());
    }
  });
  return documents;
}

/**
 * The page has made *count* document loads, and the last one runs the console
 * with its stylesheet applied. A page whose stylesheet is wrong starts unstyled
 * and reloads afterwards, so the count is awaited first.
 */
async function expectRunningAfter(
  page: Page,
  documents: string[],
  count: number,
  stylesheet: string,
): Promise<void> {
  await expect.poll(() => documents.length, { timeout: LOADED_MS }).toBe(count);
  await expect(page.locator('#app-loading')).toHaveCount(0, { timeout: LOADED_MS });
  // The console shell's content region (AppShell.tsx), present on every
  // signed-in route; #root's first child is a theme script, never visible.
  await expect(page.locator('#app-content')).toBeVisible();
  await expect.poll(() => page.evaluate((href) => Array.from(document.styleSheets).some(
    (sheet) => sheet.href === href && sheet.cssRules.length > 0,
  ), stylesheet), { message: 'the page runs with its own stylesheet applied' }).toBe(true);
}

test('a file this release does not have is an uncacheable 404, and a console route is still the page', async ({
  request,
}) => {
  const missing = await request.get(appPath('/assets/main-E2EAnotherRelease.js'));
  expect(missing.status()).toBe(404);
  expect(missing.headers()['cache-control']).toBe('no-store');
  expect(missing.headers()['content-type'] ?? '').not.toContain('text/html');

  const route = await request.get(appPath('/manage/agents'));
  expect(route.status()).toBe(200);
  expect(route.headers()['content-type']).toContain('text/html');
});

for (const file of ['entry', 'module', 'stylesheet'] as const) {
  test(`the page runs at its address after another release answered its ${file} twice`, async ({
    baseURL,
    page,
    request,
  }) => {
    const destination = new URL(appPath(`/manage/agents?e2e_boot=${file}`), baseURL);
    const { html, files } = await shellFiles(request, destination);
    const otherRelease = html.replace(/main-[\w-]+\.js/g, 'main-E2EAnotherRelease.js');
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    const documents = recordDocuments(page);

    // The other release answers this file for the first two documents.
    const answeredByOtherRelease = 2;
    await page.route((url) => url.href === files[file], async (route) => {
      if (documents.length <= answeredByOtherRelease) {
        await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          headers: { 'Cache-Control': 'no-store, no-cache, must-revalidate' },
          body: otherRelease,
        });
      } else {
        await route.continue();
      }
    });

    await page.goto(destination.href, { waitUntil: 'domcontentloaded' });
    await expectRunningAfter(page, documents, answeredByOtherRelease + 1, files.stylesheet);
    expect(documents, 'each reload keeps the address the user opened').toEqual(
      Array(answeredByOtherRelease + 1).fill(destination.href),
    );
    expect(pageErrors).toEqual([]);
  });
}

test('a page whose files never arrive stops reloading and starts from its retry', async ({
  baseURL,
  page,
  request,
}) => {
  const destination = new URL(appPath('/manage/agents?e2e_boot=retry'), baseURL);
  const { html, files } = await shellFiles(request, destination);
  const otherRelease = html.replace(/main-[\w-]+\.js/g, 'main-E2EAnotherRelease.js');
  const documents = recordDocuments(page);
  const answersModule = (url: URL) => url.href === files.module;
  await page.route(answersModule, (route) => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: otherRelease,
  }));

  await page.goto(destination.href, { waitUntil: 'domcontentloaded' });
  const status = page.locator('#app-loading');
  const retry = status.getByRole('button', { name: 'Retry' });
  await expect(retry).toBeVisible({ timeout: LOADED_MS });
  await expect(status).toContainText('Unable to load AstraBox.');
  expect(documents).toHaveLength(1 + MAX_RELOADS);
  // Longer than any wait between two attempts: a page still reloading on its
  // own would have made another request by now.
  await page.waitForTimeout(LONGEST_WAIT_MS + 2_000);
  expect(documents, 'the page stops reloading on its own').toHaveLength(1 + MAX_RELOADS);

  await page.unroute(answersModule);
  await retry.click();
  await expectRunningAfter(page, documents, MAX_RELOADS + 2, files.stylesheet);
  expect(documents).toEqual(Array(MAX_RELOADS + 2).fill(destination.href));
});

test('a page waits for an unreachable server instead of reloading into the browser error page', async ({
  baseURL,
  page,
  request,
}) => {
  const destination = new URL(appPath('/manage/agents?e2e_boot=unreachable'), baseURL);
  const { files } = await shellFiles(request, destination);
  const documents = recordDocuments(page);
  await page.route((url) => url.href === files.entry, async (route) => {
    if (documents.length <= 1) await route.abort('connectionreset');
    else await route.continue();
  });
  // The server stays unreachable for the boot script's first two checks.
  let checks = 0;
  await page.route((url) => url.href === destination.href, async (route) => {
    if (route.request().resourceType() === 'fetch' && documents.length <= 1) {
      checks += 1;
      if (checks <= 2) {
        await route.abort('connectionrefused');
        return;
      }
    }
    await route.continue();
  });

  await page.goto(destination.href, { waitUntil: 'domcontentloaded' });
  const status = page.locator('#app-loading');
  await expect(status).toContainText('Retrying');
  await expect.poll(() => checks, { timeout: 4 * LONGEST_WAIT_MS }).toBeGreaterThanOrEqual(2);
  await expect(status, 'the page keeps retrying while the server is away').toContainText('Retrying');
  expect(documents).toHaveLength(1);

  await expectRunningAfter(page, documents, 2, files.stylesheet);
  expect(checks).toBe(3);
  expect(documents).toEqual([destination.href, destination.href]);
});
