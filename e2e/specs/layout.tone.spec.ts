import { test, expect } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * TONE GATE — what a status looks like must not depend on what language it is
 * written in.
 *
 * Deriving a tone by matching English words against the label is unsafe: the
 * label is the translated string, so in Chinese nothing matches and every row
 * falls through to `idle` — the one tone with no fill — so six distinct states
 * render identically and the status column silently stops carrying
 * information. Language and theme are independent settings, so this failure
 * mode surfaces only when they diverge, and a test that varies just one of
 * them will not catch it.
 *
 * The assertion compares the two locales against each other rather than against
 * a fixed palette: the tones may be renamed or recoloured freely, they just
 * have to be the same on both sides.
 */

async function tonesFor(
  page: import("@playwright/test").Page,
  lng: string,
  path = "/agents",
) {
  await page.addInitScript((l) => {
    try {
      localStorage.setItem("i18nextLng", l);
    } catch {
      /* private mode — the detector falls back to the navigator language */
    }
  }, lng);
  await stubApi(page);
  await page.goto(path);
  await expect(
    path === "/agents"
      ? page.getByTestId("sessions-page")
      : page.getByTestId("console-table").first(),
  ).toBeVisible({ timeout: 15_000 });
  await settle(page);
  return page.evaluate(() =>
    [...document.querySelectorAll('[data-testid="status-pill"]')].map((el) => ({
      state: el.getAttribute("data-state"),
      tone: el.getAttribute("data-tone"),
    })),
  );
}

for (const surface of [
  { path: "/agents", name: "the app shell" },
  // The console carried a second copy of the pill that had lost these
  // attributes entirely, so this side could not be asserted at all until the
  // two were merged.
  { path: "/manage/sessions", name: "the console" },
]) {
test(`a status keeps its tone when the language changes — ${surface.name}`, async ({ browser }) => {
  const en = await browser.newContext({ locale: "en-US" });
  const zh = await browser.newContext({ locale: "zh-CN" });
  try {
    const [a, b] = await Promise.all([
      tonesFor(await en.newPage(), "en", surface.path),
      tonesFor(await zh.newPage(), "zh-CN", surface.path),
    ]);

    expect(a.length, "no status pills rendered — the fixture must carry sessions").toBeGreaterThan(2);
    expect(b.length).toBe(a.length);

    // Per row, so a failure names the state that lost its tone.
    const drifted = a
      .map((row, i) => ({ state: row.state, en: row.tone, zh: b[i].tone }))
      .filter((r) => r.en !== r.zh)
      .map((r) => `${r.state}: ${r.en} in en, ${r.zh} in zh`);
    expect(drifted, `Tone depends on the display language:\n  ${drifted.join("\n  ")}`).toEqual([]);

    // A single tone across every row is the specific way this failed before:
    // everything collapsed onto `idle`, which reads as "no status at all".
    expect(
      new Set(b.map((r) => r.tone)).size,
      "every row shares one tone, so the status column carries no information",
    ).toBeGreaterThan(1);
  } finally {
    await en.close();
    await zh.close();
  }
});
}
