/**
 * E2E: the first message after out-of-band sandbox loss transparently provisions
 * replacement compute.
 *
 * The test kills idle compute without clearing the session's sandbox pointer.
 * With no browser or user action, the real background lifecycle path must first
 * converge READY with no binding, and must neither accept nor answer anything
 * on its own: no input is waiting, so no replacement is borrowed. The next
 * composer send provisions a different sandbox, resumes the same conversation,
 * and renders an ordinary reply without a retry or failure surface. Exact
 * sandbox identity remains a database/API assertion because the conversation
 * page does not display it.
 */
import { execFileSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import {
  postgresRows,
  postgresTextRows,
  sessionInputRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  POSTGRES_CONTAINER_HANDLE,
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';

// Borrowing a fresh sandbox from the pool and resuming the Claude session is
// slow — give the whole two-turn + fault-injection flow a generous budget.
// Abrupt removal converges quickly; this just gates the next dispatch
// so it genuinely lands on dead compute (a kill that has not converged would let
// the turn succeed against the still-live box and no re-borrow would fire).
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);
// The re-borrow turn (fresh sandbox + resume + reply) is the long pole.
const REBORROW_TURN_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);

/** The session row's lifecycle and box binding, as the platform recorded them. */
interface SessionBindingRow {
  state: string;
  sandbox_id: string | null;
  runtime_unavailable: boolean;
  last_error: string | null;
}

function sessionBinding(sessionId: string): SessionBindingRow | null {
  return postgresRows<SessionBindingRow>(
    `SELECT state, sandbox_id, runtime_unavailable, last_error FROM sessions `
      + `WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

/**
 * Move the session's recorded lease into the past while it still names `sandboxId`.
 *
 * Compare-and-set on the box, so a convergence that already cleared the binding
 * is not overwritten; the statement returns what it wrote (session, box and the
 * stored expiry in epoch milliseconds), read back from the write itself because
 * the expiration watcher may change the row right after.
 */
function lapseSessionLease(sessionId: string, sandboxId: string, expiredAt: string): string[][] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET expires_at = ${sqlLiteral(expiredAt)}::timestamptz `
      + `WHERE session_id = ${sqlLiteral(sessionId)} AND sandbox_id = ${sqlLiteral(sandboxId)} `
      + `RETURNING session_id, sandbox_id, (extract(epoch FROM expires_at) * 1000)::bigint`,
  ).map((line) => line.split('|'));
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
    // A settled first response makes this an idle death, not pre-Result recovery.
    await sendAndReadReply(firstPrompt, 240_000);
    const idle = await api.waitForSession(sessionId, (session) => session.state === 'READY'
      && session.last_response?.outcome === 'completed');
    expect(idle.sandbox_id).toBe(oldSandbox);
    const originalInputs = sessionInputRows(sessionId);
    expect(originalInputs.map((row) => row.content)).toEqual([firstPrompt]);
    const originalResponses = sessionResponseRows(sessionId);
    expect(
      originalResponses.map((row) => ({ status: row.status, outcome: row.outcome, input_ids: row.input_ids })),
      'the first message is answered by one completed response before the box dies',
    ).toEqual([{ status: 'ended', outcome: 'completed', input_ids: [originalInputs[0].input_id] }]);
    expect(idle.last_response?.response_id).toBe(originalResponses[0].response_id);
    const originalHistory = await api.getMessages(sessionId, 100);
    expect(originalHistory.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([firstPrompt]);
    expect(originalHistory.messages.filter((message) => message.role === 'assistant')).toHaveLength(1);
    // No conversation polling, stream reconnect or detail GET may trigger the
    // convergence: this assertion must isolate the platform's background path.
    await page.goto('about:blank');
    expect(sessionBinding(sessionId)).toMatchObject({ state: 'READY', sandbox_id: oldSandbox });

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
    const lapse = lapseSessionLease(sessionId, oldSandbox, expiredAt);
    await test.info().attach('idle-sandbox-lease-fault', {
      body: JSON.stringify({ sessionId, oldSandbox, expiresAt: expiredAt, lapse,
        originalInput: originalInputs[0], originalResponse: originalResponses[0] }),
      contentType: 'application/json',
    });
    // No row means a push convergence already cleared the binding; either way
    // the pull backstop cannot overwrite it.
    if (lapse.length) expect(lapse).toEqual([[sessionId, oldSandbox, String(Date.parse(expiredAt))]]);
    let converged: SessionBindingRow | null = null;
    await expect.poll(() => {
      converged = sessionBinding(sessionId);
      return {
        state: converged?.state,
        sandbox_id: converged?.sandbox_id || null,
        runtime_unavailable: converged?.runtime_unavailable,
        open_responses: sessionResponseRows(sessionId).filter((row) => row.status !== 'ended').length,
      };
    }, { timeout: KILL_CONVERGE_MS, intervals: [500, 1_000],
      message: 'without any new send or recovery request, background convergence must clear the dead idle binding' })
      .toEqual({ state: 'READY', sandbox_id: null, runtime_unavailable: true, open_responses: 0 });
    // The convergence records why the binding went: every death-convergence
    // writer names the loss in `last_error`.
    expect(String(converged!.last_error || '').trim(), 'the convergence must record the loss on the row').not.toEqual('');
    expect(sessionInputRows(sessionId), 'background convergence must not accept another input').toEqual(originalInputs);
    expect(
      sessionResponseRows(sessionId),
      'background convergence must neither start a response nor rewrite the completed one',
    ).toEqual(originalResponses);
    await test.info().attach('idle-sandbox-background-convergence', {
      body: JSON.stringify({ sessionId, oldSandbox, lapse, converged,
        originalInput: originalInputs[0], originalResponse: originalResponses[0] }),
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
    expect(rebound.last_response?.outcome).toBe('completed');
    const inputs = sessionInputRows(sessionId);
    expect(inputs).toHaveLength(2);
    expect(inputs[0]).toEqual(originalInputs[0]);
    expect(inputs[1].content).toBe(nextPrompt);
    const responses = sessionResponseRows(sessionId);
    expect(responses[0], 'the completed first response is untouched by the replacement').toEqual(originalResponses[0]);
    expect(
      responses.slice(1).map((row) => ({ status: row.status, outcome: row.outcome, input_ids: row.input_ids })),
      'the next message is answered once, on the replacement box',
    ).toEqual([{ status: 'ended', outcome: 'completed', input_ids: [inputs[1].input_id] }]);
    expect(rebound.last_response?.response_id).toBe(responses[1].response_id);
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([firstPrompt, nextPrompt]);
    expect(history.messages.filter((message) => message.role === 'assistant')).toHaveLength(2);
    for (const original of originalHistory.messages) {
      expect(history.messages.find((message) => message.id === original.id)).toEqual(original);
    }
  } finally {
    // Tears down the CURRENT (re-borrowed) sandbox. The out-of-band fault has
    // already made the historical compute unreachable.
    // The session is NOT deleted here. `trackSessions()` decides in an
    // afterEach, where the test's real status is known — see that fixture on
    // why a `finally` cannot tell whether it is unwinding from a failure.
  }
});
