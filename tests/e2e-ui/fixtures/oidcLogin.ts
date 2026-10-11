import fs from 'node:fs';

import type { Browser, BrowserContext, Page } from '@playwright/test';

import { apiPath } from './env';

type LoginResult = {
  context: BrowserContext;
  page: Page;
  session: {
    authenticated: boolean;
    user?: { user_id?: string; roles?: string[] };
  };
};

const PLATFORM_ADMIN_ROLE = 'admin';

function required(name: string): string {
  const value = (process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function readSecret(path: string): string {
  const stat = fs.lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`OIDC password is not a regular file: ${path}`);
  }
  const value = fs.readFileSync(path, 'utf8').trim();
  if (!value) throw new Error(`OIDC password file is empty: ${path}`);
  return value;
}

/**
 * Fill in and submit the identity provider's sign-in form that *page* shows,
 * as the suite's account. The caller waits for wherever the provider sends the
 * browser next.
 */
export async function submitIssuerSignIn(page: Page): Promise<void> {
  const username = required('ASTRABOX_E2E_OIDC_USERNAME');
  const password = readSecret(required('ASTRABOX_E2E_OIDC_PASSWORD_FILE'));
  const usernameInput = page.locator(
    'input[name="username"], input[placeholder*="username" i], input[type="text"]',
  ).first();
  const passwordInput = page.locator(
    'input[name="password"], input[type="password"]',
  ).first();
  await usernameInput.fill(username);
  await passwordInput.fill(password);
  const submit = page.locator(
    'button[type="submit"], input[type="submit"], button:has-text("Sign In"), button:has-text("Login")',
  ).first();
  await submit.click();
}

export async function loginViaOidc(browser: Browser): Promise<LoginResult> {
  const consoleUrl = required('ASTRABOX_E2E_CONSOLE_URL').replace(/\/$/, '');
  const issuer = required('ASTRABOX_E2E_OIDC_ISSUER').replace(/\/$/, '');
  const context = await browser.newContext({ baseURL: consoleUrl });
  const page = await context.newPage();

  // Below ASTRABOX_E2E_APP_PREFIX when the deployment is published under a
  // path; `next` is a console route, which the server places below it.
  await page.goto(`${apiPath('/auth/login')}?next=/`);
  await page.waitForURL((url) => url.origin === issuer, { timeout: 30_000 });
  await submitIssuerSignIn(page);
  await page.waitForURL(
    (url) => url.origin === consoleUrl && !url.pathname.startsWith(apiPath('/auth/')),
    { timeout: 30_000 },
  );
  const session = await page.evaluate(async (sessionPath) => {
    const response = await fetch(sessionPath, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`auth session returned HTTP ${response.status}`);
    return response.json();
  }, apiPath('/auth/session')) as LoginResult['session'];
  if (!session.authenticated || !session.user?.user_id) {
    throw new Error(`browser OIDC login did not create an AstraBox session: ${JSON.stringify(session)}`);
  }
  if (!session.user.roles?.includes(PLATFORM_ADMIN_ROLE)) {
    throw new Error('browser OIDC login did not map the Casdoor administrator role');
  }
  return { context, page, session };
}
