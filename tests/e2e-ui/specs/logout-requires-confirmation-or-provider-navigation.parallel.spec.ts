/** Failed logout keeps the page; provider logout uses a top-level navigation. */
import { expect, test, type Route } from '@playwright/test';

import { apiPath, appPath } from '../fixtures/env';
import { loginViaOidc } from '../fixtures/oidcLogin';

test.use({ trace: 'off', video: 'off' });

test('logout preserves failures, follows the provider navigation contract and confirms native sign-out', async ({ browser }) => {
  const { context, page, session } = await loginViaOidc(browser);
  const logoutPath = apiPath('/auth/logout');
  const endpoint = `**${logoutPath}`;
  const signOut = async () => {
    await page.getByTestId('user-menu-trigger').click();
    await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
  };
  const probe = async () => {
    const response = await context.request.get(apiPath('/auth/session'));
    expect(response.status()).toBe(200);
    return response.json();
  };
  try {
    await context.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
    await page.goto(appPath('/agents?logout=keep-page'));
    await expect(page.getByTestId('user-menu-trigger')).toBeVisible();
    const originalUrl = page.url();
    const marker = '/agents?logout=retained-destination';
    await page.evaluate((value) => window.sessionStorage.setItem('astrabox:signin-redirect', value), marker);
    let navigations = 0;
    page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navigations += 1; });
    const faults: Array<{ expected: RegExp; answer: (route: Route) => Promise<void> }> = [
      { expected: /LOGOUT_FAILED: HTTP 405/, answer: (route) => route.fulfill({ status: 405 }) },
      { expected: /LOGOUT_FAILED: HTTP 500/, answer: (route) => route.fulfill({ status: 500 }) },
      { expected: /Could not sign out: Failed to fetch/, answer: (route) => route.abort('failed') },
      { expected: /LOGOUT_NOT_CONFIRMED/, answer: (route) => route.fulfill({ status: 204 }) },
    ];
    for (const fault of faults) {
      let posts = 0;
      const previousNavigations = navigations;
      await page.route(endpoint, (route) => {
        expect(route.request().method()).toBe('POST');
        posts += 1;
        return fault.answer(route);
      });
      try {
        await signOut();
        await expect(page.locator('[data-sonner-toast]').filter({ hasText: fault.expected }).last()).toBeVisible();
        expect(page.url()).toBe(originalUrl);
        expect(navigations, 'a failed logout must not navigate, including to the same URL').toBe(previousNavigations);
        expect(await page.evaluate(() => window.sessionStorage.getItem('astrabox:signin-redirect'))).toBe(marker);
        expect(await probe()).toMatchObject({ authenticated: true, user: { user_id: session.user!.user_id } });
        expect(posts, 'logout POSTs must not be replayed').toBe(1);
      } finally {
        await page.unroute(endpoint);
      }
    }

    // The navigation fixture deliberately leaves the real login intact. It
    // proves the browser handoff, not an unavailable organization's SSO logout.
    const returnTo = new URL(appPath('/'), originalUrl).href;
    const provider = new URL('https://logout-contract.invalid/end-session');
    provider.searchParams.set('post_logout_redirect_uri', returnTo);
    const providerRequests: Array<{ navigation: boolean; resource: string; method: string; returnTo: string | null }> = [];
    await page.route(provider.href, async (route) => {
      const outgoing = route.request();
      providerRequests.push({
        navigation: outgoing.isNavigationRequest() && outgoing.frame() === page.mainFrame(),
        resource: outgoing.resourceType(), method: outgoing.method(),
        returnTo: new URL(outgoing.url()).searchParams.get('post_logout_redirect_uri'),
      });
      await route.fulfill({ status: 302, headers: { location: returnTo } });
    });
    await page.route(endpoint, (route) => route.fulfill({ status: 200, json: { redirect_url: provider.href } }));
    try {
      await signOut();
      await expect.poll(() => providerRequests.length).toBe(1);
      await page.waitForURL(returnTo);
      await expect(page.getByTestId('user-menu-trigger')).toBeVisible();
      expect(providerRequests).toEqual([{ navigation: true, resource: 'document', method: 'GET', returnTo }]);
      expect(await page.evaluate(() => window.sessionStorage.getItem('astrabox:signin-redirect'))).toBeNull();
      expect(await probe(), 'a navigation instruction must not fabricate signed-out state')
        .toMatchObject({ authenticated: true, user: { user_id: session.user!.user_id } });
    } finally {
      await page.unroute(endpoint);
      await page.unroute(provider.href);
    }

    const nativeLogout = page.waitForResponse((response) => new URL(response.url()).pathname === logoutPath);
    await signOut();
    expect((await nativeLogout).status()).toBe(204);
    await page.waitForURL((url) => url.pathname === appPath('/login'));
    expect(await probe()).toEqual({ authenticated: false });
    await expect(page.getByRole('button', { name: 'Continue with your organization account' })).toBeVisible();
  } finally {
    await context.close();
  }
});
