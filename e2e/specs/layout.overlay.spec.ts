import { test, expect } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * OVERLAY GATE — opening a secondary view must not take a control away.
 *
 * The session view has two incompatible ways of showing something alongside the
 * transcript. The right panel is a resizable pane: it takes width from its
 * sibling, and everything stays reachable. The subagent transcript is an
 * absolutely-positioned overlay that covers whatever is under it — the comment
 * on it says it is drawn that way specifically to avoid the resizable group.
 *
 * Without arbitration between the two, the overlay would land on top of the
 * composer: at 1440 the submit button keeps its box at (996, 844) — still
 * there, still enabled — while `document.elementFromPoint` at its centre
 * returns the drawer's scroll container, so a user could not send a message
 * while looking at a subagent, and nothing about the button would say so.
 *
 * It passes now. The drawer is positioned against the transcript rather than
 * the whole panel, so it covers the view it replaces and leaves the controls
 * below it alone.
 *
 * The assertion is hit-testing, not geometry — it does not care where the
 * drawer is or how wide it grows, only that the composer's control still
 * belongs to the composer.
 */
test("the composer stays usable while a subagent drawer is open", async ({ page }) => {
  await stubApi(page);
  // s-004 is READY in the fixture. BUSY sessions intentionally replace submit
  // with Stop, so they cannot prove that the submit control remains reachable.
  await page.goto("/sessions/s-004");
  await settle(page);
  await page.waitForTimeout(800);

  // The dispatch button lives in the tool card's collapsible body, which is the
  // real path a user takes to the drawer. The settled response folds that work
  // behind its own header, and the finished call behind a group inside it, so
  // both are opened first — the same two presses the reader makes.
  const fold = page.getByTestId("assistant-turn-process-trigger");
  await expect(fold).toBeVisible({ timeout: 15_000 });
  await fold.click();
  await expect(fold).toHaveAttribute("aria-expanded", "true");
  const group = page.getByTestId("assistant-process-trigger");
  await expect(group).toBeVisible({ timeout: 15_000 });
  await group.click();
  await expect(group).toHaveAttribute("aria-expanded", "true");

  const card = page.getByRole("button", { name: /^Task/ }).first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await card.click();
  const open = page.getByRole("button", { name: /Open in Agents/i });
  await expect(open).toBeVisible({ timeout: 10_000 });
  await open.click();
  await page.waitForTimeout(800);

  const verdict = await page.evaluate(() => {
    const submit = document.querySelector<HTMLElement>('form button[type="submit"]');
    if (!submit) return { found: false as const };
    const r = submit.getBoundingClientRect();
    const hit = document.elementFromPoint(
      Math.round(r.x + r.width / 2),
      Math.round(r.y + r.height / 2),
    );
    return {
      found: true as const,
      reachable: !!hit && (hit === submit || submit.contains(hit) || hit.contains(submit)),
      covering: hit ? `${hit.tagName}.${String(hit.className).slice(0, 60)}` : null,
    };
  });

  expect(verdict.found, "no composer submit button on the page").toBe(true);
  expect(
    verdict.found && verdict.reachable,
    `The composer's submit button is covered by ${verdict.found ? verdict.covering : "?"}`,
  ).toBe(true);
});
