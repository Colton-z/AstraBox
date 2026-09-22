import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import { expect, test, type BrowserContext } from '@playwright/test';

import { absoluteBaseUrl } from '../fixtures/env';
import { loginViaOidc } from '../fixtures/oidcLogin';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

type Storage = Awaited<ReturnType<BrowserContext['storageState']>>;

function inferenceKeyAcceptance() {
  // Use the deployed credential path without returning the key to the runner.
  // This testbed uses native virtual keys, not browser-capability custom auth.
  const output = execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c', `
import json, urllib.request, urllib.error
from astrabox.identity.session_signing import session_signing_secret
from astrabox.providers.litellm_shared_auth import sandbox_inference_key
from astrabox.providers.model import LiteLLMModelEndpointProvider
key = sandbox_inference_key(session_signing_secret())
url = LiteLLMModelEndpointProvider.server_side_base_url().rstrip('/') + '/v1/models'
try:
    with urllib.request.urlopen(urllib.request.Request(
        url, headers={'Authorization': 'Bearer ' + key}), timeout=20) as response:
        data = json.load(response).get('data')
        print(json.dumps({'status': response.status,
                          'has_models': isinstance(data, list) and bool(data)}))
except urllib.error.HTTPError as exc:
    print(json.dumps({'status': exc.code, 'has_models': False}))
`,
  ], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] });
  return JSON.parse(output);
}

// This is a deployment-transition case: its input is a real browser login
// retained by an earlier candidate's normal OIDC global setup, not a token
// minted by the test. A fresh worker needs a baseline login before replacement.
function retiredBrowserLogin(currentSubject: string, hostname: string) {
  const stateFile = process.env.ASTRABOX_E2E_STORAGE_STATE;
  if (!stateFile) throw new Error('ASTRABOX_E2E_STORAGE_STATE is required');
  const artifacts = path.resolve(path.dirname(stateFile), '../..');
  if (path.basename(artifacts) !== 'e2e-artifacts') {
    throw new Error('deployment-reset acceptance requires the retained testbed artifact root');
  }
  const candidates: Array<{ file: string; modified: number }> = [];
  for (const group of fs.readdirSync(artifacts, { withFileTypes: true })) {
    if (!group.isDirectory()) continue;
    const directory = path.join(artifacts, group.name);
    for (const run of fs.readdirSync(directory, { withFileTypes: true })) {
      if (!run.isDirectory()) continue;
      const file = path.join(directory, run.name, 'oidc-storage-state.json');
      if (fs.existsSync(file) && fs.lstatSync(file).isFile()) {
        candidates.push({ file, modified: fs.statSync(file).mtimeMs });
      }
    }
  }
  for (const { file } of candidates.sort((a, b) => b.modified - a.modified)) {
    const state = JSON.parse(fs.readFileSync(file, 'utf8')) as Storage;
    const cookie = state.cookies.find((item) => item.name === 'astrabox_session'
      && item.domain === hostname);
    if (!cookie) continue;
    const claims = JSON.parse(Buffer.from(cookie.value.split('.')[1], 'base64url').toString('utf8'));
    if (typeof claims.sub === 'string' && claims.sub !== currentSubject
      && typeof claims.exp === 'number' && claims.exp > Date.now() / 1000 + 180) {
      return { file, cookie, subject: claims.sub as string };
    }
  }
  throw new Error('No unexpired OIDC browser login from a replaced candidate; run the baseline login before resetting this testbed');
}

test.use({ trace: 'off', video: 'off', locale: 'en-US' });

test('a clean deployment rejects the retired browser identity while a restart preserves the current login', async ({ browser, context }, info) => {
  const consoleUrl = String(process.env.ASTRABOX_E2E_CONSOLE_URL || '').replace(/\/$/, '');
  const current = await context.request.get(`${consoleUrl}/api/v1/auth/session`);
  expect(current.status()).toBe(200);
  const identity = await current.json();
  expect(identity.authenticated).toBe(true);
  const retired = retiredBrowserLogin(identity.user.user_id, new URL(consoleUrl).hostname);
  await info.attach('deployment-identities', {
    body: JSON.stringify({ current: identity.user.user_id, retired: retired.subject, source: retired.file }),
    contentType: 'application/json',
  });
  const stale = await browser.newContext({
    baseURL: consoleUrl,
    locale: 'en-US',
    storageState: { cookies: [retired.cookie], origins: [] },
  });
  try {
    const probe = await stale.request.get('/api/v1/auth/session');
    expect(probe.status()).toBe(200);
    expect(await probe.json(), 'a replaced identity database must not accept an unexpired old login').toEqual({ authenticated: false });
    const protectedResponse = await stale.request.get('/api/v1/admin/system/overview');
    expect(protectedResponse.status()).toBe(401);
    const page = await stale.newPage();
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in to AstraBox' })).toBeVisible();

    await restartServerContainer(absoluteBaseUrl(), 60_000);
    const retained = await context.request.get(`${consoleUrl}/api/v1/auth/session`);
    expect(retained.status()).toBe(200);
    expect(await retained.json()).toEqual(identity);
    const fresh = await loginViaOidc(browser);
    try {
      expect(fresh.session.user?.user_id).toBe(identity.user.user_id);
      expect(inferenceKeyAcceptance(), 'gateway must accept the new candidate inference key')
        .toEqual({ status: 200, has_models: true });
    } finally {
      await fresh.context.close();
    }
  } finally {
    await stale.close();
  }
});
