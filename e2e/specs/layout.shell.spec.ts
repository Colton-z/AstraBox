import { test, expect } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * SHELL GATE — the frame must not change when you cross between the two apps.
 *
 * The app and the console share one shell, because independently built shells
 * drift where a user notices: a rail at 256px on one side and 240px on the
 * other, and a 56px top bar existing on only one, shifting every piece of
 * chrome at once when crossing between them. Nothing about a rail's width is
 * a per-surface decision, so the assertion is simply that the two agree — it
 * names no pixel value and stays true if the shell is later redesigned.
 *
 * The second case covers the other half: a page's own chrome and its own
 * content must share a left edge. They did not at 1920, because the only
 * max-width in the app sat on an inner scroller instead of the page column, so
 * the tab strip started at x=280 and the cards it labelled started at x=512.
 * That one only showed up above 1456px, which is why it survived a suite pinned
 * to 1440.
 */

const geometry = (page: import("@playwright/test").Page) =>
  page.evaluate(() => {
    const box = (sel: string) => {
      const el = document.querySelector(sel);
      return el ? el.getBoundingClientRect() : null;
    };
    const gap = box('[data-slot="sidebar-gap"]');
    const bar = document.querySelector("header");
    const well = box('[data-slot="app-content"]');
    return {
      rail: gap ? Math.round(gap.width) : null,
      topbar: bar ? Math.round(bar.getBoundingClientRect().height) : null,
      wellLeft: well ? Math.round(well.left) : null,
    };
  });

test("the shell is the same frame on both sides of the app/console seam", async ({
  page,
}) => {
  await stubApi(page);

  await page.goto("/agents");
  await expect(page.getByTestId("sessions-page")).toBeVisible({ timeout: 15_000 });
  await settle(page);
  const app = await geometry(page);

  await page.goto("/manage/agents");
  await expect(page.getByTestId("console-table").first()).toBeVisible({ timeout: 15_000 });
  await settle(page);
  const console_ = await geometry(page);

  expect(app.rail, "the rail must not resize when crossing the seam").toBe(console_.rail);
  expect(app.topbar, "the top bar must not appear or vanish across the seam").toBe(console_.topbar);
  expect(app.wellLeft, "content must not shift horizontally across the seam").toBe(console_.wellLeft);
  // Guard against both sides being null — that would satisfy the equalities
  // above while proving nothing.
  expect(app.rail).toBeGreaterThan(0);
  expect(app.topbar).toBeGreaterThan(0);
});

test.describe("@1920", () => {
  test.use({ viewport: { width: 1920, height: 1080 } });

  test("a page's chrome and its content share one left edge on a wide display", async ({
    page,
  }) => {
    await stubApi(page);
    await page.goto("/agents");
    await expect(page.getByRole("tablist")).toBeVisible({ timeout: 15_000 });
    await settle(page);

    const edges = await page.evaluate(() => {
      const tabs = document.querySelector('[role="tablist"]');
      // The eyebrow above the first card is the content column's own left edge.
      const heading = document.querySelector("h2, h1, [data-slot='card']");
      return {
        tabs: tabs ? Math.round(tabs.getBoundingClientRect().left) : null,
        content: heading ? Math.round(heading.getBoundingClientRect().left) : null,
      };
    });

    expect(edges.tabs).not.toBeNull();
    expect(edges.content).not.toBeNull();
    // Capping the scroller instead of the column put these 232px apart.
    expect(Math.abs((edges.tabs ?? 0) - (edges.content ?? 0))).toBeLessThanOrEqual(1);
  });
});
