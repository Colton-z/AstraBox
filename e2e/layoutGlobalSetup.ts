import { chromium } from "@playwright/test";
import { CONSOLE_ROUTES, stubApi } from "./specs/layoutHelpers";

/**
 * Compile every route once before the gate runs.
 *
 * The dev server transforms modules on demand, on first request. With workers
 * in parallel, two of them hit an uncompiled route at the same moment and the
 * second one waits behind the first one's compile — long enough to blow the
 * visibility timeout. The symptom is a failure that moves to whichever spec
 * happens to run first, which is exactly what a real layout regression does not
 * do.
 *
 * Warming in a single page first makes every later navigation a cache hit. Note
 * this has to be a real browser: an HTTP request for the route returns the SPA
 * shell without pulling any of the modules the page actually renders.
 */
export default async function globalSetup(): Promise<void> {
  const port = process.env.E2E_LAYOUT_PORT ?? "5193";
  const base = process.env.E2E_LAYOUT_BASE_URL ?? `http://127.0.0.1:${port}`;

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await stubApi(page);
  try {
    // The app shell, the console shell and the session view compile separately;
    // warm one route from each, plus every console page the gate visits. A
    // route missing from this list is the shape the flakiness takes: the
    // failure follows whichever spec runs first, not a viewport.
    for (const path of [
      "/agents",
      "/assistants",
      "/sessions/s-001",
      ...CONSOLE_ROUTES.map((r) => r.path),
      // The drawer pulls in the whole form-control set, and it is the heaviest
      // thing any spec opens. Warming the list route does not touch it.
      "/manage/agents?new=1",
    ]) {
      await page
        .goto(base + path, { waitUntil: "load", timeout: 60_000 })
        .catch(() => {});
      await page.waitForTimeout(150);
    }
  } finally {
    await browser.close();
  }
}
