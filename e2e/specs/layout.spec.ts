import { test, expect } from "@playwright/test";
import {
  CONSOLE_ROUTES,
  VIEWPORTS,
  settle,
  stubApi,
} from "./layoutHelpers";

/**
 * Console information stays reachable at every supported viewport.
 *
 * A table that exceeds its frame may scroll horizontally, while the page body
 * itself must not. These assertions pin that behavior without freezing a
 * width, column count, or breakpoint. The API is stubbed because only layout
 * is under test; see `layoutHelpers.ts`.
 */

for (const vp of VIEWPORTS) {
  test.describe(`@${vp.name}`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    test(`console tables keep every column reachable @${vp.name}`, async ({
      page,
    }) => {
      await stubApi(page);
      const unreachable: string[] = [];

      for (const route of CONSOLE_ROUTES) {
        await page.goto(route.path, { waitUntil: "domcontentloaded" });
        const table = page.getByTestId("console-table").first();
        await expect(table).toBeVisible({ timeout: 15_000 });
        await settle(page);

        const verdict = await table.evaluate((el) => {
          const ox = getComputedStyle(el).overflowX;
          const selfScrolls = ox === "auto" || ox === "scroll";
          const selfClips = ox === "hidden" || ox === "clip";

          // Only content that escapes its own box can be reached by scrolling an
          // ancestor. Once the element clips itself, the overflow never reaches
          // the ancestor's scroll area, so a scrollable parent proves nothing.
          // Note an ancestor with only `overflow-y: auto` computes `overflow-x`
          // to `auto` too, which makes this an easy false negative to write.
          let node: HTMLElement | null = el.parentElement;
          let ancestorScrolls = false;
          while (node && node !== document.body) {
            const pox = getComputedStyle(node).overflowX;
            if (pox === "auto" || pox === "scroll") {
              ancestorScrolls = true;
              break;
            }
            if (pox === "hidden" || pox === "clip") break; // clipped before any scroller
            node = node.parentElement;
          }

          return {
            overflowing: el.scrollWidth > el.clientWidth + 1,
            reachable: selfScrolls || (!selfClips && ancestorScrolls),
            overflowX: ox,
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
          };
        });

        if (verdict.overflowing && !verdict.reachable) {
          unreachable.push(
            `${route.id}: ${verdict.scrollWidth}px of columns inside a ` +
              `${verdict.clientWidth}px box (overflow-x: ${verdict.overflowX}), ` +
              `with no way to scroll to them`,
          );
        }
      }

      expect(
        unreachable,
        `Columns are clipped with no scroll affordance:\n  ${unreachable.join("\n  ")}`,
      ).toEqual([]);
    });

    test(`console pages never scroll sideways @${vp.name}`, async ({ page }) => {
      await stubApi(page);
      const sideways: string[] = [];

      for (const route of CONSOLE_ROUTES) {
        await page.goto(route.path, { waitUntil: "domcontentloaded" });
        await expect(page.getByTestId("console-table").first()).toBeVisible({
          timeout: 15_000,
        });
        await settle(page);

        const overshoot = await page.evaluate(() => {
          const doc = document.documentElement;
          return doc.scrollWidth - doc.clientWidth;
        });
        if (overshoot > 1) sideways.push(`${route.id}: +${overshoot}px`);
      }

      expect(
        sideways,
        `The page body scrolls horizontally:\n  ${sideways.join("\n  ")}`,
      ).toEqual([]);
    });
  });
}
