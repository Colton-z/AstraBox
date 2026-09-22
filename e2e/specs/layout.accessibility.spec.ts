import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

import { settle, stubApi } from "./layoutHelpers";

test.use({ viewport: { width: 1440, height: 900 } });

test("management table rows show keyboard focus and open with Enter", async ({
  page,
}) => {
  await stubApi(page);
  await page.goto("/manage/agents", { waitUntil: "domcontentloaded" });
  await settle(page);

  const table = page.getByRole("table");
  const firstRecord = table.getByRole("row").nth(1);
  await firstRecord.focus();

  await expect(firstRecord).toBeFocused();
  await expect(firstRecord).toHaveAttribute("aria-keyshortcuts", "Enter Space");
  await expect(firstRecord).toHaveAttribute("aria-description", "Open record");

  const focusRing = await firstRecord.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(focusRing.style).toBe("solid");
  expect(focusRing.width).toBeGreaterThanOrEqual(2);

  await firstRecord.press("Enter");
  await expect(page).toHaveURL(/\/manage\/agents\/ag-001$/);
  await expect(
    page.getByRole("heading", { name: "Release Notes Writer 1", exact: true }),
  ).toBeVisible();
});

for (const theme of ["light", "dark"] as const) {
  for (const path of ["/agents", "/assistants", "/manage/agents", "/manage/errors"]) {
    test(`${path} (${theme}) passes automated WCAG A and AA checks`, async ({ page }) => {
      await stubApi(page);
      await page.addInitScript((value) => {
        window.localStorage.setItem("astrabox-theme", value);
      }, theme);
      await page.goto(path, { waitUntil: "domcontentloaded" });
      await settle(page);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      const violations = results.violations.map((violation) => ({
        help: violation.help,
        id: violation.id,
        impact: violation.impact,
        targets: violation.nodes.map((node) => node.target.join(" ")),
      }));

      expect(
        violations,
        `${path} (${theme}) has automatically detectable accessibility violations`,
      ).toEqual([]);
    });
  }
}
