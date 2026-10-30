/**
 * AstraBox Community — browser-level E2E (Playwright).
 *
 * The harness shape is specs/ + fixtures/, with a `.parallel.` / `.exclusive.`
 * naming convention and one non-negotiable three-minute test budget. The AWS
 * campaign uses the production OIDC flow: global setup signs in through
 * Casdoor and stores only the resulting AstraBox session cookie.
 *
 * Run: npm --prefix frontend run e2e:ui   (ASTRABOX_E2E_BASE_URL to target).
 */
import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

import { armWorkerHangReport } from './fixtures/hangReport';
import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from '../../tests/e2e-contract/test-budget';

const repoRoot = path.resolve(__dirname, '../..');
const outputRoot = path.resolve(
  process.env.ASTRABOX_E2E_OUTPUT_DIR || path.join(repoRoot, '.e2e'),
);

// The config is loaded before worker test files, so shutdown diagnostics are in
// place even when a fixture teardown hangs. The helper ignores the runner.
armWorkerHangReport({
  delayMs: 60_000,
  reportDir: path.join(outputRoot, 'worker-hang-reports'),
});

const workers = process.env.ASTRABOX_E2E_WORKERS
  ? Math.max(1, Number.parseInt(process.env.ASTRABOX_E2E_WORKERS, 10))
  : 1;
const browserServerEndpoint = (process.env.ASTRABOX_E2E_BROWSER_WS_ENDPOINT || '').trim();
const externalAccountsEnabled = process.env.ASTRABOX_E2E_EXTERNAL_ACCOUNTS === '1';
// A deployment that runs no extension catalog (ASTRABOX_EXTENSION_PROVIDER=none)
// is a different deployment shape from the lanes' one, whose Agents select from
// the gateway's catalog. Its specs are collected only against such a
// deployment, which the run declares with ASTRABOX_E2E_EXTENSION_PROVIDER=none.
const noExtensionCatalog = (process.env.ASTRABOX_E2E_EXTENSION_PROVIDER || '').trim() === 'none';
// A deployment published below a path (ASTRABOX_PUBLIC_PATH_PREFIX) is a
// different deployment shape from the lanes' root one, with its own proxy in
// front. Its specs are collected only against such a deployment, which the run
// declares with ASTRABOX_E2E_APP_PREFIX.
const pathPrefixDeployment = Boolean((process.env.ASTRABOX_E2E_APP_PREFIX || '').trim());
// A deployment whose boxes take a resource envelope other than the defaults
// (ASTRABOX_SANDBOX_*_LIMIT / *_REQUEST) is a different deployment shape from
// the lanes' one. Its specs are collected only against such a deployment, which
// the run declares by naming the envelope in ASTRABOX_E2E_SANDBOX_ENVELOPE.
const sandboxEnvelopeDeployment = Boolean((process.env.ASTRABOX_E2E_SANDBOX_ENVELOPE || '').trim());
// A deployment that sets ASTRABOX_SANDBOX_QUOTA_BOXES a few boxes above what
// it runs is a different deployment shape from the lanes' unlimited one. Its
// specs are collected only when the run declares ASTRABOX_E2E_SANDBOX_QUOTA=1.
const sandboxQuotaDeployment = process.env.ASTRABOX_E2E_SANDBOX_QUOTA === '1';
// Historical cursor recovery upgrades the host in place using a declared source-bind fixture.
const claudeCursorUpgrade = Boolean((process.env.ASTRABOX_E2E_CLAUDE_CLIENT_BIND_FILE || '').trim());
export default defineConfig({
  globalSetup: path.join(__dirname, 'global-setup.ts'),
  testDir: path.join(__dirname, 'specs'),
  // Real Slack, Feishu, DingTalk and Google accounts are an explicit
  // acceptance target. Ordinary collection must never fail because those
  // private credentials are absent; the external specs have no skips or mocks.
  testIgnore: [
    ...(externalAccountsEnabled ? [] : ['**/*.external.spec.ts']),
    ...(noExtensionCatalog ? [] : ['**/*.no-extension-catalog.spec.ts']),
    ...(pathPrefixDeployment ? [] : ['**/*.path-prefix.spec.ts']),
    ...(sandboxEnvelopeDeployment ? [] : ['**/*.sandbox-envelope.spec.ts']),
    ...(sandboxQuotaDeployment ? [] : ['**/*.sandbox-quota.spec.ts']),
    ...(claudeCursorUpgrade ? [] : ['**/claude-compacted-result-reattach.exclusive.spec.ts']),
  ],
  fullyParallel: true,
  timeout: MAX_E2E_TEST_MS,
  expect: {
    timeout: 15_000,
  },
  workers,
  retries: process.env.CI ? 1 : 0,
  // Stops at the first failure, matching the pytest live suite's forced
  // maxfail=1: a failing walk leaves its scene readable, and every spec after
  // the first failure spends live sandboxes proving nothing new.
  maxFailures: 1,
  outputDir: path.join(outputRoot, 'test-results'),
  reporter: [
    [PLAYWRIGHT_BUDGET_REPORTER],
    ['list'],
    ['html', { outputFolder: path.join(outputRoot, 'playwright-report'), open: 'never' }],
    ['json', { outputFile: path.join(outputRoot, 'results.json') }],
  ],
  use: {
    baseURL: process.env.ASTRABOX_E2E_CONSOLE_URL || 'http://127.0.0.1:8000',
    storageState: process.env.ASTRABOX_E2E_STORAGE_STATE,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: (process.env.ASTRABOX_E2E_VIDEO || 'off') as 'off' | 'on' | 'retain-on-failure',
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    connectOptions: browserServerEndpoint ? { wsEndpoint: browserServerEndpoint } : undefined,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
