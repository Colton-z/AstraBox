/**
 * E2E: an SDK-owned task notification does not become a platform user turn.
 *
 * The community history API derives messages from durable session events; it
 * does not re-project SessionStore on each request. This spec exercises the causal live
 * path with a real background Agent. The queued_command/peer vendor shape is
 * held at the SessionStore projection seam by the paired Python contract test.
 */
import { expect, test } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, messageText, type MessageRecord } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';

const sessions = trackSessions();
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function probeBackgroundAgent(api: AstraApi, sessionId: string): Promise<boolean> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const [session, childRunPage] = await Promise.all([
      api.getSession(sessionId),
      api.listChildRuns(sessionId),
    ]);
    if (session.background_task_state || childRunPage.child_runs.length > 0) return true;
    await sleep(1_500);
  }
  return false;
}

async function waitForCompletedBackgroundAgent(
  api: AstraApi,
  sessionId: string,
): Promise<MessageRecord[]> {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const [session, history, childRunPage] = await Promise.all([
      api.getSession(sessionId),
      api.getMessages(sessionId, 50),
      api.listChildRuns(sessionId),
    ]);
    if (
      session.state === 'READY'
      && !session.background_task_state
      && childRunPage.child_runs.some((childRun) => childRun.closed)
    ) return history.messages;
    await sleep(2_000);
  }
  throw new Error(`background Agent did not complete for session ${sessionId}`);
}

test('history reload keeps an internal task prompt opaque', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const childMarker = `OPAQUE_INTERNAL_CHILD_${runId}`;
  const parentMarker = `OPAQUE_INTERNAL_PARENT_${runId}`;

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const promptForAttempt = (attempt: number) => [
    `E2E internal prompt ${runId}, request ${attempt}. Follow these instructions exactly.`,
    'Launch exactly one Agent with run_in_background=true and subagent_type=general-purpose.',
    `The child must use Bash once to run: sleep 3 && printf '${childMarker}\\n'.`,
    `After Bash, its final answer must be exactly ${childMarker}.`,
    `Do not wait for it; the parent reply must be exactly ${parentMarker}.`,
    'Use no other tools.',
  ].join('\n');
  const submittedPrompts = [promptForAttempt(1)];
  const turn = await api.sendTurn(sessionId, submittedPrompts[0], 180_000);
  expect(turn.errorText, 'the parent launch turn must not fail').toBeNull();
  // Ask again rather than skip: a skip ends the round exactly as a
  // failure does, so the model's choice decided it instead of the
  // platform.
  //
  // More asks beat a longer wait for a state the model chooses. A launch this
  // spec can use shows up in the first seconds of the turn — the whole test
  // takes 30-38s when the model cooperates — so a 60s probe spends the budget
  // waiting on a turn that already moved on, and buys two asks where the same
  // 120s buys four.
  await insist<true>({
    ask: async (attempt) => {
      if (attempt > 1) {
        const prompt = promptForAttempt(attempt);
        submittedPrompts.push(prompt);
        await api.postTurnInput(sessionId, prompt);
      }
    },
    probe: async () => ((await probeBackgroundAgent(api, sessionId)) ? true : null),
    what: 'the model emitted no background Agent lifecycle, so no internal task prompt occurred',
    budgetMs: 120_000,
    probeMs: 30_000,
    attempts: 4,
  });

  const messages = await waitForCompletedBackgroundAgent(api, sessionId);
  const completedChildren = (await api.listChildRuns(sessionId)).child_runs.filter(
    (childRun) => childRun.closed,
  );
  expect(
    completedChildren.some(
      (childRun) => childRun.engine_kind === 'claude_code' && childRun.engine_status === 'completed',
    ),
    'the Claude Agent child should close with the engine\'s exact completed status',
  ).toBe(true);
  const users = messages.filter((message) => message.role === 'user');
  expect(
    users.map(messageText),
    'only explicitly submitted inputs belong in user history, each exactly once and in order',
  ).toEqual(submittedPrompts);
  const inputIds = users.map((message) => String(message.client_message_id || '').trim());
  expect(inputIds).not.toContain('');
  expect(new Set(inputIds).size).toBe(submittedPrompts.length);
  const launchTurnId = users[users.length - 1].turn_id;
  expect(launchTurnId).not.toEqual('');
  expect(
    messages.filter(
      (message) => message.role === 'assistant'
        && message.turn_id === launchTurnId
        && messageText(message).includes(parentMarker),
    ),
  ).toHaveLength(1);
  expect(
    messages.flatMap((message) => message.blocks || [])
      .filter((block) => String(block.type || '') === 'subagent'),
    'engine-owned child activity must not become part of a root message',
  ).toHaveLength(0);

  const settled = await api.getSession(sessionId);
  expect(settled.state).toBe('READY');
  expect(settled.current_turn_id ?? null, 'the internal wake-up must not open a phantom turn').toBeNull();

  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
  await expect(page.getByTestId('user-message')).toHaveCount(submittedPrompts.length);
  await expect(page.getByTestId('user-message')).toContainText(submittedPrompts);
  for (const prompt of submittedPrompts) {
    await expect(page.getByTestId('user-message').filter({ hasText: prompt })).toHaveCount(1);
  }
});
