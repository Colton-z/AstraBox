import fs from 'node:fs';
import path from 'node:path';

import { chromium, type FullConfig } from '@playwright/test';

import { loginViaOidc } from './fixtures/oidcLogin';

export default async function globalSetup(_config: FullConfig): Promise<void> {
  const statePath = (process.env.ASTRABOX_E2E_STORAGE_STATE || '').trim();
  if (!statePath) throw new Error('ASTRABOX_E2E_STORAGE_STATE is required');
  fs.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 });
  const browserServerEndpoint = (process.env.ASTRABOX_E2E_BROWSER_WS_ENDPOINT || '').trim();
  const browser = browserServerEndpoint
    ? await chromium.connect(browserServerEndpoint)
    : await chromium.launch();
  try {
    const { context } = await loginViaOidc(browser);
    try {
      await context.storageState({ path: statePath });
      fs.chmodSync(statePath, 0o600);
    } finally {
      await context.close();
    }
  } finally {
    await browser.close();
  }
}
