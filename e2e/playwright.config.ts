import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  MAX_E2E_TEST_MS,
  PLAYWRIGHT_BUDGET_REPORTER,
} from "../tests/e2e-contract/test-budget";

/**
 * Playwright config — AstraBox Community browser e2e.
 *
 * Drives the REAL redesigned INK console (frontend/, Vite dev server on :5173)
 * against a LIVE backend (uvicorn on :8000) that cold-starts an agent
 * container and streams a real `claude` turn over the AI-SDK Data-Stream-Protocol
 * SSE. The headline `happy-path` spec asserts the streamed assistant text — proof
 * the live turn is GREEN through the browser, not just curl.
 *
 * webServers (started in order; the frontend `/api` proxy targets 127.0.0.1:8000):
 *   1. backend  — e2e/scripts/serve-backend.sh: uvicorn :8000, env-sourced from the
 *      gitignored repo-root .env and mapped onto the ASTRABOX_MODEL_* names
 *      the runtime resolver reads (the script documents the full mapping + the two
 *      gates — ASTRABOX_LOCAL_MODE and ASTRABOX_MCP_PROXY_BASE_URL — that a
 *      live turn requires). Ready when GET /healthz answers 200.
 *   2. frontend — `npm run dev` in frontend/ (Vite on :5173 with the /api proxy).
 *
 * Set E2E_SKIP_WEBSERVER=1 to reuse servers you already have running (fast local
 * iteration). `reuseExistingServer` is also on outside CI so a re-run attaches to
 * a still-up stack instead of failing on the bound port.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const frontendDir = path.join(repoRoot, "frontend");

// Dedicated e2e ports (NOT the dev-stack 5173/8000): `reuseExistingServer` is
// convenient locally but silently attaches to any long-lived dev processes on
// shared ports — a 3-day-old backend with accumulated state once pushed the
// happy-path past its deadline. Dedicated ports make reuse harmless.
const FRONTEND_PORT = Number(process.env.E2E_FRONTEND_PORT ?? 5183);
const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 8123);
// ASTRABOX_E2E_BASE_URL points every live suite at one already-running
// deployment — the same variable, with the same meaning, as tests/e2e. When it
// is set, this config neither boots nor proxies a stack of its own: the
// deployed server serves the console and /api from one origin, so the specs
// exercise exactly what that deployment ships. Without it, the suite manages
// its own dedicated-port stack below.
const DEPLOYED_BASE_URL = (process.env.ASTRABOX_E2E_BASE_URL ?? "").trim();
const BASE_URL =
  DEPLOYED_BASE_URL || (process.env.E2E_BASE_URL ?? `http://localhost:${FRONTEND_PORT}`);
const isCI = !!process.env.CI;
const manageServers = process.env.E2E_SKIP_WEBSERVER !== "1" && !DEPLOYED_BASE_URL;

export default defineConfig({
  testDir: path.join(__dirname, "specs"),
  globalTeardown: path.join(__dirname, "global-teardown.ts"),
  // The layout gate lives in the same specs/ dir but needs no backend and runs at
  // four viewports; it has its own config (playwright.layout.config.ts) so that
  // starting a sandbox stack is never a prerequisite for checking geometry.
  testIgnore: [
    "**/layout*.spec.ts",
    // The API-shaped specs belong to playwright.api.config.ts, which names
    // them in its testMatch. Two of them refuse to load without their live-MCP
    // and JWT environment, so collecting them here makes a plain browser run
    // fail before its first spec on variables only the API run needs.
    "**/agent-mcp.spec.ts",
    "**/mcp-registry.spec.ts",
    "**/mcp-runtime.spec.ts",
    "**/extension-console.spec.ts",
    "**/extension-console-local.spec.ts",
    // website.spec.ts likewise has its own config.
    "**/website.spec.ts",
  ],
  // The live turn spawns real Docker containers; running specs in parallel would
  // race container/resource limits. One worker, serial.
  workers: 1,
  fullyParallel: false,
  // No silent green on a flaky live turn — a failure is a real signal (live-turn gate).
  // One retry only in CI to absorb a genuinely transient cold-LLM blip.
  retries: isCI ? 1 : 0,
  // A live run stops at its first failure, matching the pytest live suite's
  // forced maxfail=1: the first failure is the one that can be read, and every
  // spec after it spends real sandboxes proving nothing new. Pass
  // --max-failures=0 on the CLI for a deliberate full sweep.
  maxFailures: 1,
  forbidOnly: isCI,
  timeout: MAX_E2E_TEST_MS,
  expect: { timeout: 15_000 },
  reporter: isCI
    ? [[PLAYWRIGHT_BUDGET_REPORTER], ["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : [[PLAYWRIGHT_BUDGET_REPORTER], ["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: path.join(__dirname, "test-results"),

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    // The INK console is a dark, dense desktop surface — pin a desktop viewport.
    viewport: { width: 1440, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: manageServers
    ? [
        {
          // Backend first — the frontend proxy depends on it.
          command: "bash ./scripts/serve-backend.sh",
          cwd: __dirname,
          url: `http://127.0.0.1:${BACKEND_PORT}/healthz`,
          reuseExistingServer: !isCI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
          env: {
            ASTRABOX_REPO_ROOT: repoRoot,
            ASTRABOX_BACKEND_PORT: String(BACKEND_PORT),
          },
        },
        {
          command: `npm run dev -- --port ${FRONTEND_PORT} --strictPort`,
          cwd: frontendDir,
          url: BASE_URL,
          reuseExistingServer: !isCI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
          env: {
            // The Vite /api proxy must follow the e2e backend port.
            ASTRABOX_DEV_PROXY_TARGET: `http://127.0.0.1:${BACKEND_PORT}`,
          },
        },
      ]
    : undefined,
});
