/**
 * E2E: what a person sees of the engine's own input handling in one
 * conversation — a message sent while a response runs, Stop, a native
 * question, and a page reloaded in the middle of each.
 *
 * The session accepts a message at once and holds it (`held_inputs` on the
 * session detail) until the engine takes it; when that happens is the
 * engine's decision. Each message Claude takes starts a response of its own
 * (`claude_runtime_client.py`). The transcript shows a message from the moment
 * it is sent, marked Queued while the session holds it.
 *
 * The oracles are the platform's own records (`session_inputs`,
 * `session_responses` and `session_output`, read through `dbOracle`) and the
 * history the page loads, compared with what the page shows.
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Locator, Page, Response } from '@playwright/test';

import {
  AstraApi,
  answersInput,
  messageParts,
  messageText,
  messageToolCalls,
  toolCallSettled,
  visibleMessages,
  type InputReceipt,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import { insist } from '../fixtures/insist';
import { openLiveProcessGroup, revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';
import { sessionInputRows, sessionOutputRows, sessionResponseRows } from '../fixtures/dbOracle';

const TOOL_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TOOL_START_TIMEOUT_MS', 150_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
const FILE_NOT_FOUND = /404|FILE_NOT_FOUND|path not found/;
// misc:composer.queue_waiting in both locales: the line under a message the
// session holds.
const QUEUED = /^(Queued|等待发送)$/;
const INPUT_ID = /^[0-9a-f-]{36}$/;
// The input states in which the session still holds a message: accepted, and
// at most handed to the runtime, which has not taken it.
const HELD_STATES = ['pending', 'delivered'];

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
//
// One tracker for every test in this file: it splices its array in each
// afterEach, so a test only ever owns the ids it pushed.
const sessions = trackSessions();

test.describe.configure({ mode: 'serial' });

async function createConversation(api: AstraApi): Promise<SessionRecord> {
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const ready = await api.waitForSessionReady(created.session_id);
  await api.setPermissionMode(created.session_id, 'bypassPermissions');
  test.info().annotations.push({ type: 'e2e_session_id', description: created.session_id });
  return ready;
}

function blockingCommand(startedPath: string, releasePath: string, resultMarker: string): string {
  return [
    "python3 - <<'PY'",
    'import time',
    'from pathlib import Path',
    `started = Path(${JSON.stringify(startedPath)})`,
    `release = Path(${JSON.stringify(releasePath)})`,
    "started.write_text('started', encoding='utf-8')",
    'deadline = time.monotonic() + 300',
    'while not release.is_file():',
    '    if time.monotonic() >= deadline:',
    "        raise RuntimeError('E2E release marker timed out')",
    '    time.sleep(0.1)',
    `print(${JSON.stringify(resultMarker)})`,
    'PY',
  ].join('\n');
}

function blockingPrompt(
  label: string,
  startedPath: string,
  releasePath: string,
  resultMarker: string,
  finalMarker: string,
): string {
  return [
    label,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    blockingCommand(startedPath, releasePath, resultMarker),
    '```',
    'Do not use any other tool. Wait for Bash before replying.',
    `After it finishes, reply with exactly ${finalMarker}.`,
  ].join('\n');
}

async function filesExist(
  api: AstraApi,
  sessionId: string,
  filePaths: string[],
): Promise<boolean[]> {
  // The Agent runner and terminal occupy separate isolated sessions; their
  // shared workspace and its file API are the rendezvous contract. Reading a
  // terminal stream would also mistake its echoed command for probe output.
  return Promise.all(filePaths.map(async (filePath) => {
    try {
      await api.downloadFileText(sessionId, filePath, 30_000);
      return true;
    } catch (error) {
      if (FILE_NOT_FOUND.test(String((error as Error)?.message ?? error))) return false;
      throw error;
    }
  }));
}

/** The Bash calls one response carries, which commands they target, and which have a result. */
function activeBashEvidence(
  message: MessageRecord | undefined,
  targetPaths: string[],
): {
  bashCalls: number;
  distinctToolCallIds: number;
  matchedTargets: string[];
  resultIds: string[];
} {
  const bashCalls = message
    ? messageToolCalls(message).filter((call) => call.toolName === 'Bash')
    : [];
  const toolCallIds = bashCalls.map((call) => call.toolCallId.trim()).filter(Boolean);
  const matchedTargets = bashCalls.flatMap((call) => {
    const input = call.input && typeof call.input === 'object'
      ? call.input as Record<string, unknown>
      : {};
    const command = String(input.command || '');
    return targetPaths.filter((target) => command.includes(target));
  });
  const resultIds = bashCalls
    .filter(toolCallSettled)
    .map((call) => call.toolCallId.trim())
    .filter(Boolean);
  return {
    bashCalls: bashCalls.length,
    distinctToolCallIds: new Set(toolCallIds).size,
    matchedTargets: [...matchedTargets].sort(),
    resultIds: [...resultIds].sort(),
  };
}

/**
 * Wait until every `startedPaths` file exists: the blocking command is running.
 *
 * Answers false as soon as the session is at rest without it — the model
 * replied without the tool. With `inputId`, at rest also needs the response
 * to that input to have ended, so a session that has not taken the input yet
 * is not read as one that answered it.
 */
async function waitForToolStart(
  api: AstraApi,
  sessionId: string,
  startedPaths: string[],
  timeoutMs = TOOL_START_TIMEOUT_MS,
  inputId?: string,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const results = await filesExist(api, sessionId, startedPaths);
    if (results.every(Boolean)) return true;
    const answered = inputId
      ? visibleMessages(await api.getMessages(sessionId, 50)).some((message) => (
        answersInput(message, inputId) && message.metadata?.status === 'ended'
      ))
      : true;
    const detail = await api.getSession(sessionId);
    // The detail's READY: nothing runs, nothing waits on the owner and the
    // session holds no message.
    if (answered && String(detail.state || '') === 'READY') return false;
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  return false;
}

async function releaseFiles(api: AstraApi, sessionId: string, paths: string[]): Promise<void> {
  if (paths.length === 0) return;
  // Loud. A swallowed release turns a diagnosable failure into a timeout
  // sixty seconds later, against an assertion about something else entirely:
  // the blocking Bash keeps blocking, the held message stays held, and the
  // report names the wait for it.
  //
  // Then prove the file is there. `touch` exiting 0 is the terminal's verdict
  // on its own command, not evidence that the Bash tool waiting on this path
  // can see it: the two run in the box, and whether they share a view of /tmp
  // is exactly what a release that never arrives calls into question.
  const touch = await api.runTerminalCommand(
    sessionId,
    `touch ${paths.join(' ')} && ls -1 ${paths.join(' ')}`,
    '/tmp',
    30_000,
  );
  const seen = String(touch ?? '');
  for (const path of paths) {
    expect(
      seen,
      `the release file ${path} must exist after the terminal wrote it — the ` +
        'blocking tool waits on this exact path',
    ).toContain(path);
  }
}

/** The receipt `POST /inputs` answered the page with, checked against what the page sent. */
async function receiptOf(response: Response): Promise<InputReceipt> {
  const body = await response.json() as { data?: InputReceipt };
  const receipt = body.data as InputReceipt;
  const sent = response.request().postDataJSON() as { client_message_id?: string };
  expect(receipt.client_message_id, 'the receipt names the message the page sent').toBe(
    sent.client_message_id,
  );
  expect(receipt.duplicate, 'a message sent once is a new input').toBe(false);
  expect(receipt.input_id).toMatch(INPUT_ID);
  return receipt;
}

interface SentMessage {
  inputId: string;
  clientMessageId: string;
}

/** The person's message in the transcript, found by the id it has from the moment it is sent. */
function userMessageRow(page: Page, clientMessageId: string): Locator {
  return page.locator(`[data-message-id="${clientMessageId}"]`).getByTestId('user-message');
}

/** The Queued line under a message the session holds. */
function queuedLabel(page: Page, clientMessageId: string): Locator {
  return userMessageRow(page, clientMessageId).getByRole('status').filter({ hasText: QUEUED });
}

/** The state the session recorded for one input. */
function inputState(sessionId: string, inputId: string): string | undefined {
  return sessionInputRows(sessionId).find((row) => row.input_id === inputId)?.state;
}

/** Send a message from the composer while a response runs; the session holds it. */
async function queueFromComposer(
  page: Page,
  sessionId: string,
  marker: string,
): Promise<SentMessage> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'a busy conversation keeps the composer open for the next message').toBeEnabled({
    timeout: 30_000,
  });
  const receipt = await receiptOf(await sendPrompt(page, sessionId, marker));
  await expect(
    queuedLabel(page, receipt.client_message_id),
    'the transcript shows the message at once, marked Queued while the session holds it',
  ).toHaveCount(1, { timeout: 15_000 });
  return { inputId: receipt.input_id, clientMessageId: receipt.client_message_id };
}

async function waitForAssistantMarker(
  api: AstraApi,
  sessionId: string,
  marker: string,
  timeoutMs = TURN_TIMEOUT_MS,
  failWhenSettled = false,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastAssistantText = '';
  while (Date.now() < deadline) {
    // The detail is read before the history: the parts that end a response
    // are recorded with its end, so a session the detail calls READY has its
    // whole answer in the history read after it.
    const detail = failWhenSettled
      ? await api.getSession(sessionId)
      : null;
    const history = await api.getMessages(sessionId, 50);
    const messages = visibleMessages(history);
    const matchingMessages = messages.filter((message) => (
      message.role === 'assistant' && messageText(message).includes(marker)
    ));
    if (matchingMessages.length === 1) return matchingMessages[0];
    if (matchingMessages.length > 1) {
      throw new Error(`assistant response contains ${marker} more than once`);
    }
    lastAssistantText = messages
      .filter((message) => message.role === 'assistant')
      .map(messageText)
      .filter(Boolean)
      .at(-1) || '';
    if (detail && String(detail.state || '') === 'READY') {
      throw new Error(
        `the session came to rest (last response ${String(detail.last_response?.outcome)}) `
        + `without assistant marker ${marker}; last assistant response=${JSON.stringify(lastAssistantText)}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(
    `assistant response did not contain ${marker} within ${timeoutMs}ms; `
    + `last assistant response=${JSON.stringify(lastAssistantText)}`,
  );
}

async function answerWithStaleDetail(request: APIRequestContext, page: Page, coldBootstrap: boolean) {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const questionMarker = `SDK_LIVE_ASK_STALE_DETAIL_${runId}`;
  const finalMarker = `SDK_LIVE_ASK_ANSWERED_${runId}`;
  let stalePendingDetailCount = 0;
  // A live page reads one stale detail; a reloaded page reads stale details
  // from its first load on, so its history's first page must carry the
  // question.
  let forceStalePendingDetail = !coldBootstrap;

  try {
    await api.setPermissionMode(sessionId, 'default');
    await page.route(`**${apiPath(`/sessions/${sessionId}`)}`, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const body = await response.json() as Record<string, unknown>;
      const data = body.data && typeof body.data === 'object'
        ? body.data as Record<string, unknown>
        : body;
      if (
        forceStalePendingDetail
        && (coldBootstrap || stalePendingDetailCount === 0)
        && data.pending_interaction
        && typeof data.pending_interaction === 'object'
      ) {
        stalePendingDetailCount += 1;
        const stale = { ...data, pending_interaction: null };
        await route.fulfill({
          response,
          json: body.data && typeof body.data === 'object' ? { ...body, data: stale } : stale,
        });
        return;
      }
      await route.fulfill({ response, json: body });
    });

    await openSessionView(page, sessionId);
    await sendPrompt(page, sessionId, [
      `E2E live AskUserQuestion stale detail ${runId}.`,
      'Call AskUserQuestion exactly once as your first and only tool.',
      'Ask exactly two questions in that one call.',
      `Question 1 must contain ${questionMarker}_MULTI, use header SCENES, offer PRODUCT and OPS, and allow multiple selections.`,
      `Question 2 must contain ${questionMarker}_SINGLE, use header VALUE, offer YES and NO, and allow one selection.`,
      'Wait for both user answers and do not answer the questions yourself.',
      `After the user selects YES for Question 2, reply with exactly ${finalMarker}.`,
    ].join('\n'));

    const pending = await api.waitForPendingInteraction(sessionId, 150_000).catch((cause: unknown) => {
      const detail = cause instanceof Error ? cause.message : String(cause);
      throw new Error(
        `the live engine must expose and invoke native AskUserQuestion before the canonical-id oracle: ${detail}`,
      );
    });
    expect(String(pending?.presentation || '')).toBe('form');
    expect(String(pending?.tool_name || '')).toBe('AskUserQuestion');
    const questions = Array.isArray(pending?.questions)
      ? pending.questions as Array<Record<string, unknown>>
      : [];
    expect(questions, 'one AskUserQuestion call must expose both requested questions')
      .toHaveLength(2);
    const toolCallId = String(pending?.tool_call_id || '').trim();
    expect(toolCallId, 'the live questionnaire must expose the engine tool id').not.toEqual('');

    const pausedResponseId = String(pending.response_id || '').trim();
    expect(pausedResponseId, 'the question names the response it pauses').not.toBe('');
    await expect.poll(async () => (
      visibleMessages(await api.getMessages(sessionId, 50)).filter((message) => (
        message.role === 'assistant'
        && messageToolCalls(message).some((call) => call.toolCallId === toolCallId)
      )).map((message) => message.id)
    ), { message: 'the pending tool must have one owner: the response the question pauses' })
      .toEqual([pausedResponseId]);
    const panel = page.getByTestId('pending-interaction-panel');
    if (!coldBootstrap) {
      await expect.poll(() => stalePendingDetailCount, {
        timeout: 15_000,
        message: 'the open page must exercise the one stale detail response from the original live scenario',
      }).toBe(1);
    }
    await expect(panel, 'the live question must be answerable before any reload').toBeVisible();
    await expect(panel.getByRole('checkbox', { name: /product/i }).first()).toBeEnabled();
    await expect(page.getByTestId('run-view').getByTestId('status-pill').first())
      .toHaveAttribute('data-state', 'WAITING_INPUT');
    await expect(page.getByTestId('session-conversation-shell'))
      .toHaveAttribute('data-pending-tool-call-id', toolCallId);

    if (coldBootstrap) {
      stalePendingDetailCount = 0;
      forceStalePendingDetail = true;
      await page.reload();
      await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
      await expect.poll(() => stalePendingDetailCount, {
        timeout: 15_000,
        message: 'cold bootstrap must exercise a session detail that hides pending authority',
      }).toBeGreaterThan(0);
    }

    await expect(
      panel,
      'the first-page pending snapshot must remain answerable when detail is stale',
    ).toBeVisible();
    forceStalePendingDetail = false;
    await expect(panel).toContainText(`${questionMarker}_MULTI`);
    // The question switcher is a step navigation of buttons carrying
    // aria-current="step" — deliberately not a tab strip, since the answer
    // area is a fieldset rather than a tabpanel (docs/frontend-design.md §10).
    const scenesStep = panel.getByRole('button', { name: /SCENES/ });
    const valueStep = panel.getByRole('button', { name: /VALUE/ });
    const progress = panel.getByTestId('questionnaire-progress');
    const nextQuestion = panel.getByRole('button', { name: /Next question|下一题/ });
    const previousQuestion = panel.getByRole('button', { name: /Previous question|上一题/ });
    const submitHint = panel.getByTestId('questionnaire-submit-hint');
    await expect(scenesStep).toHaveAttribute('aria-current', 'step');
    await expect(progress).toContainText('0/2');
    await expect(nextQuestion).toBeDisabled();
    await expect(previousQuestion).not.toBeVisible();
    await expect(submitHint).toContainText(/Questions remaining: 2|还剩 2 题/);
    const product = panel.getByRole('checkbox', { name: /product/i }).first();
    await expect(product).toBeEnabled();
    await product.click();
    await expect(product).toBeChecked();
    await expect(progress).toContainText('1/2');
    await expect(submitHint).toContainText(/Questions remaining: 1|还剩 1 题/);
    await expect(nextQuestion).toBeEnabled();
    const navigationIsInsideScroller = await valueStep.evaluate((element) => {
      let parent = element.parentElement;
      while (parent && !parent.matches('[data-testid="pending-interaction-panel"]')) {
        if (['auto', 'scroll'].includes(window.getComputedStyle(parent).overflowY)) return true;
        parent = parent.parentElement;
      }
      return false;
    });
    expect(navigationIsInsideScroller, 'question navigation must stay outside the choices scroller').toBe(false);
    await expect(
      valueStep,
      'question navigation must remain fully visible without scrolling the answer choices',
    ).toBeInViewport({ ratio: 1 });
    const actionNavigationInsideScroller = await nextQuestion.evaluate((element) => (
      element.closest('[data-slot="pending-interaction-scroll-body"]') !== null
    ));
    expect(actionNavigationInsideScroller, 'previous/next stay outside the choices scroller').toBe(false);
    await expect(nextQuestion).toBeInViewport({ ratio: 1 });
    await nextQuestion.click();
    await expect(valueStep).toHaveAttribute('aria-current', 'step');
    await expect(panel).toContainText(`${questionMarker}_SINGLE`);
    const yes = panel.getByRole('radio', { name: /yes/i }).first();
    await expect(yes).toBeEnabled();
    await yes.click();
    await expect(yes).toBeChecked();
    await expect(progress).toContainText('2/2');
    await expect(submitHint).toContainText(/All questions answered|已完成全部问题/);
    await expect(nextQuestion).not.toBeVisible();
    await expect(previousQuestion).toBeInViewport({ ratio: 1 });
    await previousQuestion.click();
    await expect(scenesStep).toHaveAttribute('aria-current', 'step');
    await expect(product).toBeChecked();
    await expect(progress).toContainText('2/2');
    await valueStep.click();
    await expect(yes).toBeChecked();
    const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
    await expect(submit).toBeEnabled();
    await expect(
      submit,
      'the questionnaire action must remain fully visible after navigating questions',
    ).toBeInViewport({ ratio: 1 });
    const answeredResponse = page.waitForResponse((response) => {
      if (response.request().method() !== 'POST') return false;
      const path = new URL(response.url()).pathname;
      return path.startsWith(apiPath(`/sessions/${sessionId}/interactions/`)) && path.endsWith('/answer');
    });
    await submit.click();
    const answered = await answeredResponse;
    expect(answered.status(), 'the browser answer must reach the interaction endpoint').toBe(200);
    const answeredBody = await answered.json() as { data?: Record<string, unknown> };
    const answeredData = answeredBody.data ?? {};
    expect(String(answeredData.interaction_id || '').trim()).toBe(pending?.interaction_id);
    expect(answeredData.answered, 'the session must record the answer').toBe(true);

    await waitForAssistantMarker(api, sessionId, finalMarker, TURN_TIMEOUT_MS, true);
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    await expect(panel, 'the accepted answer must close the questionnaire without another reload').toHaveCount(0);
    const visibleAnswer = page.getByTestId('assistant-text').filter({ hasText: finalMarker });
    await expect(visibleAnswer, 'the continuation must reach the browser, not only durable history').toHaveCount(1);
    await expect(visibleAnswer).toBeVisible();

    // One AskUserQuestion call under the engine's own id, and the result the
    // answer produced on that same call.
    const canonicalAskEvidence = async () => {
      const calls = visibleMessages(await api.getMessages(sessionId, 50)).flatMap(messageToolCalls);
      const askIds = calls
        .filter((call) => (
          call.toolName === 'AskUserQuestion'
          && JSON.stringify(call.input ?? {}).includes(questionMarker)
        ))
        .map((call) => call.toolCallId.trim())
        .filter(Boolean);
      const resultIds = calls
        .filter((call) => call.toolCallId === toolCallId && toolCallSettled(call))
        .map((call) => call.toolCallId);
      return { askIds, resultIds };
    };
    await expect.poll(canonicalAskEvidence, {
      timeout: 45_000,
      message: 'answer settlement must retain one AskUserQuestion id and its matching result',
    }).toEqual({ askIds: [toolCallId], resultIds: [toolCallId] });

    await page.reload();
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
    await expect.poll(canonicalAskEvidence, {
      timeout: 45_000,
      message: 'cold history must preserve the same canonical AskUserQuestion tool id',
    }).toEqual({ askIds: [toolCallId], resultIds: [toolCallId] });
  } finally {
    await page.unrouteAll({ behavior: 'wait' }).catch(() => undefined);
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
}

for (const [coldBootstrap, title] of [
  [false, 'live AskUserQuestion remains answerable when the concurrent session detail read is stale'],
  [true, 'AskUserQuestion cold bootstrap stays answerable when session detail is stale'],
] as const) {
  test(title, async ({ request, page }) => {
    await answerWithStaleDetail(request, page, coldBootstrap);
  });
}

test('completed multi-step response survives a cold history bootstrap', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const toolCallCount = 16;
  const toolMarkers = Array.from({ length: toolCallCount }, (_, index) => (
    `SDK_COLD_HISTORY_TOOL_${index + 1}_${runId}`
  ));
  await openSessionView(page, sessionId);
  const prompt = [
    `E2E completed multi-step response ${runId}. Follow every step exactly.`,
    `In this one response, invoke exactly ${toolCallCount} sequential Bash tool calls.`,
    'Invoke only one call at a time and wait for its result before invoking the next call.',
    'Never combine commands and never invoke the calls concurrently.',
    ...toolMarkers.map((marker, index) => `${index + 1}. Invoke Bash with exactly: printf '${marker}'`),
    'Do not use any other tool.',
    `After all ${toolCallCount} Bash results arrive, give a brief completion report.`,
  ].join('\n');
  const receipt = await receiptOf(await sendPrompt(page, sessionId, prompt));
  const reply = await api.waitForReplyTo(sessionId, receipt.input_id, { timeoutMs: TURN_TIMEOUT_MS });
  expect(reply.metadata?.outcome, 'the response must complete').toBe('completed');
  expect(reply.metadata?.error ?? null, 'a completed response carries no error').toBeNull();
  await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
  const durable = await api.getMessages(sessionId, 50);
  expect(
    durable.messages
      .filter((message) => message.role === 'assistant')
      .every((message) => message.metadata?.status === 'ended'),
    'a session at rest has no open response in its history',
  ).toBe(true);
  expect(durable.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);
  const replies = durable.messages.filter((message) => answersInput(message, receipt.input_id));
  expect(replies, 'one message is answered by one response').toHaveLength(1);
  const parts = replies[0].parts;
  const calls = messageToolCalls(replies[0]);
  expect(calls).toHaveLength(toolCallCount);
  const toolIds = calls.map((call) => call.toolCallId.trim());
  expect(toolIds.every(Boolean)).toBe(true);
  expect(new Set(toolIds).size).toBe(toolCallCount);
  for (const [index, call] of calls.entries()) {
    expect(call.toolName).toBe('Bash');
    expect(call.input).toMatchObject({ command: `printf '${toolMarkers[index]}'` });
    expect(call.state, 'each executed call must carry its successful result').toBe('output-available');
    expect(JSON.stringify(call.output)).toContain(toolMarkers[index]);
  }
  const isToolPart = (part: Record<string, unknown>) => (
    part.type === 'dynamic-tool' || String(part.type || '').startsWith('tool-')
  );
  const lastToolIndex = parts.map(isToolPart).lastIndexOf(true);
  const terminalText = parts.slice(lastToolIndex + 1).filter((part) => part.type === 'text')
    .map((part) => String(part.text || '')).join('').trim();
  expect(terminalText, 'the actual model must answer after all tool results').not.toBe('');
  const replyText = page.getByTestId('assistant-message').getByTestId('assistant-text');
  const textParts = parts.filter((part) => part.type === 'text' && String(part.text || '').trim());
  await expect(replyText, 'the live page must include the final prose block after the tools')
    .toHaveCount(textParts.length);
  await expect(replyText.last()).toBeVisible();
  await expect(replyText.last()).not.toHaveText('');
  // Compare rendered prose with rendered prose: Markdown source contains
  // syntax that is intentionally absent from the visible answer.
  const renderedReply = await replyText.allTextContents();

  // A fresh route rebuilds the page from history alone.
  await page.goto('about:blank', { waitUntil: 'domcontentloaded' });
  await openSessionView(page, sessionId);
  const assertColdHistory = async () => {
    // A settled multi-step response arrives on a rebuilt page as one header:
    // the prose between its tool calls and the cards themselves are fetched only
    // when the reader opens it. Everything below reads that work, so it is
    // opened first. The route shell can precede the history; do not enumerate
    // its process containers until the expected history header is on the page.
    await expect(page.getByTestId('assistant-turn-process')).toHaveCount(1);
    await revealAssistantProcess(page);
    await expect(replyText, 'every prose block, including the terminal answer, must occur once')
      .toHaveText(renderedReply);
    await expect(replyText.last()).toBeVisible();
    await expect(page.getByTestId('assistant-message').getByRole('button', { name: /^Bash\b/ }))
      .toHaveCount(toolCallCount);
    await expect(page.getByTestId('assistant-message').getByRole('button', {
      name: /^Bash\s+(Working|Awaiting confirmation|处理中|等待确认)/,
    })).toHaveCount(0);
    const cold = await api.getMessages(sessionId, 50);
    expect(cold.messages, 'cold reads must retain all message identities, tool calls and final text')
      .toEqual(durable.messages);
  };
  await assertColdHistory();
  const refreshed = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}/history-blocks`)
  ));
  await page.evaluate(() => { window.dispatchEvent(new Event('astrabox:manual-refresh')); });
  expect((await refreshed).status()).toBe(200);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await assertColdHistory();
});

test('Cancel hands stopping state to an already queued next input', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-sdk-cancel-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-sdk-cancel-${runId}.release`;
  const firstMarker = `SDK_CANCEL_RUNNING_${runId}`;
  const followupMarker = `SDK_CANCEL_FOLLOWUP_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const startedPrompt = blockingPrompt(firstMarker, startedPath, releasePath, `CANCELLED_RESULT_${runId}`, `NEVER_REQUIRED_${runId}`);
    let firstInputId = (await receiptOf(await sendPrompt(page, sessionId, startedPrompt))).input_id;
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` answers false the moment the response
    // ends without the tool, so a declined ask costs that response.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) firstInputId = (await api.postTurnInput(sessionId, startedPrompt)).input_id;
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [startedPath], TOOL_START_TIMEOUT_MS, firstInputId))
          ? true
          : null,
      what: 'the configured model did not enter the requested blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const followup = await queueFromComposer(
      page,
      sessionId,
      `${followupMarker}: do not use tools; reply with exactly ${followupMarker}_DONE`,
    );
    await page.getByTestId('run-composer-stop').click();

    await expect(
      queuedLabel(page, followup.clientMessageId),
      'Stop ends the running response and the engine takes the held message next',
    ).toHaveCount(0, { timeout: 60_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: followupMarker }),
      'the held message stays one message in the transcript',
    ).toHaveCount(1, { timeout: 60_000 });
    const answer = await waitForAssistantMarker(api, sessionId, `${followupMarker}_DONE`);
    expect(answersInput(answer, followup.inputId), 'the answer is the response to the held message').toBe(true);
    const ready = await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    expect(ready.last_response?.response_id).toBe(answer.id);
    expect(ready.last_response?.outcome).toBe('completed');
    const stopped = await api.waitForReplyTo(sessionId, firstInputId, { timeoutMs: 30_000 });
    expect(stopped.metadata?.outcome, 'Stop must end the running response as cancelled').toBe('cancelled');
    await expect(page.getByText(/API Error|AGENT_RUNTIME_ERROR|Traceback/i)).toHaveCount(0);
  } finally {
    await releaseFiles(api, sessionId, [releasePath]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('Cancel remains non-fatal and the next input dequeues before model response', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstStarted = `/workspace/.astrabox-e2e-sdk-cancel-first-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-cancel-first-${runId}.release`;
  const nextStarted = `/workspace/.astrabox-e2e-sdk-cancel-next-${runId}.started`;
  const nextRelease = `/workspace/.astrabox-e2e-sdk-cancel-next-${runId}.release`;
  const nextUserMarker = `SDK_CANCEL_NEXT_USER_${runId}`;
  const nextFinalMarker = `SDK_CANCEL_NEXT_FINAL_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const firstDidStartPrompt = blockingPrompt(`SDK_CANCEL_FIRST_${runId}`, firstStarted, firstRelease, `SDK_CANCEL_FIRST_RESULT_${runId}`, `SDK_CANCEL_FIRST_FINAL_${runId}`);
    let firstInputId = (await receiptOf(await sendPrompt(page, sessionId, firstDidStartPrompt))).input_id;
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` answers false the moment the response
    // ends without the tool, so a declined ask costs that response.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) firstInputId = (await api.postTurnInput(sessionId, firstDidStartPrompt)).input_id;
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [firstStarted], TOOL_START_TIMEOUT_MS, firstInputId))
          ? true
          : null,
      what: 'the configured model did not enter the first blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const next = await queueFromComposer(page, sessionId, blockingPrompt(
      nextUserMarker,
      nextStarted,
      nextRelease,
      `SDK_CANCEL_NEXT_RESULT_${runId}`,
      nextFinalMarker,
    ));
    await page.getByTestId('run-composer-stop').click();

    const nextDidStart = await waitForToolStart(
      api,
      sessionId,
      [nextStarted],
      180_000,
      next.inputId,
    );
    expect(nextDidStart, 'the next message was answered without executing its required Bash tool').toBe(true);
    await expect(
      queuedLabel(page, next.clientMessageId),
      'the engine took the next message, so the session does not hold it',
    ).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: nextUserMarker })).toHaveCount(1);
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: nextFinalMarker }),
      'the engine takes the next message before its response has an answer',
    ).toHaveCount(0);
    await expect(page.getByTestId('run-composer-stop')).toBeEnabled();
    await expect(page.getByText(/API Error|AGENT_RUNTIME_ERROR|Traceback/i)).toHaveCount(0);
    const stopped = await api.waitForReplyTo(sessionId, firstInputId, { timeoutMs: 30_000 });
    expect(stopped.metadata?.outcome, 'Stop must end the first response as cancelled').toBe('cancelled');

    await releaseFiles(api, sessionId, [nextRelease]);
    const answer = await waitForAssistantMarker(api, sessionId, nextFinalMarker);
    expect(answersInput(answer, next.inputId), 'the answer is the response to the next message').toBe(true);
    const ready = await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    expect(ready.last_response?.response_id).toBe(answer.id);
    expect(ready.last_response?.outcome).toBe('completed');
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, nextRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('adapter-buffered input moves once from the queue at SDK consumption', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-sdk-sequential-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-sdk-sequential-${runId}.release`;
  const firstUserMarker = `SDK_SEQUENTIAL_FIRST_${runId}`;
  const secondUserMarker = `SDK_SEQUENTIAL_SECOND_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const startedPrompt = blockingPrompt(firstUserMarker, startedPath, releasePath, `SDK_SEQUENTIAL_FIRST_RESULT_${runId}`, `SDK_SEQUENTIAL_FIRST_DONE_${runId}`);
    let firstInputId = (await receiptOf(await sendPrompt(page, sessionId, startedPrompt))).input_id;
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` answers false the moment the response
    // ends without the tool, so a declined ask costs that response.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) firstInputId = (await api.postTurnInput(sessionId, startedPrompt)).input_id;
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [startedPath], TOOL_START_TIMEOUT_MS, firstInputId))
          ? true
          : null,
      what: 'the configured model did not enter the requested blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const second = await queueFromComposer(
      page,
      sessionId,
      `${secondUserMarker}: do not use tools; acknowledge this input briefly`,
    );
    await expect(
      page.getByTestId('user-message').filter({ hasText: secondUserMarker }),
      'the transcript shows the held message once, from the moment it is sent',
    ).toHaveCount(1);
    expect(
      HELD_STATES,
      'the engine does not take the message while the first response runs',
    ).toContain(inputState(sessionId, second.inputId));

    await releaseFiles(api, sessionId, [releasePath]);
    await expect(
      queuedLabel(page, second.clientMessageId),
      'the engine takes the held message once the first response ends',
    ).toHaveCount(0, { timeout: 60_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: secondUserMarker }),
      'taking the message leaves it one transcript row',
    ).toHaveCount(1);
    await expect.poll(async () => {
      const history = visibleMessages(await api.getMessages(sessionId, 50));
      const users = history.filter((message) => (
        message.role === 'user' && messageText(message).includes(secondUserMarker)
      ));
      const userIndex = history.findIndex((message) => message.id === second.clientMessageId);
      const nextUserIndex = history.findIndex((message, index) => (
        index > userIndex && message.role === 'user'
      ));
      const responseWindow = userIndex < 0 ? [] : history.slice(
        userIndex + 1,
        nextUserIndex < 0 ? undefined : nextUserIndex,
      ).filter((message) => message.role === 'assistant');
      const replies = history.filter((message) => answersInput(message, second.inputId));
      return {
        users: users.length,
        replies: replies.length,
        windowIsTheReply: responseWindow.length === 1 && responseWindow[0].id === replies[0]?.id,
        ended: replies.length === 1 && replies[0].metadata?.status === 'ended',
        nonEmpty: replies.some((message) => messageText(message).trim().length > 0),
      };
    }, {
      timeout: TURN_TIMEOUT_MS,
      intervals: [250, 500, 1_000],
      message: [
        'the message the engine took is answered by one non-empty response,',
        'right after it in history',
      ].join(' '),
    }).toEqual({ users: 1, replies: 1, windowIsTheReply: true, ended: true, nonEmpty: true });
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    const reply = await api.waitForReplyTo(sessionId, second.inputId, { timeoutMs: 30_000 });
    const inputRows = sessionInputRows(sessionId).filter((row) => (
      row.client_message_id === second.clientMessageId
    ));
    expect(
      inputRows.map((row) => ({ state: row.state, response: row.response_id })),
      'the session recorded one input for the message, taken by the response that answered it',
    ).toEqual([{ state: 'admitted', response: reply.id }]);

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: firstUserMarker })).toHaveCount(1);
    const secondUser = page.getByTestId('user-message').filter({ hasText: secondUserMarker });
    await expect(secondUser).toHaveCount(1);
    await expect.poll(async () => secondUser.evaluate((element) => {
      const log = element.closest('[role="log"]');
      if (!log) throw new Error('the message must belong to the conversation log');
      const messages = Array.from(log.querySelectorAll(
        '[data-testid="user-message"], [data-testid="assistant-message"]',
      ));
      const userIndex = messages.indexOf(element);
      if (userIndex < 0) throw new Error('the conversation log must contain the message');
      const nextUserIndex = messages.findIndex((message, index) => (
        index > userIndex && message.getAttribute('data-testid') === 'user-message'
      ));
      const responseWindow = messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
        .filter((message) => message.getAttribute('data-testid') === 'assistant-message');
      return {
        responses: responseWindow.length,
        nonEmpty: responseWindow.some((message) => (message.textContent ?? '').trim().length > 0),
      };
    }), {
      timeout: 60_000,
      message: 'reload must preserve the message and the one response that answered it',
    }).toEqual({ responses: 1, nonEmpty: true });
    await expect(queuedLabel(page, second.clientMessageId)).toHaveCount(0);
  } finally {
    await releaseFiles(api, sessionId, [releasePath]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('queued input stays visible during a native question and is consumed once after the answer', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const startedPath = `/workspace/.astrabox-e2e-pending-queue-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-pending-queue-${runId}.release`;
  const secondPrompt = `PENDING_QUEUE_INPUT_${runId}: acknowledge this message briefly without using any tools.`;

  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, [
    'Prepare a short project-status note. First collect the status with this Bash command, verbatim:',
    '```bash',
    blockingCommand(startedPath, releasePath, 'Status collection complete.'),
    '```',
    'Wait for Bash to finish; do not run it in the background or create the release file.',
    'Then use AskUserQuestion once to ask which audience the note is for.',
    'Use one single-select question with header AUDIENCE and options DEVELOPERS and OPERATORS.',
    'Wait for the user to choose before writing the short note. Do not use any other tools.',
  ].join('\n'));
  expect(
    await waitForToolStart(api, sessionId, [startedPath]),
    'the first input must reach the real Bash barrier before the second input is accepted',
  ).toBe(true);
  const second = await queueFromComposer(page, sessionId, secondPrompt);
  const queuedUser = page.getByTestId('user-message').filter({ hasText: secondPrompt });
  await expect(queuedUser, 'the transcript shows the held message once').toHaveCount(1);
  expect(HELD_STATES, 'accepting the message is not the engine taking it')
    .toContain(inputState(sessionId, second.inputId));

  await releaseFiles(api, sessionId, [releasePath]);
  const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, TOOL_START_TIMEOUT_MS);
  expect(pending, 'the original input must ask a real native question before it completes').not.toBeNull();
  expect(pending?.presentation).toBe('form');
  expect(pending?.tool_name).toBe('AskUserQuestion');
  const toolCallId = String(pending?.tool_call_id || '').trim();
  expect(toolCallId).not.toBe('');
  const panel = page.getByTestId('pending-interaction-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('composer-prompt')).toHaveCount(0);
  await expect(page.getByTestId('session-conversation-shell'))
    .toHaveAttribute('data-pending-tool-call-id', toolCallId);
  await expect(
    queuedLabel(page, second.clientMessageId),
    'the held message stays in the transcript, Queued, while the question waits',
  ).toHaveCount(1);
  await expect(
    userMessageRow(page, second.clientMessageId).getByRole('button', {
      name: /^(Retry|重试|Send again|重新发送|Remove queued message|删除排队消息)$/,
    }),
    'an accepted message offers no retry or removal',
  ).toHaveCount(0);
  expect(HELD_STATES, 'waiting for the answer must not hand the held message to the engine')
    .toContain(inputState(sessionId, second.inputId));
  const choice = panel.getByRole('radio', { name: /developers/i }).first();
  await expect(choice).toBeEnabled();
  await choice.click();
  const submit = panel.getByRole('button', { name: /Submit answer|提交回答/ });
  await expect(submit).toBeInViewport({ ratio: 1 });
  const answerResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.startsWith(apiPath(`/sessions/${sessionId}/interactions/`))
  ));
  await submit.click();
  expect((await answerResponse).status()).toBe(200);

  await expect(
    queuedLabel(page, second.clientMessageId),
    'the engine takes the held message after the answer',
  ).toHaveCount(0, { timeout: TURN_TIMEOUT_MS });
  await expect(queuedUser).toHaveCount(1);
  await expect(panel).toHaveCount(0);
  await expect.poll(async () => {
    const history = visibleMessages(await api.getMessages(sessionId, 50));
    const users = history.filter((message) => message.role === 'user' && messageText(message) === secondPrompt);
    const replies = history.filter((message) => (
      answersInput(message, second.inputId) && messageText(message).trim().length > 0
    ));
    return { userIds: users.map((message) => message.id), replies: replies.length };
  }, {
    timeout: TURN_TIMEOUT_MS,
    message: 'the exact message the engine took must own one nonempty answer, without a wording oracle',
  })
    .toEqual({ userIds: [second.clientMessageId], replies: 1 });
  await api.waitForSessionReady(sessionId);
  const reply = await api.waitForReplyTo(sessionId, second.inputId, { timeoutMs: 30_000 });
  const inputRows = sessionInputRows(sessionId).filter((row) => row.client_message_id === second.clientMessageId);
  expect(
    inputRows.map((row) => ({ content: row.content, state: row.state, response: row.response_id })),
    'the session recorded the message once, taken by exactly the response that answered it',
  ).toEqual([{ content: secondPrompt, state: 'admitted', response: reply.id }]);
  expect(
    sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(second.inputId)).map((row) => row.response_id),
    'one response answers the message',
  ).toEqual([reply.id]);
  const askCalls = visibleMessages(await api.getMessages(sessionId, 50))
    .flatMap(messageToolCalls)
    .filter((call) => call.toolCallId === toolCallId);
  expect(askCalls, 'the question is one tool call').toHaveLength(1);
  expect(toolCallSettled(askCalls[0]), 'the answered question carries its result').toBe(true);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(queuedUser, 'cold history must retain one message').toHaveCount(1);
  await expect(queuedLabel(page, second.clientMessageId)).toHaveCount(0);
  // No finally release or interrupt: trackSessions retains the still-held
  // tool/question and its accepted input when any assertion fails.
});

test('accepted input waits for the Result boundary and survives live bootstrap', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstUserMarker = `SDK_PRE_RESULT_FIRST_USER_${runId}`;
  const secondUserMarker = `SDK_PRE_RESULT_SECOND_USER_${runId}`;
  const firstStarted = `/workspace/.astrabox-e2e-sdk-pre-result-first-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-pre-result-first-${runId}.release`;
  const secondStarted = `/workspace/.astrabox-e2e-sdk-pre-result-second-${runId}.started`;
  const secondRelease = `/workspace/.astrabox-e2e-sdk-pre-result-second-${runId}.release`;
  const firstPrompt = blockingPrompt(
    firstUserMarker,
    firstStarted,
    firstRelease,
    `SDK_PRE_RESULT_FIRST_RESULT_${runId}`,
    `SDK_PRE_RESULT_FIRST_DONE_${runId}`,
  );
  const secondPrompt = [
    secondUserMarker,
    'After the current tool finishes, use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    blockingCommand(
      secondStarted,
      secondRelease,
      `SDK_PRE_RESULT_SECOND_RESULT_${runId}`,
    ),
    '```',
    'Do not use any other tool. Wait for this Bash result before finishing.',
  ].join('\n');

  try {
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);
    let firstInputId = (await receiptOf(await sendPrompt(page, sessionId, firstPrompt))).input_id;
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. `waitForToolStart` answers false the moment the response
    // ends without the tool, so a declined ask costs that response.
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) firstInputId = (await api.postTurnInput(sessionId, firstPrompt)).input_id;
      },
      probe: async () =>
        (await waitForToolStart(api, sessionId, [firstStarted], TOOL_START_TIMEOUT_MS, firstInputId))
          ? true
          : null,
      what: 'the configured model did not enter the first blocking Bash tool',
      budgetMs: TOOL_START_TIMEOUT_MS * 2,
      probeMs: TOOL_START_TIMEOUT_MS,
    });

    const second = await queueFromComposer(page, sessionId, secondPrompt);
    expect(HELD_STATES, 'a message sent before the first Result waits in the session')
      .toContain(inputState(sessionId, second.inputId));

    await releaseFiles(api, sessionId, [firstRelease]);
    const secondDidStart = await waitForToolStart(
      api,
      sessionId,
      [secondStarted],
      180_000,
      second.inputId,
    );
    // A failure, not a skip: a skipped result ends the whole Playwright
    // process group, which costs the lane instead of this one test.
    expect(secondDidStart, 'the configured model did not enter the queued Bash tool').toBe(true);

    const bashOf = (message: MessageRecord | undefined) => (
      message ? messageToolCalls(message).filter((call) => call.toolName === 'Bash') : []
    );
    const responsesSoFar = async () => {
      const history = visibleMessages(await api.getMessages(sessionId, 50));
      const firstReply = history.find((message) => answersInput(message, firstInputId));
      const secondReply = history.find((message) => answersInput(message, second.inputId));
      return {
        first: history.filter((message) => (
          message.role === 'user' && messageText(message).includes(firstUserMarker)
        )).length,
        second: history.filter((message) => (
          message.role === 'user'
          && message.id === second.clientMessageId
          && messageText(message).includes(secondUserMarker)
        )).length,
        firstReply: firstReply ? {
          status: firstReply.metadata?.status,
          outcome: firstReply.metadata?.outcome,
          results: messageParts(firstReply, 'data-result').length,
          bash: bashOf(firstReply).length,
          bashSettled: bashOf(firstReply).every(toolCallSettled),
        } : null,
        secondReply: secondReply ? {
          status: secondReply.metadata?.status,
          bash: bashOf(secondReply).length,
          bashSettled: bashOf(secondReply).some(toolCallSettled),
        } : null,
      };
    };
    await expect.poll(responsesSoFar, {
      timeout: 90_000,
      intervals: [250, 500, 1_000],
      message: 'the first response ends at its Result before the engine answers the second message',
    }).toEqual({
      first: 1,
      second: 1,
      firstReply: { status: 'ended', outcome: 'completed', results: 1, bash: 1, bashSettled: true },
      secondReply: { status: 'running', bash: 1, bashSettled: false },
    });

    const activeHistory = visibleMessages(await api.getMessages(sessionId, 50));
    const firstReply = activeHistory.find((message) => answersInput(message, firstInputId))!;
    const secondReply = activeHistory.find((message) => answersInput(message, second.inputId))!;
    // The session's own record of the order: the first response's finish is
    // relayed before the second response starts.
    const output = sessionOutputRows(sessionId);
    const firstFinish = output
      .filter((row) => row.response_id === firstReply.id && row.part.type === 'finish')
      .map((row) => Number(row.seq))
      .at(-1);
    const secondStart = output
      .find((row) => row.response_id === secondReply.id && row.part.type === 'start')?.seq;
    expect(firstFinish, 'the first response must have finished').toBeDefined();
    expect(secondStart, 'the second response must have started').toBeDefined();
    expect(
      Number(firstFinish) < Number(secondStart),
      'the engine takes the second message only after the first response finished',
    ).toBe(true);

    const responseOwners = [firstReply, secondReply].map((message) => {
      const calls = bashOf(message);
      expect(calls).toHaveLength(1);
      const toolCallId = calls[0].toolCallId.trim();
      expect(toolCallId).not.toEqual('');
      return { messageId: message.id, toolCallId };
    });
    expect(new Set(responseOwners.map((owner) => owner.toolCallId)).size).toBe(2);

    const browserResponseOwners = async () => {
      const bodies = await sessionStreamBodies(page);
      const seenOwners = new Set<string>();
      let completedReplyCursor = -1;
      for (const body of bodies) {
        const requestedCursor = new URL(body.url).searchParams.get('after_seq');
        expect(
          requestedCursor === null ? -1 : Number(requestedCursor),
          'following on must not replay a reply whose finish the page received',
        ).toBeGreaterThanOrEqual(completedReplyCursor);
        let messageId = '';
        let replyFinished = false;
        for (const line of body.text.split('\n').slice(0, -1)) {
          if (!line.startsWith('data:')) continue;
          const raw = line.slice(5).trim();
          if (!raw || raw === '[DONE]') continue;
          const frame = JSON.parse(raw) as Record<string, unknown>;
          if (frame.type === 'start') messageId = String(frame.messageId ?? '');
          if (frame.type === 'finish' && frame.finishReason === 'stop') replyFinished = true;
          if (frame.type === 'data-resume-cursor' && replyFinished) {
            const cursor = frame.data as { seq: number };
            completedReplyCursor = Math.max(completedReplyCursor, cursor.seq);
          }
          if (frame.type !== 'tool-input-start' || frame.toolName !== 'Bash') continue;
          const toolCallId = String(frame.toolCallId ?? '');
          const owner = responseOwners.find((candidate) => candidate.toolCallId === toolCallId);
          expect(owner, 'every streamed Bash call must belong to a stored response').toBeDefined();
          expect(messageId, 'the stream must name the same reply as the history').toBe(owner?.messageId);
          seenOwners.add(messageId);
        }
      }
      return [...seenOwners].sort();
    };
    const assertBrowserResponseOwners = async (stage: string, expected: string[], message: string) => {
      try {
        await expect.poll(browserResponseOwners, { message, timeout: 30_000 }).toEqual([...expected].sort());
      } finally {
        await test.info().attach(`responses-${stage}-browser-streams`, {
          body: JSON.stringify(await sessionStreamBodies(page)),
          contentType: 'application/json',
        });
      }
    };
    // One response has ended and folded its call behind a header; the other is
    // still running and holds its card in a group that starts closed. Both are
    // opened, because the count below is of what the reader can reach.
    await revealAssistantProcess(page);
    await expect(page.locator('[data-tool-call-id]').getByRole('button', { name: /^Bash\b/ })).toHaveCount(2);
    await assertBrowserResponseOwners(
      'live',
      responseOwners.map((owner) => owner.messageId),
      'the browser must receive each reply under its durable message identity',
    );

    const historyPath = apiPath(`/sessions/${sessionId}/history-blocks`);
    const reloadedHistory = page.waitForResponse((response) => {
      if (response.request().method() !== 'GET') return false;
      const url = new URL(response.url());
      return url.pathname === historyPath && !url.searchParams.get('before');
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    const reloadedPage = await (await reloadedHistory).json() as { data?: { output_seq?: number } };
    const reloadedOutputSeq = Number(reloadedPage.data?.output_seq);
    expect(Number.isFinite(reloadedOutputSeq), 'the reloaded history names its output position').toBe(true);
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('user-message').filter({ hasText: firstUserMarker })).toHaveCount(1);
    await expect(page.getByTestId('user-message').filter({ hasText: secondUserMarker })).toHaveCount(1);
    await revealAssistantProcess(page);
    await expect(
      page.locator('[data-tool-call-id]').getByRole('button', { name: /^Bash\b/ }),
      'cold bootstrap must retain both assistant tool cards across the Result boundary',
    ).toHaveCount(2, { timeout: 60_000 });
    await expect.poll(async () => (await sessionStreamBodies(page)).length, {
      message: 'the reloaded page must follow the session stream',
    }).toBeGreaterThan(0);
    const firstFollow = (await sessionStreamBodies(page))[0];
    expect(
      Number(new URL(firstFollow.url).searchParams.get('after_seq')),
      'the reloaded page follows the stream from where its history ends',
    ).toBeGreaterThanOrEqual(reloadedOutputSeq);
    await assertBrowserResponseOwners(
      'resumed',
      [],
      'the reloaded page reads both calls from history; its stream replays neither',
    );
    await expect(page.getByTestId('run-composer-stop')).toBeEnabled();

    await releaseFiles(api, sessionId, [secondRelease]);
    await expect.poll(async () => {
      const history = await api.getMessages(sessionId, 50);
      return {
        first: history.messages.filter((message) => (
          message.role === 'user' && messageText(message).includes(firstUserMarker)
        )).length,
        second: history.messages.filter((message) => (
          message.role === 'user' && messageText(message).includes(secondUserMarker)
        )).length,
        open: history.messages.filter((message) => (
          message.role === 'assistant' && message.metadata?.status !== 'ended'
        )).length,
      };
    }, {
      timeout: 180_000,
      intervals: [500, 1_000],
      message: 'both messages stay in history once each, and both responses end',
    }).toEqual({ first: 1, second: 1, open: 0 });
    await api.waitForSessionReady(sessionId, TURN_TIMEOUT_MS);
    await expect.poll(async () => {
      const history = visibleMessages(await api.getMessages(sessionId, 50));
      return responseOwners.map(({ toolCallId }) => ({
        toolCallId,
        owners: history
          .filter((message) => messageToolCalls(message).some((call) => call.toolCallId === toolCallId))
          .map((message) => message.id),
        settled: history
          .flatMap(messageToolCalls)
          .filter((call) => call.toolCallId === toolCallId)
          .every(toolCallSettled),
      }));
    }, {
      timeout: 45_000,
      message: 'each tool call and its result stay only in their original response',
    }).toEqual(responseOwners.map(({ messageId, toolCallId }) => ({
      toolCallId,
      owners: [messageId],
      settled: true,
    })));
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, secondRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});

test('same-turn Bash calls survive cold bootstrap without replacing their sibling', async ({ request, page }) => {
  const api = new AstraApi(request);
  const session = await createConversation(api);
  const sessionId = session.session_id;
  sessions.push(sessionId);
  const runId = Date.now();
  const firstStarted = `/workspace/.astrabox-e2e-sdk-parallel-a-${runId}.started`;
  const firstRelease = `/workspace/.astrabox-e2e-sdk-parallel-a-${runId}.release`;
  const secondStarted = `/workspace/.astrabox-e2e-sdk-parallel-b-${runId}.started`;
  const secondRelease = `/workspace/.astrabox-e2e-sdk-parallel-b-${runId}.release`;
  const finalMarker = `SDK_PARALLEL_FINAL_${runId}`;
  const firstCommand = `touch ${firstStarted}; while [ ! -f ${firstRelease} ]; do sleep 0.1; done; printf SDK_PARALLEL_A_RESULT_${runId}`;
  const secondCommand = `touch ${secondStarted}; while [ ! -f ${secondRelease} ]; do sleep 0.1; done; printf SDK_PARALLEL_B_RESULT_${runId}`;

  try {
    await openSessionView(page, sessionId);
    const receipt = await receiptOf(await sendPrompt(page, sessionId, [
      `E2E same-turn Bash tools ${runId}.`,
      'In one assistant response, invoke exactly two Bash tools:',
      `- ${firstCommand}`,
      `- ${secondCommand}`,
      'Submit both calls in that response before waiting for either result and use no other tool.',
      `After both results arrive, reply with exactly ${finalMarker}.`,
    ].join('\n')));

    // Claude Code executes state-changing Bash calls sequentially even when
    // the model emits both calls in one response. The first marker anchors
    // the running response; its history must already carry both calls.
    const firstDidStart = await waitForToolStart(api, sessionId, [firstStarted], TOOL_START_TIMEOUT_MS, receipt.input_id);
    // A failure, not a skip: a skipped result ends the whole Playwright
    // process group, which costs the lane instead of this one test.
    expect(firstDidStart, 'the configured model did not issue the requested same-turn Bash calls').toBe(true);

    const toolTargets = [firstStarted, secondStarted].sort();
    await expect.poll(async () => {
      const history = visibleMessages(await api.getMessages(sessionId, 50));
      const running = history.find((message) => (
        answersInput(message, receipt.input_id) && message.metadata?.status === 'running'
      ));
      return activeBashEvidence(running, toolTargets);
    }, {
      timeout: 30_000,
      intervals: [250, 500, 1_000],
      message: 'the running response must carry two distinct sibling Bash calls without results',
    }).toEqual({
      bashCalls: 2,
      distinctToolCallIds: 2,
      matchedTargets: toolTargets,
      resultIds: [],
    });

    // The response is still running, so its two sibling calls sit in a process
    // group that starts closed — on this page and again on the reloaded one.
    await openLiveProcessGroup(page);
    await expect(page.getByRole('button', { name: /Bash/ })).toHaveCount(2, { timeout: 60_000 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });
    await openLiveProcessGroup(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }),
      'cold bootstrap must retain both active sibling tool calls',
    ).toHaveCount(2, { timeout: 60_000 });

    await releaseFiles(api, sessionId, [firstRelease, secondRelease]);
    await waitForAssistantMarker(api, sessionId, finalMarker);
  } finally {
    await releaseFiles(api, sessionId, [firstRelease, secondRelease]).catch(() => {});
    // The session is not deleted here — `trackSessions()` decides in an
    // afterEach, where the test's real status is known.
  }
});
