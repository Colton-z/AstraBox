import { test, expect } from "@playwright/test";
import { settle } from "./layoutHelpers";

/**
 * Malformed API data leaves the console shell mounted and shows an error.
 *
 * `request()` returns the envelope's `data` without runtime type validation,
 * so each page must reject `null` before rendering its collection. The stub
 * keeps authentication valid and returns `data: null` for business requests.
 * An empty list is not an acceptable substitute because it means the request
 * succeeded with no records.
 */
for (const surface of [
  { path: "/manage/sessions", name: "a console list" },
  { path: "/assistants", name: "the assistant picker" },
]) {
  test(`${surface.name} reports a malformed response instead of going blank`, async ({
    page,
  }) => {
    await page.route("**/api/**", (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === "/api/v1/auth/session") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            authenticated: true,
            user: { user_id: "u-1", display_name: "Operator" },
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "OK", message: "ok", data: null }),
      });
    });

    const crashes: string[] = [];
    page.on("pageerror", (e) => crashes.push(String(e).slice(0, 120)));

    await page.goto(surface.path);
    await settle(page);
    await page.waitForTimeout(1200);

    const state = await page.evaluate(() => ({
      chars: (document.body.innerText || "").trim().length,
      shell: !!document.querySelector('[data-slot="app-content"]'),
      alerts: document.querySelectorAll('[role="alert"]').length,
    }));

    expect(state.chars, "the page rendered nothing at all").toBeGreaterThan(50);
    expect(state.shell, "the app shell was torn down").toBe(true);
    expect(
      state.alerts,
      "the page rendered but reported no failure, so the user is told nothing is wrong",
    ).toBeGreaterThan(0);
    expect(
      crashes,
      `an uncaught render error escaped:\n  ${crashes.join("\n  ")}`,
    ).toEqual([]);
  });
}
