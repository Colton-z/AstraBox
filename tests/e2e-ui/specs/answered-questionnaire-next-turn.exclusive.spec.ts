/**
 * Answering an SDK questionnaire must settle that response before the next input.
 *
 * The answer, the matching tool result, and the following browser message all
 * stay on one Agent conversation. The old card must not reopen or turn the next
 * message into a continuation of the answered interaction.
 */
import { expect, test } from '@playwright/test';

import {
  AstraApi,
  messageText,
  messageToolCalls,
  toolCallSettled,
  type SessionRecord,
} from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();

/** At rest after a completed response: nothing running, waiting or held. */
function settledAfter(session: SessionRecord, responseId?: string): boolean {
  return session.state === 'READY'
    && (session.current_responses ?? []).length === 0
    && !session.pending_interaction
    && session.last_response?.outcome === 'completed'
    && (responseId === undefined || session.last_response?.response_id === responseId);
}

test('answered foreground AskUserQuestion settles before the next browser message', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const questionMarker = `FOREGROUND_ASK_${runId}`;
  const answerMarker = `FOREGROUND_ASK_ANSWERED_${runId}`;
  const nextMarker = `FOREGROUND_ASK_NEXT_${runId}`;
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await openSessionView(page, sessionId);

  const assistantsBeforeQuestion = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, [
    `E2E foreground questionnaire ownership ${runId}.`,
    'Call AskUserQuestion exactly once as your first and only tool.',
    `Ask one single-select question containing ${questionMarker}, with options YES and NO.`,
    'Wait for the user answer and do not answer it yourself.',
    `After the user selects YES, reply with exactly ${answerMarker}.`,
  ].join('\n'));

  const pending = await api.waitForPendingInteraction(sessionId);
  expect(String(pending.tool_name || '')).toBe('AskUserQuestion');
  const toolCallId = String(pending.tool_call_id || '').trim();
  expect(toolCallId, 'the foreground questionnaire must expose its engine tool id')
    .not.toEqual('');
  const interactionId = String(pending.interaction_id || '').trim();
  const responseId = String(pending.response_id || '').trim();
  expect(responseId, 'the questionnaire must name the response it pauses').not.toEqual('');

  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(questionMarker);
  await panel.getByRole('radio', { name: /YES/ }).first().click();
  const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
  const answerResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith(
      `/sessions/${sessionId}/interactions/${encodeURIComponent(interactionId)}/answer`,
    )
  ));
  await submit.click();
  const answered = await answerResponse;
  expect(answered.status(), 'the questionnaire answer must be accepted').toBe(200);
  expect(
    ((await answered.json()) as { data?: { answered?: boolean } }).data?.answered,
    'the answer is stored for the interaction the card showed',
  ).toBe(true);

  // The answer continues the same response: the reply after it lands in the
  // message the questionnaire paused.
  await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBeforeQuestion,
    (message) => message.id === responseId && messageText(message).includes(answerMarker),
  );
  await api.waitForSession(sessionId, (session) => settledAfter(session, responseId));
  await expect(panel, 'the answered questionnaire must close').toHaveCount(0);

  const settled = await api.getMessages(sessionId, 100);
  const settledCalls = settled.messages.flatMap((message) => messageToolCalls(message));
  const askCalls = settledCalls.filter((call) => (
    call.toolName === 'AskUserQuestion'
    && JSON.stringify(call.input || {}).includes(questionMarker)
  ));
  expect(askCalls.map((call) => call.toolCallId), 'history must retain one canonical answered questionnaire')
    .toEqual([toolCallId]);
  expect(
    toolCallSettled(askCalls[0]),
    `the browser answer must settle that same questionnaire tool id (state ${askCalls[0]?.state})`,
  ).toBe(true);

  const assistantsBeforeNext = await api.assistantCount(sessionId);
  await sendPrompt(page, sessionId, [
    `E2E next-turn liveness ${runId}.`,
    `Reply with exactly ${nextMarker}.`,
    'Do not call any tool.',
  ].join('\n'));
  await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBeforeNext,
    (message) => messageText(message).includes(nextMarker),
  );
  const ready = await api.waitForSession(sessionId, (session) => (
    settledAfter(session) && session.last_response?.response_id !== responseId
  ));
  expect(ready.pending_interaction, 'the answered questionnaire must not reopen').toBeFalsy();
  await expect(panel, 'the old questionnaire must stay closed after the next message')
    .toHaveCount(0);

  const finalHistory = await api.getMessages(sessionId, 100);
  expect(
    finalHistory.messages.filter((message) => (
      message.role === 'assistant' && messageText(message).includes(nextMarker)
    )),
    'the next browser message must receive one durable assistant answer',
  ).toHaveLength(1);
});
