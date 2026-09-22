import { expect, test } from '@playwright/test';

import { loginViaOidc } from '../fixtures/oidcLogin';

test.use({ trace: 'off', video: 'off' });

test('Casdoor authorization-code login creates an AstraBox administrator session', async ({ browser }) => {
  const brandingContext = await browser.newContext();
  try {
    const login = await brandingContext.newPage();
    const consoleUrl = String(process.env.ASTRABOX_E2E_CONSOLE_URL || '').replace(/\/$/, '');
    const issuer = String(process.env.ASTRABOX_E2E_OIDC_ISSUER || '').replace(/\/$/, '');
    expect(consoleUrl).not.toBe('');
    expect(issuer).not.toBe('');
    await login.goto(`${consoleUrl}/api/v1/auth/login?next=/`);
    await login.waitForURL((url) => url.origin === issuer);
    const logo = login.getByRole('img', { name: 'AstraBox Console', exact: true });
    await expect(logo).toBeVisible();
    await expect.poll(() => logo.evaluate((image: HTMLImageElement) =>
      image.complete && image.naturalWidth > 0 && image.naturalHeight > 0), {
      message: 'Casdoor must render a decoded AstraBox logo before sign-in',
    }).toBe(true);
    const logoSize = await logo.boundingBox();
    expect(logoSize?.height).toBeGreaterThanOrEqual(24);
    expect(logoSize?.height).toBeLessThanOrEqual(80);
    await expect(login.getByRole('button', { name: 'Sign In', exact: true })).toBeInViewport();
    const logoUrl = await logo.evaluate((image: HTMLImageElement) => image.currentSrc);
    expect(new URL(logoUrl).origin, 'bundled login branding must not require an external CDN').toBe(issuer);
    const asset = await brandingContext.request.get(logoUrl);
    expect(asset.status()).toBe(200);
    expect(asset.headers()['content-type']).toContain('image/svg+xml');
    await login.reload();
    await expect.poll(() => logo.evaluate((image: HTMLImageElement) =>
      image.complete && image.naturalWidth > 0)).toBe(true);
  } finally {
    await brandingContext.close();
  }
  const { context, page, session } = await loginViaOidc(browser);
  try {
    await expect(page.locator('body')).not.toContainText('Sign in to AstraBox');
    expect(session.authenticated).toBe(true);
    expect(session.user?.roles).toContain('admin');

    const issuer = String(process.env.ASTRABOX_E2E_OIDC_ISSUER || '').replace(/\/$/, '');
    const gateway = String(process.env.ASTRABOX_E2E_HTTPS_MODEL_GATEWAY_URL || '').replace(/\/$/, '');
    expect(issuer, 'the live identity management URL must be configured').not.toBe('');
    expect(gateway, 'the live model gateway management URL must be configured').not.toBe('');

    await page.goto('/manage/system');
    const identityLink = page.getByRole('link', { name: /Casdoor identity/ });
    await expect(identityLink).toHaveAttribute('href', issuer);
    await expect(identityLink).toHaveAttribute('target', '_blank');
    const apiAccessLink = page.getByRole('link', { name: /AstraBox API API access/ });
    await expect(apiAccessLink).toHaveAttribute(
      'href',
      `${issuer}/applications/astrabox/astrabox-api`,
    );
    await expect(apiAccessLink).toHaveAttribute('target', '_blank');
    const gatewayLink = page.getByRole('link', { name: /LiteLLM gateway/ });
    await expect(gatewayLink).toHaveAttribute('href', `${gateway}/ui/`);
    await expect(gatewayLink).toHaveAttribute('target', '_blank');
  } finally {
    await context.close();
  }
});
