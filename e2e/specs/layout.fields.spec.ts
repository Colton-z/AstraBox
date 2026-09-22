import { test, expect } from "@playwright/test";
import { settle, stubApi } from "./layoutHelpers";

/**
 * FIELD-NAMING GATE — every control on a form has a name a machine can find.
 *
 * A label's `for` must resolve to an id at the label's own level. Minting the
 * id one level below, inside the control, while the label sits on the row
 * above leaves `htmlFor` pointing at nothing: clicking the label focuses
 * nothing, and a screen reader reads the control without its name.
 *
 * There are two correct shapes here, and the second is why a naive fix leaves
 * four fields behind. A single input is named by `<label for>`. A list field is
 * several inputs plus an add button — and none at all when the list is empty —
 * so there is no element for `for` to point at; a set is named by
 * `role="group"` with `aria-labelledby`. Pointing `for` at one of the inputs
 * would satisfy a count and still leave the field unnamed the moment it is
 * empty.
 *
 * Three things are asserted, and the third is the one a count cannot stand in
 * for: nothing points at a missing element, both shapes are in use, and no
 * control is left unnamed by EITHER. The first two only inspect the naming that
 * exists — a field rendered with no label and no group at all satisfies both,
 * so the third is the only one that catches a control carrying neither. Which
 * shape fits any one field stays a property of the control.
 *
 * The subject is the Agent create page, because that is where an unnamed
 * control reaches a reader first: a record page names a record that exists, and
 * this form is the one place every control is empty and the label is all there
 * is to go on. Its fixture schema carries a list field on purpose, so both
 * shapes are on the page at once (layoutHelpers.ts).
 */
test("every field on a create page is named, by a label or as a group", async ({ page }) => {
  await stubApi(page);
  await page.goto("/manage/agents/new");
  await settle(page);
  // The page says which page it is; asking the DOM before it has rendered its
  // form would report "nothing unnamed" about nothing at all.
  await expect(page.locator('[data-slot="create-page"]')).toBeAttached();
  await expect(page.getByRole("textbox", { name: "Name", exact: true })).toBeVisible();

  const naming = await page.evaluate(() => {
    // The form's own controls, not the shell's around it: the sidebar and the
    // page header are named by their own rules and are not fields.
    const form = document.querySelector('[data-slot="create-page"]');
    if (!form) throw new Error("the create page rendered no fields to name");

    const labels = [...form.querySelectorAll("label[for]")];
    const dangling = labels
      .filter((l) => !document.getElementById(l.getAttribute("for") || ""))
      .map((l) => (l.textContent || "").trim().slice(0, 30));

    const groups = [...form.querySelectorAll('[role="group"][aria-labelledby]')];
    const unnamedGroups = groups
      .filter((g) => !document.getElementById(g.getAttribute("aria-labelledby") || ""))
      .map((g) => g.getAttribute("aria-labelledby") || "?");

    // Every control the form offers, whichever element it is built from — the
    // console's select is native, its switch and its searchable select are
    // buttons carrying a role.
    const unnamed = [
      ...form.querySelectorAll(
        'input, textarea, select, [role="switch"], [role="combobox"], [role="textbox"]',
      ),
    ]
      .filter((el) => {
        if (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")) return false;
        if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) return false;
        // A row of a list field is named by the set it belongs to.
        return !el.closest('[role="group"][aria-labelledby]');
      })
      .map((el) => `${el.tagName.toLowerCase()}#${el.id || "(no id)"}`);

    return { labels: labels.length, dangling, groups: groups.length, unnamedGroups, unnamed };
  });

  // Two, not a count copied from the real schema: the stub form is deliberately
  // small, and pinning the number here would make this fail whenever a field is
  // added to or removed from the fixture rather than when naming breaks.
  expect(naming.labels, "the form rendered no labelled controls at all").toBeGreaterThan(1);
  expect(
    naming.dangling,
    `labels point at controls that do not exist:\n  ${naming.dangling.join("\n  ")}`,
  ).toEqual([]);
  expect(
    naming.unnamedGroups,
    `groups reference naming text that does not exist:\n  ${naming.unnamedGroups.join("\n  ")}`,
  ).toEqual([]);
  // The list fields are the reason the group mechanism exists; losing them
  // would mean someone pointed `for` at an input and called it fixed.
  expect(naming.groups, "no list field is named as a group").toBeGreaterThan(0);
  expect(
    naming.unnamed,
    `controls carry no name by either mechanism:\n  ${naming.unnamed.join("\n  ")}`,
  ).toEqual([]);
});
