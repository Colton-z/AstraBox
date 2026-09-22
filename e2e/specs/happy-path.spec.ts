import { test, expect } from "@playwright/test";
import {
  openSessions,
  createRunViaComposer,
  tid,
  EMPTY_REPLY_SIGNATURE,
} from "./helpers";

/**
 * HEADLINE SPEC — the real end-to-end user flow on the redesigned INK console,
 * proving the live agent turn is GREEN through the browser (not just curl):
 *
 *   (1) open the app → the sessions surface mounts (the sidebar list or the
 *       first-run empty hero);
 *   (2) on /agents, click a template card's "Start session" → land on the RunView
 *       → once the sandbox is READY, type the 1+1 prompt in the composer
 *       → submit;
 *   (3) the assistant message streams → ASSERT the rendered text contains "2" AND
 *       is NOT the literal "(empty reply, exit=0)";
 *   (4) ASSERT the StatusPill goes running (astra + pulse) → completed/idle (mint).
 *
 * The prompt pins a deterministic one-token answer ("2") so the assertion is exact.
 * The turn streams real frames (start → reasoning-delta* → text-delta "2" → finish),
 * the same chain verified at the SSE layer; this spec asserts the rendered DOM instead.
 */

const PROMPT = "Answer in one word: what is 1 plus 1? Reply with the number only.";

test("happy path: create a run, stream a live turn, assistant answers '2'", async ({
  page,
}) => {
  await openSessions(page);
  // Either the dense table or the first-run hero is present — both are valid.
  const haveTable = await tid(page, "sessions-table")
    .isVisible()
    .catch(() => false);
  const haveHero = await tid(page, "sessions-empty-hero")
    .isVisible()
    .catch(() => false);
  expect(haveTable || haveHero).toBeTruthy();

  // ── (2) Start session on /agents → land on RunView → type prompt → submit ─
  const sessionId = await createRunViaComposer(page, { prompt: PROMPT });
  expect(sessionId).toMatch(/^[0-9a-f-]{36}$/i);

  // The optimistic user bubble carries the prompt forward into the run. This
  // waits through CREATING (cold container provision + SDK connect) until the
  // sandbox is READY and RunView fires the carried-forward first turn — a cold
  // warm-pool can make that ~60s, so allow generous headroom.
  await expect(tid(page, "user-message").filter({ hasText: "1 plus 1" })).toBeVisible({
    timeout: 150_000,
  });

  // ── (4a) running: the StatusPill (astra + pulse) OR the streaming assistant
  //         header is observed while the turn is live. The assertion accepts either
  //         running signal — robust to a fast turn settling before a single DOM
  //         poll could catch the header pill alone.
  const headerPill = tid(page, "run-view").getByTestId("status-pill").first();
  const runningPill = page.locator(
    '[data-testid="run-view"] [data-testid="status-pill"][data-state="PROCESSING"][data-tone="astra"][data-pulse="true"]',
  );
  const streamingAssistant = page.locator(
    '[data-testid="assistant-message"][data-streaming="true"]',
  );
  await expect
    .poll(
      async () =>
        (await runningPill.count()) > 0 || (await streamingAssistant.count()) > 0,
      {
        message:
          "expected the turn to enter a running state (PROCESSING astra-pulse pill or streaming assistant)",
        timeout: 90_000,
      },
    )
    .toBeTruthy();

  // ── (3) the assistant text streams; ASSERT it contains "2" and is NOT the
  //         empty-reply fake-success signature. The text lands in the
  //         assistant-text part (MessageBlocks TextChunk).
  const assistantText = tid(page, "assistant-text").last();
  await expect(assistantText).toBeVisible({ timeout: 180_000 });
  await expect(assistantText).toContainText("2", { timeout: 180_000 });

  const rendered = (await assistantText.innerText()).trim();
  expect(rendered).not.toBe("");
  expect(rendered).toContain("2");

  // HARD RULE: no fake success. The empty-reply signature must appear NOWHERE on
  // the page (not in the assistant text, not as a placeholder anywhere).
  await expect(page.locator("body")).not.toContainText(EMPTY_REPLY_SIGNATURE);

  // ── (4b) completed/idle: the run header StatusPill settles to mint (READY) once
  //         the turn finishes. READY = the contract's idle-ok/completed tone (there
  //         is no literal "completed" state; mint READY is it).
  await expect
    .poll(async () => headerPill.getAttribute("data-tone"), {
      message: "expected the run-header pill to settle to mint (READY/completed)",
      timeout: 120_000,
    })
    .toBe("mint");
  await expect(headerPill).toHaveAttribute("data-state", "READY");

  // The assistant message must not carry the streaming marker once the turn settles.
  await expect(
    page.locator('[data-testid="assistant-message"][data-streaming="true"]'),
  ).toHaveCount(0);
});
