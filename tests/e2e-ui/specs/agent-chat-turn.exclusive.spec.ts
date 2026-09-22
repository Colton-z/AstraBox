/**
 * E2E: agent chat turn — the user-visible lifecycle of one message, asserted
 * from the browser (what the user experiences), not the API frames.
 *
 * This pins the "reply finished but the header kept showing generating for
 * 30s" failure (MCP-bridge black hole): after the assistant message text is
 * rendered, the status pill must settle (data-pulse=false) within
 * SETTLE_BUDGET_MS. An API-level spec cannot catch that class of bug — the
 * frames all arrive; it is the tail latency that is broken.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Generous vs the healthy ~1.5s tail, far below the 30s failure mode.
const SETTLE_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_SETTLE_BUDGET_MS', 10_000);

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('one turn: send → pulse on → reply renders → pulse settles fast', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);

    // Record what the page's own stream carried. This assertion has failed
    // intermittently with the message stuck in the composer's queue, and the
    // server side is exhausted as evidence: the consumption frame is written
    // within two seconds of the click, carries the client id the queue row is
    // keyed by, and is replayed like every other frame. What is not visible
    // from there is whether the browser received it, and a Playwright trace
    // does not keep streaming bodies. Installed before the first navigation:
    // the stream opens during the page's own bootstrap.
    await page.addInitScript(() => {
      const seen: string[] = [];
      (window as unknown as { __astraFrames: string[] }).__astraFrames = seen;
      const original = window.fetch;
      window.fetch = async (...args: Parameters<typeof fetch>) => {
        const response = await original(...args);
        const url = String(typeof args[0] === 'string' ? args[0] : (args[0] as Request).url);
        if (!url.includes('/ai-stream') || !response.body) return response;
        const [mine, theirs] = response.body.tee();
        void (async () => {
          const reader = mine.getReader();
          const decoder = new TextDecoder();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            for (const line of decoder.decode(value, { stream: true }).split('\n')) {
              const match = /"type"\s*:\s*"([^"]+)"/.exec(line);
              if (match) seen.push(`${new Date().toISOString().slice(11, 23)} ${match[1]}`);
            }
          }
        })();
        return new Response(theirs, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      };
    });

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
    await expect(pill).toBeVisible();

    // Send through the real composer (textarea + submit), like a user.
    const composer = page.locator('textarea');
    const prompt = 'What is 2+2? Reply with just the number.';
    await composer.fill(prompt);
    await page.locator('button[type="submit"]').first().click();

    // The accepted input is immediately visible. It remains in the composer
    // queue until the durable transcript carries the same message, then hands
    // over to the user bubble; both are valid surfaces for this first check.
    const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: prompt });
    const userMessage = page.getByTestId('user-message').filter({ hasText: prompt }).last();
    await expect(
      queuedPrompt.or(userMessage).first(),
      'the sent message is visible in the queue or transcript',
    ).toBeVisible({ timeout: 15_000 }).catch(async (failure: unknown) => {
      const frames = await page.evaluate(
        () => (window as unknown as { __astraFrames?: string[] }).__astraFrames ?? [],
      );
      throw new Error(
        `${String(failure)}\n--- frames this page received (${frames.length}) ---\n`
          + frames.join('\n'),
      );
    });

    // The header goes live while the turn runs — UNLESS the turn outruns the
    // poll. With the WAL fixed a trivial turn can settle between two UI polls
    // (measured: the pill went CREATING → READY with no observable running
    // window), and "live feedback while running" is then unobservable, not
    // violated. Race the pulse against the reply: whichever is seen first
    // proves the turn ran; the settle-budget assertion below keeps its teeth
    // either way.
    const reply = page.getByTestId('assistant-message').last();
    // .first(): once the turn settles BOTH sides of the race can match at
    // once, and strict mode rejects a two-element resolution.
    await expect(
      pill.and(page.locator('[data-pulse="true"]')).or(reply.filter({ hasText: '4' })).first(),
    ).toBeVisible({ timeout: 120_000 });

    // The assistant reply renders.
    await expect(reply).toContainText('4', { timeout: 120_000 });

    // Settlement includes durable transcript projection, not only an engine
    // reply. The queue may cover that latency, but it cannot replace the final
    // user message record.
    await expect(userMessage).toContainText(prompt, { timeout: 120_000 });

    // The settle-budget assertion: once the reply text is on screen, the header
    // must settle within budget — no phantom "generating" tail.
    const settleStarted = Date.now();
    await expect(pill).toHaveAttribute('data-pulse', 'false', { timeout: SETTLE_BUDGET_MS });
    const settleMs = Date.now() - settleStarted;
    test.info().annotations.push({ type: 'settle-ms', description: String(settleMs) });

    // And the composer is sendable again (no stuck busy state). An EMPTY
    // composer keeps submit disabled by design — type first, then assert.
    await composer.fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 10_000 });
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
