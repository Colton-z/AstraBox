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
const externalChannelsEnabled = process.env.ASTRABOX_E2E_EXTERNAL_CHANNELS === '1';
export default defineConfig({
  globalSetup: path.join(__dirname, 'global-setup.ts'),
  testDir: path.join(__dirname, 'specs'),
  // Real Slack, Feishu, and DingTalk accounts are an explicit acceptance
  // target. Ordinary collection must never fail because those private bot
  // credentials are absent; the external spec itself has no skips or mocks.
  testIgnore: externalChannelsEnabled ? [] : ['**/*.external.spec.ts'],
  fullyParallel: true,
  timeout: MAX_E2E_TEST_MS,
  expect: {
    timeout: 15_000,
  },
  workers,
  retries: process.env.CI ? 1 : 0,
  // Stops at the first failure, matching the pytest live suite's forced
  // maxfail=1: a failing walk leaves its scene readable, and every spec after
  // the first failure spends live sandboxes proving nothing new. Each audit
  // walk test (visual-grammar, console-interaction) aggregates every route it
  // covers before failing, but a run holds several such tests — an audit that
  // wants all of them past the first red passes --max-failures=0 on the CLI.
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
