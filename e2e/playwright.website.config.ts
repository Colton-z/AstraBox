import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from "../tests/e2e-contract/test-budget";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const websiteDir = path.join(repoRoot, "website");
const port = Number(process.env.E2E_WEBSITE_PORT ?? 5194);
const baseURL = process.env.E2E_WEBSITE_BASE_URL ?? `http://127.0.0.1:${port}/`;
const isCI = !!process.env.CI;

export default defineConfig({
  testDir: path.join(__dirname, "specs"),
  testMatch: "**/website.spec.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // Stops at the first failure, like every live suite here: the first failure
  // is the readable one. --max-failures=0 on the CLI runs a full sweep.
  maxFailures: 1,
  forbidOnly: isCI,
  timeout: MAX_E2E_TEST_MS,
  expect: { timeout: 15_000 },
  reporter: [[PLAYWRIGHT_BUDGET_REPORTER], ["list"]],
  outputDir: path.join(__dirname, "test-results-website"),
  use: {
    baseURL,
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium" }],
  webServer:
    process.env.E2E_SKIP_WEBSERVER === "1"
      ? undefined
      : {
          command: `npm run build && npm run serve -- --host 127.0.0.1 --port ${port}`,
          cwd: websiteDir,
          url: baseURL,
          reuseExistingServer: !isCI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
        },
});
