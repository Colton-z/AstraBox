import { expect, test, type Route } from "@playwright/test";

import { settle, stubApi } from "./layoutHelpers";

/**
 * CREATE GATE — a create hands back the record it made.
 *
 * A new record is authored on its own page (`/manage/<section>/new`), in the
 * same fields and the same sections the page that reads it will use, because
 * creating and editing are one act against one shape (docs/frontend-design.md
 * §4). That leaves one step this gate is about: where the reader is standing
 * when the write returns. Back on the list they have to find the record again,
 * and there is no id in front of them to find it by; still on the form they
 * cannot tell a save that happened from one that did not.
 *
 * Both records are asserted the same way — the URL is the new record's, and the
 * heading is the record's OWN name rather than the form's title, which is what
 * separates "the router moved" from "the record page loaded the record". The
 * two writes are not the same shape underneath, which is why both are here: an
 * Agent is a POST the server names (`agent_id`), an Environment is a PUT to a
 * key the author chose, and only one of them can be got right by accident.
 *
 * `/api` is stubbed (layoutHelpers.ts), so what is asserted is the console's
 * own navigation — no backend, no sandbox, no model spend.
 */

async function fulfill(route: Route, data: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ code: "OK", message: "ok", data }),
  });
}

test.describe("management create flows", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a newly created Agent opens its record page", async ({ page }) => {
    const created = {
      agent_id: "ag-created",
      name: "created-agent",
      display_meta: { display_name: "Created Agent" },
      description: "Created in the browser",
      model: "claude-opus-5",
      environment_name: "environment-1",
      enabled: true,
      version: 1,
    };
    let saved = false;

    await stubApi(page);
    await page.route("**/api/v1/agents", async (route) => {
      if (route.request().method() === "POST") {
        saved = true;
        await fulfill(route, created);
      } else if (saved) {
        // The record page reads the list and finds itself in it. Before the
        // write, the same call is the console's own agent count — hence the
        // fallback to the fixture rather than a second empty answer.
        await fulfill(route, [created]);
      } else {
        await route.fallback();
      }
    });

    await page.goto("/manage/agents/new", { waitUntil: "domcontentloaded" });
    await settle(page);
    // Bounded, and before anything is typed: an action waits out the whole test
    // budget for a control that never arrives, so let a broken form fail here
    // with the page's own name in the message.
    await expect(page.getByRole("heading", { name: "New agent", exact: true })).toBeVisible();

    const name = page.getByRole("textbox", { name: "Name", exact: true });
    await expect(name).toBeVisible();
    await name.fill("created-agent");
    // Environment before model, in that order: the models on offer are the ones
    // the selected Environment's gateway advertises, so the picker has nothing
    // in it until an Environment is chosen.
    await page
      .getByRole("combobox", { name: "Runtime environment", exact: true })
      .selectOption("environment-1");
    await page.getByRole("combobox", { name: "Model", exact: true }).click();
    await page.getByRole("option", { name: "claude-opus-5", exact: true }).click();
    await page.getByRole("button", { name: "Create", exact: true }).click();

    await expect(page).toHaveURL(/\/manage\/agents\/ag-created$/);
    await expect(page.getByRole("heading", { name: "Created Agent", exact: true })).toBeVisible();
  });

  test("a newly created Environment opens its record page", async ({ page }) => {
    const created = {
      name: "created-environment",
      display_name: "Created Environment",
      description: "Created in the browser",
      engine_kind: "claude_code",
      sandbox_backend: "open_sandbox",
      enabled: true,
    };
    let saved = false;

    await stubApi(page);
    await page.route("**/api/v1/admin/environments", async (route) => {
      if (saved) await fulfill(route, [created]);
      else await route.fallback();
    });
    // The name IS the key, so the write is a PUT at the record's own URL.
    await page.route("**/api/v1/admin/environments/created-environment", async (route) => {
      saved = true;
      await fulfill(route, created);
    });

    await page.goto("/manage/environments/new", { waitUntil: "domcontentloaded" });
    await settle(page);
    await expect(
      page.getByRole("heading", { name: "New environment", exact: true }),
    ).toBeVisible();

    const name = page.getByRole("textbox", { name: "Name", exact: true });
    await expect(name).toBeVisible();
    await name.fill("created-environment");
    await page.getByRole("button", { name: "Create", exact: true }).click();

    await expect(page).toHaveURL(/\/manage\/environments\/created-environment$/);
    await expect(
      page.getByRole("heading", { name: "Created Environment", exact: true }),
    ).toBeVisible();
  });
});
