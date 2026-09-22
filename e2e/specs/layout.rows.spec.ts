import { test, expect } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * ROW GATE — a row may clip its content, but not lose it silently, and not
 * behave differently depending on what the data happens to say.
 *
 * A row of fixed and flexible parts does not shrink correctly by default: a
 * flex or grid child's `min-width` is `auto`, so it refuses to go below its own
 * content and pushes the excess onto an ancestor — which cuts it with no
 * ellipsis, because the ellipsis is drawn by whoever clips.
 *
 * At 1440px, an unconstrained sidebar row whose badge reads "Running in
 * background" can push its track to 227px inside a 159px button, painting the
 * title's ellipsis 68px outside the visible area, while the same row with a
 * "Working" badge fits and truncates properly. On cards, an unconstrained text
 * block can push the row 366px past the edge and take the state badge with it.
 *
 * Both assertions are containment, not measurement: no pixel value is pinned,
 * so the layout may change freely and these only fail when something is again
 * placed where it cannot be seen.
 */

test("sidebar rows behave the same whatever the status says", async ({ page }) => {
  await stubApi(page);
  await page.goto("/agents");
  await expect(page.getByTestId("sessions-page")).toBeVisible({ timeout: 15_000 });
  await settle(page);

  const overflowing = await page.evaluate(() => {
    const bad: string[] = [];
    for (const row of document.querySelectorAll('[data-testid="session-row"]')) {
      const button = row.querySelector("a,button");
      if (!button) continue;
      const track = [...button.querySelectorAll(":scope > span")].find((s) =>
        getComputedStyle(s).display.includes("grid"),
      );
      if (!track) continue;
      const label = row.querySelector('[data-testid="status-pill"]')?.textContent?.trim() ?? "?";
      if (track.scrollWidth > track.clientWidth + 1) {
        bad.push(`"${label}": track ${track.scrollWidth}px inside ${track.clientWidth}px`);
      }
    }
    return bad;
  });

  // The fixture deliberately includes the longest status label the pill can
  // carry; if only some rows overflow, the row's behaviour depends on its data.
  expect(
    overflowing,
    `Rows overflow their track, so their ellipsis lands outside the visible area:\n  ${overflowing.join("\n  ")}`,
  ).toEqual([]);
});

for (const surface of [
  { path: "/agents", name: "agent cards" },
  { path: "/assistants", name: "assistant cards" },
]) {
  test(`${surface.name} keep their badge inside the card under long text`, async ({ page }) => {
    await stubApi(page, { stress: true });
    await page.goto(surface.path);
    await settle(page);
    await page.waitForTimeout(600);

    const escaped = await page.evaluate(() => {
      const bad: string[] = [];
      for (const card of document.querySelectorAll('[data-slot="card"]')) {
        const box = card.getBoundingClientRect();
        if (box.width === 0) continue;
        for (const part of card.querySelectorAll('[data-slot="badge"], [data-slot="card-title"]')) {
          const r = part.getBoundingClientRect();
          if (r.width === 0) continue;
          if (r.right > box.right + 1) {
            bad.push(
              `${part.getAttribute("data-slot")} reaches ${Math.round(r.right)} past a card ending at ${Math.round(box.right)}`,
            );
          }
        }
      }
      return bad;
    });

    expect(
      escaped,
      `Content is pushed outside its card, where it is cut or lost:\n  ${escaped.join("\n  ")}`,
    ).toEqual([]);
  });
}
