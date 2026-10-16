import { test, expect } from "@playwright/test";
import { openSessions, createRunViaComposer, tid } from "./helpers";

/**
 * INTERRUPT SPEC — start a response, then stop it mid-flight.
 *
 * This spec asks for a deliberately long response (count slowly to 50) so there
 * is a real streaming window to interrupt. While the response runs the composer
 * swaps its send control for a Stop button (data-testid="run-composer-stop");
 * clicking it calls POST /api/v1/sessions/{id}/interrupt, which the session
 * accepts while a response runs. The assertions confirm the run leaves its
 * running state: the streaming assistant marker clears, the run-header pill
 * leaves PROCESSING, the Stop control disappears, and the session records the
 * response as cancelled.
 */

const LONG_PROMPT =
  "Count from 1 to 50, one number per line, output them step by step, don't skip any, and don't summarize.";

test("interrupt: a streaming turn can be stopped", async ({ page, request }) => {
  await openSessions(page);
  const sessionId = await createRunViaComposer(page, { prompt: LONG_PROMPT });

  // Wait until the response is actually live: the composer Stop control is
  // only rendered while the session works.
  const stopButton = tid(page, "run-composer-stop");
  await expect(stopButton).toBeVisible({ timeout: 90_000 });

  const interrupted = page.waitForResponse((candidate) =>
    candidate.request().method() === "POST"
    && new URL(candidate.url()).pathname === `/api/v1/sessions/${sessionId}/interrupt`,
  );
  await stopButton.click();
  const interruptResponse = await interrupted;
  expect(interruptResponse.status(), "the session must take the interrupt").toBe(200);
  expect(
    ((await interruptResponse.json()) as { data?: { status?: string } }).data?.status,
    "a running response is interrupted, not found idle",
  ).toBe("accepted");

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

  // ...the composer Stop control is gone once the session stops working...
  await expect(tid(page, "run-composer-stop")).toHaveCount(0, { timeout: 30_000 });

  // ...and the session records the response it stopped as cancelled.
  await expect.poll(async () => {
    const response = await request.get(`/api/v1/sessions/${sessionId}`);
    const detail = ((await response.json()) as {
      data?: { last_response?: { status?: string; outcome?: string } | null };
    }).data;
    return detail?.last_response ?? null;
  }, {
    message: "the stopped response must end as cancelled",
    timeout: 30_000,
  }).toMatchObject({ status: "ended", outcome: "cancelled" });
});
