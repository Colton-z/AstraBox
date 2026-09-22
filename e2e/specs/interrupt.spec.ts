import { test, expect } from "@playwright/test";
import { openSessions, createRunViaComposer, tid } from "./helpers";

/**
 * INTERRUPT SPEC — start a turn, then stop it mid-flight.
 *
 * This spec kicks off a deliberately long-running turn (count slowly to 50) so there
 * is a real streaming window to interrupt. While the turn is busy the composer swaps
 * its send control for a Stop button (data-testid="run-composer-stop"); clicking it
 * aborts the local reader and calls POST /api/v1/sessions/{id}/interrupt. The
 * assertions confirm the run leaves its running state: the streaming assistant
 * marker clears, the run-header pill leaves PROCESSING, and the Stop control
 * disappears.
 */

const LONG_PROMPT =
  "Count from 1 to 50, one number per line, output them step by step, don't skip any, and don't summarize.";

test("interrupt: a streaming turn can be stopped", async ({ page }) => {
  // The InterruptTurn worker force-settles a zero-frame turn with a terminal
  // turn.failed, so an interrupt landing before the first token does not
  // hang the UI at "Stopping…". A turn that already produced frames still
  // settles through the bridge (which may carry partial content). This spec
  // asserts the run leaves its streaming state either way.
  await openSessions(page);
  await createRunViaComposer(page, { prompt: LONG_PROMPT });

  // Wait until the turn is actually live: the composer Stop control is only
  // rendered while busy (status submitted|streaming).
  const stopButton = tid(page, "run-composer-stop");
  await expect(stopButton).toBeVisible({ timeout: 90_000 });

  await stopButton.click();

  // After interrupting, the run must leave its running state: no streaming
  // assistant marker remains...
  await expect(
    page.locator('[data-testid="assistant-message"][data-streaming="true"]'),
  ).toHaveCount(0, { timeout: 60_000 });

  // ...the run-header pill leaves PROCESSING...
  const headerPill = tid(page, "run-view").getByTestId("status-pill").first();
  await expect
    .poll(async () => headerPill.getAttribute("data-state"), {
      message: "expected the run header pill to leave PROCESSING after interrupt",
      timeout: 60_000,
    })
    .not.toBe("PROCESSING");

  // ...and the composer Stop control is gone once the turn stops being busy.
  await expect(tid(page, "run-composer-stop")).toHaveCount(0, { timeout: 30_000 });
});
