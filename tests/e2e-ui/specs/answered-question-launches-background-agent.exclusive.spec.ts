/** A browser answer can launch a durable child without another user submission. */
import { expect, test, type Page, type Response } from '@playwright/test';

import {
  AstraApi,
  answersInput,
  messageText,
  messageToolCalls,
  toolCallSettled,
  type ChildRunRecord,
  type InputReceipt,
  type MessageRecord,
} from '../fixtures/astraApi';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionInteractionRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const sessions = trackSessions();
let sessionId = '';
let childId = '';
const observations: unknown[] = [];

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

/** Claude's root transcript, as the platform's SessionStore mirror holds it. */
function nativeRoot(session: string) {
  return nativeTranscriptRows(session)
    .filter((row) => row.subpath === null)
    .map((row) => ({ seq: Number(row.seq), entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((a, b) => a.seq - b.seq);
}

function nativeTools(session: string) {
  return nativeRoot(session).flatMap(({ seq, entry }) => {
    if (entry.type !== 'assistant' && entry.type !== 'user') return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object).map((block) => ({ seq, block })) : [];
  });
}

/** A message's prose, one entry per text part. */
function replyTextBlocks(message: MessageRecord): string[] {
  return message.parts.filter((part) => part.type === 'text').map((part) => String(part.text ?? ''));
}

/** The receipt of a message the page sent, checked against the id the page minted for it. */
async function receiptOf(response: Response): Promise<InputReceipt> {
  const receipt = (await response.json() as { data: InputReceipt }).data;
  const sent = response.request().postDataJSON() as { client_message_id?: string };
  expect(receipt.client_message_id, 'the receipt names the message the page sent').toBe(sent.client_message_id);
  return receipt;
}

async function observe(read: () => unknown | Promise<unknown>) {
  try { return { available: true, value: await read() }; }
  catch (error) { return { available: false, error: String(error) }; }
}

test.afterEach(async ({ request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  const [session, history, children, transcript, inputs, responses, interactions, native] = await Promise.all([
    observe(() => api.getSession(sessionId)),
    observe(() => api.getMessages(sessionId, 100)),
    observe(() => api.listChildRuns(sessionId)),
    observe(() => api.getChildRunMessages(sessionId, childId)),
    observe(() => sessionInputRows(sessionId)),
    observe(() => sessionResponseRows(sessionId)),
    observe(() => sessionInteractionRows(sessionId)),
    observe(() => nativeTranscriptRows(sessionId)),
  ]);
  await info.attach('answered-question-background-scene', {
    body: JSON.stringify({
      sessionId, childId, observations, session, history, children, transcript, inputs, responses, interactions, native,
    }),
    contentType: 'application/json',
  });
});

async function showChild(page: Page, child: ChildRunRecord) {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const rows = page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
  await expect(rows, 'one actual Agent survives the answer continuation').toHaveCount(1);
  const owned = page.locator(`[data-testid="subagent-agent-row"][data-child-run-id="${child.child_run_id}"]`);
  await expect(owned).toBeVisible();
  return owned;
}

test('answering a question launches one background Agent which survives live and completed reloads', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const questionMarker = `ANSWER_LAUNCH_QUESTION_${runId}`;
  const parentMarker = `ANSWER_LAUNCH_PARENT_${runId}`;
  const childMarker = `ANSWER_LAUNCH_CHILD_${runId}`;
  const started = `/workspace/.answer-launch-${runId}.started`;
  const release = `/workspace/.answer-launch-${runId}.release`;
  const command = [
    "python3 - <<'PY'", 'from pathlib import Path', 'import time',
    `Path(${JSON.stringify(started)}).write_text('started')`,
    `release = Path(${JSON.stringify(release)})`,
    'deadline = time.monotonic() + 100',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('the browser test did not release this child')",
    '    time.sleep(0.2)',
    `print(${JSON.stringify(childMarker)})`, 'PY',
  ].join('\n');
  const prompt = [
    `Answer-triggered background task ${runId}. Follow these steps in order.`,
    `First call AskUserQuestion exactly once with a single-select question containing ${questionMarker}, header E2E, options YES and NO.`,
    'Wait for the user answer. Before YES, do not call Agent, Bash, or any other tool.',
    'Only after YES, launch exactly one Agent with subagent_type=general-purpose and run_in_background=true.',
    'The Agent runs in the background, but its Bash call must run in the foreground: tell the child to set Bash run_in_background=false and wait for the command to finish.',
    'Give the child this complete command verbatim. It must call Bash exactly once:', command,
    `Only after Bash returns successfully, the child final answer must contain ${childMarker}.`,
    'The child must not use any other tool. Neither you nor the child may create the release file; the browser test owns it.',
    `Immediately after launch, reply with ${parentMarker} and end the foreground response without waiting.`,
    'After launching the child, do not call another tool.',
  ].join('\n');
  const agent = await api.defaultAgent();
  sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  await page.setViewportSize({ width: 1280, height: 720 });
  await openSessionView(page, sessionId);
  const { input_id: inputId } = await receiptOf(await sendPrompt(page, sessionId, prompt));

  const pending = await api.waitForPendingInteraction(sessionId);
  expect(pending.tool_name).toBe('AskUserQuestion');
  const responseId = String(pending.response_id || '');
  const questionId = String(pending.tool_call_id || '');
  expect(responseId, 'the question pauses the response to the prompt').not.toBe('');
  expect(questionId).not.toBe('');
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === responseId), 'the paused response answers the prompt')
    .toEqual([expect.objectContaining({ status: 'waiting', input_ids: [inputId] })]);
  expect((await api.listChildRuns(sessionId)).child_runs, 'no child may start before the answer').toEqual([]);
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toContainText(questionMarker);
  await panel.getByRole('radio', { name: /YES/ }).first().click();
  const answerResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith(
      `/sessions/${sessionId}/interactions/${encodeURIComponent(pending.interaction_id)}/answer`,
    ));
  await panel.getByRole('button', { name: /Submit answer|提交回答/ }).click();
  const response = await answerResponse;
  expect(response.status()).toBe(200);
  const envelope = object(await response.json());
  expect(object(envelope.data ?? envelope)).toMatchObject({ interaction_id: pending.interaction_id, answered: true });

  const parent = await api.waitForAssistantMessageMatching(sessionId, 0,
    (message) => replyTextBlocks(message).some((text) => text.includes(parentMarker)));
  expect(replyTextBlocks(parent).length, 'the parent reply has actual speech before the child completes').toBeGreaterThan(0);
  expect(parent.id, 'the answer continues the original foreground response').toBe(responseId);
  expect(answersInput(parent, inputId), 'and that response answers the prompt').toBe(true);
  const open = await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && !rows[0].closed);
  childId = open[0].child_run_id;
  await expect.poll(async () => {
    try { return await api.downloadFileText(sessionId, started, 30_000); }
    catch (error) {
      if (/^Error: files\/download -> 404:/.test(String(error))) return null;
      throw error;
    }
  }, {
    timeout: 30_000, message: 'the real child Bash must have started before reload',
  }).toBe('started');
  await expect.poll(async () => (await api.getChildRunMessages(sessionId, childId)).messages
    .flatMap((message) => message.content).filter((block) => block.type === 'tool_use' && block.name === 'Bash').length,
  { timeout: 30_000, message: 'the running child retains its actual Bash call before reload' }).toBe(1);
  const running = await api.getChildRunMessages(sessionId, childId);
  const runningBlocks = running.messages.flatMap((message) => message.content);
  const bash = runningBlocks.filter((block) => block.type === 'tool_use' && block.name === 'Bash');
  expect(bash).toHaveLength(1);
  const bashId = String(bash[0].id || '');
  expect(bashId).not.toBe('');
  expect(JSON.stringify(bash[0].input)).toContain(release);
  expect(object(bash[0].input).run_in_background, 'the child waits for Bash; only its parent Agent launch is backgrounded').not.toBe(true);
  expect(runningBlocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === bashId)).toEqual([]);
  const foreground = await api.waitForSession(sessionId, (session) => (session.current_responses ?? []).length === 0
    && !session.pending_interaction && session.last_response?.response_id === responseId
    && session.last_response.outcome === 'completed');
  expect(foreground.last_error).toBeFalsy();
  // The reply as it stands once its response ended: what the child's
  // completion must leave untouched.
  const ended = (await api.getMessages(sessionId, 100)).messages.filter((message) => message.id === responseId);
  expect(ended, 'the ended reply is one message').toHaveLength(1);
  const originalReply = ended[0]!;
  const originalReplyText = replyTextBlocks(originalReply);
  expect(originalReplyText.join('\n')).toContain(parentMarker);
  expect(sessionInteractionRows(sessionId).filter((row) => row.interaction_id === pending.interaction_id),
    'the browser answer is recorded once, on the response it paused')
    .toEqual([expect.objectContaining({ response_id: responseId, status: 'answered' })]);
  observations.push({ pending, parent, open, running, foreground, originalReply });

  await page.reload({ waitUntil: 'domcontentloaded' });
  const stillOpen = await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && !rows[0].closed);
  expect(stillOpen[0].child_run_id).toBe(childId);
  const openRow = await showChild(page, stillOpen[0]);
  await expect(openRow.locator('svg.animate-spin'), 'the Agents panel shows the background Agent still working')
    .toHaveCount(1);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  await openRow.click();
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer.getByRole('button', { name: /^Bash / })).toHaveCount(1);
  await page.getByTestId('subagent-close-button').click();
  await api.uploadFileText(sessionId, '/workspace', `.answer-launch-${runId}.release`, 'release');
  expect(await api.downloadFileText(sessionId, release)).toBe('release');

  const completed = await api.waitForChildRuns(sessionId, (rows) => rows.length === 1 && rows[0].closed);
  expect(completed[0]).toMatchObject({ child_run_id: childId, engine_status: 'completed', closed: true });
  await expect.poll(async () => {
    const child = await api.getChildRunMessages(sessionId, childId);
    const content = child.messages.flatMap((message) => message.content);
    return {
      calls: content.filter((block) => block.type === 'tool_use').map((block) => block.id),
      results: content.filter((block) => block.type === 'tool_result' && block.tool_use_id === bashId
        && block.is_error !== true && JSON.stringify(block.content).includes(childMarker)).length,
      finals: child.messages.filter((message) => message.role === 'assistant' && message.content.some(
        (block) => block.type === 'text' && String(block.text).includes(childMarker),
      )).length,
    };
  }, { timeout: 30_000 }).toEqual({ calls: [bashId], results: 1, finals: 1 });
  const settled = await api.waitForSession(sessionId, (session) => session.state === 'READY'
    && (session.current_responses ?? []).length === 0 && !session.pending_interaction
    && session.last_response?.outcome === 'completed');
  expect(settled.last_error).toBeFalsy();
  const history = await api.getMessages(sessionId, 100);
  expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);
  expect(sessionInputRows(sessionId).map((row) => row.input_id),
    'the answer launched the child without another user submission').toEqual([inputId]);
  const parentReplies = history.messages.filter((message) => message.id === parent.id);
  expect(parentReplies).toHaveLength(1);
  expect(answersInput(parentReplies[0], inputId)).toBe(true);
  expect(messageText(parentReplies[0])).toBe(messageText(originalReply));
  expect(replyTextBlocks(parentReplies[0]), 'child completion preserves the entire original parent speech')
    .toEqual(originalReplyText);
  const rootCalls = history.messages.flatMap((message) => messageToolCalls(message));
  const questions = rootCalls.filter((call) => call.toolName === 'AskUserQuestion');
  expect(questions.map((call) => call.toolCallId)).toEqual([questionId]);
  expect(toolCallSettled(questions[0]!), 'the question carries its answer as its result').toBe(true);
  await expect.poll(() => {
    const tools = nativeTools(sessionId);
    const answer = tools.filter(({ block }) => block.type === 'tool_result' && block.tool_use_id === questionId);
    const launches = tools.filter(({ block }) => block.type === 'tool_use' && block.name === 'Agent');
    return { answers: answer.length, launches: launches.length,
      afterAnswer: answer.length === 1 && launches.length === 1 && launches[0].seq > answer[0].seq,
      background: launches[0] ? object(launches[0].block.input).run_in_background : null };
  }, { timeout: 30_000 }).toEqual({ answers: 1, launches: 1, afterAnswer: true, background: true });
  const childBeforeReload = await api.getChildRunMessages(sessionId, childId);
  await page.reload({ waitUntil: 'domcontentloaded' });
  const row = await showChild(page, completed[0]);
  await expect(row.getByText('completed', { exact: true })).toBeVisible();
  expect(await api.getChildRunMessages(sessionId, childId)).toEqual(childBeforeReload);
  await row.click();
  await expect(drawer).toContainText(childMarker);
  const bashButton = drawer.getByRole('button', { name: /^Bash / });
  await expect(bashButton).toHaveCount(1);
  await bashButton.click();
  const result = drawer.getByRole('heading', { name: 'Result', exact: true });
  await expect(result).toBeVisible();
  await expect(result.locator('..')).toContainText(childMarker);
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('run-view').locator('header').getByTestId('status-pill')).toHaveText(/Ready|就绪/);
});
