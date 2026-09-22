/**
 * E2E: the first message after out-of-band sandbox loss transparently provisions
 * replacement compute.
 *
 * The test kills idle compute without clearing the session's sandbox pointer.
 * With no browser or user action, the real background lifecycle path must first
 * converge READY with no binding. The next composer send provisions a different
 * sandbox, resumes the same conversation, and renders an ordinary reply without
 * a retry or failure surface. Exact sandbox identity remains a database/API
 * assertion because the conversation page does not display it.
 */
import { execFileSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { lapseSessionSandboxLease, sessionDoc, sessionEvents, waitForTurnTerminalProof } from '../fixtures/dbOracle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Borrowing a fresh sandbox from the pool and resuming the Claude session is
// slow — give the whole two-turn + fault-injection flow a generous budget.
// Abrupt removal converges quickly; this just gates the next dispatch
// so it genuinely lands on dead compute (a kill that has not converged would let
// the turn succeed against the still-live box and no re-borrow would fire).
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
// The re-borrow turn (fresh sandbox + resume + reply) is the long pole.
const REBORROW_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);

function acceptedInputs(sessionId: string) {
  return sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && (row.payload as Record<string, unknown>).command_type === 'StartTurn');
}

function backgroundConvergence(sessionId: string) {
  return sessionEvents(sessionId).filter((row) => {
    if (row.event_type !== 'session.lifecycle_reconciled') return false;
    const payload = row.payload as Record<string, unknown>;
    const reason = String(payload.reason || '');
    return payload.state === 'READY' && payload.runtime_unavailable === true
      && (reason === 'sandbox_terminating_notice' || reason.startsWith('dead_binding_reconcile:')
        || reason.startsWith('sandbox_callback_'));
  });
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('out-of-band sandbox death re-borrows a fresh sandbox on the next message', async ({
  page,
  request,
}) => {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const scanInterval = Number(execFileSync('docker', [
    'exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS',
  ], { encoding: 'utf8', timeout: 10_000 }).trim());
  expect(scanInterval, 'the deployed testbed must configure a positive background scan interval').toBeGreaterThan(0);
  expect(scanInterval * 1_000, 'the real background scan must fit inside the unchanged death-convergence budget')
    .toBeLessThan(KILL_CONVERGE_MS / 2);
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_oob_reborrow_${Date.now()}_${test.info().workerIndex}`,
  );
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  /** Send and wait through the visible queue-to-transcript handover. */
  const sendAndReadReply = async (prompt: string, budgetMs: number) => {
    const before = await page.getByTestId('assistant-message').count();
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    const queuedPrompt = page.getByTestId('composer-queue').filter({ hasText: prompt });
    const userMessage = page.getByTestId('user-message').filter({ hasText: prompt });
    await expect(
      queuedPrompt.or(userMessage).first(),
      'the accepted input remains visible in the queue or transcript',
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      userMessage,
      'engine consumption hands the input from the queue to one transcript row',
    ).toHaveCount(1, { timeout: budgetMs });
    await expect(
      queuedPrompt,
      'the queue stops owning the input after the transcript receives it',
    ).toHaveCount(0, { timeout: budgetMs });
    // Count, not text: the model's wording is its own business, and a spec that
    // pins wording fails on a model that is behaving correctly.
    await expect
      .poll(() => page.getByTestId('assistant-message').count(), { timeout: budgetMs })
      .toBeGreaterThan(before);
    const reply = page.getByTestId('assistant-message').last();
    await expect(reply).not.toBeEmpty();
    // A bubble that grew the count is not yet a settled reply: while it carries
    // data-streaming=true, a provider/runtime error can still be appended after
    // a negative text assertion has already passed. Wait for both the bubble
    // and the scoped header to settle before ruling out the failure surface.
    await expect(reply).not.toHaveAttribute('data-streaming', 'true', { timeout: budgetMs });
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute(
      'data-pulse',
      'false',
      { timeout: 60_000 },
    );
    await expect(reply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
    return reply;
  };

  try {
    const ready = await api.waitForSessionReady(sessionId);
    const oldSandbox = String(ready.sandbox_id || '').trim();
    expect(oldSandbox, 'conversation should have a sandbox_id when READY').not.toEqual('');

    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    const firstPrompt = '请简短回复一句话，不要使用工具。';
    const nextPrompt = '请再简短回复一句话，不要使用工具。';
    // A settled first turn makes this an idle death, not pre-Result recovery.
    await sendAndReadReply(firstPrompt, 240_000);
    const idle = await api.waitForSession(sessionId, (session) => session.state === 'READY'
      && !session.current_turn_id && session.last_turn_status === 'COMPLETED');
    expect(idle.sandbox_id).toBe(oldSandbox);
    const originalInputs = acceptedInputs(sessionId);
    expect(originalInputs).toHaveLength(1);
    const originalTerminal = await waitForTurnTerminalProof(sessionId, String(idle.last_turn_id), 'COMPLETED', 30_000);
    const originalHistory = await api.getMessages(sessionId, 100);
    expect(originalHistory.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([firstPrompt]);
    expect(originalHistory.messages.filter((message) => message.role === 'assistant')).toHaveLength(1);
    // No conversation polling, stream reconnect or detail GET may trigger the
    // convergence: this assertion must isolate the platform's background path.
    await page.goto('about:blank');
    expect(sessionDoc(sessionId)).toMatchObject({ state: 'READY', sandbox_id: oldSandbox });

    // OUT-OF-BAND kill through the resolved substrate object (NOT the platform
    // teardown endpoint). The fixture never clears the platform binding.
    const oldSandboxHandle = await requireSandboxHandle(api, oldSandbox);
    expect(
      sandboxRunning(oldSandboxHandle),
      'sandbox should be running before the kill',
    ).toBe(true);
    killSandbox(oldSandboxHandle);
    await waitForSandboxStopped(oldSandboxHandle, KILL_CONVERGE_MS);
    expect(sandboxRunning(oldSandboxHandle), 'the killed box should be gone').toBe(false);
    test.info().annotations.push({ type: 'e2e_oob_killed_sandbox_id', description: oldSandbox });

    // Donor lease-truth makes the dead box eligible for its ordinary pull
    // backstop. Only expires_at changes; a push winner cannot be overwritten.
    const expiredAt = new Date(Date.now() - 60 * 60 * 1_000).toISOString();
    const lapse = lapseSessionSandboxLease(sessionId, oldSandbox, expiredAt);
    await test.info().attach('idle-sandbox-lease-fault', {
      body: JSON.stringify({ sessionId, oldSandbox, expiresAt: expiredAt, lapse,
        originalCommand: originalInputs[0], originalTerminal: originalTerminal.last_turn_terminal_frame }),
      contentType: 'application/json',
    });
    if (lapse.length) expect(lapse).toEqual([{ session_id: sessionId, sandbox_id: oldSandbox, expires_at: expiredAt }]);
    let converged: Record<string, unknown> | null = null;
    await expect.poll(() => {
      converged = sessionDoc(sessionId);
      return { state: converged?.state, sandbox_id: converged?.sandbox_id || null,
        current_turn_id: converged?.current_turn_id || null, runtime_unavailable: converged?.runtime_unavailable };
    }, { timeout: KILL_CONVERGE_MS, intervals: [500, 1_000],
      message: 'without any new send or recovery request, background convergence must clear the dead idle binding' })
      .toEqual({ state: 'READY', sandbox_id: null, current_turn_id: null, runtime_unavailable: true });
    await expect.poll(() => backgroundConvergence(sessionId).length,
      { message: 'the real lifecycle event must identify the background convergence path' }).toBe(1);
    const convergence = backgroundConvergence(sessionId)[0];
    expect(acceptedInputs(sessionId), 'background convergence must not submit another input').toEqual(originalInputs);
    const afterConvergence = await waitForTurnTerminalProof(sessionId, String(idle.last_turn_id), 'COMPLETED', 30_000);
    expect(afterConvergence.last_turn_terminal_frame).toEqual(originalTerminal.last_turn_terminal_frame);
    await test.info().attach('idle-sandbox-background-convergence', {
      body: JSON.stringify({ sessionId, oldSandbox, lapse, convergence,
        originalCommand: originalInputs[0], originalTerminal: originalTerminal.last_turn_terminal_frame,
        converged: { state: converged!.state, sandbox_id: converged!.sandbox_id,
          current_turn_id: converged!.current_turn_id, runtime_unavailable: converged!.runtime_unavailable } }),
      contentType: 'application/json',
    });
    await page.goto(appPath(`/sessions/${sessionId}`));
    await expect(page.getByTestId('run-view')).toBeVisible();

    // The very next message must transparently reconstruct onto a FRESH sandbox
    // and deliver a real reply — same send, no resend, and nothing on screen
    // that tells the user their box died. The browser's original delivery
    // oracle does not assume which output connection carries the reply.
    await sendAndReadReply(nextPrompt, REBORROW_TURN_MS);

    // And the header settles. A re-borrow that delivers the text but leaves the
    // turn looking live is still a broken screen.
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first()).toHaveAttribute('data-pulse', 'false', {
      timeout: 60_000,
    });

    const rebound = await api.waitForSessionReady(sessionId);
    const newSandbox = String(rebound.sandbox_id || '').trim();
    expect(newSandbox, 'reconstructed session should have a sandbox_id').not.toEqual('');
    expect(
      newSandbox,
      `out-of-band death must reconstruct onto a NEW sandbox, not reuse the dead one (old=${oldSandbox})`,
    ).not.toEqual(oldSandbox);
    test.info().annotations.push({ type: 'e2e_reborrowed_sandbox_id', description: newSandbox });
    expect(rebound.session_id).toBe(sessionId);
    expect(rebound.last_error || null).toBeNull();
    expect(rebound.last_turn_status).toBe('COMPLETED');
    const inputs = acceptedInputs(sessionId);
    expect(inputs).toHaveLength(2);
    expect(inputs[0]).toEqual(originalInputs[0]);
    expect((inputs[1].payload as Record<string, unknown>).content).toBe(nextPrompt);
    await waitForTurnTerminalProof(sessionId, String(rebound.last_turn_id), 'COMPLETED', 30_000);
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([firstPrompt, nextPrompt]);
    expect(history.messages.filter((message) => message.role === 'assistant')).toHaveLength(2);
    for (const original of originalHistory.messages) {
      expect(history.messages.find((message) => message.message_id === original.message_id)).toEqual(original);
    }
  } finally {
    // Tears down the CURRENT (re-borrowed) sandbox. The out-of-band fault has
    // already made the historical compute unreachable.
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
