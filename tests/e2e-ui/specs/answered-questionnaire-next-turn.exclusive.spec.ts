/**
 * Answering an SDK questionnaire must settle that turn before the next input.
 *
 * The answer, the matching tool result, and the following browser message all
 * stay on one Agent conversation. The old card must not reopen or turn the next
 * message into a continuation of the answered interaction.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();

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

  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(questionMarker);
  await panel.getByRole('radio', { name: /YES/ }).first().click();
  const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
  const answerResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().includes(`/sessions/${sessionId}/interaction-respond`)
  ));
  await submit.click();
  expect((await answerResponse).status(), 'the questionnaire answer must be accepted')
    .toBe(200);

  await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBeforeQuestion,
    (message) => messageText(message).includes(answerMarker),
  );
  await api.waitForSession(sessionId, (session) => (
    session.state === 'READY'
    && !session.current_turn_id
    && !session.pending_interaction
    && session.last_turn_status === 'COMPLETED'
  ));
  await expect(panel, 'the answered questionnaire must close').toHaveCount(0);

  const settled = await api.getMessages(sessionId, 100);
  const settledBlocks = settled.messages.flatMap((message) => message.blocks || []);
  const askIds = settledBlocks
    .filter((block) => (
      block.type === 'tool_use'
      && block.name === 'AskUserQuestion'
      && JSON.stringify(block.input || {}).includes(questionMarker)
    ))
    .map((block) => String(block.id || '').trim())
    .filter(Boolean);
  expect(askIds, 'history must retain one canonical answered questionnaire')
    .toEqual([toolCallId]);
  expect(
    settledBlocks.filter((block) => (
      block.type === 'tool_result' && block.tool_use_id === toolCallId
    )),
    'the browser answer must settle that same questionnaire tool id',
  ).toHaveLength(1);

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
    session.state === 'READY'
    && !session.current_turn_id
    && !session.pending_interaction
    && session.last_turn_status === 'COMPLETED'
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
