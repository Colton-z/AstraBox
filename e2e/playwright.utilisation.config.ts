import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from "../tests/e2e-contract/test-budget";

/**
 * Config for the space-utilisation report.
 *
 * Separate from the layout gate because this is not a gate: it asserts nothing
 * and prints a table, and it visits 2560 — a width the gate has no reason to
 * spend time on. Sharing the gate's config would have meant either running the
 * report on every change or teaching the gate to skip it.
 *
 *   npm --prefix e2e run utilisation
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(path.resolve(__dirname, ".."), "frontend");
const PORT = Number(process.env.E2E_LAYOUT_PORT ?? 5193);
const BASE_URL = process.env.E2E_LAYOUT_BASE_URL ?? `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: path.join(__dirname, "specs"),
  testMatch: "**/utilisation.report.ts",
  globalSetup: path.join(__dirname, "layoutGlobalSetup.ts"),
  workers: 1,
  timeout: MAX_E2E_TEST_MS,
  reporter: [[PLAYWRIGHT_BUDGET_REPORTER], ["list"]],
  outputDir: path.join(__dirname, "test-results-utilisation"),
  use: { baseURL: BASE_URL, trace: "off", screenshot: "off" },
  projects: [{ name: "chromium" }],
  webServer:
    process.env.E2E_SKIP_WEBSERVER === "1"
      ? undefined
      : {
          command: `npm run dev -- --port ${PORT} --strictPort`,
          cwd: frontendDir,
          url: BASE_URL,
          reuseExistingServer: true,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
        },
});
