/**
 * E2E: agent-chat conversations start in bypassPermissions and retain that mode
 * when a turn omits permission_mode.
 *
 * The seeded default agent is sufficient because the conversation route, not
 * agent configuration, owns this default. The page verifies the user-visible
 * behavior: the permission badge remains in bypass mode and a Write request
 * never opens an approval panel. API assertions cover the exact enum value, the
 * mode-omitting turn that the UI cannot send, and file bytes that the console
 * does not render.
 *
 * Both the mode-omitting turn and the browser turn must create their requested
 * files. Missing files and failed downloads fail the test.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// Both existing turns remain inside the suite's three-minute test budget.
const STICKY_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_DEFAULT_BYPASS_STICKY_TURN_MS', 180_000);
// The user's Write turn on the page: tool round-trip plus render.
const PAGE_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_DEFAULT_BYPASS_PAGE_TURN_MS', 240_000);
// How long the header may keep pulsing after the reply is on screen.
const PAGE_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_DEFAULT_BYPASS_PAGE_SETTLE_MS', 60_000);

// The engine enum is the protocol identity; the tone class is presentation only.
// Keep the label check too so a correctly identified badge cannot show the user a
// contradictory description.
const BYPASS_LABEL = /Skip confirmations|跳过确认/;

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('agent_chat default permission mode is bypassPermissions', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  const pendingPanel = page.getByTestId('pending-interaction-panel');

  /**
   * Send from the composer and wait for one more rendered assistant bubble —
   * while re-checking, on every tick, for the approval card bypass must never
   * raise. Both in one loop is what makes a raised prompt fail in seconds: a turn
   * parked on a permission emits nothing further, so a reply-only wait would sit
   * to the full budget and report the wrong thing.
   */
  const sendAndReadReply = async (prompt: string, echo: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
    // .fill() sets the value without key events, so a multi-line prompt is not
    // submitted early by an Enter newline.
    await composer.fill(prompt);
    await page.getByTestId('composer-submit').click();
    await expect(page.getByTestId('user-message').last()).toContainText(echo, { timeout: 30_000 });

    const deadline = Date.now() + budgetMs;
    let replied = false;
    while (Date.now() < deadline) {
      expect(
        await pendingPanel.count(),
        'bypassPermissions must run the Write without ever stopping the user for approval',
      ).toBe(0);
      // Count, not text: the model's wording is its own business, and a spec that
      // pins wording fails on a model that is behaving correctly.
      if ((await page.getByTestId('assistant-message').count()) > before) {
        replied = true;
        break;
      }
      await page.waitForTimeout(1_000);
    }
    expect(replied, `no assistant reply rendered within ${budgetMs}ms`).toBe(true);

    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a reply: a failed turn renders its
    // error INTO the transcript as an assistant message, so "one more non-empty
    // bubble" is satisfied by exactly the outcome under test.
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    return reply;
  };

  try {
    // ── Default at creation: the conversation opens in bypassPermissions. ─────
    // The exact enum has no pixel (see the header): the badge is localized and
    // untestidded, so the create-route default is read off the projection here and
    // confirmed as the user sees it on the page below.
    const ready = await api.waitForSessionReady(sessionId);
    expect(
      ready.permission_mode,
      'agent_chat conversation should start in bypassPermissions',
    ).toBe('bypassPermissions');

    // ── Sticky across a turn that OMITS permission_mode. ──────────────────────
    // Out-of-band on purpose: the console attaches the current mode to every send
    // (useSessionChat: permission_mode: permissionModeRef.current), so no user
    // action reaches the `desired = requested or previous` fallback. Driving it
    // here, BEFORE the user's turn, is what makes the page assertions that follow
    // mean "the mode the mode-less turn left behind".
    const assistantsBefore = await api.assistantCount(sessionId);
    const stickyPath = `e2e-agent-default-bypass-sticky-${runId}.txt`;
    const targetContent = 'agent default bypass e2e';
    await api.streamPrompt(
      sessionId,
      `E2E bypass sticky ${runId}: 请使用 Write 工具创建相对路径 ${stickyPath}，内容必须精确为 ${targetContent}。只创建这一个文件。`,
    );
    // streamPrompt's SSE body can close on a segment finish rather than the turn's
    // end, so settle on the durable transcript — and a settled turn is also the
    // precondition for typing into the composer next.
    await api.waitForAssistantMessageCount(sessionId, assistantsBefore, STICKY_TURN_MS);
    const sticky = await api.waitForSessionReady(sessionId);
    expect(
      sticky.permission_mode,
      'agent_chat should stay in bypassPermissions when a turn omits permission_mode',
    ).toBe('bypassPermissions');
    expect(
      sticky.pending_interaction,
      'the Write turn that omits permission_mode must not require approval',
    ).toBeFalsy();
    await expect(
      api.downloadFileText(sessionId, stickyPath),
      'the same mode-omitting Write turn must create the exact requested file',
    ).resolves.toBe(targetContent);

    // ── The user arrives: the console tells them the conversation skips
    //    confirmations. Loaded AFTER the mode-less turn, so this reads the
    //    server's mode, not a client copy from before it. ─────────────────────
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    const bypassBadge = page.getByTestId('permission-mode-badge');
    await expect(
      bypassBadge,
      'the composer should show the conversation running in bypass (skip confirmations)',
    ).toHaveAttribute('data-permission-mode', 'bypassPermissions', { timeout: 30_000 });
    await expect(bypassBadge).toHaveText(BYPASS_LABEL);

    // The mode-less turn's reply is already durable, so let history finish
    // rendering BEFORE any bubble counting: a count taken mid-hydration would be
    // grown by history arriving rather than by a new reply, and the send below
    // would "pass" on someone else's message.
    await expect(
      page.getByTestId('assistant-message').first(),
      'the earlier turn should render from the durable transcript',
    ).toBeVisible({ timeout: 45_000 });

    // ── The user asks for a file to be written, from the real composer. Under
    //    bypass this must complete without ever handing them an approval card —
    //    the card watch lives inside sendAndReadReply. ────────────────────────
    const targetPath = `e2e-agent-default-bypass-${runId}.txt`;
    const marker = `E2E agent default bypass ${runId}`;
    await sendAndReadReply(
      `${marker}: 请使用 Write 工具创建相对路径 ${targetPath}，内容必须精确为 ${targetContent}。只创建这一个文件。`,
      marker,
      PAGE_TURN_MS,
    );

    // The header settles: a turn that delivers text but keeps the screen live is
    // still broken, and a turn silently parked on a permission would never settle.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: PAGE_SETTLE_MS,
    });
    // The loop above stops watching at the first delta of the reply, and a tool
    // runs after that: re-check the tail of the turn, where a Write permission
    // would actually be raised.
    await expect(
      pendingPanel,
      'no approval card may appear in the tail of the turn either',
    ).toHaveCount(0);

    // The user is left with a composer, not an approval card — the same claim from
    // the other side, since the pending footer REPLACES the composer when an
    // interaction is raised (SessionPage renders one or the other). An EMPTY
    // composer keeps submit disabled by design — type first, then assert.
    await page.getByTestId('composer-prompt').fill('follow-up');
    await expect(page.getByTestId('composer-submit')).toBeEnabled({ timeout: 15_000 });

    // And the mode the user sees did not move under them.
    await expect(
      bypassBadge,
      'the conversation should still read as bypass after the turn',
    ).toHaveAttribute('data-permission-mode', 'bypassPermissions');

    // ── The persisted mode, and no interaction hiding behind a rendered page. ──
    // The badge can be a client copy; the projection is what the next turn will
    // dispatch under, and it is the only place the exact enum exists.
    const settled = await api.waitForSessionReady(sessionId);
    expect(
      settled.permission_mode,
      'agent_chat should stay in bypassPermissions after a turn it did not change the mode on',
    ).toBe('bypassPermissions');
    expect(
      settled.pending_interaction,
      'bypassPermissions should not raise a Write permission interaction',
    ).toBeFalsy();

    await expect(
      api.downloadFileText(sessionId, targetPath),
      'the browser Write under bypassPermissions must create the exact requested file',
    ).resolves.toBe(targetContent);
  } finally {
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
