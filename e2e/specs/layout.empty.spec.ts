import { test, expect } from "@playwright/test";
import { settle } from "./layoutHelpers";

/**
 * EMPTY-STATE GATE — "there is nothing here" is a state of the page, not a card
 * in a grid of one.
 *
 * Both pickers rendered their empty message as a card inside the same grid the
 * items would have used, so it kept the grid's height and left the rest of the
 * page as bare background — measured at 1440, the card ended around y=290 with
 * roughly 590px of nothing under it. The assistants side also inherited a
 * single grid column, so its message read as one lonely card rather than as an
 * empty page.
 *
 * The assertion is proportional, not a pixel: whatever the viewport, the empty
 * state reaches the bottom of the scrollable well apart from the page's own
 * padding. That stays true if the padding token, the measure or the copy
 * changes.
 */
const EMPTY: Record<string, unknown> = {
  "/user/current": { user_id: "u-1", display_name: "Operator" },
  "/agents": [],
  "/assistants": [],
  "/sessions": { sessions: [], has_more: false, next_cursor: null },
};

for (const surface of [
  { path: "/agents", name: "the agent picker" },
  { path: "/assistants", name: "the assistant picker" },
]) {
  test(`${surface.name} fills the page when it has nothing to show`, async ({ page }) => {
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
      const path = pathname.replace("/api/v1", "");
      const key = Object.keys(EMPTY).find((k) => path === k || path.startsWith(`${k}?`));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: "OK", message: "ok", data: key ? EMPTY[key] : null }),
      });
    });

    await page.goto(surface.path);
    await settle(page);
    await page.waitForTimeout(600);

    const box = await page.evaluate(() => {
      const card = document.querySelector(".border-dashed");
      const well = document.querySelector('[data-slot="page-scroll"]');
      if (!card || !well) return null;
      const c = card.getBoundingClientRect();
      const w = well.getBoundingClientRect();
      return {
        gapUnder: Math.round(w.bottom - c.bottom),
        widthShare: c.width / w.width,
        wellHeight: Math.round(w.height),
      };
    });

    expect(box, "no empty-state card rendered").not.toBeNull();
    // 40px covers the page's vertical padding with room for a rounding; it is
    // an order-of-magnitude check against the ~590px this replaced, not a
    // pixel-perfect one.
    expect(
      box!.gapUnder,
      `${box!.gapUnder}px of empty page under the empty state`,
    ).toBeLessThan(40);
    // And it spans the column rather than sitting in one grid cell.
    expect(box!.widthShare, "the empty state occupies one grid column").toBeGreaterThan(0.8);
  });
}
