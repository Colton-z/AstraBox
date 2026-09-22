import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from "../tests/e2e-contract/test-budget";

const e2eDir = path.dirname(fileURLToPath(import.meta.url));
const baseURL = process.env.ASTRABOX_E2E_BASE_URL?.trim();

if (!baseURL) {
  throw new Error(
    "ASTRABOX_E2E_BASE_URL is required. Run this suite against the isolated AWS test stack; local services are intentionally not started.",
  );
}

export default defineConfig({
  testDir: path.join(e2eDir, "specs"),
  testMatch: [
    "agent-mcp.spec.ts",
    "mcp-registry.spec.ts",
    "mcp-runtime.spec.ts",
    "extension-console.spec.ts",
    "extension-console-local.spec.ts",
  ],
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // Stops at the first failure, like every live suite here: the first failure
  // is the readable one. --max-failures=0 on the CLI runs a full sweep.
  maxFailures: 1,
  timeout: MAX_E2E_TEST_MS,
  reporter: [[PLAYWRIGHT_BUDGET_REPORTER], ["list"]],
  outputDir: path.join(e2eDir, "test-results-api"),
  use: {
    baseURL,
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
  },
});
