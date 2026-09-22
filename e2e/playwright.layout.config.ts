import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from "../tests/e2e-contract/test-budget";

/**
 * Playwright config — LAYOUT gate.
 *
 * Separate from playwright.config.ts because the two gates need opposite things.
 * The live-turn gate needs a real uvicorn, a real sandbox and a real model, so it
 * runs serially at one pinned viewport and costs minutes. The layout gate needs
 * neither: `/api` is stubbed in the browser (specs/layoutHelpers.ts), so it runs
 * in parallel across four viewports in seconds and can gate every change.
 *
 * Only the frontend dev server is started. There is no backend to wait for, and
 * no `.env` or credentials are required — which is what lets this run in CI where
 * the live-turn gate cannot.
 *
 * Run it:
 *   npm --prefix e2e run test:layout
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(path.resolve(__dirname, ".."), "frontend");

// A dedicated port, distinct from the dev stack (5173) and the live-turn gate
// (5183), so a running dev server is never mistaken for this one.
const PORT = Number(process.env.E2E_LAYOUT_PORT ?? 5193);
const BASE_URL = process.env.E2E_LAYOUT_BASE_URL ?? `http://127.0.0.1:${PORT}`;
const isCI = !!process.env.CI;

export default defineConfig({
  testDir: path.join(__dirname, "specs"),
  // Warm the dev server's on-demand compile once, so a parallel worker never
  // waits behind another's first-request transform. See layoutGlobalSetup.ts.
  globalSetup: path.join(__dirname, "layoutGlobalSetup.ts"),
  // Glob, not a literal: the layout gate grows one file per surface
  // (layout.spec.ts = console tables, layout.shell.spec.ts = shell geometry,
  // layout.rows.spec.ts = row truncation, layout.session.spec.ts = the reading
  // column). One file per surface keeps each spec's doc comment about one thing,
  // and keeps concurrent work off a shared file.
  testMatch: "**/layout*.spec.ts",
  testIgnore: "**/utilisation.report.ts",
  fullyParallel: true,
  // Capped rather than one-per-core. The dev server transforms modules on
  // demand, and workers arriving together on a cold server queue behind each
  // other's compiles — enough to blow the visibility timeout. The failures were
  // random across specs and always landed just past it, which is the signature
  // of contention rather than of any one assertion.
  //
  // Four held until the suite grew a spec that opens the drawer; three holds
  // with the drawer warmed as well. The number is empirical — if it goes red on
  // a cold start again, the cause is a route the warm-up does not visit, and
  // lowering this only hides it.
  workers: 3,
  forbidOnly: isCI,
  retries: 0,
  timeout: MAX_E2E_TEST_MS,
  expect: { timeout: 15_000 },
  reporter: [[PLAYWRIGHT_BUDGET_REPORTER], ["list"]],
  outputDir: path.join(__dirname, "test-results-layout"),

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    // A failing layout assertion is a geometry claim; the screenshot is the
    // evidence for it, so keep one on every failure.
    screenshot: "only-on-failure",
    // Viewport is set per-describe by the spec — see VIEWPORTS.
    ...devices["Desktop Chrome"],
  },

  projects: [{ name: "chromium" }],

  webServer:
    process.env.E2E_SKIP_WEBSERVER === "1"
      ? undefined
      : {
          command: `npm run dev -- --port ${PORT} --strictPort`,
          cwd: frontendDir,
          url: BASE_URL,
          reuseExistingServer: !isCI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
        },
});
