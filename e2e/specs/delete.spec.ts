import { test, expect } from "@playwright/test";
import { openSessions, createRunViaComposer, tid } from "./helpers";

/**
 * Session archive behavior.
 *
 * The real console exposes NO per-session Delete control in the UI. The user-facing
 * "remove a session" affordance is the sidebar row's "Archive" button, which calls
 * POST /api/v1/sessions/{id}/archive. The DELETE /api/v1/sessions/{id} endpoint is
 * covered by a separate API-level test.
 *
 * The test creates a session with NO first prompt (no turn to provision then throw
 * away), archives it from its sidebar row, asserts the POST returns 200, and asserts
 * the archived row leaves the default sidebar list.
 */

test("archive: a session can be archived from the sidebar", async ({ page }) => {
  await openSessions(page);

  // Create with an empty prompt — archiving needs no live turn, and skipping the
  // prompt avoids provisioning a turn only to discard it immediately. This lands on
  // the RunView (/sessions/:id); the new session appears in the always-mounted sidebar.
  const sessionId = await createRunViaComposer(page, { prompt: "" });

  // The sidebar row for the new session must be present before it can be archived.
  const row = page.locator(
    `[data-testid="session-row"][data-session-id="${sessionId}"]`,
  );
  await expect(row).toBeVisible({ timeout: 30_000 });

  // Click the row's "Archive" action and wait for the actual POST (through the Vite
  // /api proxy) to resolve OK.
  const [archiveResp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new RegExp(`/api/v1/sessions/${sessionId}/archive(?:\\?|$)`).test(r.url()),
      { timeout: 30_000 },
    ),
    row.getByRole("button", { name: "Archive" }).click(),
  ]);
  expect(
    archiveResp.status(),
    `archive returned ${archiveResp.status()} for session ${sessionId}`,
  ).toBe(200);

  // Archiving navigates back to the sessions surface and drops the row from the
  // default list (archiveSession filters the archived session out, then revalidates).
  await expect(tid(page, "sessions-page")).toBeVisible();
  await expect(row).toHaveCount(0, { timeout: 30_000 });

  // Sanity: reopening the sessions surface does not resurrect the archived row.
  await openSessions(page);
  await expect(
    page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`),
  ).toHaveCount(0);
});
