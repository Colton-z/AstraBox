/** Real Write approval and a subsequent question keep one response owner during first-page reads. */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi, messageText, visibleMessages, type PendingInteraction } from '../fixtures/astraApi';
import { framesForTurn } from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const sessions = trackSessions();
let sessionId = '';
let turnId = '';
const observations: unknown[] = [];

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request, page }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const [session, history, frames, stream] = await Promise.all([
    observe(() => api.getSession(sessionId)),
    observe(() => api.getMessages(sessionId, 100)),
    observe(() => framesForTurn(turnId)),
    observe(() => aiStreamBodies(page)),
  ]);
  await info.attach('interaction-handoff-scene', {
    body: JSON.stringify({ sessionId, turnId, observations, session, history, frames, stream }),
    contentType: 'application/json',
  });
});

async function browserAnswer(
  page: Page,
  pending: PendingInteraction,
  click: () => Promise<void>,
): Promise<void> {
  const response = page.waitForResponse((candidate) => (
    candidate.request().method() === 'POST'
    && candidate.url().includes(`/sessions/${sessionId}/interaction-respond`)
  ));
  await click();
  const received = await response;
  expect(received.status(), 'the browser answer must be accepted by the real endpoint').toBe(200);
  const envelope = record(await received.json());
  const answer = record(envelope.data ?? envelope);
  expect(answer.interaction_id).toBe(pending.interaction_id);
  expect(answer.answered).toBe(true);
}

test('Write approval hands off to a question on the same turn during live first-page reads', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const questionMarker = `HANDOFF_QUESTION_${runId}`;
  const finalMarker = `HANDOFF_COMPLETE_${runId}`;
  const targetPath = `e2e-interaction-handoff-${runId}.txt`;
  const targetContent = `interaction handoff ${runId}`;
  const agent = await api.defaultAgent();
  sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  expect((await api.getSession(sessionId)).permission_mode).toBe('default');
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const prompt = [
    `E2E interaction handoff ${runId}. Follow this exact protocol in one assistant turn.`,
    `Step 1: call Write exactly once to create relative file ${targetPath} with content exactly: ${targetContent}`,
    'Wait for the Write permission response. After approval, wait for the Write result and continue the same assistant turn.',
    `Step 2: call AskUserQuestion exactly once. Ask one single-select question containing ${questionMarker}, header Lock, and options YES and NO.`,
    `Step 3: wait for the questionnaire answer. If YES is selected, reply with exactly ${finalMarker} and use no more tools.`,
    'Do not call any tool other than Write and AskUserQuestion. Do not finish the turn between the two interactions.',
  ].join('\n');
  await sendPrompt(page, sessionId, prompt);
  const first = await api.waitForPendingInteraction(sessionId, 60_000);
  expect(first.tool_name).toBe('Write');
  expect(first.interaction_id).toBeTruthy();
  expect(first.tool_call_id).toBeTruthy();
  turnId = String(first.turn_id || '');
  expect(turnId).not.toBe('');

  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  const before = await api.getMessages(sessionId, 100);
  expect(record(before.pending_interaction)).toMatchObject({
    interaction_id: first.interaction_id, turn_id: turnId, tool_call_id: first.tool_call_id,
  });
  await browserAnswer(page, first, () => panel.getByRole('button', {
    name: /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/,
  }).last().click());

  const assistantOwners = new Set<string>();
  let second: PendingInteraction | undefined;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const bootstrap = await api.getMessages(sessionId, 100);
    const detail = await api.getSession(sessionId);
    const owners = visibleMessages(bootstrap)
      .filter((message) => message.role === 'assistant').map((message) => message.turn_id);
    owners.forEach((owner) => assistantOwners.add(owner));
    observations.push({ owners, bootstrap, detail });
    expect(detail.last_error, 'the interaction handoff must not fail during a real read').toBeFalsy();
    expect(detail.last_turn_status).not.toBe('FAILED');
    const pending = detail.pending_interaction as PendingInteraction | null | undefined;
    if (pending?.interaction_id !== first.interaction_id && pending?.tool_name === 'AskUserQuestion') {
      second = pending;
      break;
    }
    await page.waitForTimeout(250);
  }
  expect(second, 'approving Write must reach the next native question without another input').toBeDefined();
  const question = second!;
  expect(observations.length).toBeGreaterThan(0);
  expect([...assistantOwners]).toEqual([turnId]);
  expect(question.turn_id).toBe(turnId);
  expect(question.interaction_id).not.toBe(first.interaction_id);
  expect(question.tool_call_id).toBeTruthy();
  expect(question.tool_call_id).not.toBe(first.tool_call_id);
  expect(question.presentation).toBe('form');
  const questions = question.questions as Array<{ id: string }>;
  expect(questions).toHaveLength(1);
  expect(questions[0].id).toBeTruthy();

  const after = await api.getMessages(sessionId, 100);
  expect(record(after.pending_interaction)).toMatchObject({
    interaction_id: question.interaction_id, turn_id: turnId, tool_call_id: question.tool_call_id,
  });
  const active = visibleMessages(after).filter((message) => message.role === 'assistant');
  expect([...new Set(active.map((message) => message.turn_id))]).toEqual([turnId]);
  const asks = active.flatMap((message) => message.blocks || []).filter((block) => (
    block.type === 'tool_use' && block.name === 'AskUserQuestion' && block.id === question.tool_call_id
  ));
  expect(asks).toHaveLength(1);
  expect(JSON.stringify(asks[0].input)).toContain(questionMarker);
  await expect.poll(() => framesForTurn(turnId).some((frame) => {
    const payload = record(frame.payload);
    return payload.type === 'data-interaction'
      && record(payload.data).interaction_id === question.interaction_id
      && record(payload.data).tool_call_id === question.tool_call_id;
  }), { timeout: 15_000, message: 'the question interaction frame must retain the exact native tool and owner' })
    .toBe(true);
  await expect(panel).toContainText(questionMarker);
  await expect(panel).toContainText('YES');
  await expect(panel).toContainText('NO');
  await expect(page.getByTestId('session-conversation-shell'))
    .toHaveAttribute('data-pending-tool-call-id', String(question.tool_call_id));
  const yes = panel.getByRole('radio', { name: /yes/i }).first();
  await yes.click();
  await expect(yes).toBeChecked();
  await browserAnswer(page, question, () => panel.getByRole('button', {
    name: /Submit answer|提交回答/,
  }).click());

  const ready = await api.waitForSession(sessionId, (detail) => (
    detail.state === 'READY' && !detail.current_turn_id && !detail.pending_interaction
  ), 60_000);
  expect(ready.last_turn_status).toBe('COMPLETED');
  expect(ready.last_error).toBeFalsy();
  expect(await api.downloadFileText(sessionId, targetPath)).toBe(targetContent);
  const expectedTools = [
    { name: 'Write', id: first.tool_call_id },
    { name: 'AskUserQuestion', id: question.tool_call_id },
  ];
  const historyEvidence = async () => {
    const history = await api.getMessages(sessionId, 100);
    const own = history.messages.filter((message) => message.turn_id === turnId);
    const blocks = own.flatMap((message) => message.blocks || []);
    return {
      users: history.messages.filter((message) => message.role === 'user').map(messageText),
      tools: blocks.filter((block) => block.type === 'tool_use')
        .map((block) => ({ name: block.name, id: block.id })),
      results: blocks.filter((block) => block.type === 'tool_result').map((block) => block.tool_use_id),
      answers: own.filter((message) => message.role === 'assistant'
        && messageText(message).includes(finalMarker)).length,
    };
  };
  const expectedHistory = {
    users: [prompt], tools: expectedTools,
    results: [first.tool_call_id, question.tool_call_id], answers: 1,
  };
  await expect.poll(historyEvidence, { timeout: 30_000 }).toEqual(expectedHistory);
  await expect(panel).toHaveCount(0);
  const answer = page.getByTestId('assistant-text').filter({ hasText: finalMarker });
  await expect(answer).toHaveCount(1);
  await expect(answer).toBeVisible();
  await expect(page.getByTestId('run-view').locator('header').getByTestId('status-pill'))
    .toHaveAttribute('data-state', 'READY');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(answer).toBeVisible();
  expect(await historyEvidence()).toEqual(expectedHistory);
  test.info().annotations.push({ type: 'interaction_handoff', description: JSON.stringify({
    observation_count: observations.length, assistant_owners: [...assistantOwners], turn_id: turnId,
    write_interaction: first.interaction_id, question_interaction: question.interaction_id,
    tools: expectedTools,
  }) });
});
