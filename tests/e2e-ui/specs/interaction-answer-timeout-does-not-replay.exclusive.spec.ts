/** Both sides of an uncertain answer delivery retain their own oracle.
 * Holding the request before delivery must restore the same question.
 * The companion case loses an accepted response: the question must stay closed.
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';

const sessions = trackSessions();

function httpFailureRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected a stored object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

async function expectAnswerHttpFailure(
  page: Page,
  request: APIRequestContext,
  failure: { status: number; contentType: string; body: string; visibleError: RegExp },
): Promise<void> {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await openSessionView(page, sessionId);
  const prompt = [
    'Prepare a one-sentence greeting; the only undecided detail is whether it should say hello.',
    'First call AskUserQuestion once with one single-select question, options YES and NO.',
    'Wait for my answer before writing the greeting. Do not use other tools or ask further questions.',
    `Reference ANSWER_HTTP_${failure.status}_${Date.now()} is a label.`,
  ].join('\n');
  const submitted = await sendPrompt(page, sessionId, prompt);
  expect(submitted.status()).toBe(200);
  const receipt = httpFailureRecord(httpFailureRecord(await submitted.json()).data);
  const commandId = String(receipt.command_id || '');
  const inputId = String(receipt.input_id || '');
  expect(commandId).not.toBe('');
  expect(inputId).not.toBe('');
  const pending = await api.waitForPendingInteraction(sessionId);
  expect(pending.tool_name).toBe('AskUserQuestion');
  expect(pending.presentation).toBe('form');
  const toolId = String(pending.tool_call_id || '');
  const turnId = String(pending.turn_id || '');
  expect(toolId).not.toBe('');
  expect(turnId).not.toBe('');

  const interaction = () => {
    const rows = documentsByField('interaction_snapshots', '$.session_id', sessionId)
      .filter((row) => row.interaction_id === pending.interaction_id);
    expect(rows, 'the real native question must have exactly one durable interaction').toHaveLength(1);
    return rows[0];
  };
  const commands = () => sessionEvents(sessionId).filter((event) => event.event_type === 'command.accepted');
  const nativeQuestion = () => {
    const blocks = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
      .filter((row) => row.subpath == null)
      .flatMap((row) => {
        const entry = httpFailureRecord(JSON.parse(String(row.entry_json)));
        if (entry.type !== 'assistant' && entry.type !== 'user') return [];
        const content = httpFailureRecord(entry.message).content;
        return Array.isArray(content) ? content.map(httpFailureRecord) : [];
      });
    return {
      calls: blocks.filter((block) => block.type === 'tool_use' && block.id === toolId),
      results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolId),
    };
  };
  const originalCommands = commands();
  const originalInputs = originalCommands.filter((event) => (
    httpFailureRecord(event.payload).command_type === 'StartTurn'
  ));
  expect(originalInputs).toHaveLength(1);
  expect(originalInputs[0]).toMatchObject({
    causation_id: commandId, turn_id: turnId,
    payload: { command_type: 'StartTurn', input_id: inputId, content: prompt },
  });
  expect(originalCommands.filter((event) => httpFailureRecord(event.payload).command_type === 'AnswerInteraction'))
    .toHaveLength(0);
  const originalInteraction = interaction();
  expect(originalInteraction).toMatchObject({ interaction_state: 'OPEN', active: true, turn_id: turnId });
  expect(originalInteraction.answer_command_id || null).toBeNull();
  await expect.poll(() => nativeQuestion().calls.length, {
    message: 'the native question must reach database custody before the answer fault',
  }).toBe(1);
  const originalNativeQuestion = nativeQuestion();
  expect(originalNativeQuestion.results).toHaveLength(0);

  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  const yes = panel.getByRole('radio', { name: /yes/i }).first();
  await yes.click();
  const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
  await expect(submit).toBeEnabled();
  const answerPath = apiPath(`/sessions/${sessionId}/interaction-respond`);
  const answerRoute = `**${answerPath}`;
  const attempts: Record<string, unknown>[] = [];
  const unexpectedApiFailures: string[] = [];
  const documentOrigin = await page.evaluate(() => performance.timeOrigin);
  let documentRequests = 0;
  page.on('request', (outgoing) => {
    if (outgoing.resourceType() === 'document') documentRequests += 1;
  });
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith(apiPath('/')) && path !== answerPath && response.status() >= 400) {
      unexpectedApiFailures.push(`${response.request().method()} ${path}: HTTP ${response.status()}`);
    }
  });
  // Only the failing POST is intercepted. Session, history, SDK events and
  // persistence are real; the route never forwards an answer to the controller.
  await page.route(answerRoute, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    attempts.push(httpFailureRecord(route.request().postDataJSON()));
    await route.fulfill({ status: failure.status, contentType: failure.contentType, body: failure.body });
  });
  try {
    const failedResponse = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname === answerPath);
    await submit.click();
    const response = await failedResponse;
    expect(response.status()).toBe(failure.status);
    expect(await response.text()).toBe(failure.body);
    expect(attempts).toHaveLength(1);
    expect(attempts[0].interaction_id).toBe(pending.interaction_id);
    await expect(page.getByText(failure.visibleError)).toBeVisible();
    // Preserve the donor's one-second no-replay observation. This is a
    // negative assertion window after a received HTTP error, not setup sleep.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect(attempts, 'an ordinary failed answer POST must not be replayed automatically').toHaveLength(1);
    await expect(panel).toBeVisible();
    await expect(yes, 'the original question must remain answerable').toBeEnabled();
    expect(await api.getPendingInteraction(sessionId)).toMatchObject({
      interaction_id: pending.interaction_id, tool_call_id: toolId, turn_id: turnId,
    });
    expect(commands(), 'the rejected POST must not append an AnswerInteraction command').toEqual(originalCommands);
    expect(interaction()).toMatchObject({ interaction_state: 'OPEN', active: true, turn_id: turnId });
    expect(interaction().answer_command_id || null).toBeNull();
    expect(sessionEvents(sessionId).filter((event) => event.turn_id === turnId
      && event.event_type === 'interaction.answer_persisted')).toHaveLength(0);
    expect(nativeQuestion(), 'no supplier answer result may exist for an undelivered answer')
      .toEqual(originalNativeQuestion);
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.filter((message) => message.role === 'user').map((message) => ({
      id: message.message_id, text: messageText(message),
    }))).toEqual([{ id: `${inputId}:user`, text: prompt }]);
    expect(history.messages.flatMap((message) => message.blocks || []).filter((block) => (
      block.type === 'tool_result' && block.tool_use_id === toolId
    ))).toHaveLength(0);
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentOrigin);
    expect(documentRequests, 'a failed answer must not reload the document to recover authentication').toBe(0);
    expect(unexpectedApiFailures, 'only the selected answer route may return an API failure').toEqual([]);
    expect(attempts, 'all authoritative readbacks must still leave exactly one failed attempt').toHaveLength(1);
  } finally {
    await page.unroute(answerRoute);
    // trackSessions retains this real pending question on failure; no interrupt,
    // answer or other state-changing cleanup can empty the scene.
  }
}

test('a hung questionnaire answer request times out without replaying and restores the same question', async ({
  request,
  page,
}) => {
  const api = new AstraApi(request);
  const questionMarker = `SDK_HUNG_ASK_ANSWER_${Date.now()}`;
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');

  const answerRoute = `**${apiPath(`/sessions/${sessionId}/interaction-respond`)}`;
  let answerRequestCount = 0;
  let releaseAnswerRequest!: () => void;
  const answerRequestHeld = new Promise<void>((resolve) => { releaseAnswerRequest = resolve; });
  await page.route(answerRoute, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    answerRequestCount += 1;
    // Prevent delivery without fabricating a backend reply.
    await answerRequestHeld;
    await route.abort('timedout').catch(() => undefined);
  });

  try {
    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, [
      `E2E hung AskUserQuestion answer ${questionMarker}. Follow every step exactly.`,
      'Call AskUserQuestion exactly once as your first and only tool.',
      `Ask exactly one question containing ${questionMarker}, with options YES and NO.`,
      'After calling AskUserQuestion, wait for the user answer.',
      'Do not answer the question yourself.',
    ].join('\n'));
    const pending = await api.waitForPendingInteraction(sessionId);
    expect(pending.tool_name).toBe('AskUserQuestion');
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel).toBeVisible();
    await expect(panel).toContainText(questionMarker);
    await panel.getByRole('radio', { name: /yes/i }).first().click();
    await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
    await expect.poll(() => answerRequestCount, {
      timeout: 5_000,
      message: 'the browser must issue the questionnaire answer request once',
    }).toBe(1);
    await expect(page.getByText(/结果状态尚未确认|outcome is not yet confirmed/i)).toBeVisible({
      timeout: 25_000,
    });
    await expect(panel, 'the authoritative questionnaire must return after the timeout').toBeVisible();
    await expect(panel).toContainText(questionMarker);
    await expect(panel.getByRole('radio', { name: /yes/i }).first()).toBeEnabled();
    expect(await api.getPendingInteraction(sessionId), 'no answer reached the server').toMatchObject({
      interaction_id: pending.interaction_id,
      tool_call_id: pending.tool_call_id,
      turn_id: pending.turn_id,
    });
    const history = await api.getMessages(sessionId, 100);
    expect(history.messages.flatMap((message) => message.blocks || []).filter((block) => (
      block.type === 'tool_result' && block.tool_use_id === pending.tool_call_id
    )), 'an undelivered answer cannot have a durable tool result').toHaveLength(0);
    expect(answerRequestCount, 'an ambiguous answer POST must never be replayed automatically').toBe(1);
  } finally {
    releaseAnswerRequest();
    await page.unroute(answerRoute);
  }
});

test('an answer response timeout reports uncertainty without replaying the accepted answer', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const questionMarker = `ANSWER_RESPONSE_LOST_${runId}`;
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, [
    'Prepare a one-sentence greeting. Its only undecided detail is whether it should say hello.',
    'Call AskUserQuestion exactly once, as your first and only tool.',
    `Ask one single-select question containing ${questionMarker}, with options YES and NO.`,
    'Wait for the user answer. Then write the greeting without any further questions or tools.',
  ].join('\n'));
  const pending = await api.waitForPendingInteraction(sessionId);
  expect(pending.tool_name).toBe('AskUserQuestion');
  const toolCallId = String(pending.tool_call_id || '').trim();
  expect(toolCallId).not.toEqual('');

  const answerPath = apiPath(`/sessions/${sessionId}/interaction-respond`);
  const answerRoute = `**${answerPath}`;
  const answerRequests: Array<Record<string, unknown>> = [];
  let serverReceipt: { status: number; body: Record<string, unknown> } | null = null;
  let releaseResponse!: () => void;
  const heldResponse = new Promise<void>((resolve) => { releaseResponse = resolve; });
  let abortAt = 0;
  page.on('requestfailed', (failed) => {
    if (failed.method() === 'POST' && new URL(failed.url()).pathname === answerPath) {
      abortAt = Date.now();
    }
  });
  await page.route(answerRoute, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    answerRequests.push(route.request().postDataJSON() as Record<string, unknown>);
    // route.fetch forwards the actual browser body and authentication. Keep
    // the real response out of the page; never fulfill a fabricated success.
    const response = await route.fetch({ maxRetries: 0, maxRedirects: 0, timeout: 10_000 });
    serverReceipt = { status: response.status(), body: await response.json() as Record<string, unknown> };
    await heldResponse;
    await route.abort('aborted').catch(() => undefined);
  });

  try {
    const panel = page.getByTestId('pending-interaction-panel');
    await expect(panel).toContainText(questionMarker);
    await panel.getByRole('radio', { name: /yes/i }).first().click();
    const submittedAt = Date.now();
    await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
    await expect.poll(() => serverReceipt, {
      timeout: 12_000,
      message: 'the answer must reach and be accepted by the actual server before its response is lost',
    }).toMatchObject({
      status: 200,
      body: { code: 'OK', data: { interaction_id: pending.interaction_id, answered: true } },
    });
    expect(answerRequests).toHaveLength(1);
    expect(answerRequests[0]!.interaction_id).toBe(pending.interaction_id);
    await expect.poll(() => abortAt, {
      timeout: 20_000,
      message: 'the browser must stop waiting on the held response at its own deadline',
    }).toBeGreaterThan(0);
    expect(abortAt - submittedAt, 'the request must receive its full 15-second answer window').toBeGreaterThanOrEqual(14_000);
    expect(abortAt - submittedAt, 'the submitting state must have a bounded lifetime').toBeLessThan(22_000);
    await expect(page.getByText(/结果状态尚未确认|outcome is not yet confirmed/i),
      'the timeout must explain uncertainty, not claim that the answer was rejected').toBeVisible();

    await api.waitForSession(sessionId, (session) => (
      session.state === 'READY' && !session.current_turn_id && !session.pending_interaction
      && session.last_turn_status === 'COMPLETED'
    ), 60_000);
    await expect(panel, 'refresh must not resurrect an already answered questionnaire').toHaveCount(0);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled();
    const settled = await api.getMessages(sessionId, 100);
    const answers = settled.messages.flatMap((message) => message.blocks || []).filter((block) => (
      block.type === 'tool_result' && block.tool_use_id === toolCallId
    ));
    expect(answers, 'the real engine must record the answer exactly once').toHaveLength(1);
    expect(answers[0]!.is_error).not.toBe(true);

    const beforeNext = await api.assistantCount(sessionId);
    const nextPrompt = `Give one brief example of a farewell greeting. Do not use tools. Reference ${runId} is a label.`;
    await sendPrompt(page, sessionId, nextPrompt);
    await api.waitForAssistantMessageMatching(
      sessionId, beforeNext, (message) => messageText(message).trim().length > 0, 60_000,
    );
    await api.waitForSession(sessionId, (session) => (
      session.state === 'READY' && !session.current_turn_id && !session.pending_interaction
      && session.last_turn_status === 'COMPLETED'
    ), 60_000);
    expect(answerRequests, 'neither timeout recovery nor the next turn may resend the answer').toHaveLength(1);
    const finalHistory = await api.getMessages(sessionId, 100);
    expect(finalHistory.messages.filter((message) => (
      message.role === 'user' && messageText(message) === nextPrompt
    ))).toHaveLength(1);
    await expect(panel).toHaveCount(0);
  } finally {
    releaseResponse();
    await page.unroute(answerRoute);
  }
});

test('does not replay an unrelated application 500', async ({ page, request }) => {
  await expectAnswerHttpFailure(page, request, {
    status: 500,
    contentType: 'application/json',
    body: JSON.stringify({ code: 'E2E_ANSWER_HTTP_FAILURE', message: 'unrelated business failure', data: null }),
    visibleError: /E2E_ANSWER_HTTP_FAILURE: unrelated business failure/,
  });
});

test('does not replay an HTML 502 gateway failure', async ({ page, request }) => {
  await expectAnswerHttpFailure(page, request, {
    status: 502,
    contentType: 'text/html',
    body: '<html><body><h1>502 Bad Gateway</h1></body></html>',
    visibleError: /^HTTP_502:/,
  });
});
