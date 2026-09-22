import path from 'node:path';

import suiteContract from './suite-contract.json';

if (!Number.isInteger(suiteContract.max_test_seconds) || suiteContract.max_test_seconds !== 180) {
  throw new Error('the live E2E contract must set max_test_seconds to exactly 180');
}

export const MAX_E2E_TEST_MS = suiteContract.max_test_seconds * 1_000;
export const PLAYWRIGHT_BUDGET_REPORTER = path.join(
  __dirname,
  'playwright-test-budget.cjs',
);
